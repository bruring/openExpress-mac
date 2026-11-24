import Foundation

/// A software update the device can get (`updates.list`).
public struct SoftwareUpdate: Codable, Sendable, Hashable, Identifiable {
    public var part: String
    public var name: String
    public var version: String
    public var installed: String?
    public var size: Int64
    public var beta: Bool
    /// Copied to the device, which installs it once unplugged.
    public var waitingOnDevice: Bool

    public var id: String { part }

    public init(part: String, name: String, version: String, installed: String?, size: Int64,
                beta: Bool = false, waitingOnDevice: Bool = false) {
        self.part = part
        self.name = name
        self.version = version
        self.installed = installed
        self.size = size
        self.beta = beta
        self.waitingOnDevice = waitingOnDevice
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        part = try c.decode(String.self, forKey: .part)
        name = try c.decodeIfPresent(String.self, forKey: .name) ?? part
        version = try c.decodeIfPresent(String.self, forKey: .version) ?? ""
        installed = try c.decodeIfPresent(String.self, forKey: .installed)
        size = try c.decodeIfPresent(Int64.self, forKey: .size) ?? 0
        beta = try c.decodeIfPresent(Bool.self, forKey: .beta) ?? false
        waitingOnDevice = try c.decodeIfPresent(Bool.self, forKey: .waitingOnDevice) ?? false
    }
}

public struct UpdatesList: Codable, Sendable, Hashable {
    public var updates: [SoftwareUpdate]
    public var betaOffered: Bool
    /// "current", "available", "ready" (copied, unplug to install) or "not-installed".
    public var state: String?
    public var addOns: [SoftwareUpdate]

    public init(updates: [SoftwareUpdate], betaOffered: Bool = false, state: String? = nil,
                addOns: [SoftwareUpdate] = []) {
        self.updates = updates
        self.betaOffered = betaOffered
        self.state = state
        self.addOns = addOns
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        updates = try c.decodeIfPresent([SoftwareUpdate].self, forKey: .updates) ?? []
        betaOffered = try c.decodeIfPresent(Bool.self, forKey: .betaOffered) ?? false
        state = try? c.decodeIfPresent(String.self, forKey: .state)
        addOns = (try? c.decodeIfPresent([SoftwareUpdate].self, forKey: .addOns)) ?? []
    }
}

/// A map (`maps.list`). Named so as not to clash with MapKit's `Map`.
/// Where a map's own files are: the device's own storage, its memory card, or both (an
/// interrupted move, or what another program did); `cardOut`: on the memory card, which is not
/// in the device now (taken out) or cannot be read.
public enum MapLocation: String, Codable, Sendable, Hashable {
    case `internal`, card, both, unknown
    case cardOut = "card_out"

    public init(from decoder: any Decoder) throws {
        self = MapLocation(rawValue: try decoder.singleValueContainer().decode(String.self)) ?? .unknown
    }
}

public struct DeviceMap: Codable, Sendable, Hashable, Identifiable {
    public enum Status: String, Codable, Sendable {
        case upToDate = "up to date"
        case update
        case available
        case unknown

        public init(from decoder: any Decoder) throws {
            self = Status(rawValue: try decoder.singleValueContainer().decode(String.self)) ?? .unknown
        }
    }

    public var name: String
    public var version: String?
    public var size: Int64
    public var installed: Bool
    public var status: Status
    public var region: String?
    /// The version on the device, for an update.
    public var installedVersion: String?
    /// Translated, finer than `status`.
    public var statusText: String?
    public var canInstall: Bool?
    public var canRemove: Bool?
    /// Where it is on the device; nil when it is not on it (or that is not known).
    public var location: MapLocation?
    /// It may move to the device's other storage (or, on both, be kept on one).
    public var canMove: Bool?
    /// About the bytes a move copies (its own map files).
    public var moveSize: Int64?
    /// Of its files, the bytes in the engine's download cache (not downloaded again), and of
    /// `moveSize` (neither read off the device nor downloaded by a move).
    public var cachedSize: Int64?
    public var moveCached: Int64?
    /// Written to or removed from the device, which takes it in once unplugged: "install",
    /// "remove", or nil.
    public var waiting: String?

    /// Its files are removed; the device drops it from its list once unplugged.
    public var isBeingRemoved: Bool { waiting == "remove" }

    public var id: String { name }

    public init(name: String, version: String?, size: Int64, installed: Bool, status: Status,
                region: String? = nil, installedVersion: String? = nil, statusText: String? = nil,
                canInstall: Bool? = nil, canRemove: Bool? = nil, location: MapLocation? = nil,
                canMove: Bool? = nil, moveSize: Int64? = nil) {
        self.name = name
        self.version = version
        self.size = size
        self.installed = installed
        self.status = status
        self.region = region
        self.installedVersion = installedVersion
        self.statusText = statusText
        self.canInstall = canInstall
        self.canRemove = canRemove
        self.location = location
        self.canMove = canMove
        self.moveSize = moveSize
    }

    /// "2025.10 → 2025.20" for an update, "new · 2025.20" for a map not installed.
    public var versionChange: String? {
        guard let version else { return installed ? nil : String(localized: "new") }
        if !installed { return String(localized: "new · \(version)") }
        if let installedVersion, installedVersion != version { return "\(installedVersion) → \(version)" }
        return version
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = try c.decode(String.self, forKey: .name)
        version = try? c.decodeIfPresent(String.self, forKey: .version)
        size = try c.decodeIfPresent(Int64.self, forKey: .size) ?? 0
        installed = try c.decodeIfPresent(Bool.self, forKey: .installed) ?? false
        status = try c.decodeIfPresent(Status.self, forKey: .status) ?? (installed ? .upToDate : .available)
        region = try? c.decodeIfPresent(String.self, forKey: .region)
        installedVersion = try? c.decodeIfPresent(String.self, forKey: .installedVersion)
        statusText = try? c.decodeIfPresent(String.self, forKey: .statusText)
        canInstall = try? c.decodeIfPresent(Bool.self, forKey: .canInstall)
        canRemove = try? c.decodeIfPresent(Bool.self, forKey: .canRemove)
        location = try? c.decodeIfPresent(MapLocation.self, forKey: .location)
        canMove = try? c.decodeIfPresent(Bool.self, forKey: .canMove)
        moveSize = try? c.decodeIfPresent(Int64.self, forKey: .moveSize)
        cachedSize = try? c.decodeIfPresent(Int64.self, forKey: .cachedSize)
        moveCached = try? c.decodeIfPresent(Int64.self, forKey: .moveCached)
        waiting = try? c.decodeIfPresent(String.self, forKey: .waiting)
    }
}

/// A mounted volume with a Garmin folder: a memory card in this Mac's card reader
/// (`maps.card_reader`), and whether it is the device's card.
public struct ReaderVolume: Codable, Sendable, Hashable, Identifiable {
    public var path: String
    public var name: String
    public var isCard: Bool
    /// Why it is not the device's card.
    public var reason: String?

    public var id: String { path }

    public init(path: String, name: String, isCard: Bool, reason: String? = nil) {
        self.path = path
        self.name = name
        self.isCard = isCard
        self.reason = reason
    }
}

public struct CardReaderList: Codable, Sendable, Hashable {
    public var volumes: [ReaderVolume]
    /// openExpress has seen the device's card over USB (else no volume can be told as it).
    public var remembered: Bool

    public init(volumes: [ReaderVolume] = [], remembered: Bool = false) {
        self.volumes = volumes
        self.remembered = remembered
    }

    /// The device's card among them.
    public var card: ReaderVolume? { volumes.first(where: \.isCard) }
}

/// A storage maps may be on (`maps.list` `storages`): the device's own, or its memory card.
public struct MapStorage: Codable, Sendable, Hashable, Identifiable {
    /// `.internal` or `.card`.
    public var id: MapLocation
    /// As the device names it, e.g. "Memory Card".
    public var name: String?
    public var free: Int64?
    public var capacity: Int64?
    public var removable: Bool

    public init(id: MapLocation, name: String? = nil, free: Int64? = nil, capacity: Int64? = nil,
                removable: Bool = false) {
        self.id = id
        self.name = name
        self.free = free
        self.capacity = capacity
        self.removable = removable
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(MapLocation.self, forKey: .id)
        name = try? c.decodeIfPresent(String.self, forKey: .name)
        free = try? c.decodeIfPresent(Int64.self, forKey: .free)
        capacity = try? c.decodeIfPresent(Int64.self, forKey: .capacity)
        removable = (try? c.decodeIfPresent(Bool.self, forKey: .removable)) ?? false
    }

    /// The share in use, 0…1; nil when the capacity is not known.
    public var usedFraction: Double? {
        guard let capacity, capacity > 0, let free else { return nil }
        return min(1, max(0, Double(capacity - free) / Double(capacity)))
    }
}

/// A map of the user's own on the device: `{"file", "size", "title", "path"}`, the title
/// null when the file's header could not be read.
public struct OwnMap: Codable, Sendable, Hashable, Identifiable {
    public var name: String
    public var size: Int64?

    public var id: String { name }

    public init(name: String, size: Int64? = nil) {
        self.name = name
        self.size = size
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = try c.decodeIfPresent(String.self, forKey: .title) ?? c.decode(String.self, forKey: .file)
        size = try? c.decodeIfPresent(Int64.self, forKey: .size)
    }

    enum CodingKeys: String, CodingKey { case title, file, size }

    public func encode(to encoder: any Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(name, forKey: .file)
        try c.encodeIfPresent(size, forKey: .size)
    }
}

public struct MapsList: Codable, Sendable, Hashable {
    public var maps: [DeviceMap]
    public var ownMaps: [OwnMap]
    public var free: Int64?
    /// Set when Garmin has no maps service for the device (`maps` is then empty).
    public var message: String?
    /// The device's own storage, then its memory card when it has one.
    public var storages: [MapStorage]
    /// The bytes a second maps last downloaded at; nil before the first.
    public var downloadSpeed: Double?

    public init(maps: [DeviceMap], ownMaps: [OwnMap] = [], free: Int64? = nil, message: String? = nil,
                storages: [MapStorage] = []) {
        self.maps = maps
        self.ownMaps = ownMaps
        self.free = free
        self.message = message
        self.storages = storages
    }

    /// The memory card, when the device has one.
    public var card: MapStorage? { storages.first { $0.id == .card } }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        maps = try c.decodeIfPresent([DeviceMap].self, forKey: .maps) ?? []
        ownMaps = (try? c.decodeIfPresent([OwnMap].self, forKey: .ownMaps)) ?? []
        free = try? c.decodeIfPresent(Int64.self, forKey: .free)
        message = try? c.decodeIfPresent(String.self, forKey: .message)
        storages = (try? c.decodeIfPresent([MapStorage].self, forKey: .storages)) ?? []
        downloadSpeed = try? c.decodeIfPresent(Double.self, forKey: .downloadSpeed)
    }

    public var installed: [DeviceMap] { maps.filter(\.installed) }
    public var available: [DeviceMap] { maps.filter { !$0.installed } }
    /// Installed maps with an update the engine can install: not one it keeps its hands off
    /// (on the memory card, in a folder the device hides), whose `canInstall` is false.
    public var updatable: [DeviceMap] {
        maps.filter { $0.installed && $0.status == .update && $0.canInstall != false }
    }
}

/// The type of a Connect IQ app, as the engine's stable `kind` word.
public enum AppKind: String, Codable, Sendable, Hashable, CaseIterable, Identifiable {
    case app
    case widget
    case watchFace = "watch_face"
    case dataField = "data_field"
    case music
    case activity
    case unknown

    public var id: String { rawValue }

    /// A kind this app does not know (a newer engine) is `unknown`, never an error.
    public init(from decoder: any Decoder) throws {
        self = Self(rawValue: try decoder.singleValueContainer().decode(String.self)) ?? .unknown
    }

    /// The heading of the apps of this kind.
    public var title: String {
        switch self {
        case .app: String(localized: "Apps")
        case .widget: String(localized: "Widgets")
        case .watchFace: String(localized: "Watch Faces")
        case .dataField: String(localized: "Data Fields")
        case .music: String(localized: "Music Apps")
        case .activity: String(localized: "Activities")
        case .unknown: String(localized: "Other")
        }
    }

    public var systemImage: String {
        switch self {
        case .app: "app"
        case .widget: "rectangle.stack"
        case .watchFace: "applewatch.watchface"
        case .dataField: "gauge.with.dots.needle.33percent"
        case .music: "music.note"
        case .activity: "figure.run"
        case .unknown: "questionmark.app.dashed"
        }
    }
}

/// A newer version of an app in the Connect IQ Store.
public struct AppUpdate: Codable, Sendable, Hashable {
    /// The Store's name of the version ("4.26.23").
    public var version: String
    public var size: Int64?
    /// The new version asks for other permissions.
    public var permissionsChanged: Bool

    public init(version: String, size: Int64? = nil, permissionsChanged: Bool = false) {
        self.version = version
        self.size = size
        self.permissionsChanged = permissionsChanged
    }
}

/// A Connect IQ app on the device (`App` of `apps.list`).
public struct ConnectIQApp: Codable, Sendable, Hashable, Identifiable {
    /// The app's UUID. One app can be on the device as several kinds (a widget and an
    /// activity of the same app), so `id` is made of both.
    public var appID: String
    public var name: String
    public var kind: AppKind
    /// The kind's name, translated by the engine ("watch face").
    public var kindName: String?
    /// The app's internal version number on the device (not the Store's "x.y").
    public var version: Int?
    public var enabled: Bool
    public var update: AppUpdate?
    /// "install" or "removal": written to the device, which applies it once unplugged.
    public var waiting: String?
    public var waitingText: String?
    public var removable: Bool

    public var id: String { appID + ":" + kind.rawValue }

    enum CodingKeys: String, CodingKey {
        case appID = "id"
        case name, kind, kindName, version, enabled, update, waiting, waitingText, removable
    }

    public init(appID: String, name: String, kind: AppKind, kindName: String? = nil, version: Int? = nil,
                enabled: Bool = true, update: AppUpdate? = nil, waiting: String? = nil,
                waitingText: String? = nil, removable: Bool = true) {
        self.appID = appID
        self.name = name
        self.kind = kind
        self.kindName = kindName
        self.version = version
        self.enabled = enabled
        self.update = update
        self.waiting = waiting
        self.waitingText = waitingText
        self.removable = removable
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        appID = try c.decode(String.self, forKey: .appID)
        name = try c.decode(String.self, forKey: .name)
        kind = (try? c.decodeIfPresent(AppKind.self, forKey: .kind)) ?? .unknown
        kindName = try? c.decodeIfPresent(String.self, forKey: .kindName)
        version = try? c.decodeIfPresent(Int.self, forKey: .version)
        enabled = (try? c.decodeIfPresent(Bool.self, forKey: .enabled)) ?? true
        update = try? c.decodeIfPresent(AppUpdate.self, forKey: .update)
        waiting = try? c.decodeIfPresent(String.self, forKey: .waiting)
        waitingText = try? c.decodeIfPresent(String.self, forKey: .waitingText)
        removable = (try? c.decodeIfPresent(Bool.self, forKey: .removable)) ?? false
    }
}

/// An app of the Garmin account that is not on the device (`StoreApp` of `apps.list`).
public struct AccountApp: Codable, Sendable, Hashable, Identifiable {
    public var appID: String
    public var name: String
    public var kind: AppKind
    public var kindName: String?
    /// The Store's name of the version.
    public var version: String?
    public var size: Int64?
    public var developer: String?
    public var permissionsChanged: Bool?

    public var id: String { appID }

    enum CodingKeys: String, CodingKey {
        case appID = "id"
        case name, kind, kindName, version, size, developer, permissionsChanged
    }

    public init(appID: String, name: String, kind: AppKind, kindName: String? = nil, version: String? = nil,
                size: Int64? = nil, developer: String? = nil, permissionsChanged: Bool? = nil) {
        self.appID = appID
        self.name = name
        self.kind = kind
        self.kindName = kindName
        self.version = version
        self.size = size
        self.developer = developer
        self.permissionsChanged = permissionsChanged
    }
}

/// A built-in activity or app of the device (`apps.list` with `builtin`).
public struct BuiltinApp: Codable, Sendable, Hashable, Identifiable {
    public var appID: String
    public var name: String
    public var kind: AppKind
    public var kindName: String?
    public var enabled: Bool

    public var id: String { appID + ":" + kind.rawValue }

    enum CodingKeys: String, CodingKey {
        case appID = "id"
        case name, kind, kindName, enabled
    }

    public init(appID: String, name: String, kind: AppKind, kindName: String? = nil, enabled: Bool = true) {
        self.appID = appID
        self.name = name
        self.kind = kind
        self.kindName = kindName
        self.enabled = enabled
    }
}

/// What `apps.list` says of the device's Connect IQ room.
public struct AppsSummary: Codable, Sendable, Hashable {
    public var storeApps: Int
    /// nil when the device does not say.
    public var freeSlots: Int?
    public var freeBytes: Int64?

    public init(storeApps: Int, freeSlots: Int? = nil, freeBytes: Int64? = nil) {
        self.storeApps = storeApps
        self.freeSlots = freeSlots
        self.freeBytes = freeBytes
    }

    /// The slots in all: those taken and those free; nil when the device names no free ones.
    public var totalSlots: Int? { freeSlots.map { $0 + storeApps } }
}

/// `apps.list`: the Connect IQ apps on the device, as `apps` prints them.
public struct AppsList: Codable, Sendable, Hashable {
    public var summary: AppsSummary
    public var apps: [ConnectIQApp]
    /// The account's apps that are not on the device.
    public var account: [AccountApp]
    /// Only when asked for (`builtin`).
    public var builtin: [BuiltinApp]?
    /// No app was ever installed on the device (`apps` is then empty).
    public var message: String?
    /// Entries of the device's app list cannot be read.
    public var warning: String?
    /// Where new apps are chosen (the Connect IQ Store).
    public var note: String?

    public init(summary: AppsSummary, apps: [ConnectIQApp], account: [AccountApp] = [],
                builtin: [BuiltinApp]? = nil, message: String? = nil, warning: String? = nil,
                note: String? = nil) {
        self.summary = summary
        self.apps = apps
        self.account = account
        self.builtin = builtin
        self.message = message
        self.warning = warning
        self.note = note
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        apps = try c.decodeIfPresent([ConnectIQApp].self, forKey: .apps) ?? []
        summary = (try? c.decodeIfPresent(AppsSummary.self, forKey: .summary))
            ?? AppsSummary(storeApps: apps.count)
        account = (try? c.decodeIfPresent([AccountApp].self, forKey: .account)) ?? []
        builtin = try? c.decodeIfPresent([BuiltinApp].self, forKey: .builtin)
        message = try? c.decodeIfPresent(String.self, forKey: .message)
        warning = try? c.decodeIfPresent(String.self, forKey: .warning)
        note = try? c.decodeIfPresent(String.self, forKey: .note)
    }

    /// The apps by kind, in the order of `AppKind`, each kind in the device's order; kinds
    /// without apps left out.
    public var groups: [(kind: AppKind, apps: [ConnectIQApp])] {
        AppKind.allCases.compactMap { kind in
            let apps = apps.filter { $0.kind == kind }
            return apps.isEmpty ? nil : (kind, apps)
        }
    }

    public var updatable: [ConnectIQApp] { apps.filter { $0.update != nil } }
    public var disabledCount: Int { apps.filter { !$0.enabled }.count }
}

/// An activity on the device or in its backups (`activities.list`).
public struct Activity: Codable, Sendable, Hashable, Identifiable {
    public let start: Date
    /// The sport's name, translated by the engine ("Surfing", "Sport 99").
    public var sport: String
    /// Metres.
    public var distance: Double?
    /// Seconds.
    public var duration: Double?
    /// The FIT file on this computer or the device; nil when the engine sends none.
    public let fitPath: String?
    public var name: String?
    public var ascent: Double?
    public var calories: Double?
    public var averageHeartRate: Double?
    /// The FIT profile's sport number (1 running, 2 cycling, 38 surfing, …); picks the icon.
    public var sportID: Int?
    /// "device" or "backup".
    public let source: String?
    /// "complete", "still_being_written", "damaged" or "unreadable".
    public var state: String?
    public var size: Int64?

    /// Made once, of `source`, `fitPath` and `start` (which do not change).
    public let id: String

    private static func id(source: String?, fitPath: String?, start: Date) -> String {
        (source ?? "") + ":" + (fitPath ?? start.ISO8601Format())
    }

    /// A title worth showing: the engine's `name`, unless that is only the FIT file's name
    /// ("2026-11-13-10-36-39.fit") or the sport again.
    public var title: String? {
        guard let name = name?.trimmingCharacters(in: .whitespaces), !name.isEmpty else { return nil }
        if name.lowercased().hasSuffix(".fit") || name == sport { return nil }
        if name == fileName || name.range(of: #"^\d{4}-\d{2}-\d{2}[-_ T]\d{2}[-_.:]\d{2}"#, options: .regularExpression) != nil {
            return nil
        }
        return name
    }

    /// The FIT file's name ("2026-11-13-10-36-39.fit").
    public var fileName: String? {
        fitPath.map { ($0 as NSString).lastPathComponent }
    }

    /// The FIT file is a file on this Mac (a backup's copy), not one on the device.
    public var isOnThisMac: Bool { source == "backup" }

    enum CodingKeys: String, CodingKey {
        case start, sport, distance, duration, name, ascent, calories, averageHeartRate, source, state, size
        case fitPath = "path"
        case sportID = "sportId"
    }

    public init(start: Date, sport: String, distance: Double? = nil, duration: Double? = nil,
                fitPath: String? = nil, name: String? = nil, ascent: Double? = nil,
                calories: Double? = nil, averageHeartRate: Double? = nil, source: String? = nil,
                state: String? = nil, size: Int64? = nil, sportID: Int? = nil) {
        self.sportID = sportID
        self.source = source
        self.state = state
        self.size = size
        self.start = start
        self.sport = sport
        self.distance = distance
        self.duration = duration
        self.fitPath = fitPath
        self.name = name
        self.ascent = ascent
        self.calories = calories
        self.averageHeartRate = averageHeartRate
        id = Self.id(source: source, fitPath: fitPath, start: start)
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        start = try c.decode(Date.self, forKey: .start)
        sport = try c.decodeIfPresent(String.self, forKey: .sport) ?? "generic"
        distance = try? c.decodeIfPresent(Double.self, forKey: .distance)
        duration = try? c.decodeIfPresent(Double.self, forKey: .duration)
        fitPath = try? c.decodeIfPresent(String.self, forKey: .fitPath)
        name = try? c.decodeIfPresent(String.self, forKey: .name)
        ascent = try? c.decodeIfPresent(Double.self, forKey: .ascent)
        calories = try? c.decodeIfPresent(Double.self, forKey: .calories)
        averageHeartRate = try? c.decodeIfPresent(Double.self, forKey: .averageHeartRate)
        sportID = try? c.decodeIfPresent(Int.self, forKey: .sportID)
        source = try? c.decodeIfPresent(String.self, forKey: .source)
        state = try? c.decodeIfPresent(String.self, forKey: .state)
        size = try? c.decodeIfPresent(Int64.self, forKey: .size)
        id = Self.id(source: source, fitPath: fitPath, start: start)
    }

    public func encode(to encoder: any Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(start, forKey: .start)
        try c.encode(sport, forKey: .sport)
        try c.encodeIfPresent(distance, forKey: .distance)
        try c.encodeIfPresent(duration, forKey: .duration)
        try c.encodeIfPresent(fitPath, forKey: .fitPath)
        try c.encodeIfPresent(name, forKey: .name)
        try c.encodeIfPresent(ascent, forKey: .ascent)
        try c.encodeIfPresent(calories, forKey: .calories)
        try c.encodeIfPresent(averageHeartRate, forKey: .averageHeartRate)
        try c.encodeIfPresent(sportID, forKey: .sportID)
        try c.encodeIfPresent(source, forKey: .source)
        try c.encodeIfPresent(state, forKey: .state)
        try c.encodeIfPresent(size, forKey: .size)
    }

    /// The sport's SF Symbol, by its FIT number.
    public var systemImage: String { Self.systemImage(sportID: sportID) }

    /// SF Symbols by FIT sport number (the engine's table in activities.py).
    public static let sportSymbols: [Int: String] = [
        0: "figure.mixed.cardio", 1: "figure.run", 2: "figure.outdoor.cycle", 4: "figure.elliptical",
        5: "figure.pool.swim", 10: "figure.strengthtraining.traditional", 11: "figure.walk",
        12: "figure.skiing.crosscountry", 13: "figure.skiing.downhill", 14: "figure.snowboarding",
        15: "figure.outdoor.rowing", 16: "mountain.2", 17: "figure.hiking", 18: "figure.cross.training",
        19: "figure.outdoor.rowing", 21: "bicycle", 23: "ferry", 25: "figure.golf",
        27: "figure.equestrian.sports", 29: "figure.fishing", 30: "figure.skating", 31: "figure.climbing",
        32: "figure.sailing", 33: "figure.ice.skating", 35: "snowflake", 37: "figure.surfing",
        38: "figure.surfing", 39: "figure.surfing", 40: "figure.water.fitness", 41: "figure.outdoor.rowing",
        42: "figure.outdoor.rowing", 43: "figure.sailing", 44: "figure.surfing", 47: "figure.boxing",
        53: "figure.open.water.swim", 62: "figure.highintensity.intervaltraining", 67: "figure.mind.and.body",
    ]

    /// Every symbol the app may draw for a sport (checked to exist by a test: an unknown
    /// name draws nothing).
    public static var allSportSymbols: Set<String> { Set(sportSymbols.values) }

    /// By FIT sport number; a sport not in the table (or none) gets the generic one's.
    public static func systemImage(sportID: Int?) -> String {
        sportID.flatMap { sportSymbols[$0] } ?? sportSymbols[0]!
    }
}

public struct ActivitiesList: Codable, Sendable, Hashable {
    public var activities: [Activity]
    public var total: Int?

    public init(activities: [Activity], total: Int? = nil) {
        self.activities = activities
        self.total = total
    }
}

/// The track of an activity, for its map and charts: `activities.track {"fit": path}`. The
/// engine sends each point as `{"lat", "lon", "time" (ISO 8601), "hr"?, "alt"?, "speed"?}`.
public struct ActivityTrack: Codable, Sendable, Hashable {
    public struct Point: Codable, Sendable, Hashable {
        public var latitude: Double
        public var longitude: Double
        /// Seconds since the start.
        public var time: Double?
        public var elevation: Double?
        public var heartRate: Double?
        public var speed: Double?

        public init(latitude: Double, longitude: Double, time: Double? = nil,
                    elevation: Double? = nil, heartRate: Double? = nil, speed: Double? = nil) {
            self.latitude = latitude
            self.longitude = longitude
            self.time = time
            self.elevation = elevation
            self.heartRate = heartRate
            self.speed = speed
        }

        private enum CodingKeys: String, CodingKey { case lat, lon, alt, hr, speed, time }

        /// The point's moment as the engine sends it (ISO 8601); `time` is made from it by
        /// ActivityTrack, relative to the first point.
        fileprivate var date: Date?

        public init(from decoder: any Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            func number(_ key: CodingKeys) -> Double? { try? c.decodeIfPresent(Double.self, forKey: key) }
            guard let lat = number(.lat), let lon = number(.lon) else {
                throw DecodingError.dataCorruptedError(forKey: .lat, in: c,
                                                       debugDescription: "a point without a position")
            }
            latitude = lat
            longitude = lon
            elevation = number(.alt)
            heartRate = number(.hr)
            speed = number(.speed)
            if let seconds = try? c.decodeIfPresent(Double.self, forKey: .time) {
                time = seconds
            } else if let text = try? c.decodeIfPresent(String.self, forKey: .time) {
                date = EngineCoding.parseDate(text)
            }
        }

        public func encode(to encoder: any Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(latitude, forKey: .lat)
            try c.encode(longitude, forKey: .lon)
            try c.encodeIfPresent(elevation, forKey: .alt)
            try c.encodeIfPresent(heartRate, forKey: .hr)
            try c.encodeIfPresent(speed, forKey: .speed)
            try c.encodeIfPresent(time, forKey: .time)
        }
    }

    public var points: [Point]

    public init(points: [Point]) { self.points = points }

    private enum CodingKeys: String, CodingKey { case points }

    public init(from decoder: any Decoder) throws {
        var points = try decoder.container(keyedBy: CodingKeys.self).decode([Point].self, forKey: .points)
        // Seconds since the first point, for the charts' axis.
        if let first = points.lazy.compactMap(\.date).first {
            for i in points.indices where points[i].time == nil {
                points[i].time = points[i].date.map { $0.timeIntervalSince(first) }
            }
        }
        self.points = points
    }
}

/// One backup of a device: a folder named "YYYY-MM-DD (HH.MM.SS)" under
/// `<backups>/<unit id>/` (openexpress/backup.py).
public struct BackupSnapshot: Codable, Sendable, Hashable, Identifiable {
    public var path: String
    public var date: Date
    public var files: Int
    public var bytes: Int64

    public var id: String { path }

    public init(path: String, date: Date, files: Int, bytes: Int64) {
        self.path = path
        self.date = date
        self.files = files
        self.bytes = bytes
    }

    public var url: URL { URL(fileURLWithPath: path, isDirectory: true) }
}

public struct BackupsList: Codable, Sendable, Hashable {
    public var backups: [BackupSnapshot]

    public init(backups: [BackupSnapshot]) { self.backups = backups }
}

/// `device.backup`.
public struct BackupResult: Codable, Sendable, Hashable {
    public var path: String
    public var copied: Int
    public var copiedBytes: Int64
    public var linked: Int
    public var linkedBytes: Int64
    public var removed: [String]
    /// FIT files the device was still writing: left out.
    public var incomplete: Int

    public init(path: String, copied: Int, copiedBytes: Int64, linked: Int, linkedBytes: Int64,
                removed: [String] = [], incomplete: Int = 0) {
        self.path = path
        self.copied = copied
        self.copiedBytes = copiedBytes
        self.linked = linked
        self.linkedBytes = linkedBytes
        self.removed = removed
        self.incomplete = incomplete
    }

    public init(from decoder: any Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        path = try c.decodeIfPresent(String.self, forKey: .path) ?? ""
        copied = try c.decodeIfPresent(Int.self, forKey: .copied) ?? 0
        copiedBytes = try c.decodeIfPresent(Int64.self, forKey: .copiedBytes) ?? 0
        linked = try c.decodeIfPresent(Int.self, forKey: .linked) ?? 0
        linkedBytes = try c.decodeIfPresent(Int64.self, forKey: .linkedBytes) ?? 0
        removed = (try? c.decodeIfPresent([String].self, forKey: .removed)) ?? []
        incomplete = (try? c.decodeIfPresent(Int.self, forKey: .incomplete)) ?? 0
    }
}

/// `device.sync`, applied: `{"uploaded", "downloaded", "skipped_known", "rejected", "errors",
/// "not_confirmed", "steps", "sentence", "drop_folder": {...} or null, "strava": {...} or null}`.
/// Kept whole and read leniently (counts may be numbers or lists).
public struct SyncResult: Sendable, Hashable {
    public var raw: JSONValue

    public init(raw: JSONValue) { self.raw = raw }

    private static func count(_ v: JSONValue?) -> Int {
        guard let v else { return 0 }
        if let n = v.intValue { return n }
        if let a = v.arrayValue { return a.count }
        return 0
    }

    public var uploaded: Int { Self.count(raw["uploaded"]) }
    public var downloaded: Int { Self.count(raw["downloaded"]) }
    public var rejected: Int { Self.count(raw["rejected"]) }
    public var errors: Int { Self.count(raw["errors"]) }
    /// The engine's own one-sentence summary, translated.
    public var sentence: String? { raw["sentence"]?.stringValue }
    public var dropFolderCopies: Int { Self.count(raw["drop_folder"]?["copied"]) }
    public var stravaUploads: Int { Self.count(raw["strava"]?["uploaded"]) }
    public var stravaError: String? { raw["strava"]?["error"]?.stringValue }
}

/// Results "as the command line" (`{"exit_code", "lines"}`) and the per-map results of
/// `maps.install` / `maps.remove` (`{"maps": [{"name", "done", "code"?, "reason"?, "lines"?}]}`).
public struct OperationResult: Sendable, Hashable {
    public var raw: JSONValue

    public init(raw: JSONValue) { self.raw = raw }

    public var exitCode: Int? { raw["exit_code"]?.intValue }
    public var lines: [String] { raw["lines"]?.arrayValue?.compactMap(\.stringValue) ?? [] }

    /// What the engine refused, by item name, with its reason.
    public var refused: [String: String] {
        var out: [String: String] = [:]
        for item in raw["maps"]?.arrayValue ?? [] where item["done"]?.boolValue == false {
            let name = item["name"]?.stringValue ?? "?"
            out[name] = item["reason"]?.stringValue
                ?? item["lines"]?.arrayValue?.compactMap(\.stringValue).last
                ?? String(localized: "Not done.")
        }
        return out
    }

    /// A refusal to show: the command's last lines when it exited 1, or a map's reason.
    public var failureMessage: String? {
        if let code = exitCode, code != 0 {
            return lines.suffix(3).joined(separator: "\n").nilIfEmpty ?? String(localized: "Not done.")
        }
        return refused.first.map { "\($0.key): \($0.value)" }
    }
}

extension String {
    var nilIfEmpty: String? { isEmpty ? nil : self }
}

/// `account.readings`: what the Garmin Connect account says beside a device: the batteries of
/// the sensors on the account, and the activity time the sun gained a device that charges from
/// the sun (docs/design/odczyty-z-konta.md). The engine words `summary` and `detail` in its
/// language; a reading the account does not have for this device is nil.
public struct AccountReadings: Codable, Sendable, Hashable {
    public enum State: String, Codable, Sendable {
        case read, failed
        case signedOut = "signed_out"
    }

    /// A sensor on the account (a heart rate strap, a Varia, a power meter…).
    public struct Sensor: Codable, Sendable, Hashable {
        public var name: String
        /// "Battery 80% · last connected 2 days ago".
        public var detail: String
        /// Its battery is low or critical.
        public var low: Bool

        public init(name: String, detail: String, low: Bool = false) {
            self.name = name
            self.detail = detail
            self.low = low
        }
    }

    /// Account Sensors, low batteries first. The account does not say which device a sensor
    /// is paired with.
    public struct Sensors: Codable, Sendable, Hashable {
        public var state: State
        /// "3 sensors · 1 with a low battery".
        public var summary: String?
        public var low: Int?
        public var sensors: [Sensor]?
        /// Why it could not be read, when known.
        public var message: String?

        public init(state: State, summary: String? = nil, low: Int? = nil, sensors: [Sensor]? = nil,
                    message: String? = nil) {
            self.state = state
            self.summary = summary
            self.low = low
            self.sensors = sensors
            self.message = message
        }
    }

    /// Solar Charging: today and the last 7 days.
    public struct Solar: Codable, Sendable, Hashable {
        public var state: State
        /// "Activity time gained: today 35 min · last 7 days 4 h 10 min".
        public var summary: String?
        public var todayMs: Int64?
        public var weekMs: Int64?
        public var message: String?

        public init(state: State, summary: String? = nil, todayMs: Int64? = nil, weekMs: Int64? = nil,
                    message: String? = nil) {
            self.state = state
            self.summary = summary
            self.todayMs = todayMs
            self.weekMs = weekMs
            self.message = message
        }
    }

    public var signedIn: Bool
    /// False: the device is not on the signed-in account (no readings then); nil: not known.
    public var onAccount: Bool?
    public var sensors: Sensors?
    public var solar: Solar?

    /// Nothing to show for this device.
    public var isEmpty: Bool { sensors == nil && solar == nil }

    public init(signedIn: Bool, onAccount: Bool? = nil, sensors: Sensors? = nil, solar: Solar? = nil) {
        self.signedIn = signedIn
        self.onAccount = onAccount
        self.sensors = sensors
        self.solar = solar
    }
}
