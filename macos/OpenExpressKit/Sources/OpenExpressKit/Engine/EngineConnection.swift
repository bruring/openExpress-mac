import Foundation

/// Something that speaks the engine protocol of docs/ENGINE.md: the real engine process
/// (`EngineClient`) or the in-memory `MockEngine` of previews and tests.
public protocol EngineConnection: AnyObject, Sendable {
    /// Starts the engine if it is not running.
    func start() async throws

    /// Sends a request and waits for its response. `progress` gets the `progress` events of
    /// this request. Cancelling the calling task sends `cancel` for it; the engine stops at
    /// the next safe point and the call throws `EngineError.cancelled` (or returns, when the
    /// engine finished first).
    func call(_ method: String, params: JSONValue?,
              progress: (@Sendable (EngineProgress) -> Void)?) async throws -> JSONValue

    /// Like `call`, but gives the response line as the engine wrote it (`{"id", "result"}`),
    /// so a typed result is decoded once from its bytes, not through a `JSONValue`.
    func callLine(_ method: String, params: JSONValue?,
                  progress: (@Sendable (EngineProgress) -> Void)?) async throws -> Data

    /// A new stream of every event from now on; each subscriber gets its own.
    func events() async -> AsyncStream<EngineEvent>

    /// Asks the engine to exit and waits briefly for it.
    func shutdown() async
}

extension EngineConnection {
    public func call(_ method: String, params: JSONValue? = nil) async throws -> JSONValue {
        try await call(method, params: params, progress: nil)
    }

    /// An engine without lines of its own (the mock): its result, written as one.
    public func callLine(_ method: String, params: JSONValue?,
                         progress: (@Sendable (EngineProgress) -> Void)?) async throws -> Data {
        let value = try await call(method, params: params, progress: progress)
        return try JSONEncoder().encode(JSONValue.object(["result": value]))
    }

    func call<T: Decodable>(_ method: String, params: JSONValue? = nil, as: T.Type,
                            progress: (@Sendable (EngineProgress) -> Void)? = nil) async throws -> T {
        let line = try await callLine(method, params: params, progress: progress)
        do {
            return try EngineCoding.decodeResult(T.self, fromLine: line)
        } catch {
            throw EngineError.unexpectedResult(method: method, detail: String(describing: error))
        }
    }
}

/// The methods of docs/ENGINE.md, typed.
extension EngineConnection {
    public func hello(client: String) async throws -> EngineHello {
        try await call("hello", params: ["client": .string(client)], as: EngineHello.self)
    }

    // MARK: Account

    public func accountStatus() async throws -> AccountStatus {
        try await call("account.status", as: AccountStatus.self)
    }

    public func accountSetup(installer: URL) async throws {
        _ = try await call("account.setup", params: ["installer": .string(installer.path)])
    }

    public func signInURL() async throws -> URL {
        let v = try await call("account.sign_in_url")
        guard let s = v["url"]?.stringValue, let url = URL(string: s) else {
            throw EngineError.unexpectedResult(method: "account.sign_in_url", detail: "no url")
        }
        return url
    }

    @discardableResult
    public func signIn(ticketURL: String) async throws -> String? {
        try await call("account.sign_in", params: ["ticket_url": .string(ticketURL)])["account"]?.stringValue
    }

    public func signOut() async throws {
        _ = try await call("account.sign_out")
    }

    // MARK: Devices

    public func devices() async throws -> [Device] {
        try await call("devices.list", as: DevicesList.self).devices
    }

    public func deviceInfo(_ unitID: String) async throws -> Device {
        try await call("device.info", params: ["device": .string(unitID)], as: Device.self)
    }

    /// Into the engine's `backup_dir`, keeping its `backup_keep`.
    public func backup(_ unitID: String,
                       progress: (@Sendable (EngineProgress) -> Void)? = nil) async throws -> BackupResult {
        try await call("device.backup", params: ["device": .string(unitID), "apply": true], as: BackupResult.self,
                       progress: progress)
    }

    public func sync(_ unitID: String, firstRun: FirstSync? = nil,
                     progress: (@Sendable (EngineProgress) -> Void)? = nil) async throws -> SyncResult {
        var p: [String: JSONValue] = ["device": .string(unitID), "apply": true]
        if let firstRun { p["first_run"] = .string(firstRun.rawValue) }
        return SyncResult(raw: try await call("device.sync", params: .object(p), progress: progress))
    }

    // MARK: Updates and maps

    public func updates(_ unitID: String,
                        progress: (@Sendable (EngineProgress) -> Void)? = nil) async throws -> UpdatesList {
        try await call("updates.list", params: ["device": .string(unitID)], as: UpdatesList.self, progress: progress)
    }

    /// `parts`: nil installs all.
    public func installUpdates(_ unitID: String, parts: [String]?,
                               progress: (@Sendable (EngineProgress) -> Void)? = nil) async throws -> OperationResult {
        let partsValue: JSONValue = parts.map { .array($0.map { .string($0) }) } ?? .string("all")
        return OperationResult(raw: try await call(
            "updates.install",
            params: ["device": .string(unitID), "parts": partsValue, "apply": true],
            progress: progress))
    }

    /// Every map Garmin has for the device, installed or not.
    public func maps(_ unitID: String,
                     progress: (@Sendable (EngineProgress) -> Void)? = nil) async throws -> MapsList {
        try await call("maps.list", params: ["device": .string(unitID), "all": true], as: MapsList.self,
                       progress: progress)
    }

    /// `maps.install` / `maps.remove` / `maps.move`, one map after the other. `to`: where an
    /// install puts a map not on the device yet, or where a move takes it (`.internal`, `.card`).
    public func maps(_ action: MapAction, _ unitID: String, names: [String], to: MapLocation? = nil,
                     cardReader: String? = nil, source: MoveSource? = nil,
                     progress: (@Sendable (EngineProgress) -> Void)? = nil) async throws -> OperationResult {
        var params: [String: JSONValue] = ["device": .string(unitID), "maps": .array(names.map { .string($0) }),
                                           "apply": true]
        if let to { params["to"] = .string(to.rawValue) }
        // The device's card in this Mac's card reader: the map's card files are written there.
        if let cardReader { params["card_reader"] = .string(cardReader) }
        // A move: its files read off the device, or downloaded again.
        if let source { params["source"] = .string(source.rawValue) }
        return OperationResult(raw: try await call("maps.\(action.rawValue)", params: .object(params),
                                                   progress: progress))
    }

    /// `maps.card_reader`: the memory cards in this Mac's card reader, and which is the
    /// device's. Opens nothing on the device: asked again and again while the card is put in.
    public func cardReaders(_ unitID: String) async throws -> CardReaderList {
        try await call("maps.card_reader", params: ["device": .string(unitID)], as: CardReaderList.self)
    }

    /// `maps.update`: every installed map that has an update.
    public func updateAllMaps(_ unitID: String,
                              progress: (@Sendable (EngineProgress) -> Void)? = nil) async throws -> OperationResult {
        OperationResult(raw: try await call(
            "maps.update", params: ["device": .string(unitID), "apply": true], progress: progress))
    }

    // MARK: Apps, activities, backups

    public func apps(_ unitID: String,
                     progress: (@Sendable (EngineProgress) -> Void)? = nil) async throws -> AppsList {
        try await call("apps.list", params: ["device": .string(unitID)], as: AppsList.self, progress: progress)
    }

    /// Every activity (a limit of 100,000): the engine keeps their summaries on disk, so only
    /// the first read is slow (every FIT file over MTP); it reports `progress` with phase
    /// `reading_activities`, done/total in files.
    public func activities(_ unitID: String?,
                           progress: (@Sendable (EngineProgress) -> Void)? = nil) async throws -> [Activity] {
        var p: [String: JSONValue] = ["limit": 100_000]
        if let unitID { p["device"] = .string(unitID) }
        return try await call("activities.list", params: .object(p), as: ActivitiesList.self, progress: progress).activities
    }

    /// `unitID`: the device the file is on (the engine needs it when several are connected).
    public func activityTrack(fitPath: String, unitID: String) async throws -> ActivityTrack {
        try await call("activities.track", params: ["fit": .string(fitPath), "device": .string(unitID)],
                       as: ActivityTrack.self)
    }

    public func backups(_ unitID: String) async throws -> [BackupSnapshot] {
        try await call("backups.list", params: ["device": .string(unitID)], as: BackupsList.self).backups
    }

    // MARK: Readings of the account

    /// The sensors on the Garmin Connect account and the device's solar charging. `fresh`:
    /// asked of Garmin Connect anew, not what the engine kept (Refresh).
    public func accountReadings(_ unitID: String, fresh: Bool = false) async throws -> AccountReadings {
        try await call("account.readings", params: ["device": .string(unitID), "fresh": .bool(fresh)],
                       as: AccountReadings.self)
    }

    // MARK: Settings, Strava

    public func setting(_ key: String) async throws -> JSONValue {
        let v = try await call("settings.get", params: ["key": .string(key)])
        return v["value"] ?? v
    }

    public func setSetting(_ key: String, _ value: JSONValue) async throws {
        _ = try await call("settings.set", params: ["key": .string(key), "value": value])
    }

    /// `settings.get` without a key: every setting.
    public func settings() async throws -> [String: JSONValue] {
        try await call("settings.get")["settings"]?.objectValue ?? [:]
    }

    public func stravaStatus() async throws -> StravaStatus {
        try await call("strava.status", as: StravaStatus.self)
    }

    /// Without `code`: the status and the `authorize_url` to open (client ID and secret saved
    /// first when given). With the `code` Strava's redirect brought: connects.
    public func stravaConnect(redirectURI: String, code: String? = nil, clientID: String? = nil,
                              clientSecret: String? = nil) async throws -> StravaStatus {
        var p: [String: JSONValue] = ["redirect_uri": .string(redirectURI)]
        if let code { p["code"] = .string(code) }
        if let clientID, !clientID.isEmpty { p["client_id"] = .string(clientID) }
        if let clientSecret, !clientSecret.isEmpty { p["client_secret"] = .string(clientSecret) }
        return try await call("strava.connect", params: .object(p), as: StravaStatus.self)
    }

    public func stravaDisconnect() async throws {
        _ = try await call("strava.disconnect")
    }

    /// `strava.upload`: `{"uploaded", "duplicates", "failed", "skipped", "rate_limited"}`.
    public func stravaUpload(files: [String]) async throws -> JSONValue {
        try await call("strava.upload", params: ["files": .array(files.map { .string($0) }), "apply": true])
    }
}

public enum MapAction: String, Sendable { case install, remove, move }

/// Where a move gets the map's files: read off the device, or downloaded again from Garmin.
public enum MoveSource: String, Sendable, Hashable { case device, download }

struct DevicesList: Decodable { var devices: [Device] }

/// The first sync of a device must say what to upload (`first_sync_choice` otherwise):
/// everything on it, or only what is new from now on.
public enum FirstSync: String, Sendable {
    case everything = "all"
    case newOnly = "baseline"
}
