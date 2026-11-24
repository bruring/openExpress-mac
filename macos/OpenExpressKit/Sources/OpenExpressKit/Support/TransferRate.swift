import Foundation

/// How fast an operation moves bytes, from its `progress` events: the download speed while it
/// downloads, the transfer speed while it copies to, checks on or reads from the device, and
/// the time left at that speed. It starts over when the phase or the file changes, and says
/// nothing for its first second, nor for progress counted in steps rather than bytes.
public struct TransferRate: Sendable, Equatable {
    public enum Kind: Sendable, Equatable {
        /// From Garmin's servers to this Mac.
        case download
        /// Between this Mac and the device (copied, checked, read).
        case transfer
    }

    /// Bytes a second, smoothed; nil until there is enough to tell.
    public private(set) var bytesPerSecond: Double?
    public private(set) var kind: Kind?
    /// Seconds left at this speed, when the total is known.
    public private(set) var secondsLeft: Double?

    private var key: String?
    private var started: Date?
    private var startDone: Int64 = 0
    private var lastDone: Int64 = 0
    private var lastAt: Date?

    /// The speed shows once this much time of one phase has gone by.
    public static let firstEstimateAfter: TimeInterval = 1
    /// The time over which the speed is smoothed (an exponential average).
    public static let smoothing: TimeInterval = 3

    public init() {}

    public mutating func record(_ progress: EngineProgress, at now: Date = .now) {
        guard progress.inBytes else {
            self = TransferRate()
            return
        }
        let key = "\(progress.request)|\(progress.phase)|\(progress.item ?? "")"
        if key != self.key || progress.done < lastDone {
            self = TransferRate()
            self.key = key
            kind = progress.phase == "downloading" ? .download : .transfer
            started = now
            startDone = progress.done
            lastDone = progress.done
            lastAt = now
            return
        }
        guard let lastAt, let started else { return }
        let step = now.timeIntervalSince(lastAt)
        guard step > 0 else { return }
        let instant = Double(progress.done - lastDone) / step
        if let rate = bytesPerSecond {
            let weight = 1 - exp(-step / Self.smoothing)
            bytesPerSecond = rate + weight * (instant - rate)
        } else if now.timeIntervalSince(started) >= Self.firstEstimateAfter {
            // The first estimate: the average since the phase began.
            bytesPerSecond = Double(progress.done - startDone) / now.timeIntervalSince(started)
        }
        lastDone = progress.done
        self.lastAt = now
        if let rate = bytesPerSecond, rate > 0, let total = progress.total, total >= progress.done {
            secondsLeft = Double(total - progress.done) / rate
        } else {
            secondsLeft = nil
        }
    }

    /// "Download 12.3 MB/s · 2 min left", "Transfer 8.1 MB/s"; nil while there is no speed.
    public var text: String? {
        guard let rate = bytesPerSecond, rate > 0, let kind else { return nil }
        let speed = String(localized: "\(Format.bytes(Int64(rate)))/s")
        let label = kind == .download ? String(localized: "Download \(speed)")
            : String(localized: "Transfer \(speed)")
        guard let left = secondsLeft, left.isFinite, left >= 1 else { return label }
        return String(localized: "\(label) · \(Self.timeLeft(left)) left")
    }

    /// "45 sec", "2 min", "1 hr, 5 min": rounded up, in at most two units.
    public static func timeLeft(_ seconds: Double) -> String {
        let rounded = seconds < 60 ? (seconds / 5).rounded(.up) * 5 : (seconds / 60).rounded(.up) * 60
        return Duration.seconds(rounded).formatted(
            .units(allowed: [.hours, .minutes, .seconds], width: .abbreviated, maximumUnitCount: 2))
    }
}
