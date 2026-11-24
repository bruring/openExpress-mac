import OpenExpressKit
import SwiftUI

// MARK: - Maps

struct MapsPage: View {
    let model: DeviceModel
    @State private var search = ""
    @State private var removing: DeviceMap?

    private func matches(_ name: String) -> Bool { search.isEmpty || name.localizedStandardContains(search) }

    var body: some View {
        Group {
            if let maps = model.maps {
                list(maps)
            } else {
                PageLoadingView(model: model, page: .maps)
            }
        }
        .searchable(text: $search, placement: .toolbar, prompt: "Search Maps")
        .confirmationDialog("Remove \(removing?.name ?? "")?", isPresented: Binding(
            get: { removing != nil }, set: { if !$0 { removing = nil } }), presenting: removing
        ) { map in
            Button("Remove", role: .destructive) { model.maps(.remove, [map.name]) }
        } message: { map in
            Text("This frees \(Format.bytes(map.size)) on the device. You can install it again later.")
        }
    }

    private func list(_ maps: MapsList) -> some View {
        List {
            if let message = maps.message {
                Section {
                    Label(message, systemImage: "info.circle")
                        .foregroundStyle(.secondary)
                }
            }
            let installed = maps.installed.filter { matches($0.name) }
            if let card = maps.card {
                // A device with a memory card: each storage its own section, its space first.
                // A map removed (its files gone) has no storage left: it waits apart for the
                // device to drop it once unplugged.
                let removed = installed.filter(\.isBeingRemoved)
                let kept = installed.filter { !$0.isBeingRemoved }
                let onDevice = kept.filter { $0.location != .card && $0.location != .both }
                let onCard = kept.filter { $0.location == .card }
                let onBoth = kept.filter { $0.location == .both }
                Section {
                    if let storage = model.device.storage {
                        Gauge(value: storage.usedFraction) { EmptyView() }
                            .gaugeStyle(.linearCapacity)
                            .tint(StorageLook.device.color)
                            .labelsHidden()
                    }
                    ForEach(onDevice) { installedRow($0, maps) }
                    if onDevice.isEmpty {
                        Text("No maps on the device").foregroundStyle(.secondary)
                    }
                } header: {
                    StorageHeader(look: .device, free: model.freeSpace ?? model.device.storage?.free)
                }
                Section {
                    if let used = card.usedFraction {
                        Gauge(value: used) { EmptyView() }
                            .gaugeStyle(.linearCapacity)
                            .tint(StorageLook.card.color)
                            .labelsHidden()
                    }
                    ForEach(onCard) { installedRow($0, maps) }
                    if onCard.isEmpty {
                        Text("No maps on the memory card. Install one there, or move one from the device.")
                            .foregroundStyle(.secondary)
                    }
                } header: {
                    StorageHeader(look: .card, free: card.free)
                }
                if !removed.isEmpty {
                    Section {
                        ForEach(removed) { installedRow($0, maps) }
                    } header: {
                        StorageHeader(look: .removed, free: nil)
                    } footer: {
                        Text("Their files are removed from the device and its memory card; the device drops them from its map list once it is unplugged.")
                            .foregroundStyle(.secondary)
                    }
                }
                if !onBoth.isEmpty {
                    Section {
                        ForEach(onBoth) { installedRow($0, maps) }
                    } header: {
                        StorageHeader(look: .both, free: nil)
                    } footer: {
                        Text("Each of these is on the device and on its memory card. Keep one copy: the one kept is checked against Garmin's files, then the other is removed.")
                            .foregroundStyle(.secondary)
                    }
                }
            } else {
                if maps.maps.contains(where: { $0.location == .cardOut }) {
                    // The device's card is out (e.g. in the Mac's card reader): its maps wait.
                    Section {
                        Label("The memory card is not in the device. The maps on it can't be changed until it is back.",
                              systemImage: "sdcard")
                            .foregroundStyle(.orange)
                    }
                }
                if let storage = model.device.storage {
                    Section {
                        Gauge(value: storage.usedFraction) {
                            Text("Device storage")
                        } currentValueLabel: {
                            Text("\(Format.bytes(model.freeSpace ?? storage.free)) free")
                        }
                        .gaugeStyle(.linearCapacity)
                    }
                }
                if !installed.isEmpty {
                    Section("Installed") {
                        ForEach(installed) { installedRow($0, maps) }
                    }
                }
            }
            let available = maps.available.filter { matches($0.name) }
            if !available.isEmpty {
                Section("Available") {
                    ForEach(available) { map in
                        let fits = map.size <= (model.freeSpace ?? .max)
                        MapRow(map: map) {
                            if let card = maps.card {
                                let fitsCard = map.size <= (card.free ?? .max)
                                Menu("Install") {
                                    Button(map.size >= InstallPlan.largeMap ? "On the Device…" : "On the Device") {
                                        model.requestInstall(map: map)
                                    }
                                    .disabled(!fits)
                                    Button("On the Memory Card…") { model.requestInstall(map: map, to: .card) }
                                        .disabled(!fitsCard)
                                }
                                .menuStyle(.button)
                                .buttonStyle(.glass)
                                .fixedSize()
                                .disabled(model.isBusy || (!fits && !fitsCard) || map.canInstall == false)
                                .help(fits || fitsCard ? (map.statusText ?? "")
                                      : String(localized: "Not enough free space on the device or its memory card."))
                            } else {
                                Button(map.size >= InstallPlan.largeMap ? "Install…" : "Install") { model.requestInstall(map: map) }
                                    .buttonStyle(.glass)
                                    .disabled(model.isBusy || !fits || map.canInstall == false)
                                    .help(fits ? (map.statusText ?? "") : String(localized: "Not enough free space on the device."))
                            }
                        }
                    }
                }
            }
            let own = maps.ownMaps.filter { matches($0.name) }
            if !own.isEmpty {
                Section("Your Own Maps") {
                    ForEach(own) { map in
                        LabeledContent {
                            if let size = map.size { Text(Format.bytes(size)).monospacedDigit() }
                        } label: {
                            Label(map.name, systemImage: "doc")
                        }
                    }
                }
            }
        }
    }
}

extension MapsPage {
    /// An installed map: its status (or, on both storages, which copy to keep), and its
    /// context menu (move, keep one, remove). Grouped by storage, it does not repeat its place.
    func installedRow(_ map: DeviceMap, _ maps: MapsList) -> some View {
        MapRow(map: map, showsPlace: maps.card == nil) {
            if map.location == .both, maps.card != nil {
                // An interrupted move, or what another program did: keep one copy.
                Menu {
                    keepMenu(map, maps)
                } label: {
                    Label("Keep One", systemImage: "exclamationmark.triangle.fill")
                        .foregroundStyle(StorageLook.both.color)
                }
                .menuStyle(.borderlessButton)
                .fixedSize()
                .tint(StorageLook.both.color)
                .disabled(model.isBusy || map.canMove != true)
                .help(map.statusText ?? "")
            } else if map.status == .update {
                Text(map.versionChange ?? String(localized: "Update available"))
                    .foregroundStyle(.orange)
                    .monospacedDigit()
            } else {
                Text(map.statusText ?? String(localized: "Up to date")).foregroundStyle(.secondary)
            }
        }
        .contextMenu {
            if maps.card != nil { moveMenu(map, maps) }
            Button("Remove…", role: .destructive) { removing = map }
                .disabled(model.isBusy || map.canRemove == false)
        }
    }

    /// Move to the other storage, for a map on one; keep one copy, for a map on both.
    @ViewBuilder
    func moveMenu(_ map: DeviceMap, _ maps: MapsList) -> some View {
        let movable = !model.isBusy && map.canMove == true
        switch map.location {
        case .internal:
            Button("Move to Memory Card…") { model.requestMove(map: map, to: .card) }
                .disabled(!movable || (map.moveSize ?? 0) > (maps.card?.free ?? .max))
        case .card:
            Button("Move to Device…") { model.requestMove(map: map, to: .internal) }
                .disabled(!movable || (map.moveSize ?? 0) > (model.freeSpace ?? .max))
        case .both:
            keepMenu(map, maps)
        default:
            EmptyView()
        }
        Divider()
    }

    /// For a map on both storages: which copy stays (it is checked first, the other removed).
    @ViewBuilder
    func keepMenu(_ map: DeviceMap, _ maps: MapsList) -> some View {
        let movable = !model.isBusy && map.canMove == true
        Button("Keep on Device…") { model.requestMove(map: map, to: .internal) }
            .disabled(!movable)
        Button("Keep on Memory Card…") { model.requestMove(map: map, to: .card) }
            .disabled(!movable)
    }
}

/// The storages of a device with a memory card, each with its own look on the Maps page.
enum StorageLook {
    case device, card, both, removed

    var title: LocalizedStringKey {
        switch self {
        case .device: "On the Device"
        case .card: "On the Memory Card"
        case .both: "On Both Storages"
        case .removed: "Removed: Gone Once Unplugged"
        }
    }

    var symbol: String {
        switch self {
        case .device: "internaldrive"
        case .card: "sdcard"
        case .both: "exclamationmark.triangle.fill"
        case .removed: "trash"
        }
    }

    var color: Color {
        switch self {
        case .device: .blue
        case .card: .teal
        case .both: .orange
        case .removed: .secondary
        }
    }
}

/// A section header of the Maps page: the storage, in its colour, and its free space.
struct StorageHeader: View {
    let look: StorageLook
    let free: Int64?

    var body: some View {
        HStack {
            Label(look.title, systemImage: look.symbol)
                .foregroundStyle(look.color)
                .font(.headline)
            Spacer()
            if let free {
                Text("\(Format.bytes(free)) free")
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
            }
        }
    }
}

struct MapRow<Trailing: View>: View {
    let map: DeviceMap
    /// Says under its name where it is (memory card, both); off where the page groups maps by
    /// storage.
    var showsPlace = true
    @ViewBuilder let trailing: () -> Trailing

    /// Where the map is, when it is not on the device's own storage alone.
    private var place: String? {
        guard showsPlace else { return nil }
        return switch map.location {
        case .card: String(localized: "Memory card")
        case .both: String(localized: "Device and memory card")
        case .cardOut: String(localized: "Memory card, not in the device")
        default: nil
        }
    }

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: "map")
                .font(.title3)
                .foregroundStyle(.secondary)
                .frame(width: 24)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(map.name)
                Text([map.region, map.installed ? map.installedVersion ?? map.version : map.version, Format.bytes(map.size),
                      place]
                        .compactMap { $0 }.joined(separator: " · "))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
            }
            Spacer()
            trailing()
        }
        .padding(.vertical, 2)
    }
}

// MARK: - Apps

/// The Connect IQ apps on the device, by kind; read-only: new apps are chosen in the Store.
struct AppsPage: View {
    let model: DeviceModel
    @State private var search = ""

    private func matches(_ name: String) -> Bool { search.isEmpty || name.localizedStandardContains(search) }

    var body: some View {
        Group {
            if let list = model.apps {
                if list.apps.isEmpty, list.account.isEmpty {
                    ContentUnavailableView {
                        Label("No Connect IQ Apps", systemImage: "square.grid.2x2")
                    } description: {
                        Text(list.message ?? String(localized: "Apps, watch faces and data fields installed on the device appear here."))
                        if let note = list.note { Text(note).font(.callout) }
                    } actions: {
                        Link("Open the Connect IQ Store", destination: AppsPage.storeURL)
                    }
                } else {
                    content(list)
                }
            } else {
                PageLoadingView(model: model, page: .apps)
            }
        }
        .searchable(text: $search, placement: .toolbar, prompt: "Search Apps")
    }

    static let storeURL = URL(string: "https://apps.garmin.com")!

    private func content(_ list: AppsList) -> some View {
        List {
            if let message = list.message {
                Section {
                    Label(message, systemImage: "info.circle")
                        .foregroundStyle(.secondary)
                }
            }
            if let warning = list.warning {
                Section {
                    Label {
                        Text(warning)
                    } icon: {
                        Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                    }
                }
            }
            if list.message == nil {
                Section {
                    // An app on the device as two kinds (widget and activity) is one update.
                    AppsSummaryView(summary: list.summary, updates: Set(list.updatable.map(\.appID)).count,
                                    disabled: list.disabledCount)
                }
            }
            ForEach(list.groups, id: \.kind) { group in
                let apps = group.apps.filter { matches($0.name) }
                if !apps.isEmpty {
                    Section {
                        ForEach(apps) { AppRow(app: $0) }
                    } header: {
                        AppKindHeader(kind: group.kind, count: group.apps.count)
                    }
                }
            }
            let account = list.account.filter { matches($0.name) }
            if !account.isEmpty {
                Section {
                    ForEach(account) { AccountAppRow(app: $0) }
                } header: {
                    Text("In Your Garmin Account")
                } footer: {
                    Text("Added to your Garmin account in the Connect IQ Store, but not on the device.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            Section {
                VStack(alignment: .leading, spacing: 6) {
                    Label {
                        Text(list.note ?? String(localized: "New apps are chosen in the Connect IQ Store (website or phone); they then appear in the list above."))
                            .font(.callout)
                            .foregroundStyle(.secondary)
                    } icon: {
                        Image(systemName: "bag").foregroundStyle(.secondary)
                    }
                    Link("Open the Connect IQ Store", destination: AppsPage.storeURL)
                        .font(.callout)
                        .padding(.leading, 28)
                }
                .padding(.vertical, 2)
            }
        }
    }
}

/// The device's Connect IQ room: apps, free slots and free app memory, with a capacity bar
/// of the slots.
struct AppsSummaryView: View {
    let summary: AppsSummary
    let updates: Int
    let disabled: Int

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top, spacing: 0) {
                stat(value: summary.storeApps.formatted(),
                     caption: disabled > 0 ? String(localized: "Apps, \(disabled) disabled") : String(localized: "Apps"),
                     systemImage: "square.grid.2x2")
                Divider().frame(maxHeight: 44)
                stat(value: summary.freeSlots.map { $0.formatted() } ?? "—",
                     caption: String(localized: "Free slots"), systemImage: "square.dashed")
                Divider().frame(maxHeight: 44)
                stat(value: summary.freeBytes.map(Format.bytes) ?? "—",
                     caption: String(localized: "Free app memory"), systemImage: "memorychip")
                if updates > 0 {
                    Divider().frame(maxHeight: 44)
                    stat(value: updates.formatted(), caption: String(localized: "Updates in the Store"),
                         systemImage: "arrow.down.circle", tint: .accentColor)
                }
            }
            if let total = summary.totalSlots, total > 0 {
                let used = Double(summary.storeApps) / Double(total)
                VStack(alignment: .leading, spacing: 6) {
                    HStack {
                        Text("Connect IQ slots")
                        Spacer()
                        Text("\(summary.storeApps) of \(total) used")
                            .monospacedDigit()
                            .foregroundStyle(.secondary)
                    }
                    .font(.callout)
                    Gauge(value: used) { EmptyView() }
                        .gaugeStyle(.linearCapacity)
                        .labelsHidden()
                        .tint(used > 0.9 ? .orange : .accentColor)
                }
                .padding(.horizontal, 10)
                .accessibilityElement(children: .ignore)
                .accessibilityLabel("Connect IQ slots")
                .accessibilityValue(Text("\(summary.freeSlots ?? 0) of \(total) slots free"))
            }
        }
        .padding(.vertical, 6)
    }

    private func stat(value: String, caption: String, systemImage: String, tint: Color? = nil) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(value)
                .font(.title2.weight(.semibold))
                .monospacedDigit()
                .foregroundStyle(tint.map { AnyShapeStyle($0) } ?? AnyShapeStyle(.primary))
                .lineLimit(1)
                .minimumScaleFactor(0.8)
            Label(caption, systemImage: systemImage)
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(2)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 10)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(Text("\(caption): \(value)"))
    }
}

struct AppKindHeader: View {
    let kind: AppKind
    let count: Int

    var body: some View {
        HStack {
            Label(kind.title, systemImage: kind.systemImage)
            Spacer()
            Text(count.formatted())
                .monospacedDigit()
                .foregroundStyle(.secondary)
                .accessibilityHidden(true)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(Text("\(kind.title), \(count)"))
        .accessibilityAddTraits(.isHeader)
    }
}

struct AppRow: View {
    let app: ConnectIQApp

    private var detail: String {
        var parts: [String] = []
        if let version = app.version { parts.append(String(localized: "Version \(version)")) }
        if !app.enabled { parts.append(String(localized: "Disabled")) }
        if !app.removable { parts.append(String(localized: "Comes with the device")) }
        return parts.joined(separator: " · ")
    }

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: app.kind.systemImage)
                .font(.title3)
                .foregroundStyle(app.enabled ? AnyShapeStyle(.tint) : AnyShapeStyle(.tertiary))
                .symbolRenderingMode(.hierarchical)
                .frame(width: 24)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(app.name)
                    .foregroundStyle(app.enabled ? .primary : .secondary)
                if !detail.isEmpty {
                    Text(detail)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .monospacedDigit()
                }
            }
            Spacer()
            if let waiting = app.waitingText {
                Label(waiting, systemImage: "cable.connector.slash")
                    .font(.callout)
                    .foregroundStyle(.orange)
                    .lineLimit(2)
                    .multilineTextAlignment(.trailing)
            } else if let update = app.update {
                AppUpdateBadge(update: update)
            }
        }
        .padding(.vertical, 2)
        // The separator starts under the name, not under the badge on the right.
        .alignmentGuide(.listRowSeparatorLeading) { _ in 36 }
        .accessibilityElement(children: .combine)
    }
}

/// "Update 4.26.23": a newer version is in the Connect IQ Store.
struct AppUpdateBadge: View {
    let update: AppUpdate
    @Environment(\.colorSchemeContrast) private var contrast

    var body: some View {
        Label("Update \(update.version)", systemImage: update.permissionsChanged ? "exclamationmark.shield" : "arrow.down.circle.fill")
            .font(.caption.weight(.semibold))
            .monospacedDigit()
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .foregroundStyle(contrast == .increased ? AnyShapeStyle(.primary) : AnyShapeStyle(.tint))
            .background(Color.accentColor.opacity(contrast == .increased ? 0.35 : 0.15), in: Capsule())
            .help(update.permissionsChanged
                  ? String(localized: "Version \(update.version) is in the Connect IQ Store; it asks for other permissions.")
                  : String(localized: "Version \(update.version) is in the Connect IQ Store."))
            .accessibilityLabel(update.permissionsChanged
                                ? Text("Update available: \(update.version), asks for other permissions")
                                : Text("Update available: \(update.version)"))
    }
}

struct AccountAppRow: View {
    let app: AccountApp

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: app.kind.systemImage)
                .font(.title3)
                .foregroundStyle(.secondary)
                .frame(width: 24)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(app.name)
                Text([app.kindName, app.version, app.size.map(Format.bytes), app.developer]
                        .compactMap { $0 }.joined(separator: " · "))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
            }
            Spacer()
        }
        .padding(.vertical, 2)
        .accessibilityElement(children: .combine)
    }
}

// MARK: - Backups

struct BackupsPage: View {
    @Environment(AppModel.self) private var app
    let model: DeviceModel
    @State private var selection: BackupSnapshot.ID?

    var body: some View {
        Group {
            if let backups = model.backups {
                if backups.isEmpty {
                    ContentUnavailableView("No Backups", systemImage: "externaldrive.badge.timemachine",
                                           description: Text("Back up the device to keep a copy of its files on this Mac."))
                } else {
                    List(backups, selection: $selection) { backup in
                        BackupRow(backup: backup)
                            .contextMenu { Button("Show in Finder") { showInFinder(backup.url) } }
                    }
                    .contextMenu(forSelectionType: BackupSnapshot.ID.self) { _ in } primaryAction: { ids in
                        if let id = ids.first { showInFinder(URL(fileURLWithPath: id)) }
                    }
                    .safeAreaInset(edge: .bottom) { footer }
                }
            } else {
                PageLoadingView(model: model, page: .backups)
            }
        }
        .toolbar {
            ToolbarItem {
                Button("Show in Finder", systemImage: "folder") {
                    let folder = app.backupFolder.appending(path: model.device.unitID)
                    let target = selection.map { URL(fileURLWithPath: $0) }
                        ?? (FileManager.default.fileExists(atPath: folder.path) ? folder : app.backupFolder)
                    showInFinder(target)
                }
                .help("Show the backup in Finder")
            }
        }
    }

    private var footer: some View {
        Text(app.backupKeep > 0
             ? "Backups are kept in \(app.backupFolder.path(percentEncoded: false)); the newest \(app.backupKeep) are kept."
             : "Backups are kept in \(app.backupFolder.path(percentEncoded: false)).")
            .font(.caption)
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 20)
            .padding(.vertical, 8)
    }
}

struct BackupRow: View {
    let backup: BackupSnapshot

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: "externaldrive.badge.timemachine")
                .font(.title3)
                .foregroundStyle(.secondary)
                .frame(width: 24)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(backup.date, format: .dateTime.weekday(.wide).day().month(.wide).year().hour().minute())
                Text(Format.relative(backup.date))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 2) {
                Text(Format.bytes(backup.bytes)).monospacedDigit()
                Text("\(backup.files) files").font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 2)
    }
}
