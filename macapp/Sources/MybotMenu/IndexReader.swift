import Foundation
import SQLite3

/// Direct, read-only access to the SQLite trajectory index. This is what makes
/// the app snappy and never "offline": all displayed data comes from local
/// files, not an HTTP round-trip to the busy chat server.
final class IndexReader {
    let dbPath: String

    init(dbPath: String) { self.dbPath = dbPath }

    private func openReadOnly() -> OpaquePointer? {
        var db: OpaquePointer?
        let flags = SQLITE_OPEN_READONLY | SQLITE_OPEN_FULLMUTEX
        if sqlite3_open_v2(dbPath, &db, flags, nil) != SQLITE_OK {
            if let db { sqlite3_close(db) }
            return nil
        }
        sqlite3_busy_timeout(db, 2000)
        return db
    }

    private func text(_ stmt: OpaquePointer?, _ col: Int32) -> String {
        guard let c = sqlite3_column_text(stmt, col) else { return "" }
        return String(cString: c)
    }

    /// nil = the read failed (db locked mid-rebuild, etc.) — callers must treat
    /// that as "unknown", NOT as an empty index.
    /// App-chat sessions (Codex app / home dir) each get a generated per-chat
    /// cwd; hundreds of one-session rows would drown the real projects, so
    /// they collapse into a single aggregate row per source.
    static let appChatsRoot = NSHomeDirectory() + "/Documents/Codex"

    func projects() -> [(source: String, cwd: String, sessions: Int, chunks: Int, updatedAt: String)]? {
        guard let db = openReadOnly() else { return nil }
        defer { sqlite3_close(db) }
        var out: [(String, String, Int, Int, String)] = []
        var stmt: OpaquePointer?
        let root = IndexReader.appChatsRoot
        let sql = """
            SELECT source_name,
                   CASE WHEN cwd LIKE '\(root)/%' THEN '\(root)' ELSE cwd END AS project_cwd,
                   COUNT(DISTINCT source_ref), COUNT(*), MAX(updated_at)
            FROM trajectory_chunks
            GROUP BY source_name, project_cwd
            ORDER BY COUNT(DISTINCT source_ref) DESC
            """
        guard sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK else { return nil }
        var rc = sqlite3_step(stmt)
        while rc == SQLITE_ROW {
            out.append((
                text(stmt, 0),
                text(stmt, 1),
                Int(sqlite3_column_int(stmt, 2)),
                Int(sqlite3_column_int(stmt, 3)),
                text(stmt, 4)
            ))
            rc = sqlite3_step(stmt)
        }
        sqlite3_finalize(stmt)
        if rc != SQLITE_DONE { return nil }  // SQLITE_BUSY mid-scan — partial data
        return out.map { (source: $0.0, cwd: $0.1, sessions: $0.2, chunks: $0.3, updatedAt: $0.4) }
    }

    func sessions(source: String, cwd: String)
        -> [(ref: String, sessionId: String, title: String, updatedAt: String, chunks: Int, origin: String)] {
        guard let db = openReadOnly() else { return [] }
        defer { sqlite3_close(db) }
        var out: [(String, String, String, String, Int, String)] = []
        var stmt: OpaquePointer?
        let sql = """
            SELECT source_ref, MAX(session_id), MAX(title), MAX(updated_at), COUNT(*),
                   MAX(json_extract(metadata_json, '$.origin'))
            FROM trajectory_chunks
            WHERE source_name = ?
              AND (cwd = ? OR (? = '\(IndexReader.appChatsRoot)' AND cwd LIKE ? || '/%'))
            GROUP BY source_ref
            ORDER BY MAX(updated_at) DESC
            """
        if sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK {
            source.withCString { sPtr in
                cwd.withCString { cPtr in
                    sqlite3_bind_text(stmt, 1, sPtr, -1, nil)
                    sqlite3_bind_text(stmt, 2, cPtr, -1, nil)
                    sqlite3_bind_text(stmt, 3, cPtr, -1, nil)
                    sqlite3_bind_text(stmt, 4, cPtr, -1, nil)
                    while sqlite3_step(stmt) == SQLITE_ROW {
                        out.append((
                            text(stmt, 0), text(stmt, 1), text(stmt, 2), text(stmt, 3),
                            Int(sqlite3_column_int(stmt, 4)), text(stmt, 5)
                        ))
                    }
                }
            }
        }
        sqlite3_finalize(stmt)
        return out.map { (ref: $0.0, sessionId: $0.1, title: $0.2, updatedAt: $0.3, chunks: $0.4, origin: $0.5) }
    }

    private func scalarInt(_ db: OpaquePointer, _ sql: String) -> Int? {
        var stmt: OpaquePointer?
        var value: Int?
        if sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK,
           sqlite3_step(stmt) == SQLITE_ROW {
            value = Int(sqlite3_column_int64(stmt, 0))
        }
        sqlite3_finalize(stmt)
        return value
    }

    private func scalarText(_ db: OpaquePointer, _ sql: String) -> String {
        var stmt: OpaquePointer?
        var value = ""
        if sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK,
           sqlite3_step(stmt) == SQLITE_ROW {
            value = text(stmt, 0)
        }
        sqlite3_finalize(stmt)
        return value
    }

    /// Full-text search over the index (FTS5), grouped by session, best match
    /// first. Local and instant — no server involved.
    func search(_ query: String, limit: Int = 12) -> [SearchHit] {
        let tokens = query.split(whereSeparator: { $0.isWhitespace })
            .map { $0.replacingOccurrences(of: "\"", with: "\"\"") }
            .filter { !$0.isEmpty }
        guard !tokens.isEmpty, let db = openReadOnly() else { return [] }
        defer { sqlite3_close(db) }
        let match = tokens.map { "\"\($0)\"*" }.joined(separator: " ")

        // snippet() can't be combined with GROUP BY, so rank the top chunks
        // and keep the best chunk per session here.
        var out: [SearchHit] = []
        var seen = Set<String>()
        var stmt: OpaquePointer?
        let sql = """
            SELECT c.source_ref, c.source_name, c.title, c.cwd, c.updated_at,
                   snippet(trajectory_chunks_fts, 2, '', '', ' … ', 14)
            FROM trajectory_chunks_fts f
            JOIN trajectory_chunks c ON c.id = f.rowid
            WHERE trajectory_chunks_fts MATCH ?
            ORDER BY f.rank
            LIMIT 60
            """
        if sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK {
            match.withCString { mPtr in
                sqlite3_bind_text(stmt, 1, mPtr, -1, nil)
                while sqlite3_step(stmt) == SQLITE_ROW, out.count < limit {
                    let ref = text(stmt, 0)
                    guard seen.insert(ref).inserted else { continue }
                    out.append(SearchHit(
                        ref: ref,
                        source: text(stmt, 1),
                        title: text(stmt, 2),
                        cwd: text(stmt, 3),
                        snippet: text(stmt, 5)
                            .replacingOccurrences(of: "\n", with: " "),
                        updatedAt: text(stmt, 4)
                    ))
                }
            }
        }
        sqlite3_finalize(stmt)
        return out
    }

    /// Automated sessions per cluster (origin_detail), e.g. sdk/exec/subagent.
    func automatedClusters() -> [String: Int] {
        guard let db = openReadOnly() else { return [:] }
        defer { sqlite3_close(db) }
        var out: [String: Int] = [:]
        var stmt: OpaquePointer?
        let sql = """
            SELECT COALESCE(json_extract(metadata_json, '$.origin_detail'), ''),
                   COUNT(DISTINCT source_ref)
            FROM trajectory_chunks
            WHERE json_extract(metadata_json, '$.origin') = 'automated'
            GROUP BY 1
            """
        if sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK {
            while sqlite3_step(stmt) == SQLITE_ROW {
                let detail = text(stmt, 0)
                guard !detail.isEmpty else { continue }  // legacy rows: run classify to backfill
                out[detail, default: 0] += Int(sqlite3_column_int(stmt, 1))
            }
        }
        sqlite3_finalize(stmt)
        return out
    }

    /// Cheap health snapshot: a few counts and a max — instant.
    /// nil = read failed (locked/busy) — unknown, not "empty index".
    func health() -> (total: Int, embedded: Int, indexedAt: String, automated: Int)? {
        guard let db = openReadOnly() else { return nil }
        defer { sqlite3_close(db) }
        guard let total = scalarInt(db, "SELECT COUNT(*) FROM trajectory_chunks"),
              let embedded = scalarInt(
                  db,
                  "SELECT COUNT(*) FROM trajectory_chunks WHERE embedding_blob IS NOT NULL OR embedding_json != '[]'"
              ),
              let automated = scalarInt(
                  db,
                  "SELECT COUNT(DISTINCT source_ref) FROM trajectory_chunks WHERE json_extract(metadata_json, '$.origin') = 'automated'"
              )
        else { return nil }
        let indexedAt = scalarText(db, "SELECT MAX(indexed_at) FROM trajectory_chunks")
        return (total, embedded, indexedAt, automated)
    }
}
