import Foundation

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
    func ask(_ message: String, completion: @escaping (Result<String, Error>) -> Void) {
        var request = URLRequest(url: baseURL.appendingPathComponent("chat"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 90
        request.httpBody = try? JSONSerialization.data(withJSONObject: [
            "user": "menu",
            "actor_id": "menu",
            "session_key": Self.sessionKey,
            "message": message,
            "use_trajectory_memory": true,
            "return_sources": false,
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
            if (obj["ok"] as? Bool) == true, let text = obj["text"] as? String {
                completion(.success(text))
            } else {
                let message = (obj["error"] as? String) ?? "server returned an error"
                completion(.failure(NSError(domain: "mybot", code: 3,
                                            userInfo: [NSLocalizedDescriptionKey: message])))
            }
        }.resume()
    }

    /// POST /sessions/reset — start a fresh conversation. Fire and forget.
    func resetSession() {
        var request = URLRequest(url: baseURL.appendingPathComponent("sessions/reset"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 10
        request.httpBody = try? JSONSerialization.data(withJSONObject: [
            "user": "menu",
            "session_key": Self.sessionKey,
        ])
        URLSession.shared.dataTask(with: request).resume()
    }
}
