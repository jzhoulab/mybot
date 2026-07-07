import Foundation

/// Spawns the Python admin CLI (scripts/mybot_admin.py) for the write actions
/// that genuinely need Python (FTS-safe purge, reindex, embed). One-shot
/// subprocess — no long-running server.
struct AdminClient {
    let config: MybotConfig

    struct Result {
        let ok: Bool
        let json: [String: Any]
        let stderr: String
    }

    func run(_ arguments: [String]) -> Result {
        let process = Process()
        process.executableURL = config.venvPython
        process.arguments = [config.adminScript.path] + arguments
        process.currentDirectoryURL = config.repoRoot

        let stdout = Pipe()
        let stderr = Pipe()
        process.standardOutput = stdout
        process.standardError = stderr

        do {
            try process.run()
        } catch {
            return Result(ok: false, json: ["error": "launch failed: \(error.localizedDescription)"], stderr: "")
        }
        process.waitUntilExit()

        let outData = stdout.fileHandleForReading.readDataToEndOfFile()
        let errData = stderr.fileHandleForReading.readDataToEndOfFile()
        let errText = String(data: errData, encoding: .utf8) ?? ""

        let parsed = (try? JSONSerialization.jsonObject(with: outData)) as? [String: Any] ?? [:]
        let ok = (parsed["ok"] as? Bool) ?? (process.terminationStatus == 0)
        return Result(ok: ok, json: parsed, stderr: errText)
    }

    func exclude(source: String, cwds: [String]) -> Result {
        run(["exclude", "--source", source, "--kind", "workdir", "--value"] + cwds)
    }

    func include(source: String, cwds: [String], reindex: Bool) -> Result {
        var args = ["include", "--source", source, "--kind", "workdir", "--value"] + cwds
        if reindex { args.append("--reindex") }
        return run(args)
    }

    func review(source: String, cwd: String, decision: String) -> Result {
        run(["review", "--source", source, "--value", cwd, "--decision", decision])
    }

    func maintenance(_ action: String) -> Result {
        run(["maintenance", "--action", action])
    }
}
