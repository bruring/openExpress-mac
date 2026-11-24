import Foundation
import Testing
@testable import OpenExpressKit

@Suite("Protocol: messages and models (docs/ENGINE.md)")
struct ProtocolTests {
    @Test func requestLine() throws {
        let line = try EngineRequest(id: 7, method: "maps.list", params: ["device": "3491967800", "all": false]).line()
        #expect(String(decoding: line, as: UTF8.self)
            == #"{"id":7,"method":"maps.list","params":{"all":false,"device":"3491967800"}}"#)
    }

    @Test func parsesResponseErrorAndEvent() throws {
        #expect(EngineIncoming.parse(#"{"id": 7, "result": {"x": 1}}"#) == .response(id: 7, result: .success(["x": 1])))
        let err = EngineIncoming.parse(#"{"id": 7, "error": {"code": "device_busy", "message": "Garmin Express Service is using the device. Quit it, then try again.", "data": {"program": "Garmin Express Service"}}}"#)
        guard case .response(7, .failure(let e)) = err else { Issue.record("not an error"); return }
        #expect(e.code == "device_busy")
        #expect(e.busyProgram == "Garmin Express Service")
        #expect(e.localizedDescription.hasPrefix("Garmin Express Service is using"))

        let ev = EngineIncoming.parse(#"{"event": "progress", "params": {"request": 9, "phase": "downloading", "done": 1048576, "total": 52428800, "item": "TopoActive Western Europe"}}"#)
        guard case .event(.progress(let p)) = ev else { Issue.record("not progress"); return }
        #expect(p.request == 9)
        #expect(p.item == "TopoActive Western Europe")
        #expect(abs((p.fraction ?? 0) - 0.02) < 0.0001)
        #expect(EngineIncoming.parse("Traceback (most recent call last):") == nil)
    }

    /// `account.readings` as the engine answers it: snake case, a reading left out as null.
    @Test func accountReadingsAsTheEngineSendsThem() throws {
        let json = #"{"signed_in": true, "on_account": null, "sensors": {"state": "read", "summary": "2 sensors · 1 with a low battery", "low": 1, "sensors": [{"name": "Made-up Light", "detail": "Battery low · last connected 2 days ago", "low": true}, {"name": "Made-up Strap", "detail": "Battery 80% · last connected today, 09:00", "low": false}]}, "solar": {"state": "failed", "message": null}}"#
        let r = try EngineCoding.decoder().decode(AccountReadings.self, from: Data(json.utf8))
        #expect(r.signedIn && r.onAccount == nil && !r.isEmpty)
        #expect(r.sensors?.state == .read && r.sensors?.low == 1)
        #expect(r.sensors?.sensors?.map(\.low) == [true, false])
        #expect(r.solar == .init(state: .failed))

        let off = #"{"signed_in": true, "on_account": false, "sensors": null, "solar": null}"#
        #expect(try EngineCoding.decoder().decode(AccountReadings.self, from: Data(off.utf8)).isEmpty)
        let out = #"{"signed_in": false, "on_account": null, "sensors": {"state": "signed_out"}, "solar": null}"#
        #expect(try EngineCoding.decoder().decode(AccountReadings.self, from: Data(out.utf8)).sensors?.state == .signedOut)
    }

    /// Points in the engine's format (activities.track): short names, ISO time. Made-up place.
    @Test func trackPointsAsTheEngineSendsThem() throws {
        let json = #"{"points": [{"lat": 51.5007, "lon": -0.1246, "time": "2030-01-01T12:00:00+01:00", "hr": 72, "alt": -7.0, "speed": 0.898}, {"lat": 51.5008, "lon": -0.1247, "time": "2030-01-01T12:00:05.500000+01:00"}]}"#
        let track = try EngineCoding.decoder().decode(ActivityTrack.self, from: Data(json.utf8))
        #expect(track.points.count == 2)
        #expect(track.points[0].latitude == 51.5007)
        #expect(track.points[0].heartRate == 72)
        #expect(track.points[0].elevation == -7.0)
        #expect(track.points[0].time == 0)
        #expect(track.points[1].time == 5.5)
        #expect(track.points[1].heartRate == nil)
    }

    @Test func busyEvent() {
        let ev = EngineIncoming.parse(#"{"event": "device.busy", "params": {"device": "1", "program": "Garmin Express Service"}}"#)
        #expect(ev == .event(.deviceBusy(DeviceBusy(device: "1", program: "Garmin Express Service", name: nil))))
    }

    @Test func device() throws {
        let json: JSONValue = try JSONDecoder().decode(JSONValue.self, from: Data(#"""
        {"unit_id": "3491967800", "name": "fēnix 8", "part_number": "006-B4536-00", "software": "21.19",
         "connected": true, "busy_by": null, "storage": {"free": 1000, "capacity": 4000},
         "last_sync": "2026-11-17T09:41:00.123456+01:00"}
        """#.utf8))
        let d = try json.decode(Device.self)
        #expect(d.unitID == "3491967800")
        #expect(d.partNumber == "006-B4536-00")
        #expect(d.storage?.usedFraction == 0.75)
        #expect(d.kind == .watch)
        let expected = try Date("2026-11-17T08:41:00Z", strategy: .iso8601).addingTimeInterval(0.123456)
        #expect(abs(d.lastSync!.timeIntervalSince(expected)) < 0.001)
        // Round trip through the engine's coding.
        let again = try JSONValue(encoding: d).decode(Device.self)
        #expect(again.unitID == d.unitID && again.storage == d.storage && again.name == d.name)
        #expect(abs(again.lastSync!.timeIntervalSince(d.lastSync!)) < 0.001)
    }

    @Test func dates() {
        #expect(EngineCoding.parseDate("2026-11-17T09:41:00+01:00") != nil)
        #expect(EngineCoding.parseDate("2026-11-17T08:41:00Z") != nil)
        #expect(EngineCoding.parseDate("2026-11-17T09:41:00.5+01:00")!.timeIntervalSince(
            EngineCoding.parseDate("2026-11-17T09:41:00+01:00")!) == 0.5)
        #expect(EngineCoding.parseDate("yesterday") == nil)
    }

    @Test func updatesAndMaps() throws {
        let u = try JSONValue(["updates": [["part": "006-B4536-00", "name": "System Software", "version": "21.22",
                                             "installed": "21.19", "size": 412000000, "beta": false,
                                             "waiting_on_device": true]], "beta_offered": true])
            .decode(UpdatesList.self)
        #expect(u.betaOffered)
        #expect(u.updates.first?.waitingOnDevice == true)
        let m = try JSONValue(["maps": [["name": "TopoActive", "version": "2025.20", "size": 1, "installed": true, "status": "up to date"],
                                        ["name": "Ski", "size": 1, "installed": true, "status": "update"],
                                        ["name": "Golf", "size": 1, "installed": false, "status": "something new"]],
                               "own_maps": [["file": "contours.img", "title": nil, "size": 5, "path": "Garmin/contours.img"]], "free": 100])
            .decode(MapsList.self)
        #expect(m.installed.count == 2)
        #expect(m.updatable.map(\.name) == ["Ski"])
        #expect(m.maps.last?.status == .unknown)
        #expect(m.ownMaps.map(\.name) == ["contours.img"])   // no title: the file's name
        #expect(m.storages.isEmpty && m.card == nil)   // an engine from before the memory card
    }

    /// A handheld with a memory card (GPSMAP H1i Plus): both storages, and where each map is.
    @Test func mapsOnTheMemoryCard() throws {
        let m = try JSONValue(["maps": [["name": "TopoActive Canada", "size": 1, "installed": true, "status": "up to date",
                                         "location": "internal", "can_remove": true],
                                        ["name": "TopoActive Taiwan", "size": 1, "installed": true, "status": "update",
                                         "location": "card", "can_install": false, "can_remove": false],
                                        ["name": "TopoActive Japan", "size": 1, "installed": false, "status": "available",
                                         "location": nil],
                                        ["name": "Odd", "size": 1, "installed": true, "status": "up to date",
                                         "location": "somewhere"],
                                        ["name": "TopoActive Greenland", "size": 1, "installed": true,
                                         "status": "up to date", "location": "card_out", "can_install": false]],
                               "storages": [["id": "internal", "name": "Internal Storage", "free": 35, "capacity": 61,
                                             "removable": false],
                                            ["id": "card", "name": "Memory Card", "free": 32, "capacity": 128,
                                             "removable": true]],
                               "free": 35])
            .decode(MapsList.self)
        #expect(m.maps.map(\.location) == [.internal, .card, nil, .unknown, .cardOut])
        let removed: JSONValue = ["name": "TopoActive Taiwan", "size": 1, "installed": true,
                                  "status": "up to date", "waiting": "remove", "location": nil]
        let gone = try removed.decode(DeviceMap.self)
        #expect(gone.isBeingRemoved && gone.location == nil)  // removed: in no storage now
        #expect(m.card?.name == "Memory Card" && m.card?.removable == true)
        #expect(m.card?.usedFraction == 0.75)
        #expect(m.updatable.isEmpty)   // an update of a map on the card is not offered
    }


    /// The mock sends activities and own maps under the engine's keys.
    @Test func activityAndOwnMapRoundTrip() throws {
        let a = Activity(start: .now, sport: "Running", fitPath: "/b/x.fit", averageHeartRate: 140, source: "backup", sportID: 1)
        let json = try JSONValue(encoding: a)
        #expect(json["path"]?.stringValue == "/b/x.fit" && json["sport_id"]?.intValue == 1)
        let again = try json.decode(Activity.self)
        #expect(again.id == a.id && again.averageHeartRate == 140)
        let own = try JSONValue(encoding: OwnMap(name: "trails.img", size: 5)).decode(OwnMap.self)
        #expect(own == OwnMap(name: "trails.img", size: 5))
    }

    @Test func syncResult() {
        let r = SyncResult(raw: ["dry_run": false, "uploaded": ["a.fit", "b.fit"], "downloaded": 1,
                                 "sentence": "Synced.", "drop_folder": ["path": "/x", "copied": 3, "bytes": 1, "incomplete": 0],
                                 "strava": ["uploaded": 2, "duplicates": 0]])
        #expect(r.uploaded == 2)
        #expect(r.downloaded == 1)
        #expect(r.sentence == "Synced.")
        #expect(r.stravaUploads == 2)
        #expect(r.dropFolderCopies == 3)
        #expect(SyncResult(raw: ["strava": ["error": "rate limited"], "drop_folder": nil]).stravaError == "rate limited")
    }

    @Test func commandLineAndMapResults() {
        #expect(OperationResult(raw: ["exit_code": 0, "lines": ["ok"]]).failureMessage == nil)
        #expect(OperationResult(raw: ["exit_code": 1, "lines": ["a", "No room on the device."]]).failureMessage == "a\nNo room on the device.")
        let maps = OperationResult(raw: ["dry_run": false, "maps": [
            ["name": "A", "done": true, "exit_code": 0, "lines": []],
            ["name": "B", "done": false, "code": "failed", "reason": "Not enough space."]]])
        #expect(maps.refused == ["B": "Not enough space."])
        #expect(maps.failureMessage == "B: Not enough space.")
    }

    @Test func finalProtocolShapes() throws {
        let hello = try JSONValue(["engine": "1.0", "protocol": 1, "platform": "darwin", "methods": ["hello"]]).decode(EngineHello.self)
        #expect(hello.protocolVersion == 1)
        let busy = EngineIncoming.parse(#"{"event": "device.busy", "params": {"device": null, "program": "Garmin Express Service", "name": "fēnix 8"}}"#)
        #expect(busy == .event(.deviceBusy(DeviceBusy(device: nil, program: "Garmin Express Service", name: "fēnix 8"))))
        let log = EngineIncoming.parse(#"{"event": "log", "params": {"level": "info", "message": "x", "request": 4}}"#)
        #expect(log == .event(.log(EngineLogLine(level: "info", message: "x", request: 4))))
        let strava = try JSONValue(["configured": true, "connected": false, "athlete_id": nil, "athlete_name": nil,
                                    "authorize_url": "https://www.strava.com/oauth/authorize?x=1"]).decode(StravaStatus.self)
        #expect(strava.configured && !strava.connected)
        #expect(strava.authorizeURL == "https://www.strava.com/oauth/authorize?x=1")
        let apps = try JSONValue(["summary": ["store_apps": 1, "free_slots": nil, "free_bytes": nil],
                                  "apps": [["id": "x", "name": "Klimat", "kind": "hologram", "enabled": true]],
                                  "account": [], "message": nil, "warning": nil, "note": "n"]).decode(AppsList.self)
        // A kind this app does not know yet is "other", not an error.
        #expect(apps.apps.first?.kind == .unknown && apps.summary.totalSlots == nil)
        let a = try JSONValue(["name": "Morning Run", "source": "backup", "path": "/b/x.fit", "size": 10,
                               "start": "2026-11-17T09:41:00+01:00", "sport": "running", "sport_id": 1,
                               "distance": 5000, "duration": 1500, "ascent": nil, "average_heart_rate": 148,
                               "calories": 812, "state": "complete"]).decode(Activity.self)
        #expect(a.fitPath == "/b/x.fit" && a.isOnThisMac && a.state == "complete")
        #expect(a.averageHeartRate == 148 && a.calories == 812)
        let own = try JSONValue(["maps": [], "own_maps": [["file": "a.img", "size": 5, "title": "Alps", "path": "Garmin/a.img"]],
                                 "free": 1, "message": "No maps service."]).decode(MapsList.self)
        #expect(own.ownMaps.first?.name == "Alps" && own.message == "No maps service.")
    }

    @Test func mapVersionChange() {
        #expect(DeviceMap(name: "A", version: "2025.20", size: 1, installed: true, status: .update, installedVersion: "2025.10").versionChange == "2025.10 → 2025.20")
        #expect(DeviceMap(name: "A", version: "2025.20", size: 1, installed: false, status: .available).versionChange == "new · 2025.20")
    }

    @Test func shortName() {
        #expect(Device(unitID: "1", name: "fēnix 8 – 47 mm, AMOLED", connected: true).shortName == "fēnix 8")
        #expect(Device(unitID: "1", name: "Edge 1050", connected: true).shortName == "Edge 1050")
        #expect(Device(unitID: "1", name: "Instinct 3 - 45mm Tactical", connected: true).shortName == "Instinct 3")
    }
}

private extension JSONValue {
    init(_ v: JSONValue) { self = v }
}

@Suite("Support")
struct SupportTests {
    @Test func lang() {
        #expect(EngineEnvironment.lang(preferredLanguages: ["de-DE", "en"]) == "de_DE.UTF-8")
        #expect(EngineEnvironment.lang(preferredLanguages: ["pl"]) == "pl.UTF-8")
        #expect(EngineEnvironment.lang(preferredLanguages: ["zh-Hans-CN"]) == "zh_CN.UTF-8")
        #expect(EngineEnvironment.lang(preferredLanguages: []) == "en_US.UTF-8")
        #expect(EngineEnvironment.overrides(preferredLanguages: ["nl-NL"])["PYTHONUNBUFFERED"] == "1")
    }

    @Test func engineLocation() {
        let home = URL(fileURLWithPath: "/Users/x")
        let res = URL(fileURLWithPath: "/A.app/Contents/Resources")
        #expect(EngineLocation.resolve(environment: ["OPENEXPRESS_ENGINE": "mock"], override: "/e", resources: res, home: home) == .mock(scenario: nil))
        #expect(EngineLocation.resolve(environment: ["OPENEXPRESS_ENGINE": "mock:busy"], override: nil, resources: nil, home: home) == .mock(scenario: "busy"))
        #expect(EngineLocation.resolve(environment: [:], override: "/opt/oe", resources: res, home: home) == .executable(URL(fileURLWithPath: "/opt/oe")))
        #expect(EngineLocation.resolve(environment: [:], override: "", resources: res, home: home, fileExists: { _ in true })
            == .executable(res.appending(path: "engine/bin/openexpress")))
        #expect(EngineLocation.resolve(environment: [:], override: nil, resources: res, home: home, fileExists: { _ in false })
            == .executable(URL(fileURLWithPath: "/Users/x/.venvs/openexpress-mac/bin/openexpress")))
    }

    @Test func signInTicket() {
        #expect(SignInTicket.fromURL(URL(string: "https://sso.garmin.com/sso/embed?ticket=ST-0123-abc-cas")!)
            == "https://sso.garmin.com/sso/embed?ticket=ST-0123-abc-cas")
        #expect(SignInTicket.fromURL(URL(string: "https://sso.garmin.com/sso/signin")!) == nil)
        #expect(SignInTicket.fromPageText("{serviceUrl: 'https://sso.garmin.com/sso/embed', serviceTicket: 'ST-1234567-AbCdEf-sso'}")
            == "https://sso.garmin.com/sso/embed?ticket=ST-1234567-AbCdEf-sso")
        #expect(SignInTicket.fromPageText(#"{"serviceUrl":"https://sso.garmin.com/sso/embed","serviceTicket":"ST-9-x"}"#)
            == "https://sso.garmin.com/sso/embed?ticket=ST-9-x")
        #expect(SignInTicket.fromPageText("<html>Sign in</html>") == nil)
    }

    @Test func backupsAsTheEngineSendsThem() throws {
        let line = #"{"backups": [{"path": "/b/42/2026-11-15 (08.30.00)", "date": "2026-11-15T08:30:00+01:00", "files": 4210, "bytes": 1200000000}]}"#
        let list = try EngineCoding.decoder().decode(BackupsList.self, from: Data(line.utf8))
        #expect(list.backups[0].files == 4210 && list.backups[0].bytes == 1_200_000_000)
        #expect(list.backups[0].url.lastPathComponent == "2026-11-15 (08.30.00)")
    }

    /// Exactly what engine.py's apps_list sends (a fēnix 8, read with `builtin`).
    @Test func appsAsTheEngineSendsThem() throws {
        let line = #"""
        {"summary": {"store_apps": 6, "free_slots": 23, "free_bytes": 63315235},
         "apps": [
          {"id": "7cd38736-510a-4480-8801-0dfe2f5944af", "name": "Connect IQ Store", "kind": "app", "kind_name": "app", "version": 71, "enabled": true, "update": null, "waiting": null, "waiting_text": null, "removable": false},
          {"id": "c33a05fa-bb7b-46a4-87b4-482a3e726a11", "name": "Komoot", "kind": "widget", "kind_name": "widget", "version": 222, "enabled": true, "update": {"version": "4.26.23", "size": 412000, "permissions_changed": true}, "waiting": null, "waiting_text": null, "removable": true},
          {"id": "c33a05fa-bb7b-46a4-87b4-482a3e726a11", "name": "Komoot", "kind": "activity", "kind_name": "activity", "version": 222, "enabled": true, "update": null, "waiting": "install", "waiting_text": "will be installed after the device is unplugged", "removable": true},
          {"id": "0d1e2f3a-0000-4000-8000-000000000002", "name": "Spotify", "kind": "music", "kind_name": "music app", "version": 73, "enabled": false, "update": null, "waiting": null, "waiting_text": null, "removable": true},
          {"id": "0d1e2f3a-0000-4000-8000-000000000003", "name": "Segment34 MkII", "kind": "watch_face", "kind_name": "watch face", "version": 144, "enabled": false, "update": null, "waiting": null, "waiting_text": null, "removable": true},
          {"id": "0d1e2f3a-0000-4000-8000-000000000004", "name": "TideCastDF", "kind": "data_field", "kind_name": "data field", "version": 4, "enabled": true, "update": null, "waiting": null, "waiting_text": null, "removable": true}],
         "account": [{"id": "00000000-0000-0000-0000-000000000007", "name": "Trail Field", "kind": "data_field", "kind_name": "data field", "version": "1.0", "size": 1000, "developer": "dev", "permissions_changed": false}],
         "message": null, "warning": null,
         "note": "New apps are chosen in the Connect IQ Store (website or phone); they then appear in the list above.",
         "builtin": [{"id": "c0ffee15-600d-540c-0000-000000000000", "name": "Softball", "kind": "activity", "kind_name": "activity", "enabled": false}]}
        """#
        let list = try EngineCoding.decoder().decode(AppsList.self, from: Data(line.utf8))
        #expect(list.summary == AppsSummary(storeApps: 6, freeSlots: 23, freeBytes: 63_315_235))
        #expect(list.summary.totalSlots == 29)
        #expect(list.apps.map(\.kind) == [.app, .widget, .activity, .music, .watchFace, .dataField])
        // One app as a widget and as an activity: two rows, told apart by their kind.
        #expect(Set(list.apps.map(\.id)).count == list.apps.count)
        let komoot = list.apps[1]
        #expect(komoot.kindName == "widget" && komoot.version == 222 && komoot.removable)
        #expect(komoot.update == AppUpdate(version: "4.26.23", size: 412_000, permissionsChanged: true))
        #expect(list.apps[2].waiting == "install" && list.apps[2].waitingText?.hasPrefix("will be installed") == true)
        #expect(!list.apps[0].removable && list.apps[0].update == nil)
        #expect(list.apps[3].enabled == false && list.disabledCount == 2)
        #expect(list.updatable.map(\.name) == ["Komoot"])
        #expect(list.groups.map(\.kind) == [.app, .widget, .watchFace, .dataField, .music, .activity])
        #expect(list.account.first?.kind == .dataField && list.account.first?.version == "1.0")
        #expect(list.builtin?.first?.name == "Softball" && list.builtin?.first?.enabled == false)
        #expect(list.message == nil && list.warning == nil && list.note?.hasPrefix("New apps") == true)
    }

    @Test func typedResultStraightFromTheLine() throws {
        let list = ActivitiesList(activities: MockEngine.sampleActivities(count: 5), total: 5)
        let result = String(decoding: try EngineCoding.encoder().encode(list), as: UTF8.self)
        let line = Data(#"{"id": 12, "result": \#(result)}"#.utf8)

        let envelope = try #require(EngineEnvelope.of(line))
        #expect(envelope.id == 12 && envelope.engineError == nil && envelope.event == nil)
        let direct = try EngineCoding.decodeResult(ActivitiesList.self, fromLine: line)
        #expect(direct.total == 5 && direct.activities.map(\.id) == list.activities.map(\.id))
        // The same as through the untyped value.
        guard case .response(12, .success(let value))? = EngineIncoming.parse(String(decoding: line, as: UTF8.self))
        else { Issue.record("not a response"); return }
        #expect(try value.decode(ActivitiesList.self) == direct)

        let error = try #require(EngineEnvelope.of(Data(#"{"id": 3, "error": {"code": "device_busy", "message": "m", "data": {"program": "X"}}}"#.utf8)))
        #expect(error.engineError?.busyProgram == "X")
        #expect(EngineEnvelope.of(Data(#"{"event": "log", "params": {"level": "info", "message": "x"}}"#.utf8))?.event == "log")
        #expect(EngineEnvelope.of(Data("Traceback (most recent call last):".utf8)) == nil)
        #expect(EngineEnvelope.of(Data("[1, 2]".utf8)) == nil)
    }

    @Test func formats() {
        #expect(Format.duration(605) == "10:05")
        #expect(Format.duration(3725) == "1:02:05")
    }
}
