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
            if let project = model.detail {
                SessionDetail(model: model, project: project)
            } else {
                controls
                projectScroll
                if model.selecting {
                    batchBar
                } else if !model.newProjects.isEmpty {
                    newProjectsBar
                }
            }
            footer
        }
        .frame(width: 480, height: 640)
        .background(background)
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
                BotIcon(mood: model.mood, accent: model.accentColor)
                    .frame(width: 34, height: 34)
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
        if let busy = model.busyMessage { return busy }
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
            } label: {
                chipLabel("Maintain", "wrench.and.screwdriver.fill")
            }
            .menuStyle(.borderlessButton).fixedSize()

            Button { model.openControlRoom() } label: { chipLabel("Web UI", "safari.fill") }
                .buttonStyle(.plain)

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
                Text("\(model.sessions.count) session\(model.sessions.count == 1 ? "" : "s") indexed")
                    .font(.system(size: 10, weight: .semibold)).foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 16).padding(.bottom, 8)

            Divider().opacity(0.4)

            if model.loadingSessions {
                Spacer(); ProgressView().controlSize(.small); Spacer()
            } else {
                ScrollView {
                    VStack(spacing: 6) {
                        ForEach(model.sessions) { session in
                            SessionRow(session: session) { model.excludeSession(session) }
                        }
                    }
                    .padding(.horizontal, 12).padding(.vertical, 8)
                }
            }
        }
        .frame(maxHeight: .infinity)
    }
}

struct SessionRow: View {
    let session: Session
    let exclude: () -> Void

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "bubble.left.and.text.bubble.right")
                .font(.system(size: 12)).foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 3) {
                Text(session.displayTitle).font(.system(size: 12.5, weight: .semibold)).lineLimit(1)
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
            Button(action: exclude) {
                Text("Exclude")
                    .font(.system(size: 11, weight: .semibold))
                    .padding(.horizontal, 10).padding(.vertical, 5)
                    .background(Capsule().fill(Color.red.opacity(0.14)))
                    .foregroundStyle(.red)
            }
            .buttonStyle(.plain)
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 11, style: .continuous).fill(Color.primary.opacity(0.05)))
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

            // Rows render blank inside ScrollView in ImageRenderer, so preview
            // them in a plain VStack to judge the row styling.
            let rows = VStack(spacing: 6) {
                ForEach(AppModel.sample().projects) { p in ProjectRow(project: p) { _ in } }
            }
            .padding(12).frame(width: 480).environment(\.colorScheme, scheme).background(bg)
            write(rows, name.replacingOccurrences(of: "ui-", with: "rows-"))

            let sampleSessions = [
                Session(ref: "codex:a1", sessionId: "3f9c2a7b8e01", title: "Fix cluster SU allocation lookup", updatedAt: "2026-07-06T08:00:00Z", chunks: 42),
                Session(ref: "codex:a2", sessionId: "77d1e0a4bb2f", title: "", updatedAt: "2026-07-05T14:00:00Z", chunks: 18),
                Session(ref: "codex:a3", sessionId: "c40b9915ee77", title: "Refactor trajectory index embeddings", updatedAt: "2026-07-04T11:00:00Z", chunks: 65),
            ]
            let sessionRows = VStack(spacing: 6) {
                ForEach(sampleSessions) { s in SessionRow(session: s) { } }
            }
            .padding(12).frame(width: 480).environment(\.colorScheme, scheme).background(bg)
            write(sessionRows, name.replacingOccurrences(of: "ui-", with: "sessions-"))
        }
    }
}
