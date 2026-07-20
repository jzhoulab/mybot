import Foundation
import AppKit
import SwiftUI
import ServiceManagement

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
    @Published var clusters: [AutomatedCluster] = []
    @Published var modelPresets: [ModelPreset] = [ModelPreset.fallback]
    @Published var currentModelId: String = ModelPreset.fallback.id
    var currentModel: ModelPreset {
        modelPresets.first { $0.id == currentModelId } ?? ModelPreset.fallback
    }
    /// True while the index is being rewritten under us (read failed or looked
    /// wiped). We keep showing the last good snapshot instead of zeros.
    @Published var indexBusy = false
    // When the index read comes back degraded (mid-update), we hold the last
    // good snapshot rather than flashing 0. This marks how long it's been
    // degraded so a genuine wipe eventually reflects instead of holding forever.
    private var degradedSince: Date?
    private let maxDegradedHoldSeconds: TimeInterval = 180

    // ---- ask (search + chat) ----
    enum Mode { case projects, ask }
    @Published var mode: Mode = .projects
    @Published var searchHits: [SearchHit] = []
    @Published var chat: [ChatMsg] = []
    @Published var chatPending = false
    @Published var chatActivity = ""   // live tool line while streaming
    // Named chat threads (Ask history). activeThreadKey is the current one.
    @Published var chatThreads: [ChatClient.ChatThread] = []
    @Published var activeThreadKey: String = ChatClient.threadPrefix
    @Published var showChatList = false
    @Published var loadingThread = false
    private var searchWork: DispatchWorkItem?
    private var streamTask: Task<Void, Never>?
    @Published var busyMessage: String?
    @Published var lastError: String?

    // Owner identity onboarding: discovered by a background "Sherlock session"
    // on first boot, confirmed by the owner here or via !iam on Discord.
    @Published var ownerName = ""
    @Published var ownerAliases: [String] = []
    @Published var ownerConfirmed = false
    @Published var identityInvestigating = false

    // Launch at login (SMAppService login item).
    @Published var launchAtLogin = false

    // Build-vs-repo drift, computed off-main in reload() — the footer must
    // never touch .git files during body evaluation (menu-open latency).
    @Published var appBehindSource = false
    @Published var sourceHead: String?

    // Chat-platform connections (Connections card).
    @Published var discordConfigured = false
    @Published var slackConfigured = false
    @Published var connecting = false
    @Published var connectMessage = ""      // last connect result (success or error)
    @Published var connectOk = false

    @Published var search = ""
    @Published var sourceFilter = "all"
    @Published var statusFilter: StatusFilter = .included
    @Published var sortOrder: SortOrder = .sessions
    @Published var statusIcon = NSImage()

    // batch selection
    @Published var selecting = false
    @Published var selection: Set<String> = []

    // project → sessions drill-down
    @Published var detail: Project?
    @Published var sessions: [Session] = []
    @Published var loadingSessions = false

    // session → trajectory viewer
    @Published var trajectory: Session?
    @Published var trajectoryEvents: [TrajectoryEvent] = []
    @Published var loadingTrajectory = false
    @Published var trajectoryTruncated = false
    @Published var trajectoryOmitted = 0

    let config = MybotConfig.shared
    private var timer: Timer?

    func start() {
        renderIcon()
        reload()
        loadModelPresets()
        refreshIdentity()
        refreshConnections()
        initLoginItem()
        ensureServerRunning()
        timer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self, self.busyMessage == nil else { return }  // don't shift numbers mid-op
                self.reload()
                // Keep polling while onboarding hasn't settled (discovery in
                // flight, or a deduced name waiting for the owner's nod).
                if self.identityInvestigating || (self.ownerName.isEmpty || !self.ownerConfirmed) {
                    self.refreshIdentity()
                }
            }
        }
    }

    // MARK: owner identity

    private func applyIdentity(_ identity: ChatClient.IdentityState?) {
        guard let identity else { return }  // server down — keep last known
        DispatchQueue.main.async {
            self.ownerName = identity.name
            self.ownerAliases = identity.aliases
            self.ownerConfirmed = identity.confirmed
            self.identityInvestigating = identity.investigating
        }
    }

    func refreshIdentity() {
        ChatClient(config: config).fetchIdentity { [weak self] identity in
            self?.applyIdentity(identity)
        }
    }

    func confirmIdentity(name: String) {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        ChatClient(config: config).confirmIdentity(name: trimmed) { [weak self] identity in
            self?.applyIdentity(identity)
            DispatchQueue.main.async { self?.refreshIdentity() }
        }
    }

    func rediscoverIdentity() {
        identityInvestigating = true
        ChatClient(config: config).rediscoverIdentity { [weak self] identity in
            self?.applyIdentity(identity)
        }
    }

    // MARK: launch at login

    /// Autolaunch should only ever pin an installed copy, never the build
    /// staging bundle in the repo.
    private var installedInApplications: Bool {
        Bundle.main.bundlePath.contains("/Applications/")
    }

    /// Auto-enable once for an installed app, then follow the user's toggle.
    func initLoginItem() {
        launchAtLogin = SMAppService.mainApp.status == .enabled
        let onceKey = "mybot.loginItem.autoEnabled"
        if installedInApplications, !launchAtLogin,
           !UserDefaults.standard.bool(forKey: onceKey) {
            UserDefaults.standard.set(true, forKey: onceKey)
            setLaunchAtLogin(true)
        }
    }

    func setLaunchAtLogin(_ on: Bool) {
        if on && !installedInApplications {
            lastError = "Run the installed app (~/Applications/mybot.app) to enable launch at login"
            return
        }
        do {
            if on {
                try SMAppService.mainApp.register()
            } else {
                try SMAppService.mainApp.unregister()
            }
        } catch {
            lastError = "Login item: \(error.localizedDescription)"
        }
        launchAtLogin = SMAppService.mainApp.status == .enabled
    }

    // MARK: local server

    private var serverStartAttempted = false

    /// Start the chat server + Discord bridge when the app comes up and finds
    /// them down. The launcher is single-instance, so racing a manually
    /// started copy is harmless. One attempt per app run — if the owner shuts
    /// the server down on purpose, the app doesn't fight them.
    func ensureServerRunning() {
        guard !serverStartAttempted else { return }
        guard var parts = URLComponents(url: config.guiURL, resolvingAgainstBaseURL: false) else { return }
        parts.path = "/health"
        guard let healthURL = parts.url else { return }
        var request = URLRequest(url: healthURL)
        request.timeoutInterval = 3
        URLSession.shared.dataTask(with: request) { [weak self] _, response, _ in
            if (response as? HTTPURLResponse)?.statusCode == 200 { return }
            Task { @MainActor in self?.startLocalServer() }
        }.resume()
    }

    private func startLocalServer() {
        guard !serverStartAttempted else { return }
        serverStartAttempted = true
        let script = config.repoRoot.appendingPathComponent("run_discord_chatbot.sh").path
        let log = config.repoRoot.appendingPathComponent("state/run/mybot-launcher.log").path
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/zsh")
        process.arguments = [
            "-c",
            "MYBOT_SERVER_START_TIMEOUT_SECONDS=180 nohup '\(script)' >> '\(log)' 2>&1 &",
        ]
        try? process.run()
    }

    // MARK: connections

    func refreshConnections() {
        let admin = AdminClient(config: config)
        DispatchQueue.global(qos: .utility).async {
            let d = admin.connectStatus("discord")
            let s = admin.connectStatus("slack")
            DispatchQueue.main.async {
                self.discordConfigured = (d.json["configured"] as? Bool) ?? false
                self.slackConfigured = (s.json["configured"] as? Bool) ?? false
            }
        }
    }

    /// Validate pasted tokens and write the env file. `channels` is Discord-only;
    /// `appToken` is Slack-only.
    func connectPlatform(_ platform: String, botToken: String, appToken: String, channels: String) {
        connecting = true
        connectMessage = ""
        let admin = AdminClient(config: config)
        DispatchQueue.global(qos: .userInitiated).async {
            let r = admin.connect(platform, botToken: botToken, appToken: appToken, channels: channels)
            DispatchQueue.main.async {
                self.connecting = false
                self.connectOk = r.ok
                if r.ok {
                    let name = (r.json["bot_name"] as? String) ?? "your bot"
                    self.connectMessage = "Connected as \(name). Start it with run_\(platform)_chatbot.sh."
                    if platform == "discord" { self.discordConfigured = true }
                    if platform == "slack" { self.slackConfigured = true }
                } else {
                    self.connectMessage = (r.json["error"] as? String)
                        ?? (r.stderr.isEmpty ? "connection failed" : r.stderr)
                }
            }
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
        model.ownerName = "Ada Lovelace"
        model.ownerAliases = ["alice", "alice"]
        model.ownerConfirmed = true
        model.discordConfigured = true
        model.slackConfigured = false
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
        case .happy: return Color(red: 0.18, green: 0.62, blue: 0.50)
        case .alert: return Color(red: 0.86, green: 0.62, blue: 0.20)
        case .error: return Color(red: 0.87, green: 0.36, blue: 0.33)
        case .sleepy: return Color(white: 0.62)
        }
    }

    /// Render the cute bot to an NSImage for the menu bar (non-template so it
    /// keeps its status color).
    private var renderedIconKey = ""

    private func renderIcon() {
        let key = "\(mood)-\(newProjects.count)"
        guard key != renderedIconKey else { return }  // 15s ticks mostly change nothing
        let renderer = ImageRenderer(
            content: BotIconMono(mood: mood, badge: newProjects.count)
                .frame(width: 20, height: 20)
        )
        renderer.scale = 3
        if let image = renderer.nsImage {
            image.isTemplate = true  // menu bar tints it (black on light, white on dark)
            statusIcon = image
            renderedIconKey = key
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
    /// mtime+size fingerprint of every file reload() reads. The 15s poll used
    /// to re-run GROUP BY scans over the (GB-sized) index each tick — ~430 CPU
    /// minutes a day and real battery drain. Nothing changed → nothing to do.
    private var lastReloadFingerprint = ""

    private nonisolated static func reloadFingerprint(_ cfg: MybotConfig) -> String {
        let fm = FileManager.default
        var parts: [String] = []
        var paths = [cfg.dbPath.path, cfg.dbPath.path + "-wal", cfg.modelConfigPath.path,
                     cfg.accessConfigPath.path]
        paths.append(cfg.dbPath.deletingLastPathComponent().appendingPathComponent("known_projects.json").path)
        paths.append(cfg.dbPath.deletingLastPathComponent().appendingPathComponent("owner_identity.json").path)
        for p in paths {
            let a = (try? fm.attributesOfItem(atPath: p)) ?? [:]
            let size = (a[.size] as? NSNumber)?.int64Value ?? -1
            let mtime = (a[.modificationDate] as? Date)?.timeIntervalSince1970 ?? -1
            parts.append("\(size):\(mtime)")
        }
        return parts.joined(separator: "|")
    }

    func reload(force: Bool = false) {
        let cfg = config
        DispatchQueue.global(qos: .userInitiated).async {
            let fingerprint = AppModel.reloadFingerprint(cfg)
            let unchanged: Bool = DispatchQueue.main.sync {
                !force && !self.projects.isEmpty && !self.indexBusy
                    && fingerprint == self.lastReloadFingerprint
            }
            if unchanged { return }
            let reader = IndexReader(dbPath: cfg.dbPath.path)
            let policy = AccessPolicy.load(cfg.accessConfigPath)
            guard let rows = reader.projects(), let snapshot = reader.health() else {
                self.holdSnapshotWhileBusy()  // db locked mid-rebuild
                return
            }
            // The index is mid-update when a read comes back degraded versus what
            // we last had good: either emptied (total 0), or its embeddings
            // dropped to 0 while chunks remain — a rebuild re-inserts chunks
            // before re-embedding them. In either case hold the last good
            // snapshot and show the "updating" hint instead of flashing 0. Only
            // accept the degraded numbers if they persist past a long bound (a
            // real wipe, not a transient rebuild).
            let (hadChunks, hadEmbeddings) = DispatchQueue.main.sync {
                (self.health.totalChunks > 0, self.health.embeddedChunks > 0)
            }
            let degraded = (snapshot.total == 0 && hadChunks)
                || (snapshot.total > 0 && snapshot.embedded == 0 && hadEmbeddings)
            if degraded {
                let keepHolding: Bool = DispatchQueue.main.sync {
                    if self.degradedSince == nil { self.degradedSince = Date() }
                    return Date().timeIntervalSince(self.degradedSince ?? Date()) < self.maxDegradedHoldSeconds
                }
                if keepHolding {
                    self.holdSnapshotWhileBusy()
                    return
                }
            }
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
            health.automatedSessions = snapshot.automated

            // Automated clusters: counts from the index, exclusion from policy.
            // An excluded cluster is purged, so it exists only in the policy.
            let counts = reader.automatedClusters()
            let excluded = policy.excludedClusters()

            // Current agent model id, read straight from model_config.json (local).
            var modelId = ModelPreset.fallback.id
            if let data = try? Data(contentsOf: cfg.modelConfigPath),
               let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let preset = obj["preset"] as? String, !preset.isEmpty {
                modelId = preset
            }
            let clusters = AutomatedCluster.order.compactMap { detail -> AutomatedCluster? in
                let count = counts[detail] ?? 0
                let isExcluded = excluded.contains(detail)
                guard count > 0 || isExcluded else { return nil }
                return AutomatedCluster(detail: detail, count: count, excluded: isExcluded)
            }

            let sourceHead = MybotConfig.sourceHeadSHA()
            let behind = sourceHead.map { !MybotConfig.buildSHA.hasPrefix($0) } ?? false

            DispatchQueue.main.async {
                self.projects = projects
                self.newProjects = pending
                self.health = health
                self.clusters = clusters
                self.currentModelId = modelId
                self.sourceHead = sourceHead
                self.appBehindSource = behind
                self.indexBusy = false
                self.degradedSince = nil
                self.lastReloadFingerprint = fingerprint
                self.renderIcon()
            }
        }
    }

    /// Index is mid-rewrite: keep the last good snapshot on screen, show the
    /// "updating" hint, and retry shortly.
    private nonisolated func holdSnapshotWhileBusy() {
        DispatchQueue.main.async {
            self.indexBusy = true
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
                if self.indexBusy { self.reload(force: true) }
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
                self.reload(force: true)
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
                self.reload(force: true)
            }
        }
    }

    func reviewProject(_ project: NewProject, decision: String) {
        // Optimistic: drop the card from the pending list instantly so the click
        // feels immediate. The backend write + reindex (a Python subprocess) run
        // quietly in the background — no full-panel busy overlay for a one-tap
        // review. reload() reconciles afterward.
        newProjects.removeAll { $0.id == project.id }
        let admin = AdminClient(config: config)
        DispatchQueue.global(qos: .userInitiated).async {
            let result = admin.review(source: project.source, cwd: project.cwd, decision: decision)
            DispatchQueue.main.async {
                if !result.ok {
                    self.lastError = (result.json["error"] as? String)
                        ?? (result.stderr.isEmpty ? "review failed" : result.stderr)
                }
                self.reload(force: true)
            }
        }
    }

    func runMaintenance(_ action: String, label: String) {
        runAction("\(label)…") { $0.maintenance(action) }
    }

    func excludeAutomated() {
        runAction("Excluding \(health.automatedSessions) automated sessions…") {
            $0.run(["automated", "--action", "exclude"])
        }
    }

    func includeAutomated() {
        runAction("Re-including automated sessions…") {
            $0.run(["automated", "--action", "include"])
        }
    }

    func setCluster(_ cluster: AutomatedCluster, exclude: Bool) {
        let verb = exclude
            ? "Excluding \(cluster.count) \(cluster.displayName) sessions…"
            : "Re-including \(cluster.displayName) sessions…"
        runAction(verb) {
            $0.run(["automated", "--action", exclude ? "exclude" : "include",
                    "--cluster", cluster.detail])
        }
    }


    func setModel(_ preset: ModelPreset) {
        guard preset.id != currentModelId else { return }
        currentModelId = preset.id  // optimistic; reload() confirms from disk
        runAction("Switching to \(preset.label)…") { $0.setModel(preset.id) }
    }

    /// Discover available models/effort levels from the CLIs (once, lazily).
    func loadModelPresets() {
        let admin = AdminClient(config: config)
        DispatchQueue.global(qos: .utility).async {
            let result = admin.run(["capabilities"])
            let raw = (result.json["presets"] as? [[String: Any]]) ?? []
            let presets: [ModelPreset] = raw.compactMap { p in
                guard let id = p["id"] as? String, let label = p["label"] as? String,
                      let backend = p["backend"] as? String else { return nil }
                return ModelPreset(id: id, label: label,
                                   family: backend == "claude_cli" ? "claude" : "codex",
                                   model: (p["model"] as? String) ?? "",
                                   thinking: (p["thinking"] as? String) ?? "")
            }
            guard !presets.isEmpty else { return }
            DispatchQueue.main.async { self.modelPresets = presets }
        }
    }

    // ---- ask: local FTS search (debounced) + server chat -------------------
    func searchMemory(_ query: String) {
        searchWork?.cancel()
        let q = query.trimmingCharacters(in: .whitespaces)
        guard q.count >= 2 else { searchHits = []; return }
        let cfg = config
        let work = DispatchWorkItem { [weak self] in
            let hits = IndexReader(dbPath: cfg.dbPath.path).search(q)
            DispatchQueue.main.async { self?.searchHits = hits }
        }
        searchWork = work
        DispatchQueue.global(qos: .userInitiated).asyncAfter(deadline: .now() + 0.22, execute: work)
    }

    func ask(_ question: String) {
        let q = question.trimmingCharacters(in: .whitespaces)
        guard !q.isEmpty, !chatPending else { return }
        chat.append(ChatMsg(role: "user", text: q))
        chat.append(ChatMsg(role: "assistant", text: "", streaming: true))
        chatPending = true
        chatActivity = "Thinking…"
        let assistantId = chat.last!.id

        func update(_ mutate: (inout ChatMsg) -> Void) {
            guard let i = chat.firstIndex(where: { $0.id == assistantId }) else { return }
            var msg = chat[i]; mutate(&msg); chat[i] = msg
        }

        var client = ChatClient(config: config)
        client.sessionKey = activeThreadKey
        streamTask = client.stream(q) { [weak self] event in
            guard let self else { return }
            switch event {
            case .tool(let label):
                self.chatActivity = label
            case .delta(let text):
                self.chatActivity = ""
                update { $0.text += text }
            case .done(let reply):
                self.chatPending = false
                self.chatActivity = ""
                update {
                    if !reply.text.isEmpty { $0.text = reply.text }
                    $0.sources = reply.sources
                    $0.toolCalls = reply.toolCalls
                    $0.streaming = false
                }
                self.loadChatThreads()  // refresh titles/preview after a turn
            case .failure(let error):
                self.chatPending = false
                self.chatActivity = ""
                // Replace the empty streaming bubble with an error bubble.
                if let i = self.chat.firstIndex(where: { $0.id == assistantId }) {
                    if self.chat[i].text.isEmpty {
                        self.chat[i] = ChatMsg(role: "error", text: error)
                    } else {
                        update { $0.streaming = false }
                    }
                }
            }
        }
    }

    // MARK: chat threads (Ask history)

    func loadChatThreads() {
        ChatClient(config: config).listThreads { [weak self] threads in
            self?.chatThreads = threads
        }
    }

    /// Start a fresh named chat. New threads get a unique key so they never
    /// collide with an existing transcript.
    func newChat() {
        streamTask?.cancel()
        chat = []
        chatPending = false
        chatActivity = ""
        showChatList = false
        activeThreadKey = "\(ChatClient.threadPrefix)-\(UUID().uuidString.prefix(8).lowercased())"
    }

    /// Reopen a saved chat: load its transcript and continue in that thread.
    func openChat(_ thread: ChatClient.ChatThread) {
        guard thread.key != activeThreadKey || chat.isEmpty else { showChatList = false; return }
        streamTask?.cancel()
        chatPending = false
        chatActivity = ""
        showChatList = false
        activeThreadKey = thread.key
        chat = []
        loadingThread = true
        var client = ChatClient(config: config)
        client.sessionKey = thread.key
        client.loadHistory(thread.key) { [weak self] msgs in
            guard let self, self.activeThreadKey == thread.key else { return }
            self.chat = msgs
            self.loadingThread = false
        }
    }

    // ---- session drill-down -----------------------------------------------
    /// Inspect a pending-review project's sessions before deciding. New projects
    /// are already indexed (detection diffs indexed projects against a baseline),
    /// so the normal index-backed detail view works.
    func openNewProject(_ np: NewProject) {
        let policy = AccessPolicy.load(config.accessConfigPath)
        openDetail(Project(source: np.source, cwd: np.cwd, sessions: np.sessions,
                           chunks: 0, updatedAt: "",
                           included: policy.included(source: np.source, cwd: np.cwd)))
    }

    func openDetail(_ project: Project) {
        detail = project
        sessions = []
        loadingSessions = true
        let cfg = config
        DispatchQueue.global(qos: .userInitiated).async {
            let rows = IndexReader(dbPath: cfg.dbPath.path).sessions(source: project.source, cwd: project.cwd)
            let sessions = rows.map {
                Session(ref: $0.ref, sessionId: $0.sessionId, title: $0.title,
                        updatedAt: $0.updatedAt, chunks: $0.chunks, origin: $0.origin)
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

    func openTrajectory(_ session: Session) {
        trajectory = session
        trajectoryEvents = []
        trajectoryTruncated = false
        loadingTrajectory = true
        let source = detail?.source ?? session.ref.split(separator: ":").first.map(String.init) ?? ""
        let admin = AdminClient(config: config)
        let ref = session.ref
        DispatchQueue.global(qos: .userInitiated).async {
            let result = admin.run(["trajectory", "--source", source, "--ref", ref, "--limit", "600"],
                                   timeout: 25)
            let raw = (result.json["events"] as? [[String: Any]]) ?? []
            let events = raw.map {
                TrajectoryEvent(kind: $0["kind"] as? String ?? "",
                                tool: $0["tool"] as? String ?? "",
                                text: $0["text"] as? String ?? "")
            }
            let truncated = (result.json["truncated"] as? Bool) ?? false
            let omitted = (result.json["omitted_large"] as? Int) ?? 0
            DispatchQueue.main.async {
                guard self.trajectory?.id == session.id else { return }
                self.trajectoryEvents = events
                self.trajectoryTruncated = truncated
                self.trajectoryOmitted = omitted
                self.loadingTrajectory = false
                if !result.ok {
                    self.lastError = (result.json["error"] as? String) ?? "could not load trajectory"
                }
            }
        }
    }

    func closeTrajectory() {
        trajectory = nil
        trajectoryEvents = []
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
                self.reload(force: true)
                if let project = self.detail { self.openDetail(project) }
            }
        }
    }
}
