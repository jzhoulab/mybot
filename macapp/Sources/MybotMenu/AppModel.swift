import Foundation
import AppKit
import SwiftUI

enum SortOrder: String, CaseIterable, Identifiable {
    case sessions = "Sessions"
    case chunks = "Chunks"
    case recent = "Recent"
    case name = "Name"
    var id: String { rawValue }
}

enum StatusFilter: String, CaseIterable, Identifiable {
    case all = "All"
    case included = "Included"
    case excluded = "Excluded"
    var id: String { rawValue }
}

func fileGroupBytes(_ path: String) -> Int64 {
    let fm = FileManager.default
    var total: Int64 = 0
    for suffix in ["", "-wal", "-shm"] {
        if let attrs = try? fm.attributesOfItem(atPath: path + suffix),
           let size = attrs[.size] as? NSNumber {
            total += size.int64Value
        }
    }
    return total
}

@MainActor
final class AppModel: ObservableObject {
    static let shared = AppModel()

    @Published var projects: [Project] = []
    @Published var newProjects: [NewProject] = []
    @Published var health = Health()
    @Published var busyMessage: String?
    @Published var lastError: String?

    @Published var search = ""
    @Published var sourceFilter = "all"
    @Published var statusFilter: StatusFilter = .all
    @Published var sortOrder: SortOrder = .sessions
    @Published var statusIcon = NSImage()

    // batch selection
    @Published var selecting = false
    @Published var selection: Set<String> = []

    // project → sessions drill-down
    @Published var detail: Project?
    @Published var sessions: [Session] = []
    @Published var loadingSessions = false

    let config = MybotConfig.shared
    private var timer: Timer?

    func start() {
        renderIcon()
        reload()
        timer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.reload() }
        }
    }

    /// Static sample data for offline UI rendering / previews (no I/O, no timer).
    static func sample() -> AppModel {
        let model = AppModel()
        func proj(_ s: String, _ cwd: String, _ sess: Int, _ ch: Int, _ inc: Bool) -> Project {
            Project(source: s, cwd: cwd, sessions: sess, chunks: ch,
                    updatedAt: "2026-07-06T09:00:00Z", included: inc)
        }
        model.projects = [
            proj("codex", "/Users/you/Code/mybot", 333, 6540, true),
            proj("claude", "/Users/you/Code/demoapp-pipeline", 41, 980, true),
            proj("codex", "/Users/you/Code/batch-jobs", 22, 410, true),
            proj("claude", "/Users/you/Research/side-project", 15, 300, false),
            proj("codex", "/Users/you/scratch/throwaway-experiment", 3, 44, false),
        ]
        var health = Health()
        health.totalChunks = 43_331
        health.embeddedChunks = 43_331
        health.indexBytes = 1_150_000_000
        health.memoryBytes = 64_000_000
        health.projectCount = 79
        health.latestIndexedAt = ISO8601DateFormatter().string(from: Date().addingTimeInterval(-140))
        model.health = health
        model.newProjects = [NewProject(source: "codex", cwd: "/Users/you/Code/newapp", sessions: 4)]
        return model
    }

    var mood: BotMood {
        if health.totalChunks == 0 { return .sleepy }
        if health.embeddedChunks == 0 { return .error }
        if health.stale || !newProjects.isEmpty || health.coverage < 0.5 { return .alert }
        return .happy
    }

    var accentColor: Color {
        switch mood {
        case .happy: return Color(red: 0.30, green: 0.80, blue: 0.46)
        case .alert: return Color(red: 0.98, green: 0.74, blue: 0.20)
        case .error: return Color(red: 0.95, green: 0.36, blue: 0.36)
        case .sleepy: return Color(white: 0.62)
        }
    }

    /// Render the cute bot to an NSImage for the menu bar (non-template so it
    /// keeps its status color).
    private func renderIcon() {
        let renderer = ImageRenderer(
            content: BotIconMono(mood: mood, badge: newProjects.count)
                .frame(width: 20, height: 20)
        )
        renderer.scale = 3
        if let image = renderer.nsImage {
            image.isTemplate = true  // menu bar tints it (black on light, white on dark)
            statusIcon = image
        }
    }

    var sources: [String] {
        ["all"] + Set(projects.map { $0.source }).sorted()
    }

    var filtered: [Project] {
        var items = projects
        if sourceFilter != "all" { items = items.filter { $0.source == sourceFilter } }
        switch statusFilter {
        case .included: items = items.filter { $0.included }
        case .excluded: items = items.filter { !$0.included }
        case .all: break
        }
        let query = search.trimmingCharacters(in: .whitespaces).lowercased()
        if !query.isEmpty { items = items.filter { $0.cwd.lowercased().contains(query) } }
        switch sortOrder {
        case .sessions: items.sort { $0.sessions > $1.sessions }
        case .chunks: items.sort { $0.chunks > $1.chunks }
        case .recent: items.sort { $0.updatedAt > $1.updatedAt }
        case .name: items.sort { $0.displayName.lowercased() < $1.displayName.lowercased() }
        }
        return items
    }

    var glyph: String {
        if health.totalChunks == 0 { return "⚪" }
        if health.embeddedChunks == 0 { return "🔴" }
        if health.stale || !newProjects.isEmpty || health.coverage < 0.5 { return "🟡" }
        return "🟢"
    }

    var title: String { newProjects.isEmpty ? glyph : "\(glyph) \(newProjects.count)" }

    /// Snappy: all local reads (SQLite + two JSON files + file sizes), off the
    /// main thread. Never blocks the UI, never depends on the chat server.
    func reload() {
        let cfg = config
        DispatchQueue.global(qos: .userInitiated).async {
            let reader = IndexReader(dbPath: cfg.dbPath.path)
            let policy = AccessPolicy.load(cfg.accessConfigPath)
            let rows = reader.projects()
            let snapshot = reader.health()
            let knownPath = cfg.dbPath.deletingLastPathComponent()
                .appendingPathComponent("known_projects.json")
            let pending = KnownProjects.pending(knownPath)
            let indexBytes = fileGroupBytes(cfg.dbPath.path)
            let memoryBytes = fileGroupBytes(cfg.memoryDbPath.path)

            var projects = rows.map {
                Project(source: $0.source, cwd: $0.cwd, sessions: $0.sessions,
                        chunks: $0.chunks, updatedAt: $0.updatedAt,
                        included: policy.included(source: $0.source, cwd: $0.cwd))
            }
            // Excluding purges chunks, so excluded projects are absent from the
            // index. Add them back from the policy so they show under Excluded.
            let present = Set(projects.map { $0.id })
            for entry in policy.excludedWorkdirs() {
                let synthetic = Project(source: entry.source, cwd: entry.cwd, sessions: 0,
                                        chunks: 0, updatedAt: "", included: false)
                if !present.contains(synthetic.id) { projects.append(synthetic) }
            }
            var health = Health()
            health.totalChunks = snapshot.total
            health.embeddedChunks = snapshot.embedded
            health.latestIndexedAt = snapshot.indexedAt
            health.indexBytes = indexBytes
            health.memoryBytes = memoryBytes
            health.projectCount = rows.count

            DispatchQueue.main.async {
                self.projects = projects
                self.newProjects = pending
                self.health = health
                self.renderIcon()
            }
        }
    }

    // ---- write actions (delegate to the Python admin CLI) ------------------
    private func runAction(_ message: String, _ work: @escaping (AdminClient) -> AdminClient.Result) {
        busyMessage = message
        lastError = nil
        let admin = AdminClient(config: config)
        DispatchQueue.global(qos: .userInitiated).async {
            let result = work(admin)
            DispatchQueue.main.async {
                self.busyMessage = nil
                if !result.ok {
                    self.lastError = (result.json["error"] as? String)
                        ?? (result.stderr.isEmpty ? "action failed" : result.stderr)
                }
                self.reload()
            }
        }
    }

    func setInclusion(_ project: Project, include: Bool) {
        if include {
            runAction("Including \(project.displayName)…") {
                $0.include(source: project.source, cwds: [project.cwd], reindex: true)
            }
        } else {
            runAction("Excluding \(project.displayName)…") {
                $0.exclude(source: project.source, cwds: [project.cwd])
            }
        }
    }

    // ---- batch selection ---------------------------------------------------
    func toggleSelect(_ project: Project) {
        if selection.contains(project.id) { selection.remove(project.id) }
        else { selection.insert(project.id) }
    }

    func endSelecting() {
        selecting = false
        selection.removeAll()
    }

    func batchSet(include: Bool) {
        let chosen = projects.filter { selection.contains($0.id) }
        guard !chosen.isEmpty else { return }
        busyMessage = "\(include ? "Including" : "Excluding") \(chosen.count) project\(chosen.count == 1 ? "" : "s")…"
        lastError = nil
        let bySource = Dictionary(grouping: chosen, by: { $0.source })
        let admin = AdminClient(config: config)
        DispatchQueue.global(qos: .userInitiated).async {
            var failure: String?
            for (source, projects) in bySource {
                let cwds = projects.map { $0.cwd }
                let result = include
                    ? admin.include(source: source, cwds: cwds, reindex: true)
                    : admin.exclude(source: source, cwds: cwds)
                if !result.ok && failure == nil {
                    failure = (result.json["error"] as? String)
                        ?? (result.stderr.isEmpty ? "action failed" : result.stderr)
                }
            }
            DispatchQueue.main.async {
                self.busyMessage = nil
                self.lastError = failure
                self.endSelecting()
                self.reload()
            }
        }
    }

    func reviewProject(_ project: NewProject, decision: String) {
        runAction("\(decision == "exclude" ? "Excluding" : "Keeping") \(project.displayName)…") {
            $0.review(source: project.source, cwd: project.cwd, decision: decision)
        }
    }

    func runMaintenance(_ action: String, label: String) {
        runAction("\(label)…") { $0.maintenance(action) }
    }

    func openControlRoom() { NSWorkspace.shared.open(config.guiURL) }

    // ---- session drill-down -----------------------------------------------
    func openDetail(_ project: Project) {
        detail = project
        sessions = []
        loadingSessions = true
        let cfg = config
        DispatchQueue.global(qos: .userInitiated).async {
            let rows = IndexReader(dbPath: cfg.dbPath.path).sessions(source: project.source, cwd: project.cwd)
            let sessions = rows.map {
                Session(ref: $0.ref, sessionId: $0.sessionId, title: $0.title,
                        updatedAt: $0.updatedAt, chunks: $0.chunks)
            }
            DispatchQueue.main.async {
                guard self.detail?.id == project.id else { return }
                self.sessions = sessions
                self.loadingSessions = false
            }
        }
    }

    func closeDetail() {
        detail = nil
        sessions = []
    }

    func excludeSession(_ session: Session) {
        guard let project = detail else { return }
        busyMessage = "Excluding session…"
        lastError = nil
        let admin = AdminClient(config: config)
        let source = project.source
        DispatchQueue.global(qos: .userInitiated).async {
            let result = admin.run(["exclude", "--source", source, "--kind", "session", "--value", session.sessionId])
            DispatchQueue.main.async {
                self.busyMessage = nil
                if !result.ok {
                    self.lastError = (result.json["error"] as? String)
                        ?? (result.stderr.isEmpty ? "action failed" : result.stderr)
                }
                self.reload()
                if let project = self.detail { self.openDetail(project) }
            }
        }
    }
}
