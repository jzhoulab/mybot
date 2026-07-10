import Foundation

struct Project: Identifiable, Hashable {
    var id: String { "\(source)|\(cwd)" }
    let source: String
    let cwd: String
    let sessions: Int
    let chunks: Int
    let updatedAt: String
    var included: Bool

    var displayName: String {
        let name = (cwd as NSString).lastPathComponent
        return name.isEmpty ? cwd : name
    }

    var updatedDate: Date? {
        ISO8601DateFormatter().date(from: updatedAt)
            ?? ISO8601DateFormatter.withFractional.date(from: updatedAt)
    }
}

struct TrajectoryEvent: Identifiable, Hashable {
    let id = UUID()
    let kind: String   // user | assistant | thinking | tool_use | tool_result
    let tool: String
    let text: String
}

struct Session: Identifiable, Hashable {
    var id: String { ref }
    let ref: String
    let sessionId: String
    let title: String
    let updatedAt: String
    let chunks: Int
    var origin: String = ""

    var isAutomated: Bool { origin == "automated" }
    var displayTitle: String { title.isEmpty ? "session \(shortId)" : title }
    var shortId: String { String(sessionId.suffix(8)) }

    var updatedAgo: String {
        guard let date = ISO8601DateFormatter.withFractional.date(from: updatedAt)
            ?? ISO8601DateFormatter().date(from: updatedAt) else { return "" }
        return Health.relative(from: date)
    }
}

/// A selectable agent preset, discovered from the CLIs (mybot_admin capabilities)
/// — nothing is hardcoded here. id is "claude:opus:xhigh" style.
struct ModelPreset: Identifiable, Hashable {
    let id: String
    let label: String
    let family: String   // "claude" | "codex" — for the tag color
    let model: String
    let thinking: String

    /// Short label for the footer chip, e.g. "Opus · xhigh".
    var shortLabel: String { "\(model.capitalized) · \(thinking)" }

    /// Minimal fallback if discovery hasn't run yet (e.g. server/CLI slow).
    static let fallback = ModelPreset(id: "claude:opus:xhigh",
                                      label: "Claude Opus · xhigh",
                                      family: "claude", model: "opus", thinking: "xhigh")
}

/// One full-text hit in the trajectory index, grouped by session.
struct SearchHit: Identifiable, Hashable {
    var id: String { ref }
    let ref: String
    let source: String
    let title: String
    let cwd: String
    let snippet: String
    let updatedAt: String

    var displayTitle: String { title.isEmpty ? "session \(ref.suffix(8))" : title }
    var projectName: String {
        let name = (cwd as NSString).lastPathComponent
        return name.isEmpty ? cwd : name
    }
    var updatedAgo: String {
        guard let date = ISO8601DateFormatter.withFractional.date(from: updatedAt)
            ?? ISO8601DateFormatter().date(from: updatedAt) else { return "" }
        return Health.relative(from: date)
    }
}

/// One turn in the in-app chat with mybot. Assistant turns carry the
/// internals (sources used + retrieval steps) that Discord replies hide.
struct ChatMsg: Identifiable, Hashable {
    let id = UUID()
    let role: String   // "user" | "assistant" | "error"
    var text: String
    var sources: [ChatSource] = []
    var toolCalls: [ChatToolCall] = []
    var streaming: Bool = false   // assistant bubble still receiving deltas
}

/// One automated-session cluster (origin_detail) with its index count and
/// whether the access policy currently excludes it.
struct AutomatedCluster: Identifiable, Hashable {
    var id: String { detail }
    let detail: String
    var count: Int
    var excluded: Bool

    static let order = ["subagent", "orchestrated", "sdk", "exec", "no-user-turns"]

    var displayName: String {
        switch detail {
        case "subagent": return "Subagent runs"
        case "orchestrated": return "Orchestrated (worktree agents)"
        case "sdk": return "Claude SDK / headless"
        case "exec": return "Codex exec"
        case "no-user-turns": return "No user turns"
        default: return detail
        }
    }
}

struct NewProject: Identifiable, Hashable {
    var id: String { "\(source)|\(cwd)" }
    let source: String
    let cwd: String
    let sessions: Int

    var displayName: String {
        let name = (cwd as NSString).lastPathComponent
        return name.isEmpty ? cwd : name
    }
}

struct Health {
    var totalChunks = 0
    var embeddedChunks = 0
    var indexBytes: Int64 = 0
    var memoryBytes: Int64 = 0
    var latestIndexedAt = ""
    var latestSourceMtime: TimeInterval = 0
    var projectCount = 0
    var automatedSessions = 0

    var coverage: Double { totalChunks == 0 ? 0 : Double(embeddedChunks) / Double(totalChunks) }

    /// Stale when the newest source trajectory on disk is newer than the newest
    /// indexed chunk (there is un-indexed activity).
    var stale: Bool {
        guard latestSourceMtime > 0, !latestIndexedAt.isEmpty else { return false }
        let indexed = ISO8601DateFormatter.withFractional.date(from: latestIndexedAt)
            ?? ISO8601DateFormatter().date(from: latestIndexedAt)
        guard let indexed else { return false }
        return latestSourceMtime > indexed.timeIntervalSince1970 + 60
    }

    var indexedAgo: String {
        let fmt = ISO8601DateFormatter.withFractional
        guard let date = fmt.date(from: latestIndexedAt)
            ?? ISO8601DateFormatter().date(from: latestIndexedAt) else { return "never" }
        return Health.relative(from: date)
    }

    static func relative(from date: Date) -> String {
        let seconds = Date().timeIntervalSince(date)
        if seconds < 90 { return "just now" }
        if seconds < 3600 { return "\(Int(seconds / 60))m ago" }
        if seconds < 86_400 { return "\(Int(seconds / 3600))h ago" }
        return "\(Int(seconds / 86_400))d ago"
    }
}

extension ISO8601DateFormatter {
    static let withFractional: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()
}

func formatBytes(_ value: Int64) -> String {
    let n = Double(value)
    if n < 1024 { return "\(value) B" }
    let units = ["KB", "MB", "GB", "TB"]
    var scaled = n / 1024
    var i = 0
    while scaled >= 1024 && i < units.count - 1 { scaled /= 1024; i += 1 }
    return String(format: "%.1f %@", scaled, units[i])
}
