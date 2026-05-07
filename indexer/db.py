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


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS files (
    id            INTEGER PRIMARY KEY,
    path          TEXT    NOT NULL UNIQUE,
    last_modified REAL    NOT NULL,
    sha256        TEXT    NOT NULL,
    language      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS symbols (
    id          INTEGER PRIMARY KEY,
    file_id     INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    name        TEXT    NOT NULL,
    kind        TEXT    NOT NULL,  -- 'function' | 'class' | 'method' | 'interface' | 'struct'
    signature   TEXT,
    docstring   TEXT,
    body        TEXT,
    start_line  INTEGER NOT NULL,
    end_line    INTEGER NOT NULL
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
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ── File tracking ────────────────────────────────────────────────────────

    def get_file(self, path: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM files WHERE path = ?", (path,)
        ).fetchone()

    def upsert_file(self, path: str, last_modified: float, sha256: str, language: str) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO files (path, last_modified, sha256, language)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                last_modified = excluded.last_modified,
                sha256        = excluded.sha256,
                language      = excluded.language
            RETURNING id
            """,
            (path, last_modified, sha256, language),
        )
        row = cur.fetchone()
        self.conn.commit()
        return row[0]

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
    ) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO symbols (file_id, name, kind, signature, docstring, body, start_line, end_line)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (file_id, name, kind, signature, docstring, body, start_line, end_line),
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

    def fts_search(self, query: str, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT s.*, f.path, rank
            FROM symbols_fts
            JOIN symbols s ON s.id = symbols_fts.rowid
            JOIN files f ON f.id = s.file_id
            WHERE symbols_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()

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
            "languages": [(r[0], r[1]) for r in langs],
        }

    def close(self):
        self.conn.close()
