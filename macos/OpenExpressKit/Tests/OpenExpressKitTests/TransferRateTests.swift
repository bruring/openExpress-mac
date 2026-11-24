import Foundation
import Testing
@testable import OpenExpressKit

/// The download and transfer speed told from `progress` events: nothing for its first second,
/// then the average, then smoothed; it starts over with a new phase or file, and stays quiet
/// for progress counted in steps.
@Suite("Download and transfer speed")
struct TransferRateTests {
    let start = Date(timeIntervalSince1970: 1_000_000)
    let megabyte: Int64 = 1_000_000

    func bytes(_ done: Int64, of total: Int64? = 100_000_000, phase: String = "copying",
               item: String? = "TopoActive Canada", request: Int = 7) -> EngineProgress {
        EngineProgress(request: request, phase: phase, done: done, total: total, item: item, unit: "bytes")
    }

    @Test func nothingIsToldInTheFirstSecond() {
        var rate = TransferRate()
        rate.record(bytes(0), at: start)
        rate.record(bytes(5 * megabyte), at: start.addingTimeInterval(0.5))
        #expect(rate.bytesPerSecond == nil && rate.text == nil)
    }

    @Test func aSteadyCopyIsToldItsSpeedAndTimeLeft() {
        var rate = TransferRate()
        for second in 0...4 {  // 10 MB a second
            rate.record(bytes(Int64(second) * 10 * megabyte), at: start.addingTimeInterval(Double(second)))
        }
        let speed = try? #require(rate.bytesPerSecond)
        #expect(abs((speed ?? 0) - 10_000_000) < 1)
        #expect(rate.kind == .transfer)
        #expect(abs((rate.secondsLeft ?? 0) - 6) < 0.01)  // 60 MB left
        #expect(rate.text?.hasPrefix("Transfer 10 MB/s") == true)
        #expect(rate.text?.contains("left") == true)
    }

    @Test func aDownloadIsCalledOne() {
        var rate = TransferRate()
        rate.record(bytes(0, phase: "downloading"), at: start)
        rate.record(bytes(4 * megabyte, phase: "downloading"), at: start.addingTimeInterval(2))
        #expect(rate.kind == .download)
        #expect(rate.text?.hasPrefix("Download 2 MB/s") == true)
    }

    @Test func aChangeOfSpeedIsFollowedSmoothly() {
        var rate = TransferRate()
        rate.record(bytes(0), at: start)
        rate.record(bytes(10 * megabyte), at: start.addingTimeInterval(1))  // 10 MB/s
        rate.record(bytes(12 * megabyte), at: start.addingTimeInterval(2))  // then 2 MB/s
        let speed = rate.bytesPerSecond ?? 0
        #expect(speed < 10_000_000 && speed > 2_000_000)
    }

    @Test func aNewFileOrPhaseStartsOver() {
        var rate = TransferRate()
        rate.record(bytes(0), at: start)
        rate.record(bytes(10 * megabyte), at: start.addingTimeInterval(2))
        #expect(rate.bytesPerSecond != nil)
        rate.record(bytes(0, phase: "checking"), at: start.addingTimeInterval(2.1))
        #expect(rate.bytesPerSecond == nil && rate.kind == .transfer)
        rate.record(bytes(1 * megabyte, item: "TopoActive Taiwan"), at: start.addingTimeInterval(2.2))
        #expect(rate.bytesPerSecond == nil)
    }

    @Test func progressCountedInStepsHasNoSpeed() {
        var rate = TransferRate()
        rate.record(bytes(0), at: start)
        rate.record(bytes(10 * megabyte), at: start.addingTimeInterval(2))
        rate.record(EngineProgress(request: 7, phase: "backup", done: 3, total: 25, item: "Copying to the backup"),
                    at: start.addingTimeInterval(3))
        #expect(rate.bytesPerSecond == nil && rate.text == nil && rate.kind == nil)
    }

    @Test func theTimeLeftIsRoundedUp() {
        #expect(TransferRate.timeLeft(42) == Duration.seconds(45).formatted(
            .units(allowed: [.hours, .minutes, .seconds], width: .abbreviated, maximumUnitCount: 2)))
        #expect(TransferRate.timeLeft(61) == Duration.seconds(120).formatted(
            .units(allowed: [.hours, .minutes, .seconds], width: .abbreviated, maximumUnitCount: 2)))
    }

    @Test func theUnitComesFromTheEngine() throws {
        let bytes: JSONValue = ["request": 3, "phase": "copying", "done": 5, "total": 10, "unit": "bytes"]
        let p = try bytes.decode(EngineProgress.self)
        #expect(p.inBytes)
        let counted: JSONValue = ["request": 3, "phase": "backup", "done": 5, "total": 10]
        #expect(try !counted.decode(EngineProgress.self).inBytes)
    }
}
