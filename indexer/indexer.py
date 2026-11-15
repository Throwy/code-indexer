"""
Core indexing logic.
Walk a directory, hash each file, skip unchanged ones,
parse symbols with tree-sitter, store in SQLite.
"""

from __future__ import annotations
import hashlib
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from .db import DB
from .log import logger
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


def find_repos(root: Path, max_depth: int = 3) -> dict[str, Path]:
    """
    Discover git repos nested under `root` (a directory that holds several repos).

    Returns {repo_name: repo_path}, where repo_name is the repo's path relative to
    `root` in POSIX form. Returns {} if `root` is itself a repo (single-repo mode).
    """
    if (root / ".git").exists():
        return {}

    repos: dict[str, Path] = {}

    def walk(directory: Path, depth: int):
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError:
            return
        for e in entries:
            if e.name.startswith(".") or e.name in IGNORED_DIRS:
                continue
            if not e.is_dir(follow_symlinks=False):
                continue
            p = Path(e.path)
            if (p / ".git").exists():
                repos[p.relative_to(root).as_posix()] = p  # don't descend into a repo
            elif depth < max_depth:
                walk(p, depth + 1)

    walk(root, 1)
    return repos


def _repo_for(rel_path: Path, repos: dict[str, Path]) -> Optional[str]:
    """Name of the repo containing `rel_path` (relative to the index root), if any."""
    if not repos:
        return None
    parts = rel_path.parts
    for i in range(1, len(parts)):
        name = "/".join(parts[:i])
        if name in repos:
            return name
    return None


def index_directory(
    target_dir: Path,
    force: bool = False,
) -> dict:
    """
    Index all source files in target_dir.

    Args:
        target_dir: Root directory to index.
        force:      Re-index all files even if unchanged.

    Returns:
        Summary dict with counts.
    """
    t0 = time.perf_counter()
    db = get_db(target_dir)
    db.set_meta("root", str(target_dir))
    db.set_meta("indexed_at", str(time.time()))
    repos = find_repos(target_dir)
    logger.debug("index db: %s", db.db_path)
    logger.debug("repos: %s", ", ".join(repos) or "none (single-repo mode)")
    files = iter_source_files(target_dir)
    logger.debug("found %d source files under %s", len(files), target_dir)

    stats = {"files_scanned": 0, "files_indexed": 0, "files_skipped": 0,
             "symbols_added": 0, "errors": 0}

    stats["repos"] = len(repos)

    # Track which paths still exist so we can remove deleted ones
    existing_paths = set(db.all_indexed_paths())
    seen_paths: set[str] = set()

    for path in files:
        stats["files_scanned"] += 1
        rel = str(path.relative_to(target_dir))
        repo = _repo_for(path.relative_to(target_dir), repos)
        seen_paths.add(rel)

        try:
            mtime = path.stat().st_mtime
            sha = _sha256(path)
            lang = language_for_path(path) or "unknown"

            existing = db.get_file(rel)
            if not force and existing and existing["sha256"] == sha:
                if existing["repo"] != repo:
                    db.update_repo(rel, repo)  # e.g. index predates repo tagging
                stats["files_skipped"] += 1
                logger.info(f"  skip  {rel}")
                continue

            # Upsert file record and clear old symbols
            file_id = db.upsert_file(rel, mtime, sha, lang, repo)
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

            logger.info(f"  index {rel}  ({len(symbols)} symbols)")

        except Exception as e:
            stats["errors"] += 1
            logger.info(f"  error {rel}: {e}")
            logger.debug("traceback for %s", rel, exc_info=True)

    # Remove stale file records (deleted files)
    stale = existing_paths - seen_paths
    for path_str in stale:
        db.delete_file(path_str)
        logger.info(f"  removed stale: {path_str}")

    logger.debug("indexing finished in %.2fs: %s", time.perf_counter() - t0, stats)
    db.close()
    return stats


def _git(repo_dir: Path, *args: str, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo_dir, capture_output=True, text=text)


def _ref_exists(repo_dir: Path, ref: str) -> bool:
    return _git(repo_dir, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode == 0


def resolve_branch(repo_dir: Path, branch: str) -> Optional[str]:
    """The git ref to read for `branch` in this repo: the local branch, else origin/<branch>, else None.

    The name "main" also falls back to "master" for repos that still use the old default.
    """
    names = [branch, "master"] if branch == "main" else [branch]
    for name in names:
        for ref in (name, f"origin/{name}"):
            if _ref_exists(repo_dir, ref):
                return ref
    return None


def default_branch(repo_dir: Path) -> Optional[str]:
    """A repo's default branch ref: origin's HEAD, else main/master, else whatever is checked out."""
    r = _git(repo_dir, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if r.returncode == 0 and r.stdout.strip() and _ref_exists(repo_dir, r.stdout.strip()):
        return r.stdout.strip()
    for name in ("main", "master"):
        if _ref_exists(repo_dir, name):
            return name
    return "HEAD" if _ref_exists(repo_dir, "HEAD") else None


def _index_repo_ref(db: DB, repo_dir: Path, ref: str, prefix: str, repo: Optional[str],
                    force: bool, stats: dict, seen_paths: set[str]) -> None:
    """Index the files of `ref` in `repo_dir` into `db`, storing paths as `prefix + path-in-repo`."""
    ls = _git(repo_dir, "ls-tree", "-r", ref)
    if ls.returncode != 0:
        raise RuntimeError(f"git ls-tree failed in {repo_dir}: {ls.stderr.strip()}")
    logger.debug("git ls-tree listed %d entries on %s in %s", len(ls.stdout.splitlines()), ref, repo_dir)

    for line in ls.stdout.splitlines():
        # Format: "<mode> blob <sha>\t<path>"
        meta, in_repo_path = line.split("\t", 1)
        _, kind, blob_sha = meta.split()
        if kind != "blob":
            continue  # submodule commits, etc.

        path = Path(in_repo_path)
        if path.suffix.lower() not in EXTENSION_TO_LANGUAGE:
            continue
        if any(part in IGNORED_DIRS for part in path.parts):
            continue
        lang = language_for_path(path)
        if not lang:
            continue

        rel_path = prefix + in_repo_path
        stats["files_scanned"] += 1
        seen_paths.add(rel_path)

        try:
            existing = db.get_file(rel_path)
            if not force and existing and existing["sha256"] == blob_sha:
                if existing["repo"] != repo:
                    db.update_repo(rel_path, repo)
                stats["files_skipped"] += 1
                logger.info(f"  skip  {rel_path}")
                continue

            # Read file content from git without touching the working tree
            cat = _git(repo_dir, "show", f"{ref}:{in_repo_path}", text=False)
            if cat.returncode != 0:
                stats["errors"] += 1
                logger.info(f"  error {rel_path}: git show failed")
                continue

            file_id = db.upsert_file(rel_path, 0.0, blob_sha, lang, repo)
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
            logger.info(f"  index {rel_path}  ({len(symbols)} symbols)")

        except Exception as e:
            stats["errors"] += 1
            logger.info(f"  error {rel_path}: {e}")
            logger.debug("traceback for %s", rel_path, exc_info=True)


def index_branch(
    target_dir: Path,
    branch: str,
    force: bool = False,
) -> dict:
    """
    Index the committed files of a git branch without touching the working tree.

    Works on a single repo or on a parent directory holding several repos. In the
    parent case every repo is indexed on `branch`; a repo that lacks the branch
    falls back to its default branch (origin's HEAD, else main/master, else HEAD)
    and is reported in the returned `fallbacks`.

    Args:
        target_dir: A git repository, or a directory containing git repositories.
        branch:     Branch (or any git ref) to index.
        force:      Re-index all files even if unchanged.

    Returns:
        Summary dict with counts, `repos`, `fallbacks` ({repo: ref actually used})
        and `skipped_repos` (repos with no usable commit).
    """
    repos = find_repos(target_dir)
    single = (target_dir / ".git").exists()
    if not single and not repos:
        raise ValueError(f"{target_dir} is not a git repository and contains none (searched 3 levels deep).")

    # Decide which ref to read in each repo before touching the database
    plan: list[tuple[Optional[str], Path, str]] = []   # (repo name, repo dir, ref)
    fallbacks: dict[str, str] = {}
    unusable: list[str] = []
    if single:
        ref = resolve_branch(target_dir, branch)
        if ref is None:
            raise ValueError(f"Branch '{branch}' not found in {target_dir}")
        plan.append((None, target_dir, ref))
    else:
        for name, repo_dir in repos.items():
            ref = resolve_branch(repo_dir, branch)
            if ref is None:
                ref = default_branch(repo_dir)
                if ref is None:
                    unusable.append(name)  # e.g. a repo with no commits
                    continue
                fallbacks[name] = ref
            plan.append((name, repo_dir, ref))
        if len(fallbacks) + len(unusable) == len(repos):
            raise ValueError(f"Branch '{branch}' not found in any repo under {target_dir}")

    t0 = time.perf_counter()
    db = get_db(target_dir, branch=branch)
    db.set_meta("root", str(target_dir))
    db.set_meta("branch", branch)
    db.set_meta("indexed_at", str(time.time()))
    logger.debug("index db: %s", db.db_path)

    stats = {"files_scanned": 0, "files_indexed": 0, "files_skipped": 0,
             "symbols_added": 0, "errors": 0, "repos": len(repos),
             "fallbacks": fallbacks, "skipped_repos": unusable}
    existing_paths = set(db.all_indexed_paths())
    seen_paths: set[str] = set()

    for name, repo_dir, ref in plan:
        logger.info(f"repo {name or repo_dir.name}: {ref}")
        prefix = f"{name}/" if name else ""
        _index_repo_ref(db, repo_dir, ref, prefix, name, force, stats, seen_paths)

    # Remove stale records (files deleted from the branch, or repos that went away)
    for path_str in existing_paths - seen_paths:
        db.delete_file(path_str)
        logger.info(f"  removed stale: {path_str}")

    logger.debug("indexing finished in %.2fs: %s", time.perf_counter() - t0, stats)
    db.close()
    return stats


def index_default(target_dir: Path, force: bool = False) -> tuple[dict, Optional[str]]:
    """
    Index `target_dir` the default way: the main branch (else master) of its git
    repo(s), leaving the working tree untouched. A directory with no git repos at
    all is indexed as a plain working tree. Returns (stats, branch used or None).
    """
    if not (target_dir / ".git").exists() and not find_repos(target_dir):
        return index_directory(target_dir=target_dir, force=force), None
    try:
        return index_branch(target_dir=target_dir, branch="main", force=force), "main"
    except ValueError as e:
        raise ValueError(f"{str(e).rstrip('.')}. Pass --branch <name> to pick a branch, or --worktree to index the working tree.") from e


# ── Index management (list / clean) ──────────────────────────────────────────

def _read_index_info(db_file: Path) -> dict:
    """Describe one index file in the store. Origin (root/branch) comes from its meta table."""
    info = {
        "path": db_file, "name": db_file.stem, "root": None, "branch": None,
        "files": None, "symbols": None, "indexed_at": None,
        "size": 0, "modified": db_file.stat().st_mtime, "corrupt": False,
    }
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(db_file) + suffix)
        if f.exists():
            info["size"] += f.stat().st_size
    try:
        conn = sqlite3.connect(str(db_file), timeout=5)
        try:
            meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        except sqlite3.Error:
            meta = {}  # index predates metadata; it gets filled in on the next index run
        info["root"] = meta.get("root")
        info["branch"] = meta.get("branch")
        if meta.get("indexed_at"):
            info["indexed_at"] = float(meta["indexed_at"])
        try:
            info["files"] = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            info["symbols"] = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        except sqlite3.Error:
            info["corrupt"] = True
        conn.close()
    except sqlite3.Error:
        info["corrupt"] = True
    return info


def _stale_reason(info: dict) -> Optional[str]:
    if info["corrupt"]:
        return "unreadable index file"
    root = info["root"]
    if root is None:
        return None  # origin unknown (index predates metadata) - never auto-flagged
    if not Path(root).is_dir():
        return "directory no longer exists"
    branch = info["branch"]
    if branch:
        root_dir = Path(root)
        if (root_dir / ".git").exists():
            has_branch = resolve_branch(root_dir, branch) is not None
        else:  # parent directory of repos: stale only when no repo has the branch any more
            has_branch = any(resolve_branch(r, branch) for r in find_repos(root_dir).values())
        if not has_branch:
            return f"branch '{branch}' no longer exists"
    return None


def list_indexes() -> list[dict]:
    """All indexes in the store, newest first, each with a `stale_reason` (None if healthy/unknown)."""
    infos = []
    for f in _index_store_dir().glob("*.db"):
        try:
            info = _read_index_info(f)
        except OSError:
            continue
        info["stale_reason"] = _stale_reason(info)
        infos.append(info)
    infos.sort(key=lambda i: i["modified"], reverse=True)
    return infos


def branch_indexes_for(directory: Path) -> list[str]:
    """Branch names that have an index for `directory` (the working-tree index is not included)."""
    key = hashlib.sha256(str(directory).encode()).hexdigest()[:16]
    return sorted(i["branch"] for i in list_indexes()
                  if i["name"].startswith(key + "_") and i["branch"])


def no_index_message(directory: Path, branch: Optional[str] = None, shown_dir: Optional[str] = None) -> str:
    """Explain a missing index, pointing at the branch indexes that do exist for the directory."""
    shown_dir = shown_dir or str(directory)
    where = f"{directory} branch '{branch}'" if branch else str(directory)
    msg = f"No index found for {where}."
    others = [b for b in branch_indexes_for(directory) if b != branch]
    if others:
        listed = ", ".join(others)
        if branch:
            msg += f" Indexed branches for this directory: {listed}."
        else:
            msg += (f" It has no working-tree index, but is indexed for branch(es): {listed}."
                    f" Pass --branch <name> (MCP: branch) to use one.")
    flag = f" --branch {branch}" if branch else ""
    return msg + f" To create it, run `code-indexer index{flag} {shown_dir}`."


def resolve_index(directory: Path, branch: Optional[str] = None,
                  shown_dir: Optional[str] = None) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Decide which index a query should use. Returns (branch, note, error).

    An explicit branch is used as given. With no branch the working-tree index is
    used if it exists; otherwise, if the directory has exactly one branch index,
    that one is used, and with several, main (then master) is the default
    (`note` says which). `error` is set when nothing usable exists.
    """
    if not branch and not db_path_for(directory).exists():
        branches = branch_indexes_for(directory)
        if len(branches) == 1:
            branch = branches[0]
            return branch, f"No working-tree index; using the only indexed branch '{branch}'.", None
        for default in ("main", "master"):
            if default in branches:
                return default, f"No working-tree index; using default branch '{default}'.", None
    if not db_path_for(directory, branch).exists():
        return branch, None, no_index_message(directory, branch, shown_dir)
    return branch, None, None


def delete_index(db_file: Path) -> tuple[bool, str]:
    """Delete an index file plus its SQLite -wal/-shm companions."""
    try:
        for suffix in ("-wal", "-shm", ""):
            f = Path(str(db_file) + suffix)
            if f.exists():
                f.unlink()
        return True, ""
    except OSError as e:  # e.g. still open in another process on Windows
        logger.debug("failed to delete %s: %s", db_file, e)
        return False, str(e)


def clean_indexes(
    directory: Optional[Path] = None,
    branch: Optional[str] = None,
    all_branches: bool = False,
    stale: bool = False,
    everything: bool = False,
    dry_run: bool = False,
) -> list[dict]:
    """
    Remove indexes from the store. Choose exactly one scope:
      directory (+ optional branch, or all_branches) | stale | everything
    Returns the selected indexes, each with `removed` (bool) and `error` (str).
    """
    infos = list_indexes()
    if everything:
        chosen = infos
    elif stale:
        chosen = [i for i in infos if i["stale_reason"]]
    elif directory is not None:
        target = Path(directory).expanduser().resolve()
        key = hashlib.sha256(str(target).encode()).hexdigest()[:16]
        if branch:
            wanted = db_path_for(target, branch).stem
            chosen = [i for i in infos if i["name"] == wanted]
        elif all_branches:
            chosen = [i for i in infos if i["name"] == key or i["name"].startswith(key + "_")]
        else:
            chosen = [i for i in infos if i["name"] == key]
    else:
        raise ValueError("Specify a directory, stale=True, or everything=True.")

    logger.debug("clean: %d of %d index(es) selected", len(chosen), len(infos))
    for info in chosen:
        logger.debug("%s %s (%s)", "would remove" if dry_run else "removing",
                     info["path"], info["root"] or "unknown origin")
        if dry_run:
            info["removed"], info["error"] = False, ""
        else:
            info["removed"], info["error"] = delete_index(info["path"])
    return chosen
