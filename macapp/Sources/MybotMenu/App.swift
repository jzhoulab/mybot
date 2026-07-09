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
    static func source(_ name: String) -> Color {
        switch name {
        case "codex": return Color(red: 0.47, green: 0.49, blue: 0.96)
        case "claude": return Color(red: 0.93, green: 0.56, blue: 0.26)
        default: return .gray
        }
    }
}

private struct SourceTag: View {
    let source: String
    var body: some View {
        Text(source.uppercased())
            .font(.system(size: 9, weight: .heavy))
            .tracking(0.4)
            .padding(.horizontal, 6).padding(.vertical, 2.5)
            .background(Capsule().fill(Palette.source(source).opacity(0.16)))
            .foregroundStyle(Palette.source(source))
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

// MARK: - Root

struct ContentView: View {
    @ObservedObject var model: AppModel

    var body: some View {
        VStack(spacing: 0) {
            headerBlock
            if let busy = model.busyMessage { busyBanner(busy) }
            Group {
                if let session = model.trajectory {
                    TrajectoryView(model: model, session: session)
                } else if let project = model.detail {
                    SessionDetail(model: model, project: project)
                } else {
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
        .background(Color.accentColor.opacity(0.16))
        .overlay(Rectangle().frame(height: 1).foregroundStyle(Color.accentColor.opacity(0.3)), alignment: .bottom)
    }

    private var background: some View {
        ZStack {
            Color.primary.opacity(0.015)
            LinearGradient(
                colors: [model.accentColor.opacity(0.16), .clear],
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
                         tint: Color(red: 0.47, green: 0.49, blue: 0.96))
                StatChip(icon: "sparkles", value: "\(pct(h.coverage))%", label: "embedded",
                         tint: h.coverage > 0.5 ? .green : .orange)
                StatChip(icon: "internaldrive.fill", value: formatBytes(h.indexBytes), label: "on disk",
                         tint: .secondary)
            }
        }
        .padding(.horizontal, 14).padding(.top, 14).padding(.bottom, 12)
    }

    private var statusLine: String {
        if model.indexBusy { return "Index updating… showing last snapshot" }
        let h = model.health
        if h.totalChunks > 0 && h.embeddedChunks == 0 { return "Semantic search off · lexical only" }
        return "Index \(h.stale ? "stale" : "fresh") · updated \(h.indexedAgo)"
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
                .foregroundStyle(Color.accentColor)
            }
            .padding(.horizontal, 18).padding(.top, 8).padding(.bottom, 4)

            ScrollView {
                VStack(spacing: 6) {
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
                Image(systemName: "sparkles").font(.system(size: 12)).foregroundStyle(.orange)
                Text("\(model.newProjects.count) new project\(model.newProjects.count == 1 ? "" : "s") to review")
                    .font(.system(size: 12, weight: .bold))
                Spacer()
            }
            ForEach(model.newProjects.prefix(3)) { np in
                HStack(spacing: 8) {
                    SourceTag(source: np.source)
                    Text(np.displayName).font(.system(size: 11.5, weight: .medium)).lineLimit(1)
                    Text("· \(np.sessions) sess").font(.system(size: 10)).foregroundStyle(.secondary)
                    Spacer()
                    Button("Keep") { model.reviewProject(np, decision: "keep") }
                        .buttonStyle(.plain).font(.system(size: 11, weight: .semibold)).foregroundStyle(.blue)
                    Button("Exclude") { model.reviewProject(np, decision: "exclude") }
                        .buttonStyle(.plain).font(.system(size: 11, weight: .semibold)).foregroundStyle(.red)
                }
            }
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Color.orange.opacity(0.12)))
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(Color.orange.opacity(0.28)))
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
                    .background(Capsule().fill(Color.green.opacity(0.18))).foregroundStyle(.green)
            }
            .buttonStyle(.plain).disabled(model.selection.isEmpty).opacity(model.selection.isEmpty ? 0.5 : 1)
            Button {
                model.batchSet(include: false)
            } label: {
                Text("Exclude").font(.system(size: 11, weight: .semibold))
                    .padding(.horizontal, 12).padding(.vertical, 6)
                    .background(Capsule().fill(Color.red.opacity(0.16))).foregroundStyle(.red)
            }
            .buttonStyle(.plain).disabled(model.selection.isEmpty).opacity(model.selection.isEmpty ? 0.5 : 1)
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(Color.accentColor.opacity(0.12)))
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).strokeBorder(Color.accentColor.opacity(0.3)))
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
            } label: {
                chipLabel("Maintain", "wrench.and.screwdriver.fill")
            }
            .menuStyle(.borderlessButton).fixedSize()
            .disabled(model.busyMessage != nil)

            Button { model.openControlRoom() } label: { chipLabel("Web UI", "safari.fill") }
                .buttonStyle(.plain)

            modelMenu

            Spacer()

            if let err = model.lastError {
                Image(systemName: "exclamationmark.triangle.fill")
                    .font(.system(size: 12)).foregroundStyle(.red).help(err)
            }
            Button { NSApplication.shared.terminate(nil) } label: { chipLabel("Quit", "power") }
                .buttonStyle(.plain)
        }
        .padding(.horizontal, 12).padding(.vertical, 10)
        .background(Color.primary.opacity(0.035))
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
                    .font(.system(size: 16)).foregroundStyle(isSelected ? Color.accentColor : .secondary)
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
                .fill(project.included ? Color.green.opacity(0.8) : Color.clear)
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
        Button { toggle(!project.included) } label: {
            Text(project.included ? "Exclude" : "Include")
                .font(.system(size: 11, weight: .semibold))
                .padding(.horizontal, 11).padding(.vertical, 6)
                .background(Capsule().fill(project.included ? Color.red.opacity(0.14) : Color.green.opacity(0.18)))
                .foregroundStyle(project.included ? Color.red : Color.green)
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
                            .font(.system(size: 10, weight: .medium)).foregroundStyle(.green)
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
                    VStack(spacing: 6) {
                        ForEach(visibleSessions) { session in
                            SessionRow(session: session,
                                       onOpen: { model.openTrajectory(session) },
                                       exclude: { model.excludeSession(session) })
                        }
                        if visibleSessions.isEmpty {
                            Text(showAutomated ? "No sessions indexed"
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
                    .background(Capsule().fill(Color.purple.opacity(0.18)))
            }
            .foregroundStyle(showAutomated ? Color.purple : Color.purple.opacity(0.85))
            .padding(.horizontal, 11).padding(.vertical, 7)
            .background(RoundedRectangle(cornerRadius: 9, style: .continuous)
                .fill(Color.purple.opacity(showAutomated ? 0.10 : 0.07)))
            .overlay(RoundedRectangle(cornerRadius: 9, style: .continuous)
                .strokeBorder(Color.purple.opacity(0.25)))
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
                                    .background(Capsule().fill(Color.orange.opacity(0.22)))
                                    .foregroundStyle(.orange)
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
                    .background(Capsule().fill(Color.red.opacity(0.14)))
                    .foregroundStyle(.red)
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
            }
            .padding(.horizontal, 14).padding(.top, 4).padding(.bottom, 6)

            VStack(alignment: .leading, spacing: 2) {
                Text(session.displayTitle).font(.system(size: 13, weight: .bold)).lineLimit(2)
                Text("\(session.shortId) · \(model.trajectoryEvents.count) events")
                    .font(.system(size: 10)).foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 16).padding(.bottom, 6)

            Divider().opacity(0.4)

            if model.loadingTrajectory {
                Spacer(); ProgressView().controlSize(.small); Spacer()
            } else {
                ScrollView {
                    LazyVStack(spacing: 8) {
                        ForEach(model.trajectoryEvents) { event in EventRow(event: event) }
                        if model.trajectoryTruncated || model.trajectoryOmitted > 0 {
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
}

struct EventRow: View {
    let event: TrajectoryEvent
    @State private var expanded = false

    var body: some View {
        switch event.kind {
        case "user": bubble(role: "You", tint: Color.accentColor, align: .trailing)
        case "assistant": bubble(role: "Assistant", tint: .secondary, align: .leading)
        case "thinking": foldable(icon: "brain", title: "Thinking", tint: .purple, mono: false, italic: true)
        case "tool_use": foldable(icon: "wrench.and.screwdriver.fill",
                                  title: event.tool.isEmpty ? "Tool call" : event.tool, tint: .orange, mono: true)
        case "tool_result": foldable(icon: "arrow.turn.down.right",
                                     title: "Result · \(event.text.count) chars", tint: .secondary, mono: true)
        default: EmptyView()
        }
    }

    private func bubble(role: String, tint: Color, align: HorizontalAlignment) -> some View {
        VStack(alignment: align, spacing: 3) {
            Text(role.uppercased()).font(.system(size: 8.5, weight: .heavy)).tracking(0.5)
                .foregroundStyle(tint)
            Text(event.text)
                .font(.system(size: 12))
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(9)
                .background(RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .fill(align == .trailing ? Color.accentColor.opacity(0.12) : Color.primary.opacity(0.05)))
        }
        .frame(maxWidth: .infinity, alignment: align == .trailing ? .trailing : .leading)
    }

    private func foldable(icon: String, title: String, tint: Color, mono: Bool, italic: Bool = false) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Button { expanded.toggle() } label: {
                HStack(spacing: 6) {
                    Image(systemName: icon).font(.system(size: 10)).foregroundStyle(tint)
                    Text(title).font(.system(size: 11, weight: .semibold)).foregroundStyle(tint).lineLimit(1)
                    Spacer()
                    Image(systemName: expanded ? "chevron.down" : "chevron.right")
                        .font(.system(size: 9, weight: .semibold)).foregroundStyle(.secondary)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            if expanded {
                Text(event.text)
                    .font(mono ? .system(size: 10.5, design: .monospaced) : .system(size: 11))
                    .italic(italic)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.top, 6)
            }
        }
        .padding(9)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(tint.opacity(0.06)))
        .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(tint.opacity(0.16)))
    }
}

// MARK: - Ask (search + chat)

struct AskView: View {
    @ObservedObject var model: AppModel
    @State private var input = ""
    @FocusState private var focused: Bool

    var body: some View {
        VStack(spacing: 0) {
            inputBar
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
                        if model.chatPending {
                            HStack(spacing: 7) {
                                ProgressView().controlSize(.small)
                                Text("mybot is thinking…").font(.system(size: 11)).foregroundStyle(.secondary)
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
        .padding(.horizontal, 12).padding(.bottom, 10)
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
            Text(msg.text)
                .font(.system(size: 12)).textSelection(.enabled)
                .padding(9)
                .background(RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .fill(Color.accentColor.opacity(0.14)))
                .frame(maxWidth: .infinity, alignment: .trailing)
                .id(msg.id)
        case "error":
            HStack(spacing: 6) {
                Image(systemName: "exclamationmark.triangle.fill")
                    .font(.system(size: 10)).foregroundStyle(.orange)
                Text(msg.text).font(.system(size: 11.5)).foregroundStyle(.secondary)
            }
            .padding(9)
            .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(Color.orange.opacity(0.08)))
            .frame(maxWidth: .infinity, alignment: .leading)
            .id(msg.id)
        default:
            VStack(alignment: .leading, spacing: 0) {
                HStack(alignment: .top, spacing: 8) {
                    BotAvatar(mood: .happy).frame(width: 20, height: 20)
                    Text(.init(msg.text))   // renders basic markdown
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
