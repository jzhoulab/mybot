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
    /// The active chat thread. Each named chat in the Ask list is its own
    /// `menuapp-<id>` key; the server pins these (no idle rollover) so a
    /// reopened chat continues in place.
    var sessionKey: String = ChatClient.threadPrefix
    static let threadPrefix = "menuapp"

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
            "session_key": sessionKey,
            "message": message,
            "pin_session": true,
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

    /// Streamed events from POST /chat/stream.
    enum StreamEvent {
        case tool(String)          // live activity, e.g. "Searching memory: fable"
        case toolResult(label: String, summary: String, ok: Bool, hits: [ChatSource])  // that call landed
        case delta(String)         // answer text chunk
        case done(ChatReply)       // final: full text + sources + tool calls
        case failure(String)
    }

    /// POST /chat/stream (SSE). Calls onEvent on the main queue as events arrive.
    /// Returns a Task so the caller can cancel it.
    @discardableResult
    func stream(_ message: String, onEvent: @escaping (StreamEvent) -> Void) -> Task<Void, Never> {
        var request = URLRequest(url: baseURL.appendingPathComponent("chat/stream"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        request.timeoutInterval = 200
        request.httpBody = try? JSONSerialization.data(withJSONObject: [
            "user": config.ownerActorID, "actor_id": config.ownerActorID,
            "session_key": sessionKey, "message": message, "pin_session": true,
            "use_trajectory_memory": true, "return_sources": true,
        ] as [String: Any])

        return Task {
            func send(_ e: StreamEvent) { DispatchQueue.main.async { onEvent(e) } }
            do {
                let (bytes, response) = try await URLSession.shared.bytes(for: request)
                if let http = response as? HTTPURLResponse, http.statusCode >= 400 {
                    send(.failure("server error \(http.statusCode)")); return
                }
                for try await line in bytes.lines {
                    if Task.isCancelled { return }
                    guard line.hasPrefix("data:") else { continue }
                    let json = line.dropFirst(5).trimmingCharacters(in: .whitespaces)
                    guard let data = json.data(using: .utf8),
                          let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                          let kind = obj["kind"] as? String else { continue }
                    switch kind {
                    case "tool":   send(.tool((obj["label"] as? String) ?? "Working…"))
                    case "tool_result":
                        let hits = ((obj["hits"] as? [[String: Any]]) ?? []).map { h in
                            ChatSource(ref: (h["source_ref"] as? String) ?? "",
                                       sourceName: (h["source_name"] as? String) ?? "",
                                       kind: "chunk",
                                       title: (h["title"] as? String) ?? "",
                                       score: nil)
                        }
                        send(.toolResult(label: (obj["label"] as? String) ?? "Tool",
                                         summary: (obj["summary"] as? String) ?? "",
                                         ok: (obj["ok"] as? Bool) ?? true,
                                         hits: hits))
                    case "delta":  send(.delta((obj["text"] as? String) ?? ""))
                    case "error":  send(.failure((obj["error"] as? String) ?? "error"))
                    case "done":   send(.done(Self.reply(from: obj)))
                    default: break
                    }
                }
            } catch {
                if !Task.isCancelled {
                    let hint = (error as? URLError)?.code == .cannotConnectToHost
                        ? "chat server is not running — start it with run_discord_chatbot.sh"
                        : error.localizedDescription
                    send(.failure(hint))
                }
            }
        }
    }

    /// Build a ChatReply from a /chat or /chat/stream done payload.
    static func reply(from obj: [String: Any]) -> ChatReply {
        let sources = ((obj["sources"] as? [[String: Any]]) ?? []).map { s in
            ChatSource(ref: (s["source_ref"] as? String) ?? "",
                       sourceName: (s["source_name"] as? String) ?? "",
                       kind: (s["record_kind"] as? String) ?? "",
                       title: (s["title"] as? String) ?? "",
                       score: s["match_score"] as? Double)
        }
        let toolCalls = ((obj["retrieval_budget"] as? [[String: Any]]) ?? []).map { b in
            ChatToolCall(tool: (b["tool"] as? String) ?? "tool",
                         query: (b["query"] as? String) ?? "",
                         seconds: (b["seconds"] as? Double) ?? 0,
                         results: (b["result_count"] as? Int) ?? 0,
                         tokens: (b["tokens_estimate"] as? Int) ?? 0)
        }
        var text = (obj["text"] as? String) ?? ""
        if !toolCalls.isEmpty, let r = text.range(of: "\n\nRetrieval budget:") { text = String(text[..<r.lowerBound]) }
        return ChatReply(text: text, sources: sources, toolCalls: toolCalls)
    }

    /// POST /sessions/reset — clear a thread's transcript. Fire and forget.
    func resetSession() {
        var request = URLRequest(url: baseURL.appendingPathComponent("sessions/reset"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 10
        request.httpBody = try? JSONSerialization.data(withJSONObject: [
            "user": config.ownerActorID,
            "session_key": sessionKey,
        ])
        URLSession.shared.dataTask(with: request).resume()
    }

    /// One stored chat thread, for the Ask chat list.
    struct ChatThread: Identifiable, Hashable {
        var id: String { key }
        let key: String
        let title: String
        let preview: String
        let updatedAt: String
        let messageCount: Int
    }

    /// POST /sessions/list — enumerate the menu's saved chats, newest first.
    func listThreads(completion: @escaping ([ChatThread]) -> Void) {
        var request = URLRequest(url: baseURL.appendingPathComponent("sessions/list"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 10
        request.httpBody = try? JSONSerialization.data(withJSONObject: [
            "user": config.ownerActorID, "prefix": Self.threadPrefix,
        ])
        URLSession.shared.dataTask(with: request) { data, _, _ in
            let obj = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            let rawThreads = (obj?["threads"] as? [[String: Any]]) ?? []
            let threads = rawThreads.compactMap { t -> ChatThread? in
                guard let key = t["session_key"] as? String else { return nil }
                return ChatThread(
                    key: key,
                    title: (t["title"] as? String) ?? "New chat",
                    preview: (t["preview"] as? String) ?? "",
                    updatedAt: (t["updated_at"] as? String) ?? "",
                    messageCount: (t["message_count"] as? Int) ?? 0)
            }
            DispatchQueue.main.async { completion(threads) }
        }.resume()
    }

    /// POST /sessions/history — load a thread's messages (to reopen a chat).
    func loadHistory(_ key: String, completion: @escaping ([ChatMsg]) -> Void) {
        var request = URLRequest(url: baseURL.appendingPathComponent("sessions/history"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 12
        request.httpBody = try? JSONSerialization.data(withJSONObject: [
            "user": config.ownerActorID, "session_key": key, "limit": 200,
        ])
        URLSession.shared.dataTask(with: request) { data, _, _ in
            let obj = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            let raw = (obj?["history"] as? [[String: Any]]) ?? []
            let msgs: [ChatMsg] = raw.compactMap { m in
                let role = (m["role"] as? String) ?? ""
                guard role == "user" || role == "assistant" else { return nil }
                var text = (m["content"] as? String) ?? ""
                if let r = text.range(of: "\n\nRetrieval budget:") { text = String(text[..<r.lowerBound]) }
                // Rehydrate the retrieval the turn did: meta carries the same
                // retrieval_budget + sources the streamed `done` event does, so
                // a reopened chat shows its tool calls and grounding again.
                let meta = m["meta"] as? [String: Any] ?? [:]
                let reply = Self.reply(from: [
                    "text": text,
                    "sources": meta["sources"] ?? [],
                    "retrieval_budget": meta["retrieval_budget"] ?? [],
                ])
                return ChatMsg(role: role, text: text,
                               sources: reply.sources, toolCalls: reply.toolCalls)
            }
            DispatchQueue.main.async { completion(msgs) }
        }.resume()
    }

    // MARK: owner identity (onboarding)

    struct IdentityState {
        var name: String
        var aliases: [String]
        var confirmed: Bool
        var investigating: Bool
    }

    private func identityRequest(_ payload: [String: Any],
                                 completion: @escaping (IdentityState?) -> Void) {
        var request = URLRequest(url: baseURL.appendingPathComponent("owner/identity"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 15
        request.httpBody = try? JSONSerialization.data(withJSONObject: payload)
        URLSession.shared.dataTask(with: request) { data, _, _ in
            guard let data,
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  (obj["ok"] as? Bool) == true else {
                completion(nil)
                return
            }
            let identity = (obj["identity"] as? [String: Any]) ?? [:]
            completion(IdentityState(
                name: (identity["display_name"] as? String) ?? "",
                aliases: (identity["aliases"] as? [String]) ?? [],
                confirmed: (identity["confirmed"] as? Bool) ?? false,
                investigating: (obj["investigating"] as? Bool) ?? false
            ))
        }.resume()
    }

    /// Current owner identity + whether a Sherlock investigation is running.
    func fetchIdentity(completion: @escaping (IdentityState?) -> Void) {
        identityRequest(["action": "get"], completion: completion)
    }

    /// Owner confirms (or corrects) their name.
    func confirmIdentity(name: String, completion: @escaping (IdentityState?) -> Void) {
        identityRequest([
            "action": "set",
            "actor_id": config.ownerActorID,
            "display_name": name,
        ], completion: completion)
    }

    /// Kick off a background Sherlock session (deduce the owner from trajectories).
    func rediscoverIdentity(completion: @escaping (IdentityState?) -> Void) {
        identityRequest(["action": "investigate", "background": true, "save": true], completion: completion)
    }
}
