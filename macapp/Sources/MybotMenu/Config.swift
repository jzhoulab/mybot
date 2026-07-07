import Foundation

/// Resolves where the mybot repo, venv, index DB, and access config live so the
/// app can read SQLite directly and shell out to the Python admin CLI. No
/// running webserver is required.
struct MybotConfig {
    let repoRoot: URL
    let venvPython: URL
    let adminScript: URL
    let dbPath: URL
    let accessConfigPath: URL
    let memoryDbPath: URL
    let guiURL: URL

    static let shared = MybotConfig.resolve()

    static func resolve() -> MybotConfig {
        let repo = MybotConfig.repoRoot()
        let env = MybotConfig.parseDotEnv(repo.appendingPathComponent(".env"))

        func resolvePath(_ key: String, _ fallback: String) -> URL {
            if let raw = env[key], !raw.isEmpty {
                let expanded = (raw as NSString).expandingTildeInPath
                return expanded.hasPrefix("/")
                    ? URL(fileURLWithPath: expanded)
                    : repo.appendingPathComponent(expanded)
            }
            return repo.appendingPathComponent(fallback)
        }

        let port = env["CHATBOT_PORT"] ?? env["PORT"] ?? "8788"
        return MybotConfig(
            repoRoot: repo,
            venvPython: repo.appendingPathComponent(".venv/bin/python"),
            adminScript: repo.appendingPathComponent("scripts/mybot_admin.py"),
            dbPath: resolvePath("TRAJECTORY_INDEX_DB_PATH", "state/trajectory_index.sqlite3"),
            accessConfigPath: resolvePath("TRAJECTORY_ACCESS_CONFIG_PATH", "config/access.json"),
            memoryDbPath: resolvePath("MEMORY_DB_PATH", "state/semantic_memory.sqlite3"),
            guiURL: URL(string: "http://127.0.0.1:\(port)/gui")!
        )
    }

    static func repoRoot() -> URL {
        if let home = ProcessInfo.processInfo.environment["MYBOT_HOME"], !home.isEmpty {
            return URL(fileURLWithPath: (home as NSString).expandingTildeInPath)
        }
        return URL(fileURLWithPath: "/Users/you/Code/mybot")
    }

    static func parseDotEnv(_ url: URL) -> [String: String] {
        guard let text = try? String(contentsOf: url, encoding: .utf8) else { return [:] }
        var out: [String: String] = [:]
        for rawLine in text.split(separator: "\n", omittingEmptySubsequences: true) {
            let line = rawLine.trimmingCharacters(in: .whitespaces)
            if line.isEmpty || line.hasPrefix("#") || !line.contains("=") { continue }
            let parts = line.split(separator: "=", maxSplits: 1, omittingEmptySubsequences: false)
            let key = parts[0].trimmingCharacters(in: .whitespaces)
            var value = parts.count > 1 ? String(parts[1]) : ""
            value = value.trimmingCharacters(in: .whitespaces)
            value = value.trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
            if !key.isEmpty { out[key] = value }
        }
        return out
    }
}
