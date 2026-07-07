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

struct Session: Identifiable, Hashable {
    var id: String { ref }
    let ref: String
    let sessionId: String
    let title: String
    let updatedAt: String
    let chunks: Int

    var displayTitle: String { title.isEmpty ? "session \(shortId)" : title }
    var shortId: String { String(sessionId.suffix(8)) }

    var updatedAgo: String {
        guard let date = ISO8601DateFormatter.withFractional.date(from: updatedAt)
            ?? ISO8601DateFormatter().date(from: updatedAt) else { return "" }
        return Health.relative(from: date)
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
