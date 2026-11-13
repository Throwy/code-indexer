"""
Database layer for code-indexer.
Schema:
  files       — tracked source files with hash for change detection
  symbols     — extracted functions/classes/methods
  symbols_fts — FTS5 virtual table for full-text search
"""

import sqlite3
from pathlib import Path
from typing import Optional

from .log import logger


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS files (
    id            INTEGER PRIMARY KEY,
    path          TEXT    NOT NULL UNIQUE,
    last_modified REAL    NOT NULL,
    sha256        TEXT    NOT NULL,
    language      TEXT    NOT NULL,
    repo          TEXT    -- git repo (relative to index root) when indexing a parent dir of repos
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS symbols (
    id           INTEGER PRIMARY KEY,
    file_id      INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    name         TEXT    NOT NULL,
    kind         TEXT    NOT NULL,  -- 'function' | 'class' | 'method' | 'interface' | 'struct'
    signature    TEXT,
    docstring    TEXT,
    body         TEXT,
    start_line   INTEGER NOT NULL,
    end_line     INTEGER NOT NULL,
    base_classes TEXT    -- JSON array of parent class / implemented interface names
);

CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_id);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);

CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(
    name,
    kind,
    signature,
    docstring,
    body,
    content='symbols',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS symbols_ai AFTER INSERT ON symbols BEGIN
    INSERT INTO symbols_fts(rowid, name, kind, signature, docstring, body)
    VALUES (new.id, new.name, new.kind, new.signature, new.docstring, new.body);
END;

CREATE TRIGGER IF NOT EXISTS symbols_ad AFTER DELETE ON symbols BEGIN
    INSERT INTO symbols_fts(symbols_fts, rowid, name, kind, signature, docstring, body)
    VALUES ('delete', old.id, old.name, old.kind, old.signature, old.docstring, old.body);
END;

CREATE TRIGGER IF NOT EXISTS symbols_au AFTER UPDATE ON symbols BEGIN
    INSERT INTO symbols_fts(symbols_fts, rowid, name, kind, signature, docstring, body)
    VALUES ('delete', old.id, old.name, old.kind, old.signature, old.docstring, old.body);
    INSERT INTO symbols_fts(rowid, name, kind, signature, docstring, body)
    VALUES (new.id, new.name, new.kind, new.signature, new.docstring, new.body);
END;

"""


class DB:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        logger.debug("opening database %s", db_path)
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._migrate()

    def _migrate(self):
        # Add columns to databases created before these features
        for stmt in (
            "ALTER TABLE symbols ADD COLUMN base_classes TEXT",
            "ALTER TABLE files ADD COLUMN repo TEXT",
        ):
            try:
                self.conn.execute(stmt)
                self.conn.commit()
                logger.debug("migration applied: %s", stmt)
            except Exception:
                pass  # Column already exists
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_files_repo ON files(repo)")
        self.conn.commit()

    # ── Index metadata ───────────────────────────────────────────────────────

    def set_meta(self, key: str, value: str):
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def get_meta(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    # ── File tracking ────────────────────────────────────────────────────────

    def get_file(self, path: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM files WHERE path = ?", (path,)
        ).fetchone()

    def upsert_file(self, path: str, last_modified: float, sha256: str, language: str,
                    repo: Optional[str] = None) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO files (path, last_modified, sha256, language, repo)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                last_modified = excluded.last_modified,
                sha256        = excluded.sha256,
                language      = excluded.language,
                repo          = excluded.repo
            RETURNING id
            """,
            (path, last_modified, sha256, language, repo),
        )
        row = cur.fetchone()
        self.conn.commit()
        return row[0]

    def update_repo(self, path: str, repo: Optional[str]):
        self.conn.execute("UPDATE files SET repo = ? WHERE path = ?", (repo, path))
        self.conn.commit()

    def list_repos(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT f.repo AS repo, COUNT(DISTINCT f.id) AS files, COUNT(s.id) AS symbols "
            "FROM files f LEFT JOIN symbols s ON s.file_id = f.id "
            "WHERE f.repo IS NOT NULL GROUP BY f.repo ORDER BY f.repo"
        ).fetchall()

    def delete_symbols_for_file(self, file_id: int):
        self.conn.execute("DELETE FROM symbols WHERE file_id = ?", (file_id,))
        self.conn.commit()

    def all_indexed_paths(self) -> list[str]:
        rows = self.conn.execute("SELECT path FROM files").fetchall()
        return [r[0] for r in rows]

    def delete_file(self, path: str):
        self.conn.execute("DELETE FROM files WHERE path = ?", (path,))
        self.conn.commit()

    # ── Symbol storage ───────────────────────────────────────────────────────

    def insert_symbol(
        self,
        file_id: int,
        name: str,
        kind: str,
        signature: Optional[str],
        docstring: Optional[str],
        body: Optional[str],
        start_line: int,
        end_line: int,
        base_classes: Optional[list] = None,
    ) -> int:
        import json
        bases_json = json.dumps(base_classes) if base_classes else None
        cur = self.conn.execute(
            """
            INSERT INTO symbols (file_id, name, kind, signature, docstring, body, start_line, end_line, base_classes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (file_id, name, kind, signature, docstring, body, start_line, end_line, bases_json),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_symbols_by_ids(self, ids: list[int]) -> list[sqlite3.Row]:
        placeholders = ",".join("?" * len(ids))
        return self.conn.execute(
            f"SELECT s.*, f.path FROM symbols s JOIN files f ON f.id = s.file_id WHERE s.id IN ({placeholders})",
            ids,
        ).fetchall()

    # ── Full-text search ─────────────────────────────────────────────────────

    def fts_search(self, query: str, limit: int = 20, repo: Optional[str] = None) -> list[sqlite3.Row]:
        sql = (
            "SELECT s.*, f.path, f.repo, rank "
            "FROM symbols_fts "
            "JOIN symbols s ON s.id = symbols_fts.rowid "
            "JOIN files f ON f.id = s.file_id "
            "WHERE symbols_fts MATCH ?"
        )
        params: list = [query]
        if repo:
            sql += " AND f.repo = ?"
            params.append(repo)
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        return self.conn.execute(sql, params).fetchall()

    # ── Navigation queries ───────────────────────────────────────────────────

    def goto_symbol(self, name: str, kind: Optional[str] = None, repo: Optional[str] = None) -> list[sqlite3.Row]:
        """Find all definitions of a symbol by exact name."""
        sql = ("SELECT s.*, f.path, f.repo FROM symbols s JOIN files f ON f.id = s.file_id "
               "WHERE s.name = ?")
        params: list = [name]
        if kind:
            sql += " AND s.kind = ?"
            params.append(kind)
        if repo:
            sql += " AND f.repo = ?"
            params.append(repo)
        sql += " ORDER BY f.path, s.start_line"
        return self.conn.execute(sql, params).fetchall()

    def find_implementations(self, name: str, repo: Optional[str] = None) -> list[sqlite3.Row]:
        """Find all types (classes/structs) that list `name` in their base_classes."""
        sql = ("SELECT s.*, f.path, f.repo FROM symbols s JOIN files f ON f.id = s.file_id "
               "WHERE s.base_classes IS NOT NULL AND s.base_classes LIKE ?")
        params: list = [f'%"{name}"%']
        if repo:
            sql += " AND f.repo = ?"
            params.append(repo)
        sql += " ORDER BY f.path, s.start_line"
        return self.conn.execute(sql, params).fetchall()

    def find_usages(self, name: str, limit: int = 20, repo: Optional[str] = None) -> list[sqlite3.Row]:
        """Approximate: find symbols whose body mentions `name` (likely callers/users)."""
        sql = ("SELECT s.*, f.path, f.repo FROM symbols s JOIN files f ON f.id = s.file_id "
               "WHERE s.body LIKE ? AND s.name != ?")
        params: list = [f"%{name}%", name]
        if repo:
            sql += " AND f.repo = ?"
            params.append(repo)
        sql += " ORDER BY f.path, s.start_line LIMIT ?"
        params.append(limit)
        return self.conn.execute(sql, params).fetchall()

    # ── Stats ────────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        files = self.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        symbols = self.conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        langs = self.conn.execute(
            "SELECT language, COUNT(*) as n FROM files GROUP BY language ORDER BY n DESC"
        ).fetchall()
        return {
            "files": files,
            "symbols": symbols,
            "repos": self.conn.execute(
                "SELECT COUNT(DISTINCT repo) FROM files WHERE repo IS NOT NULL"
            ).fetchone()[0],
            "languages": [(r[0], r[1]) for r in langs],
        }

    def close(self):
        self.conn.close()
