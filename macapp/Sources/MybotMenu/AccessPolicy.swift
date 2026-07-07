import Foundation

/// A read-only view of config/access.json — just enough to decide which indexed
/// projects are currently included. The authoritative mutation (and FTS-safe
/// purge) is always done by the Python admin CLI; this is a display convenience.
struct AccessPolicy {
    struct Root {
        let visibility: String
        let included: [String]
        let excluded: [String]
    }
    var rootsBySource: [String: [Root]] = [:]

    static func load(_ url: URL) -> AccessPolicy {
        var policy = AccessPolicy()
        guard
            let data = try? Data(contentsOf: url),
            let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let sources = obj["sources"] as? [String: Any]
        else { return policy }

        for (source, value) in sources {
            guard
                let sourceValue = value as? [String: Any],
                let roots = sourceValue["roots"] as? [[String: Any]]
            else { continue }
            policy.rootsBySource[source] = roots.map { root in
                Root(
                    visibility: (root["visibility_mode"] as? String) ?? "blacklist",
                    included: (root["included_workdirs"] as? [String]) ?? [],
                    excluded: (root["excluded_workdirs"] as? [String]) ?? []
                )
            }
        }
        return policy
    }

    func included(source: String, cwd: String) -> Bool {
        guard let roots = rootsBySource[source], let root = roots.first else { return true }
        if root.visibility == "whitelist" {
            return root.included.contains { matches(cwd, $0) }
        }
        return !root.excluded.contains { matches(cwd, $0) }
    }

    private func matches(_ cwd: String, _ rule: String) -> Bool {
        cwd == rule || cwd.hasPrefix(rule + "/")
    }

    /// Excluded workdirs (source, cwd). These have been purged from the index, so
    /// they only exist here — surface them so the Excluded tab isn't empty and
    /// they can be re-included.
    func excludedWorkdirs() -> [(source: String, cwd: String)] {
        var out: [(String, String)] = []
        for (source, roots) in rootsBySource {
            for root in roots {
                for cwd in root.excluded { out.append((source, cwd)) }
            }
        }
        return out
    }
}

/// Pending "new project" review items, read straight from known_projects.json
/// (the file both the server and the admin CLI maintain).
struct KnownProjects {
    static func pending(_ url: URL) -> [NewProject] {
        guard
            let data = try? Data(contentsOf: url),
            let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let projects = obj["projects"] as? [String: Any]
        else { return [] }

        var pending: [(String, NewProject)] = []
        for (_, value) in projects {
            guard let entry = value as? [String: Any] else { continue }
            let reviewed = (entry["reviewed"] as? Bool) ?? false
            if reviewed { continue }
            let np = NewProject(
                source: (entry["source_name"] as? String) ?? "",
                cwd: (entry["cwd"] as? String) ?? "",
                sessions: (entry["sessions"] as? Int) ?? 0
            )
            let firstSeen = (entry["first_seen"] as? String) ?? ""
            pending.append((firstSeen, np))
        }
        return pending.sorted { $0.0 > $1.0 }.map { $0.1 }
    }
}
