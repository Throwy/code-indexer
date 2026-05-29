"""
Core indexing logic.
Walk a directory, hash each file, skip unchanged ones,
parse symbols with tree-sitter, store in SQLite.
"""

from __future__ import annotations
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .db import DB
from .parser import iter_source_files, parse_file, parse_content, language_for_path, EXTENSION_TO_LANGUAGE, IGNORED_DIRS


def _index_store_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    store = base / "code-indexer"
    store.mkdir(parents=True, exist_ok=True)
    return store


def db_path_for(target_dir: Path, branch: Optional[str] = None) -> Path:
    key = hashlib.sha256(str(target_dir).encode()).hexdigest()[:16]
    if branch:
        safe = re.sub(r"[^a-zA-Z0-9._-]", "_", branch)
        return _index_store_dir() / f"{key}_{safe}.db"
    return _index_store_dir() / f"{key}.db"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def get_db(target_dir: Path, branch: Optional[str] = None) -> DB:
    return DB(db_path_for(target_dir, branch))


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


def index_branch(
    target_dir: Path,
    branch: str,
    force: bool = False,
    verbose: bool = False,
) -> dict:
    """
    Index all source files on a git branch without modifying the working tree.

    Args:
        target_dir: Root of the git repository.
        branch:     Branch (or any git ref) to index.
        force:      Re-index all files even if unchanged.
        verbose:    Print per-file progress.

    Returns:
        Summary dict with counts.
    """
    # Validate that target_dir is a git repo and the branch exists
    verify = subprocess.run(
        ["git", "rev-parse", "--verify", branch],
        cwd=target_dir,
        capture_output=True,
    )
    if verify.returncode != 0:
        raise ValueError(f"Branch '{branch}' not found in {target_dir}")

    db = get_db(target_dir, branch=branch)

    # Enumerate all files on the branch with their blob SHAs
    ls = subprocess.run(
        ["git", "ls-tree", "-r", branch],
        cwd=target_dir,
        capture_output=True,
        text=True,
    )
    if ls.returncode != 0:
        raise RuntimeError(f"git ls-tree failed: {ls.stderr.strip()}")

    stats = {"files_scanned": 0, "files_indexed": 0, "files_skipped": 0,
             "symbols_added": 0, "errors": 0}

    existing_paths = set(db.all_indexed_paths())
    seen_paths: set[str] = set()

    for line in ls.stdout.splitlines():
        # Format: "<mode> blob <sha>\t<path>"
        meta, rel_path = line.split("\t", 1)
        _, _, blob_sha = meta.split()

        path = Path(rel_path)
        if path.suffix.lower() not in EXTENSION_TO_LANGUAGE:
            continue
        if any(part in IGNORED_DIRS for part in path.parts):
            continue

        lang = language_for_path(path)
        if not lang:
            continue

        stats["files_scanned"] += 1
        seen_paths.add(rel_path)

        try:
            existing = db.get_file(rel_path)
            if not force and existing and existing["sha256"] == blob_sha:
                stats["files_skipped"] += 1
                if verbose:
                    print(f"  skip  {rel_path}")
                continue

            # Read file content from git without touching the working tree
            cat = subprocess.run(
                ["git", "show", f"{branch}:{rel_path}"],
                cwd=target_dir,
                capture_output=True,
            )
            if cat.returncode != 0:
                stats["errors"] += 1
                if verbose:
                    print(f"  error {rel_path}: git show failed")
                continue

            file_id = db.upsert_file(rel_path, 0.0, blob_sha, lang)
            db.delete_symbols_for_file(file_id)

            symbols = parse_content(cat.stdout, lang)
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
                print(f"  index {rel_path}  ({len(symbols)} symbols)")

        except Exception as e:
            stats["errors"] += 1
            if verbose:
                print(f"  error {rel_path}: {e}")

    # Remove stale records (files deleted from branch since last index)
    stale = existing_paths - seen_paths
    for path_str in stale:
        db.delete_file(path_str)
        if verbose:
            print(f"  removed stale: {path_str}")

    db.close()
    return stats
