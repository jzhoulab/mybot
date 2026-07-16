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
    let modelConfigPath: URL
    let guiURL: URL
    /// The actor that owns the imported memories (MEMORY_IMPORTED_OWNER_ID).
    /// Chat must identify as this actor or the server blocks trajectory lookup
    /// and private-scope memory search comes back empty.
    let ownerActorID: String

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
            modelConfigPath: resolvePath("MODEL_CONFIG_PATH", "state/model_config.json"),
            guiURL: URL(string: "http://127.0.0.1:\(port)/gui")!,
            ownerActorID: env["MEMORY_IMPORTED_OWNER_ID"] ?? "local-owner"
        )
    }

    static func repoRoot() -> URL {
        if let home = ProcessInfo.processInfo.environment["MYBOT_HOME"], !home.isEmpty {
            return URL(fileURLWithPath: (home as NSString).expandingTildeInPath)
        }
        return URL(fileURLWithPath: "/Users/you/Code/mybot")
    }

    /// Commit the running binary was built from (stamped by macapp/build.sh).
    static var buildSHA: String {
        (Bundle.main.infoDictionary?["MybotGitSHA"] as? String) ?? "dev"
    }

    static var buildDate: String {
        (Bundle.main.infoDictionary?["MybotBuildDate"] as? String) ?? ""
    }

    /// Current HEAD of the source repo, read straight from .git (no subprocess)
    /// so the footer can flag an installed app that has fallen behind.
    static func sourceHeadSHA() -> String? {
        let repo = repoRoot()
        guard let head = try? String(contentsOf: repo.appendingPathComponent(".git/HEAD"), encoding: .utf8)
            .trimmingCharacters(in: .whitespacesAndNewlines) else { return nil }
        if !head.hasPrefix("ref: ") { return String(head.prefix(7)) }
        let ref = String(head.dropFirst(5))
        if let sha = try? String(contentsOf: repo.appendingPathComponent(".git/\(ref)"), encoding: .utf8) {
            return String(sha.trimmingCharacters(in: .whitespacesAndNewlines).prefix(7))
        }
        if let packed = try? String(contentsOf: repo.appendingPathComponent(".git/packed-refs"), encoding: .utf8) {
            for line in packed.split(separator: "\n") where line.hasSuffix(" \(ref)") {
                return String(line.prefix(7))
            }
        }
        return nil
    }

    /// True when the app was built from a different commit than the repo HEAD.
    static var appBehindSource: Bool {
        guard let head = sourceHeadSHA() else { return false }
        return !buildSHA.hasPrefix(head)
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
