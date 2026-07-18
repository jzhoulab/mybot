import SwiftUI
import AppKit

@main
struct MybotMenuApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var model = AppModel.shared

    var body: some Scene {
        MenuBarExtra {
            ContentView(model: model)
        } label: {
            Image(nsImage: model.statusIcon)
                .renderingMode(.template)
        }
        .menuBarExtraStyle(.window)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        let args = CommandLine.arguments
        if args.contains("--render-icons") || args.contains("--render-ui") {
            if args.contains("--render-icons") {
                IconExporter.exportSheet()
                IconExporter.exportAppIcon()
            }
            if args.contains("--render-ui") {
                UIExporter.exportAll()
            }
            NSApp.terminate(nil)
            return
        }
        AppModel.shared.start()
    }
}

// MARK: - Design helpers

private enum Palette {
    // Drawn from the bunny icon: crisp neutrals carry the UI, ONE confident
    // teal-blue (the icon's gradient) marks everything interactive, and status
    // hues stay small — dots, icons, and single words, never colored slabs.
    static let iconTeal = Color(red: 0.20, green: 0.84, blue: 0.76)   // icon gradient start (vivid)
    static let iconBlue = Color(red: 0.12, green: 0.56, blue: 0.94)   // icon gradient end (vivid)
    // A saturated teal-blue, not a greyed-down one — deepening it for contrast
    // shouldn't leave it dull.
    static let accent   = Color(red: 0.03, green: 0.58, blue: 0.90)   // the gradient, for text/controls
    static var brandGradient: LinearGradient {
        LinearGradient(colors: [iconTeal, iconBlue], startPoint: .topLeading, endPoint: .bottomTrailing)
    }

    static let good = Color(red: 0.15, green: 0.60, blue: 0.44)       // teal-leaning green
    static let warn = Color(red: 0.86, green: 0.62, blue: 0.20)       // clear amber
    static let bad  = Color(red: 0.87, green: 0.36, blue: 0.33)       // coral (badge-red family)

    static func source(_ name: String) -> Color {
        switch name {
        case "codex": return Color(red: 0.44, green: 0.49, blue: 0.90)
        case "claude": return Color(red: 0.80, green: 0.51, blue: 0.30)
        default: return .gray
        }
    }
}

private struct SourceTag: View {
    // These sit on nearly every row: the hue lives in a small dot so the tag
    // identifies the source without shouting over the row's actual content.
    let source: String
    var body: some View {
        HStack(spacing: 4) {
            Circle().fill(Palette.source(source)).frame(width: 5, height: 5)
            Text(source.uppercased())
                .font(.system(size: 8.5, weight: .semibold))
                .tracking(0.7)
                .foregroundStyle(.secondary)
        }
        .padding(.leading, 6).padding(.trailing, 7).padding(.vertical, 2.5)
        .background(Capsule().fill(Color.primary.opacity(0.055)))
    }
}

private struct StatChip: View {
    let icon: String
    let value: String
    let label: String
    var tint: Color = .secondary
    var body: some View {
        HStack(spacing: 7) {
            Image(systemName: icon).font(.system(size: 13, weight: .semibold)).foregroundStyle(tint)
            VStack(alignment: .leading, spacing: 0) {
                Text(value).font(.system(size: 14, weight: .bold)).lineLimit(1)
                Text(label).font(.system(size: 9.5)).foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
        }
        .padding(.vertical, 8).padding(.horizontal, 10)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color.primary.opacity(0.05)))
    }
}

private func chipLabel(_ text: String, _ icon: String) -> some View {
    HStack(spacing: 4) {
        Image(systemName: icon).font(.system(size: 9, weight: .semibold))
        Text(text).font(.system(size: 11, weight: .medium))
    }
    .padding(.horizontal, 9).padding(.vertical, 6)
    .background(Capsule().fill(Color.primary.opacity(0.07)))
    .foregroundStyle(.primary)
}

/// "You're Ada Lovelace, right?" — one-click confirm of the deduced identity,
/// with an inline correction field for when Sherlock got it wrong.
private struct IdentityConfirmCard: View {
    @ObservedObject var model: AppModel
    @State private var correcting = false
    @State private var correctedName = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Image(systemName: "person.crop.circle.badge.questionmark")
                    .font(.system(size: 14, weight: .semibold)).foregroundStyle(Palette.accent)
                VStack(alignment: .leading, spacing: 1) {
                    Text("I did some digging — you're \(model.ownerName), right?")
                        .font(.system(size: 12, weight: .bold))
                    if !model.ownerAliases.isEmpty {
                        Text("also goes by " + model.ownerAliases.prefix(4).joined(separator: ", "))
                            .font(.system(size: 10.5)).foregroundStyle(.secondary)
                    }
                }
                Spacer()
            }
            if correcting {
                HStack(spacing: 8) {
                    TextField("Your name", text: $correctedName)
                        .textFieldStyle(.roundedBorder).font(.system(size: 12))
                        .onSubmit { model.confirmIdentity(name: correctedName) }
                    Button("Save") { model.confirmIdentity(name: correctedName) }
                        .buttonStyle(.plain).font(.system(size: 11, weight: .semibold))
                        .padding(.horizontal, 12).padding(.vertical, 6)
                        .background(Capsule().fill(Palette.accent)).foregroundStyle(.white)
                        .disabled(correctedName.trimmingCharacters(in: .whitespaces).isEmpty)
                }
            } else {
                HStack(spacing: 8) {
                    Button {
                        model.confirmIdentity(name: model.ownerName)
                    } label: {
                        Text("That's me").font(.system(size: 11, weight: .semibold))
                            .padding(.horizontal, 12).padding(.vertical, 6)
                            .background(Capsule().fill(Palette.accent)).foregroundStyle(.white)
                    }
                    .buttonStyle(.plain)
                    Button {
                        correctedName = model.ownerName
                        correcting = true
                    } label: {
                        Text("Not quite…").font(.system(size: 11, weight: .semibold))
                            .padding(.horizontal, 12).padding(.vertical, 6)
                            .background(Capsule().fill(Color.primary.opacity(0.06)))
                            .foregroundStyle(.secondary)
                    }
                    .buttonStyle(.plain)
                    Spacer()
                }
            }
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Color.primary.opacity(0.05)))
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(Palette.accent.opacity(0.35)))
    }
}

/// Collapsible "Connections" card: shows Discord/Slack configured status and,
/// on expand, lets the owner paste tokens and connect (validated live via the
/// connect doctor's --json mode). Collapsed by default — a slim status bar.
private struct ConnectionsCard: View {
    @ObservedObject var model: AppModel
    @State private var expanded = false
    @State private var openForm = ""           // which platform's form is showing
    @State private var botToken = ""
    @State private var appToken = ""
    @State private var channels = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button { withAnimation(.easeOut(duration: 0.15)) { expanded.toggle() } } label: {
                HStack(spacing: 8) {
                    Image(systemName: "link").font(.system(size: 11, weight: .semibold)).foregroundStyle(.secondary)
                    Text("Connections").font(.system(size: 11.5, weight: .semibold))
                    Spacer()
                    statusDot(model.discordConfigured); Text("Discord").font(.system(size: 10)).foregroundStyle(.secondary)
                    statusDot(model.slackConfigured); Text("Slack").font(.system(size: 10)).foregroundStyle(.secondary)
                    Image(systemName: expanded ? "chevron.down" : "chevron.right")
                        .font(.system(size: 9, weight: .semibold)).foregroundStyle(.secondary)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            if expanded {
                platformRow("discord", "Discord", configured: model.discordConfigured)
                Divider().opacity(0.3).padding(.vertical, 2)
                platformRow("slack", "Slack", configured: model.slackConfigured)
                if !model.connectMessage.isEmpty {
                    Text(model.connectMessage)
                        .font(.system(size: 10.5))
                        .foregroundStyle(model.connectOk ? Palette.good : Palette.bad)
                        .padding(.top, 6)
                }
            }
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 11, style: .continuous).fill(Color.primary.opacity(0.04)))
        .padding(.horizontal, 12).padding(.top, 4)
    }

    private func statusDot(_ ok: Bool) -> some View {
        Circle().fill(ok ? Palette.good : Color.secondary.opacity(0.4)).frame(width: 6, height: 6)
    }

    @ViewBuilder private func platformRow(_ platform: String, _ title: String, configured: Bool) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                statusDot(configured)
                Text(title).font(.system(size: 12, weight: .semibold))
                Text(configured ? "connected" : "not connected")
                    .font(.system(size: 10)).foregroundStyle(.secondary)
                Spacer()
                Button(openForm == platform ? "Cancel" : (configured ? "Reconfigure" : "Set up")) {
                    withAnimation(.easeOut(duration: 0.12)) {
                        openForm = openForm == platform ? "" : platform
                        botToken = ""; appToken = ""; channels = ""; model.connectMessage = ""
                    }
                }
                .buttonStyle(.plain).font(.system(size: 11, weight: .semibold)).foregroundStyle(Palette.accent)
            }
            if openForm == platform {
                if platform == "discord" {
                    SecureField("Bot token", text: $botToken).textFieldStyle(.roundedBorder).font(.system(size: 11))
                    TextField("Channel IDs (optional, comma-separated)", text: $channels)
                        .textFieldStyle(.roundedBorder).font(.system(size: 11))
                } else {
                    SecureField("Bot token (xoxb-…)", text: $botToken).textFieldStyle(.roundedBorder).font(.system(size: 11))
                    SecureField("App token (xapp-…)", text: $appToken).textFieldStyle(.roundedBorder).font(.system(size: 11))
                }
                HStack(spacing: 8) {
                    Text("Create the app first — see .\(platform).env.example for the exact steps.")
                        .font(.system(size: 9.5)).foregroundStyle(.secondary)
                    Spacer()
                    if model.connecting {
                        ProgressView().controlSize(.small)
                    } else {
                        Button("Connect") {
                            model.connectPlatform(platform, botToken: botToken, appToken: appToken, channels: channels)
                        }
                        .buttonStyle(.plain).font(.system(size: 11, weight: .semibold))
                        .padding(.horizontal, 12).padding(.vertical, 5)
                        .background(Capsule().fill(Palette.accent)).foregroundStyle(.white)
                        .disabled(botToken.isEmpty || (platform == "slack" && appToken.isEmpty))
                    }
                }
            }
        }
        .padding(.top, 6)
    }
}

// MARK: - Root

struct ContentView: View {
    @ObservedObject var model: AppModel

    var body: some View {
        VStack(spacing: 0) {
            headerBlock
            if let busy = model.busyMessage { busyBanner(busy) }
            if model.trajectory == nil && model.detail == nil { identityOnboarding }
            Group {
                if let session = model.trajectory {
                    TrajectoryView(model: model, session: session)
                } else if let project = model.detail {
                    SessionDetail(model: model, project: project)
                } else {
                    if !model.selecting { ConnectionsCard(model: model) }
                    modeTabs
                    if model.mode == .ask {
                        AskView(model: model)
                    } else {
                        controls
                        projectScroll
                        if model.selecting {
                            batchBar
                        } else if !model.newProjects.isEmpty {
                            newProjectsBar
                        }
                    }
                }
            }
            .disabled(model.busyMessage != nil)
            .opacity(model.busyMessage != nil ? 0.45 : 1)
            footer
        }
        .frame(width: 480, height: 640)
        .background(background)
    }

    private func busyBanner(_ message: String) -> some View {
        HStack(spacing: 9) {
            ProgressView().controlSize(.small)
            Text(message).font(.system(size: 12, weight: .semibold)).lineLimit(1)
            Spacer()
            Text("working…").font(.system(size: 10)).foregroundStyle(.secondary)
        }
        .padding(.horizontal, 14).padding(.vertical, 9)
        .background(Palette.accent.opacity(0.14))
        .overlay(Rectangle().frame(height: 1).foregroundStyle(Palette.accent.opacity(0.3)), alignment: .bottom)
    }

    // MARK: identity onboarding
    /// First-run experience: mybot deduces who its owner is from the
    /// trajectories (Sherlock session), then asks for a one-click nod here.
    @ViewBuilder private var identityOnboarding: some View {
        if model.identityInvestigating && model.ownerName.isEmpty {
            HStack(spacing: 9) {
                ProgressView().controlSize(.small)
                VStack(alignment: .leading, spacing: 1) {
                    Text("Getting to know you").font(.system(size: 12, weight: .bold))
                    Text("mybot is deducing who its owner is from your session history…")
                        .font(.system(size: 11)).foregroundStyle(.secondary)
                }
                Spacer()
            }
            .padding(12)
            .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Color.primary.opacity(0.05)))
            .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(Palette.accent.opacity(0.35)))
            .padding(.horizontal, 12).padding(.bottom, 8)
        } else if !model.ownerName.isEmpty && !model.ownerConfirmed {
            IdentityConfirmCard(model: model)
                .padding(.horizontal, 12).padding(.bottom, 8)
        }
    }

    private var background: some View {
        // A quiet, constant brand wash. Status/mood belongs to the menu-bar
        // icon and status dot — tinting the whole canvas by mood turned the
        // popover brown whenever anything needed attention.
        ZStack {
            Color.primary.opacity(0.015)
            LinearGradient(
                colors: [Palette.accent.opacity(0.09), .clear],
                startPoint: .top, endPoint: .init(x: 0.5, y: 0.28)
            )
        }
        .ignoresSafeArea()
    }

    // MARK: header
    private var headerBlock: some View {
        let h = model.health
        return VStack(spacing: 12) {
            HStack(spacing: 11) {
                BotAvatar(mood: model.mood)
                    .frame(width: 36, height: 36)
                    .contextMenu {
                        Button("Re-discover owner (Sherlock session)") { model.rediscoverIdentity() }
                            .disabled(model.identityInvestigating)
                    }
                VStack(alignment: .leading, spacing: 1) {
                    Text("mybot memory").font(.system(size: 15, weight: .bold))
                    Text(statusLine).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(1)
                }
                Spacer()
                Button { model.reload() } label: {
                    Image(systemName: "arrow.clockwise").font(.system(size: 12, weight: .bold))
                }
                .buttonStyle(.plain)
                .frame(width: 30, height: 30)
                .background(Circle().fill(Color.primary.opacity(0.07)))
                .disabled(model.busyMessage != nil)
                .help("Refresh from disk")
            }
            HStack(spacing: 8) {
                StatChip(icon: "folder.fill", value: "\(h.projectCount)", label: "projects",
                         tint: Palette.accent)
                StatChip(icon: "sparkles", value: "\(pct(h.coverage))%", label: "embedded",
                         tint: h.coverage > 0.5 ? Palette.good : Palette.warn)
                StatChip(icon: "internaldrive.fill", value: formatBytes(h.indexBytes), label: "on disk",
                         tint: .secondary)
            }
        }
        .padding(.horizontal, 14).padding(.top, 14).padding(.bottom, 12)
    }

    private var statusLine: String {
        let serving = model.ownerConfirmed && !model.ownerName.isEmpty ? "Serving \(model.ownerName) · " : ""
        if model.indexBusy { return serving + "Index updating… showing last snapshot" }
        let h = model.health
        if h.totalChunks > 0 && h.embeddedChunks == 0 { return serving + "Semantic search off · lexical only" }
        return serving + "Index \(h.stale ? "stale" : "fresh") · updated \(h.indexedAgo)"
    }

    private func pct(_ value: Double) -> Int { Int((value * 100).rounded()) }

    // MARK: controls
    private var controls: some View {
        VStack(spacing: 8) {
            HStack(spacing: 8) {
                HStack(spacing: 6) {
                    Image(systemName: "magnifyingglass").font(.system(size: 12)).foregroundStyle(.secondary)
                    TextField("Filter by path…", text: $model.search)
                        .textFieldStyle(.plain).font(.system(size: 12))
                }
                .padding(.horizontal, 10).padding(.vertical, 7)
                .background(RoundedRectangle(cornerRadius: 9, style: .continuous).fill(Color.primary.opacity(0.06)))

                Menu {
                    ForEach(model.sources, id: \.self) { s in
                        Button(s == "all" ? "All sources" : s.capitalized) { model.sourceFilter = s }
                    }
                } label: {
                    chipLabel(model.sourceFilter == "all" ? "All" : model.sourceFilter.capitalized,
                              "line.3.horizontal.decrease")
                }
                .menuStyle(.borderlessButton).fixedSize()
            }
            HStack(spacing: 8) {
                Picker("", selection: $model.statusFilter) {
                    ForEach(StatusFilter.allCases) { Text($0.rawValue).tag($0) }
                }
                .pickerStyle(.segmented).labelsHidden()

                Menu {
                    ForEach(SortOrder.allCases) { o in Button(o.rawValue) { model.sortOrder = o } }
                } label: {
                    chipLabel(model.sortOrder.rawValue, "arrow.up.arrow.down")
                }
                .menuStyle(.borderlessButton).fixedSize()
            }
        }
        .padding(.horizontal, 14).padding(.bottom, 6)
    }

    // MARK: project list
    private var projectScroll: some View {
        let items = model.filtered
        return VStack(spacing: 0) {
            HStack {
                Text("\(items.count) PROJECT\(items.count == 1 ? "" : "S")")
                    .font(.system(size: 9.5, weight: .heavy)).tracking(0.6).foregroundStyle(.secondary)
                Spacer()
                Button(model.selecting ? "Done" : "Select") {
                    model.selecting ? model.endSelecting() : (model.selecting = true)
                }
                .buttonStyle(.plain)
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(Palette.accent)
            }
            .padding(.horizontal, 18).padding(.top, 8).padding(.bottom, 4)

            ScrollView {
                LazyVStack(spacing: 6) {
                    ForEach(items) { project in
                        ProjectRow(
                            project: project,
                            selecting: model.selecting,
                            isSelected: model.selection.contains(project.id),
                            onOpen: { model.openDetail(project) },
                            onToggleSelect: { model.toggleSelect(project) }
                        ) { include in
                            model.setInclusion(project, include: include)
                        }
                    }
                }
                .padding(.horizontal, 12).padding(.bottom, 10)
            }
        }
        .frame(maxHeight: .infinity)
    }

    // MARK: new projects
    private var newProjectsBar: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(spacing: 6) {
                Image(systemName: "sparkles").font(.system(size: 12)).foregroundStyle(Palette.accent)
                Text("\(model.newProjects.count) new project\(model.newProjects.count == 1 ? "" : "s") to review")
                    .font(.system(size: 12, weight: .bold))
                Spacer()
            }
            ForEach(model.newProjects.prefix(3)) { np in
                HStack(spacing: 8) {
                    // Tap the project to inspect its sessions before deciding.
                    Button { model.openNewProject(np) } label: {
                        HStack(spacing: 8) {
                            SourceTag(source: np.source)
                            Text(np.displayName).font(.system(size: 11.5, weight: .medium)).lineLimit(1)
                            Text("· \(np.sessions) sess").font(.system(size: 10)).foregroundStyle(.secondary)
                            Image(systemName: "chevron.right").font(.system(size: 9, weight: .semibold))
                                .foregroundStyle(.secondary.opacity(0.6))
                        }
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    Spacer()
                    Button("Keep") { model.reviewProject(np, decision: "keep") }
                        .buttonStyle(.plain).font(.system(size: 11, weight: .semibold)).foregroundStyle(Palette.accent)
                    Button("Exclude") { model.reviewProject(np, decision: "exclude") }
                        .buttonStyle(.plain).font(.system(size: 11, weight: .semibold)).foregroundStyle(.secondary)
                }
            }
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Color.primary.opacity(0.05)))
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(Palette.accent.opacity(0.35)))
        .padding(.horizontal, 12).padding(.vertical, 6)
    }

    // MARK: batch action bar
    private var batchBar: some View {
        HStack(spacing: 10) {
            Text("\(model.selection.count) selected")
                .font(.system(size: 12, weight: .semibold))
            Spacer()
            Button {
                model.batchSet(include: true)
            } label: {
                Text("Include").font(.system(size: 11, weight: .semibold))
                    .padding(.horizontal, 12).padding(.vertical, 6)
                    .background(Capsule().fill(Palette.accent)).foregroundStyle(.white)
            }
            .buttonStyle(.plain).disabled(model.selection.isEmpty).opacity(model.selection.isEmpty ? 0.5 : 1)
            Button {
                model.batchSet(include: false)
            } label: {
                Text("Exclude").font(.system(size: 11, weight: .semibold))
                    .padding(.horizontal, 12).padding(.vertical, 6)
                    .background(Capsule().fill(Color.primary.opacity(0.07))).foregroundStyle(Palette.bad)
            }
            .buttonStyle(.plain).disabled(model.selection.isEmpty).opacity(model.selection.isEmpty ? 0.5 : 1)
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Palette.accent.opacity(0.10)))
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(Palette.accent.opacity(0.3)))
        .padding(.horizontal, 12).padding(.vertical, 6)
    }

    // MARK: model switcher
    /// Presets grouped by model (e.g. "Claude Opus" → low/medium/high/xhigh/max),
    /// so the effort tiers nest under each model instead of a flat list of 19.
    private var groupedPresets: [(String, [ModelPreset])] {
        var order: [String] = []
        var groups: [String: [ModelPreset]] = [:]
        for p in model.modelPresets {
            let key = "\(p.family == "claude" ? "Claude" : "Codex") \(p.model.capitalized)"
            if groups[key] == nil { order.append(key) }
            groups[key, default: []].append(p)
        }
        return order.map { ($0, groups[$0] ?? []) }
    }

    private var modelMenu: some View {
        Menu {
            ForEach(groupedPresets, id: \.0) { group, presets in
                Menu(group) {
                    ForEach(presets) { preset in
                        Button {
                            model.setModel(preset)
                        } label: {
                            if preset.id == model.currentModelId {
                                Label(preset.thinking, systemImage: "checkmark")
                            } else {
                                Text(preset.thinking)
                            }
                        }
                    }
                }
            }
        } label: {
            HStack(spacing: 5) {
                Image(systemName: "brain").font(.system(size: 11, weight: .semibold))
                Text(model.currentModel.shortLabel)
                    .font(.system(size: 11, weight: .semibold)).lineLimit(1)
            }
            .foregroundStyle(Palette.source(model.currentModel.family))
        }
        .menuStyle(.borderlessButton).fixedSize()
        .disabled(model.busyMessage != nil)
        .help("Agent model: \(model.currentModel.label)")
    }

    // MARK: mode tabs
    private var modeTabs: some View {
        Picker("", selection: $model.mode) {
            Text("Projects").tag(AppModel.Mode.projects)
            Text("Ask mybot").tag(AppModel.Mode.ask)
        }
        .pickerStyle(.segmented)
        .labelsHidden()
        .padding(.horizontal, 12)
        .padding(.bottom, 8)
    }

    // MARK: footer
    private var footer: some View {
        HStack(spacing: 8) {
            Menu {
                Button("Refresh changed") { model.runMaintenance("refresh", label: "Refreshing index") }
                Button("Backfill embeddings") { model.runMaintenance("embed", label: "Embedding") }
                Button("Rebuild index") { model.runMaintenance("rebuild", label: "Rebuilding") }
                Divider()
                Button("Shrink embeddings") { model.runMaintenance("compact_embeddings", label: "Shrinking embeddings") }
                Button("Reclaim space") { model.runMaintenance("compact", label: "Reclaiming space") }
                Divider()
                if model.clusters.isEmpty {
                    if model.health.automatedSessions > 0 {
                        Button("Exclude \(model.health.automatedSessions) automated sessions") { model.excludeAutomated() }
                    } else {
                        Button("Re-include automated sessions (rebuild)") { model.includeAutomated() }
                    }
                } else {
                    Menu("AI-driven sessions") {
                        ForEach(model.clusters) { cluster in
                            if cluster.excluded {
                                Button("Re-include \(cluster.displayName)") {
                                    model.setCluster(cluster, exclude: false)
                                }
                            } else {
                                Button("Exclude \(cluster.displayName) (\(cluster.count))") {
                                    model.setCluster(cluster, exclude: true)
                                }
                            }
                        }
                        Divider()
                        if model.clusters.contains(where: { !$0.excluded }) {
                            Button("Exclude all automated") { model.excludeAutomated() }
                        }
                        if model.clusters.contains(where: { $0.excluded }) {
                            Button("Re-include all automated") { model.includeAutomated() }
                        }
                    }
                }
                Divider()
                Toggle("Start at login", isOn: Binding(
                    get: { model.launchAtLogin },
                    set: { model.setLaunchAtLogin($0) }
                ))
            } label: {
                chipLabel("Maintain", "wrench.and.screwdriver.fill")
            }
            .menuStyle(.borderlessButton).fixedSize()
            .disabled(model.busyMessage != nil)

            modelMenu

            Spacer()

            versionChip

            if let err = model.lastError {
                Image(systemName: "exclamationmark.triangle.fill")
                    .font(.system(size: 12)).foregroundStyle(Palette.bad).help(err)
            }
            Button { NSApplication.shared.terminate(nil) } label: { chipLabel("Quit", "power") }
                .buttonStyle(.plain)
        }
        .padding(.horizontal, 12).padding(.vertical, 10)
        .background(Color.primary.opacity(0.035))
    }

    /// Build commit, with a warning tint when the repo HEAD has moved past it —
    /// the installed app is stale; scripts/deploy.sh rebuilds and reinstalls.
    /// Drift state comes from reload() — no file I/O during body evaluation.
    private var versionChip: some View {
        let behind = model.appBehindSource
        return Text(MybotConfig.buildSHA)
            .font(.system(size: 10, design: .monospaced))
            .foregroundStyle(behind ? Palette.bad : Color.secondary.opacity(0.7))
            .help(behind
                ? "App built from \(MybotConfig.buildSHA) but the repo is at \(model.sourceHead ?? "?") — run scripts/deploy.sh to update"
                : "Built \(MybotConfig.buildDate) from commit \(MybotConfig.buildSHA)")
    }
}

// MARK: - Row

struct ProjectRow: View {
    let project: Project
    var selecting: Bool = false
    var isSelected: Bool = false
    var onOpen: (() -> Void)? = nil
    var onToggleSelect: (() -> Void)? = nil
    let toggle: (Bool) -> Void

    var body: some View {
        HStack(spacing: 8) {
            if selecting {
                Image(systemName: isSelected ? "checkmark.circle.fill" : "circle")
                    .font(.system(size: 16)).foregroundStyle(isSelected ? Palette.accent : .secondary)
            }
            Button { selecting ? onToggleSelect?() : onOpen?() } label: {
                HStack(spacing: 10) {
                    VStack(alignment: .leading, spacing: 3) {
                        HStack(spacing: 6) {
                            SourceTag(source: project.source)
                            Text(project.displayName).font(.system(size: 13, weight: .semibold)).lineLimit(1)
                        }
                        Text(project.cwd).font(.system(size: 10)).foregroundStyle(.secondary)
                            .lineLimit(1).truncationMode(.middle)
                        HStack(spacing: 10) {
                            metric("\(project.sessions)", "sessions")
                            metric("\(project.chunks)", "chunks")
                        }
                    }
                    Spacer(minLength: 6)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            if !selecting {
                toggleButton
                Image(systemName: "chevron.right")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(.secondary.opacity(0.6))
            }
        }
        .padding(10)
        .background(
            RoundedRectangle(cornerRadius: 11, style: .continuous)
                .fill(Color.primary.opacity(project.included ? 0.05 : 0.02))
        )
        .overlay(alignment: .leading) {
            RoundedRectangle(cornerRadius: 2)
                .fill(project.included ? Palette.accent.opacity(0.75) : Color.clear)
                .frame(width: 3).padding(.vertical, 9)
        }
        .opacity(project.included ? 1 : 0.6)
    }

    private func metric(_ value: String, _ label: String) -> some View {
        HStack(spacing: 3) {
            Text(value).font(.system(size: 10, weight: .bold))
            Text(label).font(.system(size: 10)).foregroundStyle(.secondary)
        }
    }

    private var toggleButton: some View {
        // The action, not an alarm: removing is a quiet neutral pill; adding
        // back is the accent — a whole list of red "Exclude" slabs shouted.
        Button { toggle(!project.included) } label: {
            Text(project.included ? "Exclude" : "Include")
                .font(.system(size: 11, weight: .semibold))
                .padding(.horizontal, 11).padding(.vertical, 6)
                .background(Capsule().fill(project.included ? Color.primary.opacity(0.06) : Palette.accent.opacity(0.16)))
                .foregroundStyle(project.included ? Color.secondary : Palette.accent)
        }
        .buttonStyle(.plain)
    }
}

// MARK: - Session detail

struct SessionDetail: View {
    @ObservedObject var model: AppModel
    let project: Project
    @State private var showAutomated = false

    private var humanSessions: [Session] { model.sessions.filter { !$0.isAutomated } }
    private var automatedSessions: [Session] { model.sessions.filter { $0.isAutomated } }
    private var visibleSessions: [Session] { showAutomated ? model.sessions : humanSessions }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 6) {
                Button { model.closeDetail() } label: {
                    HStack(spacing: 3) {
                        Image(systemName: "chevron.left").font(.system(size: 11, weight: .bold))
                        Text("Projects").font(.system(size: 12, weight: .semibold))
                    }
                    .padding(.horizontal, 9).padding(.vertical, 5)
                    .background(Capsule().fill(Color.primary.opacity(0.06)))
                }
                .buttonStyle(.plain)
                Spacer()
            }
            .padding(.horizontal, 14).padding(.top, 4).padding(.bottom, 8)

            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 6) {
                    SourceTag(source: project.source)
                    Text(project.displayName).font(.system(size: 14, weight: .bold)).lineLimit(1)
                    Spacer()
                    if project.included {
                        Label("included", systemImage: "checkmark.circle.fill")
                            .font(.system(size: 10, weight: .medium)).foregroundStyle(Palette.good)
                    } else {
                        Label("excluded", systemImage: "minus.circle.fill")
                            .font(.system(size: 10, weight: .medium)).foregroundStyle(.secondary)
                    }
                }
                Text(project.cwd).font(.system(size: 10)).foregroundStyle(.secondary)
                    .lineLimit(1).truncationMode(.middle)
                Text(sessionCountLine)
                    .font(.system(size: 10, weight: .semibold)).foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 16).padding(.bottom, 8)

            if !automatedSessions.isEmpty { automatedToggle }

            Divider().opacity(0.4)

            if model.loadingSessions {
                Spacer(); ProgressView().controlSize(.small); Spacer()
            } else {
                ScrollView {
                    LazyVStack(spacing: 6) {
                        ForEach(visibleSessions) { session in
                            SessionRow(session: session,
                                       onOpen: { model.openTrajectory(session) },
                                       exclude: { model.excludeSession(session) })
                        }
                        if visibleSessions.isEmpty {
                            Text(showAutomated || automatedSessions.isEmpty
                                 ? "No sessions indexed yet — the index may still be catching up"
                                 : "Only AI-driven sessions here — toggle above to see them")
                                .font(.system(size: 11)).foregroundStyle(.secondary)
                                .frame(maxWidth: .infinity).padding(.top, 30)
                        }
                    }
                    .padding(.horizontal, 12).padding(.vertical, 8)
                }
            }
        }
        .frame(maxHeight: .infinity)
    }

    private var sessionCountLine: String {
        let human = humanSessions.count
        var line = "\(human) session\(human == 1 ? "" : "s")"
        if !automatedSessions.isEmpty { line += " · \(automatedSessions.count) AI-driven" }
        return line
    }

    /// Prominent, always-visible switch for the hidden AI-driven sessions.
    private var automatedToggle: some View {
        Button { withAnimation(.easeOut(duration: 0.15)) { showAutomated.toggle() } } label: {
            HStack(spacing: 8) {
                Image(systemName: "sparkles")
                    .font(.system(size: 11, weight: .semibold))
                Text(showAutomated
                     ? "Showing \(automatedSessions.count) AI-driven session\(automatedSessions.count == 1 ? "" : "s")"
                     : "\(automatedSessions.count) AI-driven session\(automatedSessions.count == 1 ? "" : "s") hidden")
                    .font(.system(size: 11.5, weight: .semibold))
                Spacer()
                Text(showAutomated ? "HIDE" : "SHOW")
                    .font(.system(size: 10, weight: .heavy)).tracking(0.5)
                    .padding(.horizontal, 9).padding(.vertical, 3.5)
                    .background(Capsule().fill(Color.primary.opacity(0.08)))
            }
            .foregroundStyle(.secondary)
            .padding(.horizontal, 11).padding(.vertical, 7)
            .background(RoundedRectangle(cornerRadius: 9, style: .continuous)
                .fill(Color.primary.opacity(showAutomated ? 0.055 : 0.035)))
            .overlay(RoundedRectangle(cornerRadius: 9, style: .continuous)
                .strokeBorder(Color.primary.opacity(0.09)))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .padding(.horizontal, 12).padding(.bottom, 8)
    }
}

struct SessionRow: View {
    let session: Session
    var onOpen: (() -> Void)? = nil
    let exclude: () -> Void

    var body: some View {
        HStack(spacing: 10) {
            Button { onOpen?() } label: {
                HStack(spacing: 10) {
                    Image(systemName: "bubble.left.and.text.bubble.right")
                        .font(.system(size: 12)).foregroundStyle(.secondary)
                    VStack(alignment: .leading, spacing: 3) {
                        HStack(spacing: 6) {
                            Text(session.displayTitle).font(.system(size: 12.5, weight: .semibold)).lineLimit(1)
                            if session.isAutomated {
                                Text("AUTO").font(.system(size: 8, weight: .heavy)).tracking(0.3)
                                    .padding(.horizontal, 4).padding(.vertical, 1.5)
                                    .background(Capsule().fill(Color.primary.opacity(0.08)))
                                    .foregroundStyle(.secondary)
                            }
                        }
                        HStack(spacing: 8) {
                            Text(session.shortId).font(.system(size: 10, design: .monospaced)).foregroundStyle(.secondary)
                            Text("·").foregroundStyle(.secondary)
                            Text("\(session.chunks) chunks").font(.system(size: 10)).foregroundStyle(.secondary)
                            if !session.updatedAgo.isEmpty {
                                Text("·").foregroundStyle(.secondary)
                                Text(session.updatedAgo).font(.system(size: 10)).foregroundStyle(.secondary)
                            }
                        }
                    }
                    Spacer(minLength: 6)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            Button(action: exclude) {
                Text("Exclude")
                    .font(.system(size: 11, weight: .semibold))
                    .padding(.horizontal, 10).padding(.vertical, 5)
                    .background(Capsule().fill(Color.primary.opacity(0.06)))
                    .foregroundStyle(.secondary)
            }
            .buttonStyle(.plain)
            Image(systemName: "chevron.right")
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(.secondary.opacity(0.6))
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 11, style: .continuous).fill(Color.primary.opacity(0.05)))
    }
}

// MARK: - Trajectory viewer

struct TrajectoryView: View {
    @ObservedObject var model: AppModel
    let session: Session
    @State private var search = ""
    @State private var conversationOnly = false
    @State private var copied = false

    /// Flat events after the conversation-only filter and the in-session search.
    private var filteredEvents: [TrajectoryEvent] {
        var events = model.trajectoryEvents
        if conversationOnly {
            events = events.filter { $0.kind == "user" || $0.kind == "assistant" }
        }
        let query = search.trimmingCharacters(in: .whitespaces).lowercased()
        if !query.isEmpty {
            events = events.filter { $0.text.lowercased().contains(query) || $0.tool.lowercased().contains(query) }
        }
        return events
    }

    /// Render units: each tool_use is paired with its tool_result. Claude emits
    /// a whole batch of calls in one turn and all the results in the next
    /// (use,use,result,result), so we collect the run of calls and the following
    /// run of results and match them positionally — the k-th call to the k-th
    /// result — rather than assuming call/result strictly alternate.
    private var items: [TrajectoryItem] {
        let events = filteredEvents
        var out: [TrajectoryItem] = []
        var index = 0
        while index < events.count {
            guard events[index].kind == "tool_use" else {
                out.append(TrajectoryItem(event: events[index], result: nil))
                index += 1
                continue
            }
            var uses: [TrajectoryEvent] = []
            while index < events.count, events[index].kind == "tool_use" {
                uses.append(events[index]); index += 1
            }
            var results: [TrajectoryEvent] = []
            while index < events.count, events[index].kind == "tool_result" {
                results.append(events[index]); index += 1
            }
            for (offset, use) in uses.enumerated() {
                out.append(TrajectoryItem(event: use, result: offset < results.count ? results[offset] : nil))
            }
            if results.count > uses.count {
                for extra in results[uses.count...] {
                    out.append(TrajectoryItem(event: extra, result: nil))
                }
            }
        }
        return out
    }

    /// A contiguous run of tool calls collapses into one summary block
    /// ("Glob ×3 · Bash ×5"); conversation turns and thinking stay as-is.
    private var blocks: [TrajectoryBlock] {
        var out: [TrajectoryBlock] = []
        var run: [TrajectoryItem] = []
        func flush() { if !run.isEmpty { out.append(.tools(run)); run = [] } }
        for item in items {
            if item.event.kind == "tool_use" {
                run.append(item)
            } else {
                flush()
                out.append(.event(item))
            }
        }
        flush()
        return out
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 6) {
                Button { model.closeTrajectory() } label: {
                    HStack(spacing: 3) {
                        Image(systemName: "chevron.left").font(.system(size: 11, weight: .bold))
                        Text("Sessions").font(.system(size: 12, weight: .semibold))
                    }
                    .padding(.horizontal, 9).padding(.vertical, 5)
                    .background(Capsule().fill(Color.primary.opacity(0.06)))
                }
                .buttonStyle(.plain)
                Spacer()
                SourceTag(source: sourceName)
                Button { copyTranscript() } label: {
                    Image(systemName: copied ? "checkmark" : "doc.on.doc")
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(copied ? Palette.good : .secondary)
                }
                .buttonStyle(.plain).help("Copy transcript")
            }
            .padding(.horizontal, 14).padding(.top, 4).padding(.bottom, 6)

            VStack(alignment: .leading, spacing: 2) {
                Text(session.displayTitle).font(.system(size: 13, weight: .bold)).lineLimit(2)
                Text(metaLine).font(.system(size: 10)).foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 16).padding(.bottom, 6)

            filterBar

            Divider().opacity(0.4)

            if model.loadingTrajectory {
                Spacer(); ProgressView().controlSize(.small); Spacer()
            } else {
                ScrollView {
                    LazyVStack(spacing: 6) {
                        ForEach(blocks) { block in
                            switch block {
                            case .event(let item):
                                EventRow(event: item.event, result: item.result)
                            case .tools(let group):
                                if group.count == 1 {
                                    EventRow(event: group[0].event, result: group[0].result)
                                } else {
                                    ToolGroupView(items: group)
                                }
                            }
                        }
                        if blocks.isEmpty {
                            Text(search.isEmpty ? "No conversation turns here — turn off the filter to see tool activity."
                                                : "No events match \"\(search)\".")
                                .font(.system(size: 11)).foregroundStyle(.secondary)
                                .frame(maxWidth: .infinity).padding(.top, 30)
                        }
                        if search.isEmpty && !conversationOnly && (model.trajectoryTruncated || model.trajectoryOmitted > 0) {
                            VStack(spacing: 2) {
                                if model.trajectoryTruncated {
                                    Text("Showing the first \(model.trajectoryEvents.count) events (truncated)")
                                }
                                if model.trajectoryOmitted > 0 {
                                    Text("\(model.trajectoryOmitted) oversized event\(model.trajectoryOmitted == 1 ? "" : "s") skipped to save memory")
                                }
                            }
                            .font(.system(size: 10)).foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity).padding(.top, 4)
                        }
                    }
                    .padding(.horizontal, 12).padding(.vertical, 8)
                }
            }
        }
        .frame(maxHeight: .infinity)
    }

    private var filterBar: some View {
        HStack(spacing: 8) {
            Button { withAnimation(.easeOut(duration: 0.12)) { conversationOnly.toggle() } } label: {
                HStack(spacing: 4) {
                    Image(systemName: conversationOnly ? "text.bubble.fill" : "text.bubble")
                        .font(.system(size: 10))
                    Text("Conversation").font(.system(size: 10.5, weight: .semibold))
                }
                .padding(.horizontal, 9).padding(.vertical, 5)
                .background(Capsule().fill(conversationOnly ? Palette.accent.opacity(0.16) : Color.primary.opacity(0.06)))
                .foregroundStyle(conversationOnly ? Palette.accent : Color.secondary)
            }
            .buttonStyle(.plain)
            HStack(spacing: 5) {
                Image(systemName: "magnifyingglass").font(.system(size: 10)).foregroundStyle(.secondary)
                TextField("Search this session", text: $search).textFieldStyle(.plain).font(.system(size: 11))
                if !search.isEmpty {
                    Button { search = "" } label: { Image(systemName: "xmark.circle.fill").font(.system(size: 11)) }
                        .buttonStyle(.plain).foregroundStyle(.secondary)
                }
            }
            .padding(.horizontal, 8).padding(.vertical, 5)
            .background(Capsule().fill(Color.primary.opacity(0.06)))
        }
        .padding(.horizontal, 14).padding(.bottom, 6)
    }

    private var sourceName: String {
        session.ref.split(separator: ":").first.map(String.init) ?? ""
    }

    private var metaLine: String {
        var line = "\(session.shortId) · \(model.trajectoryEvents.count) events"
        if conversationOnly || !search.isEmpty { line += " · \(filteredEvents.count) shown" }
        if !session.updatedAgo.isEmpty { line += " · \(session.updatedAgo)" }
        return line
    }

    private func copyTranscript() {
        let text = filteredEvents.map { event -> String in
            let label = event.tool.isEmpty ? event.kind.uppercased() : "\(event.kind.uppercased()) [\(event.tool)]"
            return "\(label)\n\(event.text)"
        }.joined(separator: "\n\n")
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        withAnimation { copied = true }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { withAnimation { copied = false } }
    }
}

/// A render unit for the trajectory: a single event, plus the paired
/// tool_result when the event is a tool_use.
struct TrajectoryItem: Identifiable {
    var id: UUID { event.id }
    let event: TrajectoryEvent
    let result: TrajectoryEvent?
}

/// Top-level render block: either a conversation/thinking event, or a
/// contiguous run of tool calls shown as one collapsed summary.
enum TrajectoryBlock: Identifiable {
    case event(TrajectoryItem)
    case tools([TrajectoryItem])
    var id: UUID {
        switch self {
        case .event(let item): return item.id
        case .tools(let items): return items.first?.id ?? UUID()
        }
    }
}

/// Extract a short, clean preview from tool args/output — the value of a common
/// arg key (command, pattern, path…) or the first substantive line — never the
/// raw JSON braces.
func toolPreview(_ text: String) -> String? {
    let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
    if trimmed.hasPrefix("{"), let data = trimmed.data(using: .utf8),
       let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
        for key in ["command", "cmd", "pattern", "query", "path", "file_path", "url", "prompt", "description"] {
            if let value = obj[key] as? String, !value.isEmpty { return value }
        }
        for (_, value) in obj { if let str = value as? String, !str.isEmpty { return str } }
        return nil
    }
    for raw in text.split(whereSeparator: \.isNewline) {
        let line = raw.trimmingCharacters(in: .whitespaces)
        if !line.isEmpty && !line.allSatisfy({ "{}[](),".contains($0) }) { return line }
    }
    return nil
}

/// A run of tool calls as one collapsed row ("Glob ×3 · Bash ×5"); expands to
/// the individual calls, each still individually openable.
struct ToolGroupView: View {
    let items: [TrajectoryItem]
    @State private var expanded = false

    private var summary: String {
        var order: [String] = []
        var counts: [String: Int] = [:]
        for item in items {
            let name = item.event.tool.isEmpty ? "Tool" : item.event.tool
            if counts[name] == nil { order.append(name) }
            counts[name, default: 0] += 1
        }
        return order.map { counts[$0]! > 1 ? "\($0) ×\(counts[$0]!)" : $0 }.joined(separator: " · ")
    }

    var body: some View {
        let tint = Palette.accent
        return VStack(alignment: .leading, spacing: 0) {
            Button { expanded.toggle() } label: {
                HStack(spacing: 5) {
                    Image(systemName: "wrench.and.screwdriver.fill").font(.system(size: 9)).foregroundStyle(tint)
                    Text(summary).font(.system(size: 10.5, weight: .semibold)).foregroundStyle(tint)
                        .lineLimit(1)
                    Spacer(minLength: 4)
                    Text("\(items.count) calls").font(.system(size: 9)).foregroundStyle(.secondary.opacity(0.7))
                    Image(systemName: expanded ? "chevron.down" : "chevron.right")
                        .font(.system(size: 8, weight: .semibold)).foregroundStyle(.secondary.opacity(0.6))
                }
                .contentShape(Rectangle())
                .padding(.vertical, 3).padding(.horizontal, 8)
            }
            .buttonStyle(.plain)
            if expanded {
                VStack(spacing: 2) {
                    ForEach(items) { EventRow(event: $0.event, result: $0.result) }
                }
                .padding(.leading, 8).padding(.bottom, 4)
            }
        }
        .background(RoundedRectangle(cornerRadius: 8, style: .continuous)
            .fill(expanded ? tint.opacity(0.05) : Color.clear))
        .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous)
            .strokeBorder(tint.opacity(expanded ? 0.14 : 0)))
    }
}

struct EventRow: View {
    let event: TrajectoryEvent
    var result: TrajectoryEvent? = nil
    @State private var expanded = false

    var body: some View {
        switch event.kind {
        case "user": bubble(role: "You", tint: Palette.accent, align: .trailing)
        case "assistant": bubble(role: "Assistant", tint: .secondary, align: .leading)
        case "thinking": foldable(icon: "brain", title: "Thinking", tint: .secondary, mono: false, italic: true)
        case "tool_use": toolRow()
        case "tool_result": foldable(icon: "arrow.turn.down.right",
                                     title: "Result · \(event.text.count) chars", tint: .secondary, mono: true)
        default: EmptyView()
        }
    }

    /// A tool call and its result as one compact entry: name + args preview on a
    /// slim row (with a "↳ N" result-size hint), expanding to args then result.
    private func toolRow() -> some View {
        let tint = Palette.accent
        return VStack(alignment: .leading, spacing: 0) {
            Button { expanded.toggle() } label: {
                HStack(spacing: 5) {
                    Image(systemName: "wrench.and.screwdriver.fill").font(.system(size: 9)).foregroundStyle(tint)
                    Text(event.tool.isEmpty ? "Tool call" : event.tool)
                        .font(.system(size: 10.5, weight: .semibold)).foregroundStyle(tint).lineLimit(1).fixedSize()
                    if !expanded, let preview = inlinePreview {
                        Text(preview).font(.system(size: 10, design: .monospaced))
                            .foregroundStyle(.secondary).lineLimit(1).truncationMode(.tail)
                    }
                    Spacer(minLength: 4)
                    if let result, !expanded {
                        Text("↳ \(result.text.count)").font(.system(size: 9)).foregroundStyle(.secondary.opacity(0.7))
                    }
                    Image(systemName: expanded ? "chevron.down" : "chevron.right")
                        .font(.system(size: 8, weight: .semibold)).foregroundStyle(.secondary.opacity(0.6))
                }
                .contentShape(Rectangle())
                .padding(.vertical, 3).padding(.horizontal, 8)
            }
            .buttonStyle(.plain)
            if expanded {
                Text(event.text).font(.system(size: 10.5, design: .monospaced)).textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 8).padding(.bottom, result == nil ? 7 : 5)
                if let result {
                    HStack(spacing: 4) {
                        Image(systemName: "arrow.turn.down.right").font(.system(size: 9))
                        Text("Result · \(result.text.count) chars").font(.system(size: 9.5, weight: .semibold))
                    }
                    .foregroundStyle(.secondary).padding(.horizontal, 8).padding(.bottom, 3)
                    Text(result.text).font(.system(size: 10.5, design: .monospaced)).textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.horizontal, 8).padding(.bottom, 7)
                }
            }
        }
        .background(RoundedRectangle(cornerRadius: 8, style: .continuous)
            .fill(expanded ? tint.opacity(0.06) : Color.clear))
        .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous)
            .strokeBorder(tint.opacity(expanded ? 0.16 : 0)))
    }

    private func bubble(role: String, tint: Color, align: HorizontalAlignment) -> some View {
        VStack(alignment: align, spacing: 3) {
            Text(role.uppercased()).font(.system(size: 8.5, weight: .heavy)).tracking(0.5)
                .foregroundStyle(tint)
            // Render inline markdown (bold/italic/code/links) so conversation
            // turns read like the chat view rather than raw asterisks.
            Text(.init(event.text))
                .font(.system(size: 12))
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(9)
                .background(RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .fill(align == .trailing ? Palette.accent.opacity(0.12) : Color.primary.opacity(0.05)))
        }
        .frame(maxWidth: .infinity, alignment: align == .trailing ? .trailing : .leading)
    }

    /// Clean inline preview for the compact row — no raw JSON braces.
    private var inlinePreview: String? { toolPreview(event.text) }

    /// Tool calls, results, and thinking are secondary to the conversation, so
    /// they render as slim single-line rows (icon + name + a dimmed preview of
    /// the args/output) instead of full-width cards. Tap to expand the detail;
    /// the card treatment is reserved for the expanded state.
    private func foldable(icon: String, title: String, tint: Color, mono: Bool, italic: Bool = false) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Button { expanded.toggle() } label: {
                HStack(spacing: 5) {
                    Image(systemName: icon).font(.system(size: 9)).foregroundStyle(tint)
                    Text(title).font(.system(size: 10.5, weight: .semibold)).foregroundStyle(tint)
                        .lineLimit(1).fixedSize()
                    if !expanded, let preview = inlinePreview {
                        Text(preview)
                            .font(.system(size: 10, design: mono ? .monospaced : .default))
                            .foregroundStyle(.secondary)
                            .lineLimit(1).truncationMode(.tail)
                    }
                    Spacer(minLength: 4)
                    Image(systemName: expanded ? "chevron.down" : "chevron.right")
                        .font(.system(size: 8, weight: .semibold)).foregroundStyle(.secondary.opacity(0.6))
                }
                .contentShape(Rectangle())
                .padding(.vertical, 3).padding(.horizontal, 8)
            }
            .buttonStyle(.plain)
            if expanded {
                Text(event.text)
                    .font(mono ? .system(size: 10.5, design: .monospaced) : .system(size: 11))
                    .italic(italic)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 8).padding(.bottom, 7)
            }
        }
        .background(RoundedRectangle(cornerRadius: 8, style: .continuous)
            .fill(expanded ? tint.opacity(0.06) : Color.clear))
        .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous)
            .strokeBorder(tint.opacity(expanded ? 0.16 : 0)))
    }
}

// MARK: - Ask (search + chat)

struct AskView: View {
    @ObservedObject var model: AppModel
    @State private var input = ""
    @FocusState private var focused: Bool

    var body: some View {
        VStack(spacing: 0) {
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 10) {
                        if model.chat.isEmpty && model.searchHits.isEmpty && !model.chatPending {
                            emptyHint
                        }
                        ForEach(model.chat) { msg in
                            ChatBubble(msg: msg) { source in
                                model.openTrajectory(Session(
                                    ref: source.ref,
                                    sessionId: String(source.ref.split(separator: ":").last ?? ""),
                                    title: source.title,
                                    updatedAt: "",
                                    chunks: 0))
                            }
                        }
                        if model.chatPending && !model.chatActivity.isEmpty {
                            HStack(spacing: 7) {
                                ProgressView().controlSize(.small)
                                Text(model.chatActivity).font(.system(size: 11)).foregroundStyle(.secondary)
                                    .lineLimit(1)
                            }
                            .id("pending")
                        }
                        if !model.searchHits.isEmpty {
                            Text("MEMORY MATCHES")
                                .font(.system(size: 10, weight: .heavy)).tracking(0.6)
                                .foregroundStyle(.secondary)
                                .padding(.top, model.chat.isEmpty ? 0 : 6)
                            ForEach(model.searchHits) { hit in
                                SearchHitRow(hit: hit) {
                                    model.openTrajectory(Session(
                                        ref: hit.ref,
                                        sessionId: String(hit.ref.split(separator: ":").last ?? ""),
                                        title: hit.title,
                                        updatedAt: hit.updatedAt,
                                        chunks: 0))
                                }
                            }
                        }
                    }
                    .padding(.horizontal, 12).padding(.bottom, 10)
                }
                .onChange(of: model.chat.count) {
                    if let last = model.chat.last { withAnimation { proxy.scrollTo(last.id, anchor: .bottom) } }
                }
                .onChange(of: model.chatPending) {
                    if model.chatPending { withAnimation { proxy.scrollTo("pending", anchor: .bottom) } }
                }
            }
            Divider().opacity(0.3)
            inputBar   // at the bottom, per chatbot convention
        }
        .onAppear { focused = true }
    }

    private var inputBar: some View {
        HStack(spacing: 8) {
            Image(systemName: "sparkle.magnifyingglass")
                .font(.system(size: 12)).foregroundStyle(.secondary)
            TextField("Search memory — press ⏎ to ask mybot", text: $input)
                .textFieldStyle(.plain)
                .font(.system(size: 12.5))
                .focused($focused)
                .onChange(of: input) { model.searchMemory(input) }
                .onSubmit {
                    model.ask(input)
                    input = ""
                }
            if !model.chat.isEmpty {
                Button {
                    model.newChat()
                } label: {
                    Image(systemName: "square.and.pencil").font(.system(size: 11, weight: .semibold))
                }
                .buttonStyle(.plain).foregroundStyle(.secondary)
                .help("New chat")
            }
        }
        .padding(.horizontal, 10).padding(.vertical, 8)
        .background(RoundedRectangle(cornerRadius: 9, style: .continuous).fill(Color.primary.opacity(0.06)))
        .padding(.horizontal, 12).padding(.vertical, 10)
    }

    private var emptyHint: some View {
        VStack(spacing: 6) {
            Image(systemName: "sparkle.magnifyingglass")
                .font(.system(size: 22)).foregroundStyle(.secondary.opacity(0.6))
            Text("Type to search your trajectory memory.\nPress ⏎ to ask mybot a question.")
                .font(.system(size: 11.5)).foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
        }
        .frame(maxWidth: .infinity).padding(.top, 60)
    }
}

struct ChatBubble: View {
    let msg: ChatMsg
    var onOpenSource: ((ChatSource) -> Void)? = nil
    @State private var showInternals: Bool

    init(msg: ChatMsg, startExpanded: Bool = false, onOpenSource: ((ChatSource) -> Void)? = nil) {
        self.msg = msg
        self.onOpenSource = onOpenSource
        _showInternals = State(initialValue: startExpanded)
    }

    var body: some View {
        switch msg.role {
        case "user":
            // The one saturated element in the chat: the bunny icon's own
            // gradient, so "you talking to mybot" carries the brand.
            Text(msg.text)
                .font(.system(size: 12)).textSelection(.enabled)
                .foregroundStyle(.white)
                .padding(.horizontal, 11).padding(.vertical, 8)
                .background(RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .fill(Palette.brandGradient))
                .frame(maxWidth: .infinity, alignment: .trailing)
                .id(msg.id)
        case "error":
            HStack(spacing: 6) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .font(.system(size: 10)).foregroundStyle(Palette.warn)
                Text(msg.text).font(.system(size: 11.5)).foregroundStyle(.secondary)
            }
            .padding(9)
            .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color.primary.opacity(0.045)))
            .frame(maxWidth: .infinity, alignment: .leading)
            .id(msg.id)
        default:
            VStack(alignment: .leading, spacing: 0) {
                HStack(alignment: .top, spacing: 8) {
                    BotAvatar(mood: .happy).frame(width: 20, height: 20)
                    Text(.init(msg.text.isEmpty && msg.streaming ? "…" : msg.text)
                         + (msg.streaming && !msg.text.isEmpty ? .init(" ▍") : ""))
                        .font(.system(size: 12)).textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                if !msg.sources.isEmpty || !msg.toolCalls.isEmpty {
                    internals.padding(.top, 8)
                }
            }
            .padding(9)
            .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color.primary.opacity(0.05)))
            .id(msg.id)
        }
    }

    /// What mybot did behind the scenes: retrieval steps + grounding sources.
    private var internals: some View {
        VStack(alignment: .leading, spacing: 6) {
            Button { showInternals.toggle() } label: {
                HStack(spacing: 5) {
                    Image(systemName: "gearshape.2.fill").font(.system(size: 9))
                    Text(internalsSummary).font(.system(size: 10, weight: .semibold))
                    Image(systemName: showInternals ? "chevron.down" : "chevron.right")
                        .font(.system(size: 8, weight: .semibold))
                }
                .foregroundStyle(.secondary)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)

            if showInternals {
                ForEach(msg.toolCalls) { call in
                    HStack(spacing: 5) {
                        Image(systemName: "magnifyingglass").font(.system(size: 8.5))
                        Text(call.query.isEmpty ? call.tool : "\(call.tool) · “\(call.query)”")
                            .lineLimit(1)
                        Spacer(minLength: 4)
                        Text("\(call.results) hit\(call.results == 1 ? "" : "s") · \(String(format: "%.2fs", call.seconds))")
                    }
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.secondary)
                }
                ForEach(msg.sources) { source in
                    Button {
                        onOpenSource?(source)
                    } label: {
                        HStack(spacing: 6) {
                            SourceTag(source: source.sourceName)
                            Text(source.title.isEmpty ? source.ref : source.title)
                                .font(.system(size: 10.5, weight: .medium)).lineLimit(1)
                            Spacer(minLength: 4)
                            if let score = source.score {
                                Text(String(format: "%.0f", score))
                                    .font(.system(size: 9.5, design: .monospaced))
                                    .foregroundStyle(.secondary)
                            }
                            if source.isOpenable {
                                Image(systemName: "chevron.right")
                                    .font(.system(size: 8, weight: .semibold)).foregroundStyle(.secondary)
                            }
                        }
                        .padding(.vertical, 4).padding(.horizontal, 6)
                        .background(RoundedRectangle(cornerRadius: 7, style: .continuous)
                            .fill(Color.primary.opacity(0.045)))
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .disabled(!source.isOpenable)
                }
            }
        }
    }

    private var internalsSummary: String {
        var parts: [String] = []
        if !msg.toolCalls.isEmpty {
            let secs = msg.toolCalls.reduce(0) { $0 + $1.seconds }
            parts.append("\(msg.toolCalls.count) retrieval step\(msg.toolCalls.count == 1 ? "" : "s") · \(String(format: "%.2fs", secs))")
        }
        if !msg.sources.isEmpty {
            parts.append("\(msg.sources.count) source\(msg.sources.count == 1 ? "" : "s")")
        }
        return parts.joined(separator: " · ")
    }
}

struct SearchHitRow: View {
    let hit: SearchHit
    let onOpen: () -> Void

    var body: some View {
        Button(action: onOpen) {
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 6) {
                    SourceTag(source: hit.source)
                    Text(hit.projectName).font(.system(size: 10.5, weight: .semibold))
                        .foregroundStyle(.secondary).lineLimit(1)
                    Spacer()
                    Text(hit.updatedAgo).font(.system(size: 10)).foregroundStyle(.secondary)
                    Image(systemName: "chevron.right")
                        .font(.system(size: 9, weight: .semibold)).foregroundStyle(.secondary)
                }
                Text(hit.displayTitle).font(.system(size: 12, weight: .semibold)).lineLimit(1)
                Text(hit.snippet).font(.system(size: 11)).foregroundStyle(.secondary)
                    .lineLimit(2).multilineTextAlignment(.leading)
            }
            .padding(9)
            .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color.primary.opacity(0.045)))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}

// MARK: - UI preview export

enum UIExporter {
    @MainActor private static func write(_ view: some View, _ name: String) {
        let renderer = ImageRenderer(content: view)
        renderer.scale = 2
        guard let image = renderer.nsImage,
              let tiff = image.tiffRepresentation,
              let rep = NSBitmapImageRep(data: tiff),
              let png = rep.representation(using: .png, properties: [:]) else { return }
        let dir = "/tmp/mybot_icons"
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        try? png.write(to: URL(fileURLWithPath: "\(dir)/\(name).png"))
    }

    @MainActor static func exportAll() {
        for (name, scheme) in [("ui-dark", ColorScheme.dark), ("ui-light", ColorScheme.light)] {
            let bg = scheme == .dark ? Color(white: 0.12) : Color(white: 0.95)
            write(ContentView(model: AppModel.sample()).environment(\.colorScheme, scheme).background(bg), name)

            let busyModel = AppModel.sample()
            busyModel.busyMessage = "Excluding 394 automated sessions…"
            write(ContentView(model: busyModel).environment(\.colorScheme, scheme).background(bg),
                  name.replacingOccurrences(of: "ui-", with: "busy-"))

            let askModel = AppModel.sample()
            askModel.mode = .ask
            askModel.chat = [
                ChatMsg(role: "user", text: "what did I do with fable recently?"),
                ChatMsg(role: "assistant",
                        text: "You mostly worked on the **fable evaluation harness** — last week you fixed the scoring regression and re-ran the benchmark suite in `~/Code/demo-project`.",
                        sources: [
                            ChatSource(ref: "codex:0199aaa", sourceName: "codex", kind: "chunk",
                                       title: "Fix fable scoring regression", score: 87),
                            ChatSource(ref: "claude:bb31c02", sourceName: "claude", kind: "session",
                                       title: "fable benchmark sweep", score: 61),
                        ],
                        toolCalls: [
                            ChatToolCall(tool: "memory-search", query: "fable recent work",
                                         seconds: 0.02, results: 5, tokens: 140),
                            ChatToolCall(tool: "trajectory-search", query: "fable",
                                         seconds: 0.31, results: 3, tokens: 480),
                        ]),
            ]
            askModel.searchHits = [
                SearchHit(ref: "codex:0199aaa", source: "codex", title: "Fix fable scoring regression",
                          cwd: "/Users/you/Code/demo-project",
                          snippet: "the fable scorer was double-counting partial matches … patched normalize step and re-ran",
                          updatedAt: "2026-07-05T12:00:00Z"),
                SearchHit(ref: "claude:bb31c02", source: "claude", title: "fable benchmark sweep",
                          cwd: "/Users/you/Code/demo-project",
                          snippet: "ran the full fable suite across 3 model configs … results in results/2026-07-01",
                          updatedAt: "2026-07-01T09:00:00Z"),
            ]
            write(ContentView(model: askModel).environment(\.colorScheme, scheme).background(bg),
                  name.replacingOccurrences(of: "ui-", with: "ask-"))

            // ScrollView contents don't render offline — render the ask pieces directly
            write(VStack(alignment: .leading, spacing: 10) {
                ForEach(askModel.chat) { msg in ChatBubble(msg: msg, startExpanded: true) }
                ChatBubble(msg: ChatMsg(role: "assistant",
                    text: "You mostly worked on the fable evaluation harness", streaming: true))
                HStack(spacing: 7) { ProgressView().controlSize(.small)
                    Text("Searching memory: fable recent work").font(.system(size: 11)).foregroundStyle(.secondary) }
                ChatBubble(msg: ChatMsg(role: "error", text: "chat server is not running — start it with run_discord_chatbot.sh"))
                Text("MEMORY MATCHES").font(.system(size: 10, weight: .heavy)).tracking(0.6)
                    .foregroundStyle(.secondary)
                ForEach(askModel.searchHits) { hit in SearchHitRow(hit: hit) {} }
            }
            .padding(12).frame(width: 480).background(bg)
            .environment(\.colorScheme, scheme),
                  name.replacingOccurrences(of: "ui-", with: "askbits-"))

            // Rows render blank inside ScrollView in ImageRenderer, so preview
            // them in a plain VStack to judge the row styling.
            let rows = VStack(spacing: 6) {
                ForEach(AppModel.sample().projects) { p in ProjectRow(project: p) { _ in } }
            }
            .padding(12).frame(width: 480).environment(\.colorScheme, scheme).background(bg)
            write(rows, name.replacingOccurrences(of: "ui-", with: "rows-"))

            let sampleSessions = [
                Session(ref: "codex:a1", sessionId: "3f9c2a7b8e01", title: "Fix cluster SU allocation lookup", updatedAt: "2026-07-06T08:00:00Z", chunks: 42),
                Session(ref: "codex:a2", sessionId: "77d1e0a4bb2f", title: "", updatedAt: "2026-07-05T14:00:00Z", chunks: 18, origin: "automated"),
                Session(ref: "codex:a3", sessionId: "c40b9915ee77", title: "Refactor trajectory index embeddings", updatedAt: "2026-07-04T11:00:00Z", chunks: 65),
            ]
            let sessionRows = VStack(spacing: 6) {
                ForEach(sampleSessions) { s in SessionRow(session: s) { } }
            }
            .padding(12).frame(width: 480).environment(\.colorScheme, scheme).background(bg)
            write(sessionRows, name.replacingOccurrences(of: "ui-", with: "sessions-"))

            // Full session-detail with the AI-driven toggle (2 human + 1 AI)
            let detailModel = AppModel.sample()
            detailModel.sessions = sampleSessions
            let detail = SessionDetail(model: detailModel,
                                       project: Project(source: "codex", cwd: "/Users/you/Code/demoapp",
                                                        sessions: 3, chunks: 125, updatedAt: "2026-07-06T08:00:00Z", included: true))
                .frame(width: 480, height: 360).environment(\.colorScheme, scheme).background(bg)
            write(detail, name.replacingOccurrences(of: "ui-", with: "detail-"))

            let events = [
                TrajectoryEvent(kind: "user", tool: "", text: "can you read README.md and tell me the best model for variant-effect prediction?"),
                TrajectoryEvent(kind: "thinking", tool: "", text: "The user wants me to read README.md and find the best model. Let me search for it first."),
                TrajectoryEvent(kind: "tool_use", tool: "Glob", text: "{\n  \"pattern\": \"**/README.md\"\n}"),
                TrajectoryEvent(kind: "tool_result", tool: "result", text: "Found: /Users/you/Code/demoapp/README.md"),
                TrajectoryEvent(kind: "assistant", tool: "", text: "The best model for variant-effect prediction was the fine-tuned baseline-model variant — it beat the baseline by 12% AUROC."),
            ]
            let traj = VStack(spacing: 8) {
                ForEach(events) { EventRow(event: $0) }
            }
            .padding(12).frame(width: 480).environment(\.colorScheme, scheme).background(bg)
            write(traj, name.replacingOccurrences(of: "ui-", with: "traj-"))
        }
    }
}
