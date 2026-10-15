import Foundation

/// Requests to the local mybot server carry its API key (~/.mybot/api-key,
/// created by the server on first start). Answer agents can't read that file,
/// so holding it is what marks the menu app as a trusted client.
enum MybotHTTP {
    private static var cached: (key: String, session: URLSession)?

    static var session: URLSession {
        let key = (try? String(contentsOfFile: keyPath, encoding: .utf8))?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if let cached, cached.key == key { return cached.session }
        let configuration = URLSessionConfiguration.default
        if !key.isEmpty {
            configuration.httpAdditionalHeaders = ["Authorization": "Bearer \(key)"]
        }
        let session = URLSession(configuration: configuration)
        cached = (key, session)
        return session
    }

    static var keyPath: String {
        ProcessInfo.processInfo.environment["MYBOT_API_KEY_PATH"]
            ?? (NSHomeDirectory() as NSString).appendingPathComponent(".mybot/api-key")
    }
}
