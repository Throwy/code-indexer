"""
Search and navigation layer — FTS5 search and symbol-based navigation.
"""

from __future__ import annotations
import json
from pathlib import Path
from typing import Optional

from .db import DB
from .indexer import get_db


def _format_result(row: dict, score: Optional[float] = None) -> dict:
    raw_bases = row.get("base_classes")
    return {
        "name": row["name"],
        "kind": row["kind"],
        "signature": row.get("signature"),
        "docstring": row.get("docstring"),
        "path": row["path"],
        "start_line": row["start_line"],
        "end_line": row["end_line"],
        "score": round(score, 4) if score is not None else None,
        "base_classes": json.loads(raw_bases) if raw_bases else [],
    }


def fts_search(target_dir: Path, query: str, limit: int = 20, branch: Optional[str] = None) -> list[dict]:
    db = get_db(target_dir, branch=branch)
    rows = db.fts_search(query, limit=limit)
    db.close()
    return [_format_result(dict(r)) for r in rows]


def goto_symbol(target_dir: Path, name: str, kind: Optional[str] = None, branch: Optional[str] = None) -> list[dict]:
    """Find all definitions of a symbol by exact name."""
    db = get_db(target_dir, branch=branch)
    rows = db.goto_symbol(name, kind=kind)
    db.close()
    return [_format_result(dict(r)) for r in rows]


def find_implementations(target_dir: Path, name: str, branch: Optional[str] = None) -> list[dict]:
    """Find all types that declare `name` as a base class or implemented interface."""
    db = get_db(target_dir, branch=branch)
    rows = db.find_implementations(name)
    db.close()
    return [_format_result(dict(r)) for r in rows]


def find_usages(target_dir: Path, name: str, limit: int = 20, branch: Optional[str] = None) -> list[dict]:
    """Approximate: find symbols whose body mentions `name`."""
    db = get_db(target_dir, branch=branch)
    rows = db.find_usages(name, limit=limit)
    db.close()
    return [_format_result(dict(r)) for r in rows]
