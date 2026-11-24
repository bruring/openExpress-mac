import OpenExpressKit
import SwiftUI

/// The pages of one device, chosen in the toolbar; the floating action of the page at the bottom.
struct DeviceDetailView: View {
    @Environment(AppModel.self) private var app
    let model: DeviceModel
    /// A plan for which the card reader was chosen: its sheet opens once the confirmation's is
    /// gone.
    @State private var readerPlan: InstallPlan?

    /// The window is too narrow for the page titles (below this width the toolbar would push
    /// its trailing items into the overflow menu).
    @Environment(\.windowWidth) private var windowWidth
    private var compactToolbar: Bool { windowWidth > 0 && windowWidth < 1150 }

    var body: some View {
        @Bindable var app = app
        @Bindable var model = model
        page
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            // An inset, not an overlay: lists and forms scroll their last rows clear of the
            // floating action, under the scroll edge effect.
            .safeAreaInset(edge: .bottom, spacing: 0) {
                if model.busyBy == nil {
                    FloatingActionBar(model: model, page: app.page)
                }
            }
            // The short name: the full one is on Overview.
            .navigationTitle(model.device.shortName)
            .navigationSubtitle(subtitle)
            .toolbar {
                ToolbarItem(placement: .principal) {
                    // In a narrow window the pages become icons (titles in their tooltips and
                    // for VoiceOver), so the inspector toggle and search never overflow.
                    Picker("Page", selection: $app.page) {
                        ForEach(DevicePage.allCases) { page in
                            Group {
                                if compactToolbar {
                                    Image(systemName: page.systemImage)
                                        .accessibilityLabel(page.title)
                                } else {
                                    Text(page.title)
                                }
                            }
                            .help(page.title)
                            .tag(page)
                        }
                    }
                    .pickerStyle(.segmented)
                    .labelsHidden()
                    .accessibilityLabel("Device page")
                }
                ToolbarSpacer(.flexible)
                ToolbarItemGroup(placement: .primaryAction) {
                    Button("Refresh", systemImage: "arrow.clockwise") {
                        Task { await model.load(app.page) }
                    }
                    .help("Read this page from the device again")
                    .disabled(model.loading.contains(app.page) || (app.page.needsConnection && !model.device.connected))
                }
            }
            // The model owns the load: leaving the page never cancels it, and a page read
            // once is shown again without reading the device (Refresh reads it again).
            .task(id: app.page) {
                await model.loadIfNeeded(app.page)
                // Development: `-OEInitialPage updates -OEShowInstallPlan YES` opens the confirmation.
                if app.page == .updates, UserDefaults.standard.bool(forKey: "OEShowInstallPlan") {
                    model.requestInstallAll()
                }
            }
            .sheet(item: $model.pendingPlan, onDismiss: {
                // Asked for the card reader: its sheet once this one is gone.
                if let plan = readerPlan {
                    readerPlan = nil
                    model.pendingReaderPlan = plan
                }
            }) { plan in
                InstallConfirmationSheet(plan: plan, deviceName: model.device.shortName,
                                         useCardReader: { readerPlan = $0 }) { chosen in
                    model.perform(chosen)
                }
            }
            .sheet(item: $model.pendingReaderPlan) { plan in
                CardReaderSheet(model: model, plan: plan)
            }
            .confirmationDialog("First Sync of \(model.device.shortName)", isPresented: $model.needsFirstSyncChoice) {
                Button("Upload New Data Only") { model.sync(firstRun: .newOnly) }
                Button("Upload Everything on the Device") { model.sync(firstRun: .everything) }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("openExpress for Mac has not synced this device before. Upload only what the device records from now on, or everything still on it (activities Garmin Connect already has are recognised).")
            }
            .alert("openExpress for Mac", isPresented: Binding(
                get: { model.errorMessage != nil },
                set: { if !$0 { model.errorMessage = nil } })
            ) {
                Button("OK", role: .cancel) {}
            } message: {
                Text(model.errorMessage ?? "")
            }
    }

    private var subtitle: String {
        if let outcome = model.lastOutcome { return outcome }
        if model.device.connected { return model.device.software.map { String(localized: "Software \($0)") } ?? "" }
        return String(localized: "Not connected")
    }

    @ViewBuilder
    private var page: some View {
        if let program = model.busyBy {
            BusyView(model: model, program: program)
        } else if app.page.needsConnection, !model.device.connected {
            ContentUnavailableView {
                Label("\(model.device.name) Is Not Connected", systemImage: "cable.connector")
            } description: {
                Text("Connect it with its USB cable to see its \(app.page.title.lowercased()).")
            }
        } else {
            switch app.page {
            case .overview: OverviewPage(model: model)
            case .activities: ActivitiesPage(model: model)
            case .updates: UpdatesPage(model: model)
            case .maps: MapsPage(model: model)
            case .apps: AppsPage(model: model)
            case .backups: BackupsPage(model: model)
            }
        }
    }
}

/// Another program (Garmin Express Service) holds the device. openExpress never quits it.
struct BusyView: View {
    let model: DeviceModel
    let program: String

    var body: some View {
        ContentUnavailableView {
            Label("\(model.device.name) Is in Use", systemImage: "lock.circle")
        } description: {
            Text("\(program) is using the device, so openExpress for Mac cannot read it.")
            Text("Quit Garmin Express: choose Quit from its menu bar icon, so that Garmin Express Service stops too. Then try again. openExpress for Mac does not quit other apps for you.")
                .font(.callout)
        } actions: {
            Button("Try Again") { Task { await model.retryAfterBusy() } }
                .buttonStyle(.glassProminent)
                .keyboardShortcut(.defaultAction)
        }
    }
}

/// The page's primary action as a floating glass button that morphs into the progress
/// capsule of the running operation (Install All → progress).
struct FloatingActionBar: View {
    let model: DeviceModel
    let page: DevicePage
    @Namespace private var glass
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private struct Action {
        var title: LocalizedStringKey
        var systemImage: String
        var perform: () -> Void
    }

    private var action: Action? {
        guard model.device.connected else { return nil }
        switch page {
        case .overview:
            return Action(title: "Sync Now", systemImage: "arrow.triangle.2.circlepath") { model.sync() }
        case .updates:
            guard model.badgeCount > 0 else { return nil }
            return Action(title: model.badgeCount == 1 ? "Install…" : "Install All…", systemImage: "arrow.down.circle") {
                model.requestInstallAll()
            }
        case .maps:
            guard !model.mapUpdates.isEmpty else { return nil }
            return Action(title: "Update All Maps", systemImage: "map") { model.updateAllMaps() }
        case .backups:
            return Action(title: "Back Up Now", systemImage: "externaldrive.badge.timemachine") { model.backUp() }
        case .activities, .apps:
            return nil
        }
    }

    var body: some View {
        GlassEffectContainer(spacing: 16) {
            if let operation = model.operation {
                OperationCapsule(operation: operation) { model.cancelOperation() }
                    .glassEffect(.regular.interactive(), in: .capsule)
                    .glassEffectID("action", in: glass)
            } else if let action {
                Button(action: action.perform) {
                    Label(action.title, systemImage: action.systemImage)
                        .padding(.horizontal, 6)
                }
                .buttonStyle(.glassProminent)
                .controlSize(.extraLarge)
                .glassEffectID("action", in: glass)
            }
        }
        .padding(.vertical, 14)
        .frame(maxWidth: .infinity)
        .animation(reduceMotion ? nil : .bouncy, value: model.operation?.kind)
    }
}

struct OperationCapsule: View {
    let operation: DeviceModel.Operation
    let cancel: () -> Void

    var body: some View {
        HStack(spacing: 12) {
            Group {
                if let fraction = operation.progress?.fraction {
                    ProgressView(value: fraction)
                } else {
                    ProgressView()
                }
            }
            .progressViewStyle(.circular)
            .controlSize(.small)

            VStack(alignment: .leading, spacing: 1) {
                Text(operation.kind.title)
                    .font(.headline)
                Text(operation.detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .contentTransition(.opacity)
                if let speed = operation.rate.text {
                    // "Download 12.3 MB/s · 2 min left", while bytes are moved.
                    Label(speed, systemImage: operation.rate.kind == .download ? "arrow.down.circle" : "arrow.left.arrow.right")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .monospacedDigit()
                        .lineLimit(1)
                        .contentTransition(.numericText())
                }
            }
            .frame(minWidth: 180, alignment: .leading)

            Button("Cancel", systemImage: "xmark.circle.fill", action: cancel)
                .labelStyle(.iconOnly)
                .buttonStyle(.borderless)
                .foregroundStyle(.secondary)
                .disabled(operation.cancelling)
                .help("Stops at the next safe point; a file being written to the device is finished first.")
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 10)
        .accessibilityElement(children: .combine)
        .accessibilityLabel(Text("\(operation.kind.title), \(operation.detail)" + (operation.rate.text.map { ", \($0)" } ?? "")))
        .accessibilityValue(operation.progress?.fraction.map { Text($0, format: .percent.precision(.fractionLength(0))) } ?? Text(""))
    }
}

/// Install All (and a large map) asks first: what, how much, the space left, and that the
/// software installs only once the device is unplugged.
struct InstallConfirmationSheet: View {
    let proposed: InstallPlan
    let deviceName: String
    /// Offered for a slow write of the memory card: the card in this Mac's card reader instead.
    let useCardReader: ((InstallPlan) -> Void)?
    let install: (InstallPlan) -> Void
    /// A move: where its files come from (read off the device, or downloaded again).
    @State private var source: MoveSource
    @Environment(\.dismiss) private var dismiss

    init(plan: InstallPlan, deviceName: String, useCardReader: ((InstallPlan) -> Void)? = nil,
         install: @escaping (InstallPlan) -> Void) {
        proposed = plan
        self.deviceName = deviceName
        self.useCardReader = useCardReader
        self.install = install
        _source = State(initialValue: plan.source)
    }

    /// The plan as chosen here.
    private var plan: InstallPlan {
        var chosen = proposed
        chosen.source = source
        return chosen
    }

    /// "From the device, about 4 min" / "Download again from Garmin, about 1 min".
    private func sourceLabel(_ from: MoveSource) -> String {
        let time = plan.secondsMoving(from: from).map { TransferRate.timeLeft($0) }
        switch from {
        case .device:
            return time.map { String(localized: "Copy from the device, about \($0)") }
                ?? String(localized: "Copy from the device")
        case .download:
            return time.map { String(localized: "Download again from Garmin, about \($0)") }
                ?? String(localized: "Download again from Garmin (its time depends on your connection)")
        }
    }

    private var title: String {
        let name = plan.items.first?.name ?? ""
        switch plan.action {
        case .moveMap(_, let to):
            if plan.keepsOne {
                return to == .card ? String(localized: "Keep \(name) on the Memory Card?")
                    : String(localized: "Keep \(name) on the Device?")
            }
            return to == .card ? String(localized: "Move \(name) to the Memory Card?")
                : String(localized: "Move \(name) to the Device?")
        case .installMap(_, let to):
            return to == .card ? String(localized: "Install \(name) on the Memory Card?")
                : String(localized: "Install \(name)?")
        case .installAll:
            return plan.items.count == 1 ? String(localized: "Install \(name)?")
                : String(localized: "Install \(plan.items.count) Updates on \(deviceName)?")
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Label(title, systemImage: plan.isMove ? "arrow.left.arrow.right.circle" : "arrow.down.circle")
                .font(.title2.weight(.semibold))

            Form {
                Section {
                    ForEach(plan.items) { item in
                        LabeledContent {
                            Text(Format.bytes(item.size)).monospacedDigit()
                        } label: {
                            Label {
                                VStack(alignment: .leading, spacing: 1) {
                                    Text(item.name)
                                    if let change = item.change {
                                        Text(change).font(.caption).foregroundStyle(.secondary).monospacedDigit()
                                    }
                                }
                            } icon: {
                                Image(systemName: item.kind == .software ? "cpu" : "map")
                            }
                        }
                    }
                }
                if plan.isMove, !plan.keepsOne {
                    Section("Its Map Files") {
                        Picker("Its Map Files", selection: $source) {
                            Text(sourceLabel(.device)).tag(MoveSource.device)
                            Text(sourceLabel(.download)).tag(MoveSource.download)
                        }
                        .pickerStyle(.radioGroup)
                        .labelsHidden()
                        Text("Either way, the map stays where it is until its new copy is written and checked.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
                Section {
                    if !plan.keepsOne {
                        LabeledContent(plan.isMove ? "Copied on the device" : "Download",
                                       value: Format.bytes(plan.totalBytes))
                    }
                    if let after = plan.freeAfter, !plan.keepsOne {
                        LabeledContent(plan.toCard ? "Free on the memory card afterwards" : "Free on the device afterwards") {
                            Text(after >= 0 ? Format.bytes(after) : String(localized: "\(Format.bytes(-after)) too little"))
                                .foregroundStyle(after >= 0 ? AnyShapeStyle(.secondary) : AnyShapeStyle(.red))
                        }
                    }
                }
            }
            .formStyle(.grouped)
            .scrollDisabled(plan.items.count < 6)
            .frame(minHeight: 140, maxHeight: 360)

            VStack(alignment: .leading, spacing: 6) {
                Label("Keep the device connected while the files are copied.", systemImage: "cable.connector")
                if plan.keepsOne {
                    Label("The copy kept is checked against Garmin's files first; then the other copy is removed.",
                          systemImage: "checkmark.shield")
                } else if plan.isMove {
                    Label("The map's files are copied and checked, and only then removed from where they were. Its unlock and search files stay on the device.",
                          systemImage: "checkmark.shield")
                } else if plan.toCard {
                    Label("The map's files go to the memory card; its unlock and search files stay on the device, as Garmin Express puts them.",
                          systemImage: "sdcard")
                }
                if plan.offersCardReader, useCardReader != nil, let usb = plan.secondsOverUSB,
                   let reader = plan.secondsInReader {
                    Label("Over USB this takes about \(TransferRate.timeLeft(usb)): the device writes its memory card slowly. With the card in your Mac's card reader, about \(TransferRate.timeLeft(reader))\(plan.isMove ? "" : String(localized: " after the download")).",
                          systemImage: "timer")
                        .foregroundStyle(.orange)
                }
                if plan.hasSoftware {
                    Label("Then unplug the device: it installs the software by itself and restarts. Keep it charged.",
                          systemImage: "cable.connector.slash")
                }
            }
            .font(.callout)
            .foregroundStyle(.secondary)
            .fixedSize(horizontal: false, vertical: true)

            HStack {
                Spacer()
                Button("Cancel", role: .cancel) { dismiss() }
                    .keyboardShortcut(.cancelAction)
                if plan.offersCardReader, let useCardReader {
                    Button("Use Card Reader…") {
                        useCardReader(plan)
                        dismiss()
                    }
                    .buttonStyle(.glass)
                }
                Button(plan.keepsOne ? "Keep" : plan.offersCardReader && useCardReader != nil ? "Over USB"
                       : plan.isMove ? "Move" : "Install") {
                    dismiss()
                    install(plan)
                }
                .buttonStyle(.glassProminent)
                .keyboardShortcut(.defaultAction)
                .disabled(!plan.fits)
            }
        }
        .padding(24)
        .frame(width: 500)
    }
}

/// The memory card written in this Mac's card reader: take it out of the device (which stays
/// connected), put it in the Mac; the card is recognised as the device's before anything is
/// written, then the map's card files go to it, its unlock and search files to the device over
/// USB, and the card is ejected to be put back.
struct CardReaderSheet: View {
    let model: DeviceModel
    let plan: InstallPlan
    @State private var found: CardReaderList?
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Label("Use Your Mac's Card Reader", systemImage: "sdcard")
                .font(.title2.weight(.semibold))
            VStack(alignment: .leading, spacing: 8) {
                Label("Take the memory card out of \(model.device.shortName). Leave the device connected.",
                      systemImage: "1.circle")
                Label("Put the card in your Mac's card reader.", systemImage: "2.circle")
                Label("Once it is recognised, \(plan.items.first?.name ?? "the map") goes onto it; the card is then ejected, to go back into the device.",
                      systemImage: "3.circle")
            }
            .fixedSize(horizontal: false, vertical: true)

            GroupBox {
                if let card = found?.card {
                    Label("\(card.name) is \(model.device.shortName)'s memory card.", systemImage: "checkmark.circle.fill")
                        .foregroundStyle(.green)
                } else if let other = found?.volumes.first {
                    Label("\(other.name): \(other.reason ?? String(localized: "not this device's memory card"))",
                          systemImage: "exclamationmark.triangle.fill")
                        .foregroundStyle(.orange)
                } else if found?.remembered == false {
                    Label("openExpress has not seen this device's memory card yet: put it back in the device, open the Maps page once, then try again.",
                          systemImage: "exclamationmark.triangle.fill")
                        .foregroundStyle(.orange)
                } else {
                    HStack(spacing: 8) {
                        ProgressView().controlSize(.small)
                        Text("Waiting for the card…").foregroundStyle(.secondary)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            HStack {
                Spacer()
                Button("Cancel", role: .cancel) { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button(plan.isMove ? "Move" : "Install") {
                    var withReader = plan
                    withReader.cardReader = found?.card?.path
                    dismiss()
                    model.perform(withReader)
                }
                .buttonStyle(.glassProminent)
                .keyboardShortcut(.defaultAction)
                .disabled(found?.card == nil)
            }
        }
        .padding(24)
        .frame(width: 500)
        .task {
            // The reader asked every two seconds while the sheet is open (nothing is opened on
            // the device for it).
            while !Task.isCancelled {
                found = await model.cardReaders()
                try? await Task.sleep(for: .seconds(2))
            }
        }
    }
}

/// A page while it is read: counted progress when the engine reports it (the first read of
/// the activities goes through every FIT file), "Waiting for the device…" while the request
/// waits behind another for the same device, else a plain spinner.
struct PageLoadingView: View {
    let model: DeviceModel
    let page: DevicePage
    var label: LocalizedStringKey? = nil

    var body: some View {
        VStack(spacing: 10) {
            if let progress = model.loadProgress[page] {
                if let fraction = progress.fraction {
                    ProgressView(value: fraction)
                        .frame(width: 280)
                } else {
                    ProgressView()
                }
                Text(progress.detail)
                    .font(.headline)
                    .monospacedDigit()
                    .contentTransition(.numericText())
                if progress.phase == "reading_activities" {
                    Text("The first time, every activity file is read from the device; this takes a while. Later it is quick.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                        .frame(maxWidth: 340)
                }
            } else if model.isWaiting(page) {
                ProgressView()
                Text("Waiting for the device…")
                    .font(.headline)
                Text("The device answers one request at a time; this page is next.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            } else {
                ProgressView()
                if let label { Text(label).foregroundStyle(.secondary) }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .accessibilityElement(children: .combine)
    }
}
