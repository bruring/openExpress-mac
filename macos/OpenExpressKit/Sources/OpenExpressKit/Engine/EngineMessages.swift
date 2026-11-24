import Foundation

/// App → engine: one line per request (docs/ENGINE.md, "Messages").
public struct EngineRequest: Encodable, Sendable, Equatable {
    public var id: Int
    public var method: String
    public var params: JSONValue?

    public init(id: Int, method: String, params: JSONValue? = nil) {
        self.id = id
        self.method = method
        self.params = params
    }

    /// The request as one line of JSON, without the newline.
    public func line() throws -> Data {
        let e = JSONEncoder()
        e.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        return try e.encode(self)
    }
}

/// An error the engine answered with, or a failure of the connection to it.
public enum EngineError: Error, Sendable, Equatable, LocalizedError {
    /// `{"error": {"code", "message", "data"}}`: `message` is already translated.
    case remote(code: String, message: String, data: JSONValue?)
    /// The engine exited (or crashed) before it answered.
    case terminated
    /// The engine could not be started.
    case launchFailed(String)
    /// The engine is not running and the app does not restart it any more.
    case notRunning
    /// The result did not have the shape docs/ENGINE.md gives it.
    case unexpectedResult(method: String, detail: String)
    /// The request was cancelled by the app.
    case cancelled

    public var code: String? {
        if case .remote(let code, _, _) = self { return code }
        return nil
    }

    /// The other program holding the device, for `device_busy`.
    public var busyProgram: String? {
        guard case .remote("device_busy", _, let data) = self else { return nil }
        return data?["program"]?.stringValue ?? ""
    }

    public var errorDescription: String? {
        switch self {
        case .remote(_, let message, _): message
        case .terminated: String(localized: "The engine of openExpress for Mac stopped unexpectedly.")
        case .launchFailed(let why): String(localized: "The engine of openExpress for Mac could not be started: \(why)")
        case .notRunning: String(localized: "The engine of openExpress for Mac is not running.")
        case .unexpectedResult(let method, _):
            String(localized: "The engine of openExpress for Mac gave an answer this version does not understand (\(method)).")
        case .cancelled: String(localized: "Cancelled.")
        }
    }
}

/// Engine → app: a response to a request, or an event.
public enum EngineIncoming: Sendable, Equatable {
    case response(id: Int, result: Result<JSONValue, EngineError>)
    case event(EngineEvent)

    /// Parses one line of the engine's stdout; nil for a line that is not a message (the
    /// engine's log goes to stderr, but a stray line must not stop the app).
    public static func parse(_ line: some StringProtocol) -> EngineIncoming? {
        guard let data = line.data(using: .utf8),
              let value = try? JSONDecoder().decode(JSONValue.self, from: data),
              case .object(let o) = value
        else { return nil }
        if let id = o["id"]?.intValue {
            if let err = o["error"] {
                return .response(id: id, result: .failure(.remote(
                    code: err["code"]?.stringValue ?? "error",
                    message: err["message"]?.stringValue ?? "",
                    data: err["data"])))
            }
            return .response(id: id, result: .success(o["result"] ?? .null))
        }
        if let name = o["event"]?.stringValue {
            return .event(EngineEvent(name: name, params: o["params"] ?? .null))
        }
        return nil
    }
}

/// What the client routes a line by (`id`, `error`, `event`), decoded without building the
/// result: a typed result is then decoded once, straight from the line's bytes.
struct EngineEnvelope: Decodable {
    struct RemoteError: Decodable {
        var code: String?
        var message: String?
        var data: JSONValue?
    }

    var id: Int?
    var error: RemoteError?
    var event: String?

    /// nil for a line that is not a JSON object.
    static func of(_ line: Data) -> EngineEnvelope? {
        try? JSONDecoder().decode(EngineEnvelope.self, from: line)
    }

    var engineError: EngineError? {
        error.map { .remote(code: $0.code ?? "error", message: $0.message ?? "", data: $0.data) }
    }
}

/// `{"id": …, "result": T}`: only the result is decoded.
struct EngineResultLine<T: Decodable>: Decodable {
    var result: T
}

/// `{"id": …, "result": …}` as an untyped value; a missing result is null.
struct EngineUntypedResultLine: Decodable {
    var result: JSONValue?
}

/// An event: no id, at any time (docs/ENGINE.md, "Events").
public enum EngineEvent: Sendable, Equatable {
    case deviceAttached(Device)
    case deviceDetached(Device)
    case deviceBusy(DeviceBusy)
    case progress(EngineProgress)
    case log(EngineLogLine)
    /// The app's own: the engine was started again after it stopped; requests that were
    /// running failed with `EngineError.terminated`.
    case engineRestarted
    /// The app's own: the engine stopped and is not restarted any more.
    case engineStopped(String)
    case unknown(name: String, params: JSONValue)

    public init(name: String, params: JSONValue) {
        func model<T: Decodable>(_: T.Type) -> T? { try? params.decode(T.self) }
        switch name {
        case "device.attached": self = model(Device.self).map { .deviceAttached($0) } ?? .unknown(name: name, params: params)
        case "device.detached": self = model(Device.self).map { .deviceDetached($0) } ?? .unknown(name: name, params: params)
        case "device.busy": self = model(DeviceBusy.self).map { .deviceBusy($0) } ?? .unknown(name: name, params: params)
        case "progress": self = model(EngineProgress.self).map { .progress($0) } ?? .unknown(name: name, params: params)
        case "log": self = model(EngineLogLine.self).map { .log($0) } ?? .unknown(name: name, params: params)
        default: self = .unknown(name: name, params: params)
        }
    }
}

/// `device.busy`: another program holds a device. `device` is nil unless its USB serial
/// names a remembered device; `name` is its product name.
public struct DeviceBusy: Codable, Sendable, Hashable {
    public var device: String?
    public var program: String
    public var name: String?

    public init(device: String?, program: String, name: String? = nil) {
        self.device = device
        self.program = program
        self.name = name
    }
}

public struct EngineProgress: Codable, Sendable, Hashable {
    public var request: Int
    public var phase: String
    public var done: Int64
    public var total: Int64?
    public var item: String?
    /// "bytes" when `done` and `total` count bytes (then a speed can be told); else steps.
    public var unit: String?

    public init(request: Int, phase: String, done: Int64, total: Int64?, item: String? = nil,
                unit: String? = nil) {
        self.request = request
        self.phase = phase
        self.done = done
        self.total = total
        self.item = item
        self.unit = unit
    }

    /// `done` and `total` count bytes.
    public var inBytes: Bool { unit == "bytes" }

    /// The phase in words: the transfer phases are named, the method's own steps
    /// (`backup`, `sync`, `reading`, `working`) carry their words in `item`.
    public var phaseText: String {
        switch phase {
        case "downloading": String(localized: "Downloading")
        case "copying": String(localized: "Copying to the device")
        case "writing": String(localized: "Writing")
        case "checking": String(localized: "Checking")
        case "waiting": String(localized: "Waiting for Garmin Connect")
        case "backup": String(localized: "Backing up")
        case "sync": String(localized: "Syncing")
        case "reading": String(localized: "Reading the device")
        case "reading_activities": String(localized: "Reading activities from the device")
        default: String(localized: "Working")
        }
    }

    /// "Downloading · TopoActive Europe"; for the method's own steps just the step's words.
    public var detail: String {
        switch phase {
        case "downloading", "copying", "writing", "checking", "waiting":
            return [phaseText, item].compactMap { $0 }.joined(separator: " · ")
        case "reading_activities":
            // Counted in files: "Reading activities from the device… 120 of 200".
            guard let total else { return phaseText + "…" }
            return String(localized: "\(phaseText)… \(done) of \(total)")
        default:
            return item ?? phaseText
        }
    }

    /// 0…1, or nil while the total is not known.
    public var fraction: Double? {
        guard let total, total > 0 else { return nil }
        return min(1, max(0, Double(done) / Double(total)))
    }
}

public struct EngineLogLine: Codable, Sendable, Hashable {
    public var level: String
    public var message: String
    /// The request it belongs to.
    public var request: Int?

    public init(level: String, message: String, request: Int? = nil) {
        self.level = level
        self.message = message
        self.request = request
    }
}
