"""
Core indexing logic.
Walk a directory, hash each file, skip unchanged ones,
parse symbols with tree-sitter, store in SQLite.
"""

from __future__ import annotations
import hashlib
import os
import sys
from pathlib import Path
from typing import Optional

from .db import DB
from .parser import iter_source_files, parse_file, language_for_path


def _index_store_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    store = base / "code-indexer"
    store.mkdir(parents=True, exist_ok=True)
    return store


def db_path_for(target_dir: Path) -> Path:
    key = hashlib.sha256(str(target_dir).encode()).hexdigest()[:16]
    return _index_store_dir() / f"{key}.db"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def get_db(target_dir: Path) -> DB:
    return DB(db_path_for(target_dir))


def index_directory(
    target_dir: Path,
    force: bool = False,
    verbose: bool = False,
) -> dict:
    """
    Index all source files in target_dir.

    Args:
        target_dir: Root directory to index.
        force:      Re-index all files even if unchanged.
        verbose:    Print per-file progress.

    Returns:
        Summary dict with counts.
    """
    db = get_db(target_dir)
    files = iter_source_files(target_dir)

    stats = {"files_scanned": 0, "files_indexed": 0, "files_skipped": 0,
             "symbols_added": 0, "errors": 0}

    # Track which paths still exist so we can remove deleted ones
    existing_paths = set(db.all_indexed_paths())
    seen_paths: set[str] = set()

    for path in files:
        stats["files_scanned"] += 1
        rel = str(path.relative_to(target_dir))
        seen_paths.add(rel)

        try:
            mtime = path.stat().st_mtime
            sha = _sha256(path)
            lang = language_for_path(path) or "unknown"

            existing = db.get_file(rel)
            if not force and existing and existing["sha256"] == sha:
                stats["files_skipped"] += 1
                if verbose:
                    print(f"  skip  {rel}")
                continue

            # Upsert file record and clear old symbols
            file_id = db.upsert_file(rel, mtime, sha, lang)
            db.delete_symbols_for_file(file_id)

            # Parse and insert symbols
            symbols = parse_file(path)
            for sym in symbols:
                db.insert_symbol(
                    file_id=file_id,
                    name=sym.name,
                    kind=sym.kind,
                    signature=sym.signature,
                    docstring=sym.docstring,
                    body=sym.body,
                    start_line=sym.start_line,
                    end_line=sym.end_line,
                    base_classes=sym.base_classes or None,
                )
            stats["files_indexed"] += 1
            stats["symbols_added"] += len(symbols)

            if verbose:
                print(f"  index {rel}  ({len(symbols)} symbols)")

        except Exception as e:
            stats["errors"] += 1
            if verbose:
                print(f"  error {rel}: {e}")

    # Remove stale file records (deleted files)
    stale = existing_paths - seen_paths
    for path_str in stale:
        db.delete_file(path_str)
        if verbose:
            print(f"  removed stale: {path_str}")

    db.close()
    return stats
