import Foundation

/// An engine in memory, speaking the same protocol as the real one: for SwiftUI previews,
/// tests and `OPENEXPRESS_ENGINE=mock`. It never touches a device or the network.
public actor MockEngine: EngineConnection {
    public enum Scenario: String, Sendable, CaseIterable {
        /// A fēnix 8 connected with updates, an Edge 1050 remembered; signed in.
        case standard
        /// The fēnix 8 is held by Garmin Express Service.
        case busy
        /// Setup not done, not signed in.
        case firstRun
        /// Set up, not signed in.
        case signedOut
        /// No devices.
        case empty
        /// As standard, with 280 activities (a well-used watch).
        case many
        /// A GPSMAP H1i Plus connected with a memory card (a map on it, one on both storages);
        /// the fēnix 8 and the Edge 1050 remembered.
        case handheld
    }

    public let scenario: Scenario
    /// How long each simulated step of a long operation takes.
    public var stepDelay: Duration
    private let broadcaster = EventBroadcaster()
    private var nextID = 1
    private(set) public var calls: [EngineRequest] = []
    private var account: AccountStatus
    private var stravaConnected = false
    private var devices: [Device]
    private var updates: [String: [SoftwareUpdate]]
    private var maps: [String: [DeviceMap]]
    private var settings: [String: JSONValue] = [
        "copy_fit_to": "", "copy_fit_types": "", "environment": "production", "locale": "",
        "backup_dir": "~/Documents/Garmin/Backups", "backup_keep": 3,
        "strava_upload": false,
    ]
    private var stravaConfigured = false
    /// Devices with a request running: the engine runs one request per device at a time.
    private var deviceInUse: Set<String> = []
    /// Devices whose activities were read once (later reads come from the engine's cache).
    private var activitiesCached: Set<String> = []
    /// Devices whose next sync is their first: it needs `first_run`.
    private var firstSyncPending: Set<String> = []
    private var backups: [String: [BackupSnapshot]] = [:]

    public static let fenixID = "3491967800"
    public static let edgeID = "3398172205"
    public static let handheldID = "3412345678"
    /// The storages maps may be on, by device: the handheld's own and its memory card.
    private var mapStorages: [String: [MapStorage]] = [:]

    public init(scenario: Scenario = .standard, stepDelay: Duration = .milliseconds(350)) {
        self.scenario = scenario
        self.stepDelay = stepDelay
        let now = Date()
        account = switch scenario {
        case .firstRun: AccountStatus(signedIn: false, account: nil, setupDone: false)
        case .signedOut: AccountStatus(signedIn: false, account: nil, setupDone: true)
        default: AccountStatus(signedIn: true, account: "d***@e***.com", setupDone: true)
        }
        let fenix = Device(
            unitID: Self.fenixID, name: "fēnix 8 – 47 mm, AMOLED", partNumber: "006-B4536-00",
            software: "21.19", connected: scenario != .handheld,
            busyBy: scenario == .busy ? "Garmin Express Service" : nil,
            storage: .init(free: 18_740_000_000, capacity: 31_200_000_000),
            lastSync: now.addingTimeInterval(-3600 * 26), lastBackup: now.addingTimeInterval(-3600 * 26))
        let edge = Device(
            unitID: Self.edgeID, name: "Edge 1050", partNumber: "006-B4440-00", software: "33.20",
            connected: false, storage: .init(free: 41_000_000_000, capacity: 59_600_000_000),
            lastSync: now.addingTimeInterval(-86400 * 9))
        let handheld = Device(
            unitID: Self.handheldID, name: "GPSMAP H1i Plus", partNumber: "006-B4552-00", software: "14.08",
            connected: true, storage: .init(free: 35_800_000_000, capacity: 61_900_000_000),
            lastSync: now.addingTimeInterval(-3600 * 50))
        devices = switch scenario {
        case .empty: []
        case .handheld: [fenix, edge, handheld]
        default: [fenix, edge]
        }
        updates = [
            Self.fenixID: [
                SoftwareUpdate(part: "006-B4536-00", name: "System Software", version: "21.22",
                               installed: "21.19", size: 412_000_000),
                SoftwareUpdate(part: "006-B3290-00", name: "GPS Chipset", version: "8.10",
                               installed: "8.04", size: 2_400_000),
                SoftwareUpdate(part: "006-B4536-BETA", name: "System Software (Beta)", version: "22.05",
                               installed: "21.19", size: 418_000_000, beta: true),
            ],
            Self.edgeID: [
                SoftwareUpdate(part: "006-B4440-00", name: "System Software", version: "33.21",
                               installed: "33.20", size: 97_700_000, beta: true),
            ],
        ]
        maps = [
            Self.fenixID: [
                DeviceMap(name: "TopoActive Europe", version: "2025.20", size: 9_800_000_000, installed: true, status: .update, region: "Europe", installedVersion: "2025.10"),
                DeviceMap(name: "Ski Resorts", version: "2025.10", size: 120_000_000, installed: true, status: .upToDate),
                DeviceMap(name: "CourseView Golf", version: "2025.30", size: 1_300_000_000, installed: true, status: .upToDate),
                DeviceMap(name: "TopoActive North America", version: "2025.20", size: 11_200_000_000, installed: false, status: .available, region: "North America"),
                DeviceMap(name: "TopoActive Australia & New Zealand", version: "2025.20", size: 3_100_000_000, installed: false, status: .available, region: "Oceania"),
            ],
            Self.edgeID: [
                DeviceMap(name: "TopoActive Europe", version: "2025.20", size: 9_800_000_000, installed: true, status: .upToDate, region: "Europe"),
            ],
            Self.handheldID: [
                DeviceMap(name: "TopoActive Canada", version: "2026.11", size: 6_300_000_000, installed: true,
                          status: .upToDate, canInstall: true, canRemove: true, location: .internal,
                          canMove: true, moveSize: 2_864_971_776),
                DeviceMap(name: "TopoActive Taiwan", version: "2026.10", size: 143_200_000, installed: true,
                          status: .upToDate, canInstall: true, canRemove: true, location: .card,
                          canMove: true, moveSize: 119_967_744),
                DeviceMap(name: "TopoActive Hong Kong & Macao", version: "2026.10", size: 29_800_000, installed: true,
                          status: .upToDate,
                          statusText: "On the device and on its memory card: openExpress does not change it",
                          canInstall: false, canRemove: false, location: .both, canMove: true, moveSize: 23_949_312),
                DeviceMap(name: "TopoActive Japan", version: "2026.10", size: 1_100_000_000, installed: false,
                          status: .available, canInstall: true),
            ],
        ]
        mapStorages[Self.handheldID] = [
            MapStorage(id: .internal, name: "Internal Storage", free: 35_800_000_000, capacity: 61_900_000_000),
            MapStorage(id: .card, name: "Memory Card", free: 127_600_000_000, capacity: 127_800_000_000,
                       removable: true),
        ]
        for id in [Self.fenixID, Self.edgeID] {
            backups[id] = (0..<3).map { i in
                let date = now.addingTimeInterval(-86400 * Double(i * 7 + 1))
                return BackupSnapshot(path: "/Users/demo/Documents/Garmin/Backups/\(id)/\(Self.stamp(date))",
                                      date: date, files: 4210 - i * 50, bytes: 1_200_000_000 - Int64(i) * 40_000_000)
            }
        }
    }

    public func start() async throws {}

    public func events() -> AsyncStream<EngineEvent> { broadcaster.subscribe() }

    public func shutdown() async { broadcaster.finish() }

    /// Simulates plugging in the remembered Edge (for previews and tests).
    public func simulateAttach(_ unitID: String = edgeID) {
        guard let i = devices.firstIndex(where: { $0.unitID == unitID }) else { return }
        devices[i].connected = true
        broadcaster.yield(.deviceAttached(devices[i]))
    }

    public func simulateDetach(_ unitID: String) {
        guard let i = devices.firstIndex(where: { $0.unitID == unitID }) else { return }
        devices[i].connected = false
        broadcaster.yield(.deviceDetached(devices[i]))
    }

    public func simulateBusy(_ unitID: String, program: String = "Garmin Express Service", knownSerial: Bool = true) {
        guard let i = devices.firstIndex(where: { $0.unitID == unitID }) else { return }
        devices[i].busyBy = program
        broadcaster.yield(.deviceBusy(DeviceBusy(device: knownSerial ? unitID : nil, program: program, name: devices[i].name)))
    }

    /// The next sync of this device is its first: it answers `first_sync_choice`.
    public func requireFirstSyncChoice(_ unitID: String) {
        firstSyncPending.insert(unitID)
    }

    public func call(_ method: String, params: JSONValue?,
                     progress: (@Sendable (EngineProgress) -> Void)?) async throws -> JSONValue {
        let id = nextID
        nextID += 1
        calls.append(EngineRequest(id: id, method: method, params: params))
        let p = params ?? .object([:])
        // `device` may be left out when exactly one device is connected.
        var unit = p["device"]?.stringValue
        if unit == nil, method.hasPrefix("device.") || method.hasPrefix("updates.") || method.hasPrefix("maps.") || method == "apps.list" {
            let connected = devices.filter(\.connected)
            guard connected.count == 1 else {
                throw EngineError.remote(code: connected.isEmpty ? "no_device" : "several_devices",
                                         message: connected.isEmpty ? "No device is connected." : "Several devices are connected: say which.", data: nil)
            }
            unit = connected[0].unitID
        }
        // One request per device at a time, as the engine: the others wait their turn.
        // The readings of the account are not the device's: they wait for no device.
        let queued = method == "account.readings" ? nil : unit.flatMap { u in devices.contains { $0.unitID == u } ? u : nil }
        if let queued {
            while deviceInUse.contains(queued) { try await Task.sleep(for: .milliseconds(5)) }
            deviceInUse.insert(queued)
        }
        defer { if let queued { deviceInUse.remove(queued) } }
        if let unit, let d = devices.first(where: { $0.unitID == unit }) {
            if let busy = d.busyBy, method != "device.info" {
                throw EngineError.remote(
                    code: "device_busy",
                    message: "\(busy) is using the device. Quit it, then try again.",
                    data: ["program": .string(busy)])
            }
            if !d.connected, ["device.backup", "device.sync", "updates.install", "maps.install",
                              "maps.update", "maps.remove", "maps.move", "apps.list"].contains(method) {
                throw EngineError.remote(code: "no_device",
                                         message: "\(d.name) is not connected.", data: nil)
            }
        }
        switch method {
        case "hello":
            return try JSONValue(encoding: EngineHello(engine: "0.0-mock", protocolVersion: 1, platform: "darwin", methods: Self.methods))
        case "account.status":
            return try JSONValue(encoding: account)
        case "account.setup":
            account.setupDone = true
            return [:]
        case "account.sign_in_url":
            return ["url": "https://sso.garmin.com/sso/embed?mock=1"]
        case "account.sign_in":
            guard p["ticket_url"]?.stringValue?.contains("ticket=ST-") == true else {
                throw EngineError.remote(code: "invalid_ticket", message: "That address has no sign-in ticket.", data: nil)
            }
            account.signedIn = true
            account.account = "d***@e***.com"
            return ["account": .string(account.account!)]
        case "account.sign_out":
            let was = account.signedIn
            account.signedIn = false
            account.account = nil
            return ["removed": .bool(was)]
        case "devices.list":
            return ["devices": .array(try devices.map { try JSONValue(encoding: $0) })]
        case "device.info":
            guard let d = devices.first(where: { $0.unitID == unit }) else { throw Self.noDevice }
            return try JSONValue(encoding: d)
        case "device.backup":
            try await simulate(id, phases: [("reading", 12)], item: "Activities", progress: progress)
            let now = Date()
            let path = "/Users/demo/Documents/Garmin/Backups/\(unit ?? "")/\(Self.stamp(now))"
            if p["apply"]?.boolValue == true, let unit {
                backups[unit, default: []].insert(BackupSnapshot(path: path, date: now, files: 4260, bytes: 1_210_000_000), at: 0)
                if let i = devices.firstIndex(where: { $0.unitID == unit }) { devices[i].lastBackup = now }
            }
            var r = try JSONValue(encoding: BackupResult(path: path, copied: 37, copiedBytes: 18_400_000,
                                                         linked: 4223, linkedBytes: 1_191_600_000)).objectValue ?? [:]
            r["dry_run"] = false
            r["files"] = 4260
            r["bytes"] = 1_210_000_000
            return .object(r)
        case "device.sync":
            if let unit, firstSyncPending.contains(unit) {
                guard p["first_run"]?.stringValue != nil else {
                    throw EngineError.remote(code: "first_sync_choice",
                                             message: "This is the first sync of the device: choose what to upload.", data: nil)
                }
                firstSyncPending.remove(unit)
            }
            try await simulate(id, phases: [("sync", 6)], item: "Uploading activities", progress: progress)
            broadcaster.yield(.log(EngineLogLine(level: "info", message: "4 files uploaded.", request: id)))
            if p["apply"]?.boolValue == true, let i = devices.firstIndex(where: { $0.unitID == unit }) {
                devices[i].lastSync = Date()
            }
            return ["dry_run": false, "uploaded": 4, "downloaded": 1, "skipped_known": 120, "rejected": 0,
                    "errors": 0, "not_confirmed": 0, "steps": ["4 files uploaded.", "1 course written."],
                    "sentence": "Synced: 4 files uploaded, 1 written to the device.",
                    "drop_folder": ["path": "~/Desktop/FIT", "copied": 2, "bytes": 412_000, "incomplete": 0],
                    "strava": stravaConnected ? ["uploaded": 1, "duplicates": 0, "failed": 0] : .null]
        case "updates.list":
            let list = updates[unit ?? ""] ?? []
            let state = list.contains(where: \.waitingOnDevice) ? "ready" : list.isEmpty ? "current" : "available"
            return ["updates": .array(try list.map { try JSONValue(encoding: $0) }),
                    "beta_offered": .bool(list.contains(where: \.beta)), "state": .string(state), "add_ons": []]
        case "updates.install":
            guard let unit else { throw Self.noDevice }
            let wanted: [String]? = p["parts"]?.arrayValue?.compactMap(\.stringValue)
            var lines: [JSONValue] = []
            for (i, u) in (updates[unit] ?? []).enumerated() where wanted?.contains(u.part) ?? !u.beta {
                try await simulate(id, phases: [("downloading", 8), ("copying", 4)], item: u.name,
                                   total: u.size, progress: progress)
                updates[unit]?[i].waitingOnDevice = true
                lines.append(.string("\(u.name) \(u.version) copied; unplug the device to install it."))
            }
            return ["exit_code": 0, "lines": .array(lines)]
        case "maps.list":
            return ["maps": .array(try (maps[unit ?? ""] ?? []).map { try JSONValue(encoding: $0) }),
                    "own_maps": [["file": "Alps-Contours.img", "title": "Alps contours", "size": 245_000_000,
                                  "path": "Garmin/Alps-Contours.img"]],
                    "more_available": 0,
                    "download_speed": unit == Self.handheldID ? 25_000_000 : nil,
                    "storages": .array(try (mapStorages[unit ?? ""] ?? []).map { try JSONValue(encoding: $0) }),
                    "free": .number(Double(devices.first { $0.unitID == unit }?.storage?.free ?? 0))]
        case "maps.update":
            guard let unit else { throw Self.noDevice }
            var lines: [JSONValue] = []
            for (i, m) in (maps[unit] ?? []).enumerated() where m.installed && m.status == .update {
                try await simulate(id, phases: [("downloading", 10), ("copying", 4)], item: m.name,
                                   total: m.size, progress: progress)
                maps[unit]?[i].status = .upToDate
                maps[unit]?[i].installedVersion = m.version
                lines.append(.string("\(m.name) updated."))
            }
            return ["exit_code": 0, "lines": .array(lines)]
        case "maps.card_reader":
            // The handheld's card, as if put in the Mac's reader.
            return unit == Self.handheldID
                ? ["volumes": [["path": "/Volumes/MAPCARD", "name": "MAPCARD", "is_card": true, "reason": nil]],
                   "remembered": true]
                : ["volumes": [], "remembered": false]
        case "maps.install", "maps.remove", "maps.move":
            guard let unit else { throw Self.noDevice }
            let to = p["to"]?.stringValue.flatMap(MapLocation.init(rawValue:))
            var done: [JSONValue] = []
            for name in p["maps"]?.arrayValue?.compactMap(\.stringValue) ?? [] {
                guard let i = maps[unit]?.firstIndex(where: { $0.name == name }) else {
                    done.append(["name": .string(name), "done": false, "code": "failed", "reason": "No such map."])
                    continue
                }
                if method == "maps.remove" {
                    maps[unit]?[i].installed = false
                    maps[unit]?[i].status = .available
                    maps[unit]?[i].location = nil
                } else if method == "maps.move", let to {
                    try await simulate(id, phases: [("reading", 6), ("copying", 4), ("removing", 2)], item: name,
                                       total: maps[unit]?[i].moveSize ?? maps[unit]?[i].size, progress: progress)
                    maps[unit]?[i].location = to
                    maps[unit]?[i].canInstall = true   // on one storage again
                    maps[unit]?[i].canRemove = true
                    maps[unit]?[i].statusText = nil
                } else {
                    try await simulate(id, phases: [("downloading", 10), ("copying", 4)], item: name,
                                       total: maps[unit]?[i].size, progress: progress)
                    maps[unit]?[i].installed = true
                    maps[unit]?[i].status = .upToDate
                    let version = maps[unit]?[i].version
                    maps[unit]?[i].installedVersion = version
                    if mapStorages[unit] != nil {
                        let size = maps[unit]?[i].size
                        maps[unit]?[i].location = to ?? .internal
                        maps[unit]?[i].canMove = true
                        maps[unit]?[i].moveSize = size
                    }
                }
                done.append(["name": .string(name), "done": true, "exit_code": 0, "lines": []])
            }
            return ["dry_run": false, "maps": .array(done)]
        case "apps.list":
            return try JSONValue(encoding: Self.sampleApps(unit: unit))
        case "activities.list":
            let all = Self.sampleActivities(unit: unit, count: scenario == .many ? 280 : 24)
            if let unit, !activitiesCached.contains(unit) {
                // The first read goes through every FIT file on the device.
                for (i, a) in all.enumerated() {
                    try Task.checkCancellation()
                    let ev = EngineProgress(request: id, phase: "reading_activities", done: Int64(i), total: Int64(all.count),
                                            item: a.start.formatted(date: .abbreviated, time: .omitted))
                    progress?(ev)
                    broadcaster.yield(.progress(ev))
                    // About as long in all: 24 files at half a step each.
                    try await Task.sleep(for: stepDelay * 12 / all.count)
                }
                activitiesCached.insert(unit)
            }
            return ["activities": .array(try all.map { try JSONValue(encoding: $0) }), "total": .number(Double(all.count)),
                    "only_in_backups": 0]
        case "activities.track":
            // Indoor activities (training, pool swims) have no GPS points.
            if p["fit"]?.stringValue?.contains("-indoor") == true { return ["points": []] }
            try await Task.sleep(for: stepDelay)
            // Development: `OEMOCK_TRACK=<engine response .json>` serves a real track (kept
            // outside the repository) for loops.
            if p["fit"]?.stringValue?.contains("-ride") != true,
               let path = ProcessInfo.processInfo.environment["OEMOCK_TRACK"],
               let data = FileManager.default.contents(atPath: path),
               let response = try? JSONDecoder().decode(JSONValue.self, from: data) {
                return response["result"] ?? response
            }
            // Rides go from A to B; everything else is a loop (start and finish together).
            return try JSONValue(encoding: Self.sampleTrack(loop: p["fit"]?.stringValue?.contains("-ride") != true))
        case "backups.list":
            return ["backups": .array(try (backups[unit ?? ""] ?? []).map { try JSONValue(encoding: $0) })]
        case "account.readings":
            return try JSONValue(encoding: readings(of: unit))
        case "settings.get":
            guard let key = p["key"]?.stringValue else { return ["settings": .object(settings)] }
            guard let value = settings[key] else {
                throw EngineError.remote(code: "unknown_setting", message: "No setting \(key).", data: nil)
            }
            return ["key": .string(key), "value": value]
        case "settings.set":
            let key = p["key"]?.stringValue ?? ""
            settings[key] = p["value"] ?? .null
            return ["key": .string(key), "value": settings[key] ?? .null]
        case "strava.status":
            return stravaStatus
        case "strava.connect":
            if p["client_id"]?.stringValue != nil, p["client_secret"]?.stringValue != nil { stravaConfigured = true }
            guard stravaConfigured else {
                throw EngineError.remote(code: "failed", message: "Enter the client ID and secret of your Strava API application.", data: nil)
            }
            if p["code"]?.stringValue != nil {
                stravaConnected = true
                return stravaStatus
            }
            var r = stravaStatus.objectValue ?? [:]
            let redirect = p["redirect_uri"]?.stringValue ?? ""
            r["authorize_url"] = .string("https://www.strava.com/oauth/authorize?client_id=1&response_type=code&redirect_uri=\(redirect)&scope=activity:write")
            return .object(r)
        case "strava.disconnect":
            let was = stravaConnected
            stravaConnected = false
            return ["removed": .bool(was)]
        case "strava.upload":
            let n = p["files"]?.arrayValue?.count ?? 0
            return ["uploaded": .number(Double(n)), "duplicates": 0, "failed": 0, "skipped": 0, "rate_limited": false]
        case "cancel":
            return ["cancelling": true]
        case "shutdown":
            return [:]
        default:
            throw EngineError.remote(code: "unknown_method", message: "Unknown method \(method).", data: nil)
        }
    }

    private var stravaStatus: JSONValue {
        ["configured": .bool(stravaConfigured), "connected": .bool(stravaConnected),
         "athlete_id": stravaConnected ? 12345 : .null, "athlete_name": stravaConnected ? "Demo Athlete" : .null,
         "scope": stravaConnected ? "read,activity:write,activity:read_all" : .null, "expires_at": .null]
    }

    public static let methods = [
        "hello", "account.status", "account.setup", "account.sign_in_url", "account.sign_in",
        "account.sign_out", "devices.list", "device.info", "device.backup", "device.sync",
        "updates.list", "updates.install", "maps.list", "maps.install", "maps.update", "maps.remove", "maps.move", "maps.card_reader",
        "apps.list", "activities.list", "activities.track", "backups.list", "account.readings", "settings.get",
        "settings.set", "strava.status", "strava.connect", "strava.disconnect", "strava.upload", "cancel", "shutdown",
    ]

    /// Made-up sensors on the account; the fēnix stands for a watch that charges from the sun.
    private func readings(of unit: String?) -> AccountReadings {
        guard account.signedIn else {
            return AccountReadings(signedIn: false, sensors: .init(state: .signedOut),
                                   solar: unit == Self.fenixID ? .init(state: .signedOut) : nil)
        }
        let sensors = AccountReadings.Sensors(
            state: .read, summary: "3 sensors · 1 with a low battery", low: 1, sensors: [
                .init(name: "Varia RTL515", detail: "Battery low · last connected 2 days ago", low: true),
                .init(name: "HRM-Pro Plus", detail: "Battery 80% · last connected today, 07:12"),
                .init(name: "Speed Sensor 2", detail: "Battery good · last connected 5 days ago"),
            ])
        let solar = unit == Self.fenixID
            ? AccountReadings.Solar(state: .read, summary: "Activity time gained: today 35 min · last 7 days 4 h 10 min",
                                    todayMs: 35 * 60_000, weekMs: 250 * 60_000)
            : nil
        return AccountReadings(signedIn: true, onAccount: true, sensors: sensors, solar: solar)
    }

    private static let noDevice = EngineError.remote(code: "no_device", message: "No such device.", data: nil)

    private func simulate(_ request: Int, phases: [(String, Int)], item: String?, total: Int64? = nil,
                          progress: (@Sendable (EngineProgress) -> Void)?) async throws {
        let totalBytes = total != nil
        for (phase, steps) in phases {
            let total = total ?? Int64(steps)
            for step in 0...steps {
                try Task.checkCancellation()
                // A given total is bytes (a download, a copy), else steps.
                let ev = EngineProgress(request: request, phase: phase, done: total * Int64(step) / Int64(steps),
                                        total: total, item: item, unit: totalBytes ? "bytes" : nil)
                progress?(ev)
                broadcaster.yield(.progress(ev))
                if step < steps { try await Task.sleep(for: stepDelay) }
            }
        }
    }

    static func stamp(_ date: Date) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd (HH.mm.ss)"
        return f.string(from: date)
    }

    /// The Connect IQ apps of a device, as `apps.list` sends them: on the fēnix a mix of every
    /// kind, some disabled, one with an update, one waiting to be installed; on the Edge a few.
    public static func sampleApps(unit: String? = fenixID) -> AppsList {
        let note = "New apps are chosen in the Connect IQ Store (website or phone); they then appear in the list above."
        func app(_ n: Int, _ name: String, _ kind: AppKind, _ kindName: String, _ version: Int,
                 enabled: Bool = true, update: AppUpdate? = nil, waiting: String? = nil,
                 removable: Bool = true) -> ConnectIQApp {
            ConnectIQApp(appID: String(format: "c33a05fa-bb7b-46a4-87b4-%012d", n), name: name, kind: kind,
                         kindName: kindName, version: version, enabled: enabled, update: update,
                         waiting: waiting,
                         waitingText: waiting == nil ? nil : "will be installed after the device is unplugged",
                         removable: removable)
        }
        if unit == edgeID {
            return AppsList(summary: AppsSummary(storeApps: 3, freeSlots: 14, freeBytes: 126_668_166), apps: [
                app(1, "Komoot", .app, "app", 222),
                app(2, "Connect IQ Store", .app, "app", 71, removable: false),
                app(1, "Komoot", .widget, "widget", 222),
            ], note: note)
        }
        return AppsList(
            summary: AppsSummary(storeApps: 12, freeSlots: 21, freeBytes: 63_315_235),
            apps: [
                app(2, "Connect IQ Store", .app, "app", 71, removable: false),
                app(3, "WhatsApp", .app, "app", 49),
                app(1, "Komoot", .widget, "widget", 222, update: AppUpdate(version: "4.26.23", size: 412_000)),
                app(4, "Klimat", .widget, "widget", 12, enabled: false),
                app(5, "Crystal Face", .watchFace, "watch face", 213),
                app(6, "Segment34 MkII", .watchFace, "watch face", 144, enabled: false),
                app(7, "Fenix7 Pro Analog MB", .watchFace, "watch face", 23, enabled: false),
                app(8, "Strava Live Segments", .dataField, "data field", 241),
                app(9, "TideCastDF", .dataField, "data field", 4),
                app(10, "Surf Tracker3", .dataField, "data field", 7, waiting: "install"),
                app(11, "Spotify", .music, "music app", 73, enabled: false),
                app(1, "Komoot", .activity, "activity", 222, update: AppUpdate(version: "4.26.23", size: 412_000)),
            ],
            account: [AccountApp(appID: "c33a05fa-bb7b-46a4-87b4-000000000012", name: "Garmin Golf",
                                 kind: .app, kindName: "app", version: "1.3.0", size: 280_000,
                                 developer: "Garmin")],
            note: note)
    }

    public static func sampleActivities(unit: String? = fenixID, count: Int = 24) -> [Activity] {
        // Display names and FIT sport numbers, as the engine sends them.
        let sports: [(String, Int, Double)] = [   // name, sport_id, speed m/s (0: no distance)
            ("Running", 1, 3.1), ("Cycling", 2, 8.1), ("Hiking", 17, 1.3), ("Surfing", 38, 0.4),
            ("Swimming", 5, 0.8), ("Walking", 11, 1.4), ("Training", 10, 0), ("Running", 1, 2.9),
        ]
        let now = Date()
        let stamp = DateFormatter()
        stamp.locale = Locale(identifier: "en_US_POSIX")
        stamp.dateFormat = "yyyy-MM-dd-HH-mm-ss"
        return (0..<count).map { i in
            let (sport, id, speed) = sports[(i * 5 + 3) % sports.count]
            // 15 min to 2 h 45: short walks to long rides.
            let duration = Double(900 + (i * 1777) % 9000)
            let onDevice = i < 12
            let start = now.addingTimeInterval(-Double(i) * 86400 * 1.7 - 7200)
            let file = stamp.string(from: start) + ([10, 5].contains(id) ? "-indoor" : id == 2 ? "-ride" : "") + ".fit"
            return Activity(
                start: start,
                sport: sport,
                distance: speed > 0 ? duration * speed : nil,
                duration: duration,
                fitPath: (onDevice ? "GARMIN/Activity/" : "/Users/demo/Documents/Garmin/Backups/\(unit ?? fenixID)/Activity/") + file,
                // The engine's name is mostly the file's; a few carry a title of their own.
                name: i % 7 == 2 ? "Heuvelrug loop" : file,
                ascent: [2, 17, 1].contains(id) ? Double(40 + (i * 37) % 600) : nil,
                calories: duration / 6,
                averageHeartRate: id == 38 ? nil : Double(118 + (i * 7) % 40),
                source: onDevice ? "device" : "backup",
                state: "complete",
                sportID: id)
        }
    }

    public static func sampleTrack(loop: Bool = true) -> ActivityTrack {
        // A loop around the Utrechtse Heuvelrug, or (not a loop) its first half, A to B.
        // A loop closes on its first point.
        let n = 240
        return ActivityTrack(points: (0...(loop ? n : n - 1)).map { i in
            let t = Double(i) / Double(n) * (loop ? 2 : 1) * .pi
            let lat = 52.06 + 0.018 * sin(t) + 0.004 * sin(3 * t)
            let lon = 5.30 + 0.03 * cos(t) + 0.005 * cos(5 * t)
            return .init(latitude: lat, longitude: lon, time: Double(i) * 15,
                         elevation: 20 + 18 * sin(2 * t) + 6 * sin(7 * t),
                         heartRate: 132 + 18 * sin(t * 1.5) + 4 * sin(t * 9),
                         speed: 3.0 + 0.4 * sin(4 * t))
        })
    }
}
