"""
Search and navigation layer — FTS5 search and symbol-based navigation.
"""

from __future__ import annotations
import json
import time
from pathlib import Path
from typing import Optional

from .db import DB
from .indexer import get_db
from .log import logger


def _format_result(row: dict, score: Optional[float] = None) -> dict:
    raw_bases = row.get("base_classes")
    return {
        "name": row["name"],
        "kind": row["kind"],
        "signature": row.get("signature"),
        "docstring": row.get("docstring"),
        "path": row["path"],
        "repo": row.get("repo"),
        "start_line": row["start_line"],
        "end_line": row["end_line"],
        "score": round(score, 4) if score is not None else None,
        "base_classes": json.loads(raw_bases) if raw_bases else [],
    }


def fts_search(target_dir: Path, query: str, limit: int = 20, branch: Optional[str] = None,
               repo: Optional[str] = None) -> list[dict]:
    db = get_db(target_dir, branch=branch)
    t0 = time.perf_counter()
    rows = db.fts_search(query, limit=limit, repo=repo)
    logger.debug("fts_search %r dir=%s branch=%s repo=%s limit=%d -> %d row(s) in %.1f ms", query, target_dir, branch, repo, limit, len(rows), (time.perf_counter() - t0) * 1000)
    db.close()
    return [_format_result(dict(r)) for r in rows]


def goto_symbol(target_dir: Path, name: str, kind: Optional[str] = None, branch: Optional[str] = None,
                repo: Optional[str] = None) -> list[dict]:
    """Find all definitions of a symbol by exact name."""
    db = get_db(target_dir, branch=branch)
    t0 = time.perf_counter()
    rows = db.goto_symbol(name, kind=kind, repo=repo)
    logger.debug("goto_symbol %r kind=%s dir=%s branch=%s repo=%s -> %d row(s) in %.1f ms", name, kind, target_dir, branch, repo, len(rows), (time.perf_counter() - t0) * 1000)
    db.close()
    return [_format_result(dict(r)) for r in rows]


def find_implementations(target_dir: Path, name: str, branch: Optional[str] = None,
                         repo: Optional[str] = None) -> list[dict]:
    """Find all types that declare `name` as a base class or implemented interface."""
    db = get_db(target_dir, branch=branch)
    t0 = time.perf_counter()
    rows = db.find_implementations(name, repo=repo)
    logger.debug("find_implementations %r dir=%s branch=%s repo=%s -> %d row(s) in %.1f ms", name, target_dir, branch, repo, len(rows), (time.perf_counter() - t0) * 1000)
    db.close()
    return [_format_result(dict(r)) for r in rows]


def find_usages(target_dir: Path, name: str, limit: int = 20, branch: Optional[str] = None,
                repo: Optional[str] = None) -> list[dict]:
    """Approximate: find symbols whose body mentions `name`."""
    db = get_db(target_dir, branch=branch)
    t0 = time.perf_counter()
    rows = db.find_usages(name, limit=limit, repo=repo)
    logger.debug("find_usages %r dir=%s branch=%s repo=%s limit=%d -> %d row(s) in %.1f ms", name, target_dir, branch, repo, limit, len(rows), (time.perf_counter() - t0) * 1000)
    db.close()
    return [_format_result(dict(r)) for r in rows]


def list_repos(target_dir: Path, branch: Optional[str] = None) -> list[dict]:
    """Repos discovered under target_dir, with file/symbol counts."""
    db = get_db(target_dir, branch=branch)
    rows = db.list_repos()
    db.close()
    return [dict(r) for r in rows]
