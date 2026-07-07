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

    func projects() -> [(source: String, cwd: String, sessions: Int, chunks: Int, updatedAt: String)] {
        guard let db = openReadOnly() else { return [] }
        defer { sqlite3_close(db) }
        var out: [(String, String, Int, Int, String)] = []
        var stmt: OpaquePointer?
        let sql = """
            SELECT source_name, cwd, COUNT(DISTINCT source_ref), COUNT(*), MAX(updated_at)
            FROM trajectory_chunks
            GROUP BY source_name, cwd
            ORDER BY COUNT(DISTINCT source_ref) DESC
            """
        if sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK {
            while sqlite3_step(stmt) == SQLITE_ROW {
                out.append((
                    text(stmt, 0),
                    text(stmt, 1),
                    Int(sqlite3_column_int(stmt, 2)),
                    Int(sqlite3_column_int(stmt, 3)),
                    text(stmt, 4)
                ))
            }
        }
        sqlite3_finalize(stmt)
        return out.map { (source: $0.0, cwd: $0.1, sessions: $0.2, chunks: $0.3, updatedAt: $0.4) }
    }

    func sessions(source: String, cwd: String)
        -> [(ref: String, sessionId: String, title: String, updatedAt: String, chunks: Int)] {
        guard let db = openReadOnly() else { return [] }
        defer { sqlite3_close(db) }
        var out: [(String, String, String, String, Int)] = []
        var stmt: OpaquePointer?
        let sql = """
            SELECT source_ref, MAX(session_id), MAX(title), MAX(updated_at), COUNT(*)
            FROM trajectory_chunks
            WHERE source_name = ? AND cwd = ?
            GROUP BY source_ref
            ORDER BY MAX(updated_at) DESC
            """
        if sqlite3_prepare_v2(db, sql, -1, &stmt, nil) == SQLITE_OK {
            source.withCString { sPtr in
                cwd.withCString { cPtr in
                    sqlite3_bind_text(stmt, 1, sPtr, -1, nil)
                    sqlite3_bind_text(stmt, 2, cPtr, -1, nil)
                    while sqlite3_step(stmt) == SQLITE_ROW {
                        out.append((
                            text(stmt, 0), text(stmt, 1), text(stmt, 2), text(stmt, 3),
                            Int(sqlite3_column_int(stmt, 4))
                        ))
                    }
                }
            }
        }
        sqlite3_finalize(stmt)
        return out.map { (ref: $0.0, sessionId: $0.1, title: $0.2, updatedAt: $0.3, chunks: $0.4) }
    }

    private func scalarInt(_ db: OpaquePointer, _ sql: String) -> Int {
        var stmt: OpaquePointer?
        var value = 0
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

    /// Cheap health snapshot: two counts and a max — indexed columns, instant.
    func health() -> (total: Int, embedded: Int, indexedAt: String) {
        guard let db = openReadOnly() else { return (0, 0, "") }
        defer { sqlite3_close(db) }
        let total = scalarInt(db, "SELECT COUNT(*) FROM trajectory_chunks")
        let embedded = scalarInt(
            db,
            "SELECT COUNT(*) FROM trajectory_chunks WHERE embedding_blob IS NOT NULL OR embedding_json != '[]'"
        )
        let indexedAt = scalarText(db, "SELECT MAX(indexed_at) FROM trajectory_chunks")
        return (total, embedded, indexedAt)
    }
}
