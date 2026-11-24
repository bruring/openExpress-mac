import Foundation
import Testing
@testable import OpenExpressKit

@MainActor
final class RecordingNotifier: Notifier {
    var notes: [(String, String)] = []
    func notify(title: String, body: String) { notes.append((title, body)) }
}

@MainActor
@Suite("View models against the MockEngine")
struct ModelTests {
    func makeApp(_ scenario: MockEngine.Scenario = .standard) -> (AppModel, MockEngine) {
        let engine = MockEngine(scenario: scenario, stepDelay: .milliseconds(1))
        let defaults = UserDefaults(suiteName: "oe-test-\(UUID().uuidString)")!
        let app = AppModel(engine: engine, settings: AppSettings(defaults: defaults))
        return (app, engine)
    }

    @Test func bootstrapSelectsConnectedDevice() async {
        let (app, _) = makeApp()
        await app.start()
        #expect(app.engineState == .ready)
        #expect(app.account?.signedIn == true)
        #expect(app.connectedDevices.map(\.unitID) == [MockEngine.fenixID])
        #expect(app.rememberedDevices.map(\.unitID) == [MockEngine.edgeID])
        #expect(app.selection == .device(MockEngine.fenixID))
        #expect(app.isShowingSetup == false)
    }

    @Test func firstRunShowsSetup() async {
        let (app, _) = makeApp(.firstRun)
        await app.start()
        #expect(app.isShowingSetup)
    }

    @Test func automaticModeBacksUpSyncsChecksAndNeverInstalls() async throws {
        let (app, engine) = makeApp()
        let notifier = RecordingNotifier()
        app.notifier = notifier
        await app.start()
        await app.runAutomatic(MockEngine.fenixID)
        let methods = await engine.calls.map(\.method)
        let tail = methods.filter { ["device.backup", "device.sync", "updates.list", "maps.list", "updates.install", "maps.install", "maps.update"].contains($0) }
        // Updates and maps are read once, by the Overview of this connection, not again
        // after the sync.
        #expect(tail == ["updates.list", "maps.list", "device.backup", "device.sync"])
        #expect(!methods.contains("updates.install"))
        #expect(notifier.notes.count == 1)
        #expect(notifier.notes.first?.1.contains("Synced") == true)
        let model = try #require(app.model(for: MockEngine.fenixID))
        #expect(model.installAllParts.count == 2)
        #expect(app.pendingUpdatesCount == 3) // 2 software + 1 map
    }

    @Test func automaticModeOnAReconnectionChecksUpdatesOnce() async throws {
        let (app, engine) = makeApp()
        await app.start()
        let model = try #require(app.model(for: MockEngine.fenixID))
        await model.load(.overview)
        await app.runAutomatic(MockEngine.fenixID)
        // Checked before this run: the run asks once, after the sync.
        let methods = await engine.calls.map(\.method)
        #expect(methods.filter { $0 == "updates.list" }.count == 2)
        #expect(methods.filter { $0 == "maps.list" }.count == 2)
        #expect(Array(methods.drop { $0 != "device.sync" }.filter { $0 == "updates.list" }).count == 1)
    }

    @Test func overviewReadsUpdatesAndMapsForTheirPagesToo() async throws {
        let (app, engine) = makeApp()
        await app.start()
        let model = try #require(app.model(for: MockEngine.fenixID))
        await model.loadIfNeeded(.overview)
        await model.loadIfNeeded(.updates)
        await model.loadIfNeeded(.maps)
        var methods = await engine.calls.map(\.method)
        #expect(methods.filter { $0 == "updates.list" }.count == 1)
        #expect(methods.filter { $0 == "maps.list" }.count == 1)
        #expect(model.loaded.isSuperset(of: [.overview, .updates, .maps]))
        // Refresh reads anew.
        await model.load(.updates)
        methods = await engine.calls.map(\.method)
        #expect(methods.filter { $0 == "updates.list" }.count == 2)
        #expect(methods.filter { $0 == "maps.list" }.count == 2)
    }

    @Test func overviewReadsTheAccountsReadingsAndRefreshAsksThemAnew() async throws {
        let (app, engine) = makeApp()
        await app.start()
        let model = try #require(app.model(for: MockEngine.fenixID))
        await model.loadIfNeeded(.overview)
        #expect(model.readings?.sensors?.sensors?.first?.low == true)
        #expect(model.readings?.solar?.state == .read)
        await model.load(.overview)  // Refresh
        let asked = await engine.calls.filter { $0.method == "account.readings" }.map { $0.params?["fresh"] }
        #expect(asked == [.bool(false), .bool(true)])

        // A remembered device has them too: they are the account's.
        let edge = try #require(app.model(for: MockEngine.edgeID))
        await edge.loadIfNeeded(.overview)
        #expect(edge.readings?.sensors?.state == .read && edge.readings?.solar == nil)
    }

    @Test func signedOutTheReadingsAskToSignIn() async throws {
        let (app, _) = makeApp(.signedOut)
        await app.start()
        let model = try #require(app.model(for: MockEngine.fenixID))
        await model.loadIfNeeded(.overview)
        #expect(model.readings?.sensors?.state == .signedOut)
        #expect(model.readings?.solar?.state == .signedOut)
    }

    @Test func activityTracksAreReadOnce() async throws {
        let (app, engine) = makeApp()
        await app.start()
        let model = try #require(app.model(for: MockEngine.fenixID))
        let activities = MockEngine.sampleActivities(count: DeviceModel.trackCacheLimit + 1)
        func reads() async -> Int { await engine.calls.filter { $0.method == "activities.track" }.count }

        #expect(model.cachedTrack(for: activities[0]) == nil)
        let first = await model.track(for: activities[0])
        #expect(first != nil)
        #expect(model.cachedTrack(for: activities[0]) == first)
        _ = await model.track(for: activities[0])
        #expect(await reads() == 1)
        // Named by its device: with several connected the engine cannot tell which one.
        #expect(await engine.calls.last { $0.method == "activities.track" }?.params?["device"]?.stringValue
                == MockEngine.fenixID)

        // Bounded: the one used least recently goes first.
        for a in activities.dropFirst() { _ = await model.track(for: a) }
        #expect(await reads() == activities.count)
        #expect(model.cachedTrack(for: activities[0]) == nil)
        #expect(model.cachedTrack(for: activities[1]) != nil)
    }

    @Test func attachEventTriggersAutomaticMode() async throws {
        let (app, engine) = makeApp()
        let notifier = RecordingNotifier()
        app.notifier = notifier
        await app.start()
        await engine.simulateAttach(MockEngine.edgeID)
        for _ in 0..<200 where notifier.notes.isEmpty { try await Task.sleep(for: .milliseconds(10)) }
        #expect(notifier.notes.first?.0 == "Edge 1050")
        #expect(app.connectedDevices.count == 2)
    }

    @Test func busyDevice() async throws {
        let (app, _) = makeApp(.busy)
        await app.start()
        let model = try #require(app.model(for: MockEngine.fenixID))
        #expect(model.busyBy == "Garmin Express Service")
        await model.load(.updates)
        #expect(model.busyBy == "Garmin Express Service")
        #expect(model.errorMessage == nil)
    }

    @Test func installAllAsksFirstThenInstallsSoftwareAndMaps() async throws {
        let (app, engine) = makeApp()
        await app.start()
        let model = try #require(app.model(for: MockEngine.fenixID))
        await model.load(.updates)
        model.requestInstallAll()
        let plan = try #require(model.pendingPlan)
        #expect(model.operation == nil)                           // nothing before the confirmation
        #expect(plan.items.map(\.name) == ["System Software", "GPS Chipset", "TopoActive Europe"])
        #expect(plan.items.last?.change == "2025.10 → 2025.20")
        #expect(plan.totalBytes == 412_000_000 + 2_400_000 + 9_800_000_000)
        #expect(plan.hasSoftware)
        #expect(plan.freeAfter == 18_740_000_000 - plan.totalBytes)
        model.perform(plan)
        #expect(model.pendingPlan == nil)
        await model.waitForOperation()
        #expect(model.waitingUpdates.count == 2)
        #expect(model.pendingUpdates.map(\.beta) == [true])
        #expect(model.mapUpdates.isEmpty)
        let methods = await engine.calls.map(\.method)
        #expect(methods.contains("updates.install") && methods.contains("maps.update"))
        #expect(model.lastOutcome == "Unplug the device to install the software.")
    }

    @Test func largeMapIsConfirmedSmallIsNot() async throws {
        let (app, engine) = makeApp()
        await app.start()
        let model = try #require(app.model(for: MockEngine.fenixID))
        await model.load(.maps)
        let big = try #require(model.maps?.available.first { $0.size >= InstallPlan.largeMap })
        model.requestInstall(map: big)
        #expect(model.pendingPlan?.items.first?.change == "new · 2025.20")
        #expect(model.operation == nil)
        model.pendingPlan = nil
        model.requestInstall(map: DeviceMap(name: "Ski Resorts", version: "2025.10", size: 120_000_000, installed: false, status: .available))
        #expect(model.pendingPlan == nil)
        await model.waitForOperation()
        #expect(await engine.calls.map(\.method).contains("maps.install"))
    }

    /// A handheld with a memory card (GPSMAP H1i Plus): an install on the card is always
    /// confirmed, with the card's free space; it goes to the engine with `"to": "card"`.
    @Test func installOnTheMemoryCard() async throws {
        let (app, engine) = makeApp(.handheld)
        await app.start()
        let model = try #require(app.model(for: MockEngine.handheldID))
        await model.load(.maps)
        let card = try #require(model.maps?.card)
        let japan = try #require(model.maps?.available.first { $0.name == "TopoActive Japan" })
        model.requestInstall(map: japan, to: .card)
        let plan = try #require(model.pendingPlan)
        #expect(plan.toCard && !plan.isMove && plan.freeNow == card.free)
        model.perform(plan)
        await model.waitForOperation()
        let call = try #require(await engine.calls.last { $0.method == "maps.install" })
        #expect(call.params?["to"]?.stringValue == "card")
        #expect(model.maps?.maps.first { $0.name == "TopoActive Japan" }?.location == .card)
    }

    @Test func moveBetweenTheDeviceAndItsCard() async throws {
        let (app, engine) = makeApp(.handheld)
        await app.start()
        let model = try #require(app.model(for: MockEngine.handheldID))
        await model.load(.maps)
        let canada = try #require(model.maps?.installed.first { $0.name == "TopoActive Canada" })
        model.requestMove(map: canada, to: .card)
        let plan = try #require(model.pendingPlan)  // always confirmed
        #expect(plan.isMove && plan.toCard && !plan.keepsOne)
        #expect(plan.totalBytes == canada.moveSize && plan.items.first?.change == "Device → Memory Card")
        model.perform(plan)
        await model.waitForOperation()
        let call = try #require(await engine.calls.last { $0.method == "maps.move" })
        #expect(call.params?["to"]?.stringValue == "card")
        #expect(call.params?["maps"]?.arrayValue?.compactMap(\.stringValue) == ["TopoActive Canada"])
        #expect(model.maps?.maps.first { $0.name == "TopoActive Canada" }?.location == .card)
    }

    /// A long write of the memory card over USB offers the card reader; the move then goes to
    /// the engine with the card's volume.
    @Test func aSlowCardWriteOffersTheCardReader() async throws {
        let (app, engine) = makeApp(.handheld)
        await app.start()
        let model = try #require(app.model(for: MockEngine.handheldID))
        await model.load(.maps)
        let canada = try #require(model.maps?.installed.first { $0.name == "TopoActive Canada" })
        model.requestMove(map: canada, to: .card)
        var plan = try #require(model.pendingPlan)
        // Downloads go at 25 MB/s (the mock's): faster than the device reads at 8 MB/s, so a
        // download is chosen; then written to the card at 3.9 MB/s over USB, fast in the reader.
        let size = 2_864_971_776.0
        #expect(plan.source == .download)
        #expect(abs((plan.secondsMoving(from: .device) ?? 0) - (size / 8_000_000 + size / 3_900_000)) < 1)
        #expect(abs((plan.secondsMoving(from: .download) ?? 0) - (size / 25_000_000 + size / 3_900_000)) < 1)
        let usb = try #require(plan.secondsOverUSB)
        #expect(abs(usb - (size / 25_000_000 + size / 3_900_000)) < 1)
        let reader = try #require(plan.secondsInReader)
        #expect(abs(reader - (size / 25_000_000 + size / 60_000_000)) < 1)
        #expect(plan.offersCardReader)
        let readers = try #require(await model.cardReaders())
        let card = try #require(readers.card)
        #expect(card.name == "MAPCARD" && readers.remembered)
        plan.cardReader = card.path
        #expect(!plan.offersCardReader)  // chosen already
        model.perform(plan)
        await model.waitForOperation()
        let call = try #require(await engine.calls.last { $0.method == "maps.move" })
        #expect(call.params?["card_reader"]?.stringValue == "/Volumes/MAPCARD")
        #expect(call.params?["to"]?.stringValue == "card")
        #expect(call.params?["source"]?.stringValue == "download")
    }

    /// Without a known download speed the device is the source; what is in the cache is
    /// neither read nor downloaded, so it takes no time.
    @Test func theSourceOfAMoveAndWhatTheCacheSaves() {
        var plan = InstallPlan(items: [.init(id: "move:A", kind: .map, name: "A", change: nil, size: 80_000_000)],
                               action: .moveMap("A", to: .internal), freeNow: nil)
        #expect(plan.fasterSource == .device)
        #expect(plan.secondsMoving(from: .download) == nil)
        #expect(abs((plan.secondsMoving(from: .device) ?? 0) - (10 + 80_000_000 / 11_700_000)) < 0.01)
        plan.cachedBytes = 80_000_000
        #expect(abs((plan.secondsMoving(from: .device) ?? 0) - 80_000_000 / 11_700_000) < 0.01)
        plan.downloadSpeed = 1_000_000  // slow: the device stays the faster
        plan.cachedBytes = 0
        #expect(plan.fasterSource == .device)
    }

    @Test func aShortCardWriteDoesNotOfferTheCardReader() async throws {
        let (app, _) = makeApp(.handheld)
        await app.start()
        let model = try #require(app.model(for: MockEngine.handheldID))
        await model.load(.maps)
        let taiwan = try #require(model.maps?.installed.first { $0.name == "TopoActive Taiwan" })
        model.requestMove(map: taiwan, to: .internal)  // to the device: no card written
        #expect(model.pendingPlan?.offersCardReader == false)
        let japan = try #require(model.maps?.available.first { $0.name == "TopoActive Japan" })
        model.requestInstall(map: japan, to: .card)  // 1.1 GB: about 5 min over USB
        #expect(model.pendingPlan?.offersCardReader == true)
    }

    @Test func aMapOnBothStoragesIsKeptOnOne() async throws {
        let (app, _) = makeApp(.handheld)
        await app.start()
        let model = try #require(app.model(for: MockEngine.handheldID))
        await model.load(.maps)
        let both = try #require(model.maps?.installed.first { $0.location == .both })
        #expect(both.canRemove == false && both.canInstall == false && both.canMove == true)
        model.requestMove(map: both, to: .internal)
        let plan = try #require(model.pendingPlan)
        #expect(plan.keepsOne && plan.isMove && !plan.toCard && plan.totalBytes == 0)
        model.perform(plan)
        await model.waitForOperation()
        let kept = try #require(model.maps?.maps.first { $0.name == both.name })
        #expect(kept.location == .internal && kept.canRemove == true)
    }

    /// One source of truth: the badge after Overview equals the badge after Updates, the
    /// menu bar count and what Install All lists; betas are left out.
    @Test func badgeCountIsTheSameEverywhere() async throws {
        let (app, _) = makeApp()
        await app.start()
        let model = try #require(app.model(for: MockEngine.fenixID))
        await model.load(.overview)
        let afterOverview = model.badgeCount
        await model.load(.updates)
        #expect(afterOverview == 3)
        #expect(model.badgeCount == afterOverview)
        #expect(app.pendingUpdatesCount == afterOverview)
        model.requestInstallAll()
        #expect(model.pendingPlan?.items.count == afterOverview)
        #expect(model.updates.contains { $0.beta })
    }

    @Test func firstSyncAsksForTheChoice() async throws {
        let (app, engine) = makeApp()
        await app.start()
        await engine.requireFirstSyncChoice(MockEngine.fenixID)
        let model = try #require(app.model(for: MockEngine.fenixID))
        model.sync()
        await model.waitForOperation()
        #expect(model.needsFirstSyncChoice)
        #expect(model.errorMessage == nil)
        model.sync(firstRun: .newOnly)
        await model.waitForOperation()
        #expect(!model.needsFirstSyncChoice)
        #expect(model.lastOutcome?.hasPrefix("Synced: 4 files uploaded") == true)
        let last = await engine.calls.last { $0.method == "device.sync" }
        #expect(last?.params?["first_run"]?.stringValue == "baseline")
    }

    @Test func busyEventWithoutDeviceMatchesByName() async throws {
        let (app, engine) = makeApp()
        await app.start()
        await engine.simulateBusy(MockEngine.fenixID, knownSerial: false)
        for _ in 0..<100 where app.model(for: MockEngine.fenixID)?.busyBy == nil { try await Task.sleep(for: .milliseconds(5)) }
        #expect(app.model(for: MockEngine.fenixID)?.busyBy == "Garmin Express Service")
        #expect(app.unassignedBusy == nil)
    }

    @Test func backupSettingsComeFromTheEngine() async {
        let (app, _) = makeApp()
        await app.start()
        #expect(app.backupKeep == 3)
        #expect(app.backupFolder.path.hasSuffix("Documents/Garmin/Backups"))
        await app.setEngineSetting("backup_keep", 5)
        #expect(app.backupKeep == 5)
    }

    @Test func stravaConnectFlow() async {
        let (app, _) = makeApp()
        await app.start()
        await app.startStrava(clientID: "1", clientSecret: "s")
        #expect(app.stravaAuthorizeURL?.absoluteString.contains("redirect_uri=\(AppModel.stravaRedirectURI)") == true)
        await app.finishStrava(code: "abc")
        #expect(app.strava?.connected == true)
        #expect(app.strava?.athleteName == "Demo Athlete")
        #expect(app.strava?.scope?.contains("activity:write") == true)
        #expect(app.engineSettings["strava_upload"] == false)        // off unless chosen
        await app.setEngineSetting("strava_upload", true)
        #expect(app.engineSettings["strava_upload"] == true)
        #expect(app.stravaAuthorizeURL == nil)
    }

    @Test func cancelOperation() async throws {
        let engine = MockEngine(stepDelay: .milliseconds(50))
        let app = AppModel(engine: engine, settings: AppSettings(defaults: UserDefaults(suiteName: "oe-\(UUID())")!))
        await app.start()
        let model = try #require(app.model(for: MockEngine.fenixID))
        model.sync()
        try await Task.sleep(for: .milliseconds(80))
        model.cancelOperation()
        await model.waitForOperation()
        #expect(model.lastOutcome == "Cancelled.")
    }

    @Test func signIn() async {
        let (app, _) = makeApp(.signedOut)
        await app.start()
        #expect(app.account?.signedIn == false)
        await app.completeSignIn(ticketURL: "https://sso.garmin.com/sso/embed?ticket=ST-1-x")
        #expect(app.account?.signedIn == true)
        #expect(app.alertMessage == nil)
    }

    /// Switching pages cancels the view's task, never the load: no "Cancelled." error, one
    /// request, and coming back joins the load in flight.
    @Test func pageLoadSurvivesTheViewGoingAway() async throws {
        let engine = MockEngine(stepDelay: .milliseconds(20))
        let app = AppModel(engine: engine, settings: AppSettings(defaults: UserDefaults(suiteName: "oe-\(UUID())")!))
        await app.start()
        let model = try #require(app.model(for: MockEngine.fenixID))
        let view = Task { await model.load(.activities) }
        try await Task.sleep(for: .milliseconds(60))
        view.cancel()                                          // the user switched tabs
        #expect(model.loading.contains(.activities))
        let progress = try #require(model.loadProgress[.activities])
        #expect(progress.phase == "reading_activities" && progress.total == 24)
        #expect(progress.detail.hasPrefix("Reading activities from the device"))
        await model.loadIfNeeded(.activities)                  // the user came back: joins
        #expect(model.activities?.count == 24)
        #expect(model.errorMessage == nil)
        #expect(model.loadProgress[.activities] == nil)
        await model.loadIfNeeded(.activities)                  // loaded: not read again
        let reads = await engine.calls.filter { $0.method == "activities.list" }.count
        #expect(reads == 1)
    }

    /// The Apps page: the device's apps as data, grouped by kind, with the summary and the
    /// Store's note; each device its own.
    @Test func appsPageReadsTheAppsByKind() async throws {
        let (app, engine) = makeApp()
        await app.start()
        let model = try #require(app.model(for: MockEngine.fenixID))
        await model.load(.apps)
        let apps = try #require(model.apps)
        #expect(await engine.calls.last { $0.method == "apps.list" }?.params?["device"]?.stringValue == MockEngine.fenixID)
        #expect(apps.summary.storeApps == apps.apps.count && apps.summary.freeSlots == 21)
        #expect(apps.groups.map(\.kind) == [.app, .widget, .watchFace, .dataField, .music, .activity])
        #expect(apps.groups.first { $0.kind == .watchFace }?.apps.count == 3)
        #expect(apps.updatable.allSatisfy { $0.name == "Komoot" } && !apps.updatable.isEmpty)
        #expect(apps.apps.contains { !$0.enabled } && apps.apps.contains { $0.waiting == "install" })
        #expect(apps.account.map(\.name) == ["Garmin Golf"])
        #expect(apps.note?.contains("Connect IQ Store") == true)
        #expect(apps == MockEngine.sampleApps(unit: MockEngine.fenixID))
    }

    /// The engine runs one request per device at a time: a page asked for during a long
    /// load waits, and says so.
    @Test func pageWaitsBehindAnotherLoad() async throws {
        let engine = MockEngine(stepDelay: .milliseconds(20))
        let app = AppModel(engine: engine, settings: AppSettings(defaults: UserDefaults(suiteName: "oe-\(UUID())")!))
        await app.start()
        let model = try #require(app.model(for: MockEngine.fenixID))
        for _ in 0..<200 where !model.loading.isEmpty { try await Task.sleep(for: .milliseconds(5)) }
        let activities = Task { await model.load(.activities) }
        try await Task.sleep(for: .milliseconds(30))
        let apps = Task { await model.load(.apps) }
        try await Task.sleep(for: .milliseconds(30))
        #expect(model.isWaiting(.apps))
        #expect(!model.isWaiting(.activities))
        await activities.value
        await apps.value
        #expect(model.apps != nil)
        #expect(!model.isWaiting(.apps))
    }
}
