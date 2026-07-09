import Foundation

/// One memory/trajectory source the server used to ground a reply.
struct ChatSource: Identifiable, Hashable {
    let id = UUID()
    let ref: String        // e.g. "codex:0199…" — openable in the trajectory viewer
    let sourceName: String // codex | claude | session | …
    let kind: String       // chunk | session | session_summary | …
    let title: String
    let score: Double?

    var isOpenable: Bool { ["codex", "claude"].contains(sourceName) && ref.contains(":") }
}

/// One retrieval step the model took while answering.
struct ChatToolCall: Identifiable, Hashable {
    let id = UUID()
    let tool: String
    let query: String
    let seconds: Double
    let results: Int
    let tokens: Int
}

/// The full server reply, including the internals the Discord bridge discards.
struct ChatReply {
    let text: String
    let sources: [ChatSource]
    let toolCalls: [ChatToolCall]
}

/// Talks to the local chat server (/chat) for the in-app "Ask mybot" feature.
/// This is the ONE place the menu app touches the webserver — always async,
/// never on the UI thread, and failure degrades to a visible error bubble
/// (search stays fully local either way).
struct ChatClient {
    let config: MybotConfig
    static let sessionKey = "menuapp"

    private var baseURL: URL {
        var comps = URLComponents(url: config.guiURL, resolvingAgainstBaseURL: false)!
        comps.path = ""
        return comps.url!
    }

    /// POST /chat. Completion is invoked on a background queue.
    /// Identifies as the memory owner (MEMORY_IMPORTED_OWNER_ID) — any other
    /// actor is blocked from trajectory lookup and sees an empty memory.
    func ask(_ message: String, completion: @escaping (Result<ChatReply, Error>) -> Void) {
        var request = URLRequest(url: baseURL.appendingPathComponent("chat"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 150  // hard questions take ~2min of budgeted retrieval
        request.httpBody = try? JSONSerialization.data(withJSONObject: [
            "user": config.ownerActorID,
            "actor_id": config.ownerActorID,
            "session_key": Self.sessionKey,
            "message": message,
            "use_trajectory_memory": true,
            "return_sources": true,
        ] as [String: Any])

        URLSession.shared.dataTask(with: request) { data, _, error in
            if let error {
                let hint = (error as? URLError)?.code == .cannotConnectToHost
                    ? "chat server is not running — start it with run_discord_chatbot.sh"
                    : error.localizedDescription
                completion(.failure(NSError(domain: "mybot", code: 1,
                                            userInfo: [NSLocalizedDescriptionKey: hint])))
                return
            }
            guard let data,
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                completion(.failure(NSError(domain: "mybot", code: 2,
                                            userInfo: [NSLocalizedDescriptionKey: "unreadable server response"])))
                return
            }
            guard (obj["ok"] as? Bool) == true, let text = obj["text"] as? String else {
                let message = (obj["error"] as? String) ?? "server returned an error"
                completion(.failure(NSError(domain: "mybot", code: 3,
                                            userInfo: [NSLocalizedDescriptionKey: message])))
                return
            }

            let sources = ((obj["sources"] as? [[String: Any]]) ?? []).map { s in
                ChatSource(
                    ref: (s["source_ref"] as? String) ?? "",
                    sourceName: (s["source_name"] as? String) ?? "",
                    kind: (s["record_kind"] as? String) ?? "",
                    title: (s["title"] as? String) ?? "",
                    score: s["match_score"] as? Double
                )
            }
            let toolCalls = ((obj["retrieval_budget"] as? [[String: Any]]) ?? []).map { b in
                ChatToolCall(
                    tool: (b["tool"] as? String) ?? "tool",
                    query: (b["query"] as? String) ?? "",
                    seconds: (b["seconds"] as? Double) ?? 0,
                    results: (b["result_count"] as? Int) ?? 0,
                    tokens: (b["tokens_estimate"] as? Int) ?? 0
                )
            }
            // The server appends a plain-text "Retrieval budget: …" footer for
            // clients that can't show structure. We can — drop the text version.
            var clean = text
            if !toolCalls.isEmpty,
               let range = clean.range(of: "\n\nRetrieval budget:") {
                clean = String(clean[..<range.lowerBound])
            }
            completion(.success(ChatReply(text: clean, sources: sources, toolCalls: toolCalls)))
        }.resume()
    }

    /// POST /sessions/reset — start a fresh conversation. Fire and forget.
    func resetSession() {
        var request = URLRequest(url: baseURL.appendingPathComponent("sessions/reset"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 10
        request.httpBody = try? JSONSerialization.data(withJSONObject: [
            "user": config.ownerActorID,
            "session_key": Self.sessionKey,
        ])
        URLSession.shared.dataTask(with: request).resume()
    }
}
