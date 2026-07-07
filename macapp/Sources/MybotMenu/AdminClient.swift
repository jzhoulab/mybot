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

    func run(_ arguments: [String], timeout: TimeInterval? = nil) -> Result {
        let process = Process()
        process.executableURL = config.venvPython
        process.arguments = [config.adminScript.path] + arguments
        process.currentDirectoryURL = config.repoRoot

        let outPipe = Pipe()
        let errPipe = Pipe()
        process.standardOutput = outPipe
        process.standardError = errPipe

        // Drain both pipes concurrently: a full stderr buffer must never block the
        // stdout read (classic Process deadlock) and vice-versa.
        var outData = Data()
        var errData = Data()
        let ioGroup = DispatchGroup()
        let ioQueue = DispatchQueue(label: "mybot.admin.io", attributes: .concurrent)
        ioGroup.enter()
        ioQueue.async { outData = outPipe.fileHandleForReading.readDataToEndOfFile(); ioGroup.leave() }
        ioGroup.enter()
        ioQueue.async { errData = errPipe.fileHandleForReading.readDataToEndOfFile(); ioGroup.leave() }

        do {
            try process.run()
        } catch {
            return Result(ok: false, json: ["error": "launch failed: \(error.localizedDescription)"], stderr: "")
        }

        if let timeout {
            let exited = DispatchSemaphore(value: 0)
            DispatchQueue.global().async { process.waitUntilExit(); exited.signal() }
            if exited.wait(timeout: .now() + timeout) == .timedOut {
                process.terminate()                      // unblocks the pipe reads
                _ = exited.wait(timeout: .now() + 3)
                ioGroup.wait()
                return Result(ok: false, json: ["error": "timed out after \(Int(timeout))s"], stderr: "")
            }
        } else {
            process.waitUntilExit()
        }
        ioGroup.wait()

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
