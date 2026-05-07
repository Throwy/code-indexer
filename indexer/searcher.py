"""
Search layer — full-text search (FTS5).
"""

from __future__ import annotations
from pathlib import Path
from typing import Optional

from .db import DB
from .indexer import get_db


def _format_result(row: dict, score: Optional[float] = None) -> dict:
    return {
        "name": row["name"],
        "kind": row["kind"],
        "signature": row.get("signature"),
        "docstring": row.get("docstring"),
        "path": row["path"],
        "start_line": row["start_line"],
        "end_line": row["end_line"],
        "score": round(score, 4) if score is not None else None,
    }


def fts_search(target_dir: Path, query: str, limit: int = 20) -> list[dict]:
    db = get_db(target_dir)
    rows = db.fts_search(query, limit=limit)
    db.close()
    return [_format_result(dict(r)) for r in rows]
