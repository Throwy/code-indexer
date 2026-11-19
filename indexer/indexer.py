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
from concurrent.futures import ThreadPoolExecutor
import sys
import time
from pathlib import Path
from typing import Optional

from .db import DB
from .log import logger, detail
from .parser import iter_source_files, parse_file, parse_content, language_for_path, EXTENSION_TO_LANGUAGE, IGNORED_DIRS


# Commit every N indexed files inside a batch so an interrupted run keeps most of its work.
_CHECKPOINT_EVERY = 250


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
    logger.info("indexing %d source file(s) under %s (%d repo(s))", len(files), target_dir, len(repos))

    stats = {"files_scanned": 0, "files_indexed": 0, "files_skipped": 0,
             "symbols_added": 0, "errors": 0}

    stats["repos"] = len(repos)

    # Track which paths still exist so we can remove deleted ones
    existing_paths = set(db.all_indexed_paths())
    seen_paths: set[str] = set()

    with db.batch():
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
                    detail(f"  skip  {rel}")
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
                if stats["files_indexed"] % _CHECKPOINT_EVERY == 0:
                    db.checkpoint()

                detail(f"  index {rel}  ({len(symbols)} symbols)")

            except Exception as e:
                stats["errors"] += 1
                detail(f"  error {rel}: {e}")
                logger.debug("traceback for %s", rel, exc_info=True)

        # Remove stale file records (deleted files)
        stale = existing_paths - seen_paths
        for path_str in stale:
            db.delete_file(path_str)
            detail(f"  removed stale: {path_str}")

    logger.info("done: %d indexed, %d skipped, %d removed (stale), %d error(s), %d symbols",
                stats["files_indexed"], stats["files_skipped"], len(stale),
                stats["errors"], stats["symbols_added"])
    logger.debug("indexing finished in %.2fs: %s", time.perf_counter() - t0, stats)
    db.close()
    return stats


def _git(repo_dir: Path, *args: str, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo_dir, capture_output=True, text=text)


class _BlobReader:
    """Reads git blobs by SHA through one long-lived `git cat-file --batch` process,
    instead of spawning `git show` per file (process startup dominated branch indexing)."""

    def __init__(self, repo_dir: Path):
        self._proc = subprocess.Popen(
            ["git", "cat-file", "--batch"], cwd=repo_dir,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )

    def read(self, blob_sha: str) -> Optional[bytes]:
        """Blob content, or None if git doesn't have it. Raises OSError if git died."""
        self._proc.stdin.write(blob_sha.encode() + b"\n")
        self._proc.stdin.flush()
        header = self._proc.stdout.readline().split()  # b"<sha> <type> <size>" | b"<sha> missing"
        if not header:
            raise OSError("git cat-file exited unexpectedly")
        if len(header) < 3 or header[1] != b"blob":
            return None
        size = int(header[2])
        data = self._proc.stdout.read(size)
        self._proc.stdout.read(1)  # trailing LF after the content
        if len(data) != size:
            raise OSError("git cat-file returned truncated blob")
        return data

    def close(self):
        try:
            self._proc.stdin.close()
        except OSError:
            pass
        self._proc.stdout.close()
        self._proc.wait()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _ref_exists(repo_dir: Path, ref: str) -> bool:
    return _git(repo_dir, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode == 0


def _fetch(repo_dir: Path, *refspecs: str, warn: bool = True) -> bool:
    """Fetch from origin (optionally only `refspecs`). Returns True on success; warns (never raises) on failure."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}  # never hang on a credential prompt
    logger.info("fetching origin in %s ...", repo_dir)
    t0 = time.perf_counter()
    try:
        r = subprocess.run(["git", "fetch", "--prune", "origin", *refspecs], cwd=repo_dir, env=env,
                           capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        logger.warning("git fetch timed out in %s; indexing possibly stale refs", repo_dir)
        return False
    if r.returncode != 0:
        if not warn:
            logger.debug("git fetch %s failed in %s: %s", " ".join(refspecs), repo_dir, r.stderr.strip())
            return False
        logger.warning("git fetch failed in %s (indexing possibly stale refs): %s", repo_dir, r.stderr.strip())
        return False
    logger.debug("git fetch finished in %.1fs in %s", time.perf_counter() - t0, repo_dir)
    return True


def _is_ancestor(repo_dir: Path, ancestor: str, descendant: str) -> bool:
    return _git(repo_dir, "merge-base", "--is-ancestor", ancestor, descendant).returncode == 0


def resolve_branch(repo_dir: Path, branch: str) -> Optional[str]:
    """The git ref to read for `branch` in this repo, or None.

    Considers both the local branch and origin/<branch> and returns the most
    up-to-date one: origin when the local branch is behind it (a fetch never
    advances local branches), the local branch when it has unpushed commits.
    If they have diverged the local branch wins, with a warning.
    The name "main" also falls back to "master" for repos that still use the old default.
    """
    names = [branch, "master"] if branch == "main" else [branch]
    for name in names:
        local = name if _ref_exists(repo_dir, f"refs/heads/{name}") else None
        remote = f"origin/{name}" if _ref_exists(repo_dir, f"refs/remotes/origin/{name}") else None
        if local and remote:
            if _is_ancestor(repo_dir, local, remote):
                return remote
            if not _is_ancestor(repo_dir, remote, local):
                logger.warning("%s: local '%s' and '%s' have diverged; indexing local", repo_dir, local, remote)
            return local
        if local or remote:
            return local or remote
        if _ref_exists(repo_dir, name):  # tag, sha, or other ref
            return name
    return None


def _find_ref_ignoring_case(repo_dir: Path, branch: str) -> Optional[str]:
    """Branch name as it is actually spelled in this repo (e.g. 'UAT' for 'uat'), or None."""
    r = _git(repo_dir, "for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes/origin")
    for ref in r.stdout.splitlines():
        name = ref.split("/", 3)[-1] if ref.startswith("refs/remotes/origin/") else ref.split("/", 2)[-1]
        if name.lower() == branch.lower() and name != "HEAD":
            return name
    return None


def _resolve_after_fetch(repo_dir: Path, branch: str) -> Optional[str]:
    """resolve_branch, then if that finds nothing, try harder: fetch the branch explicitly
    (clones with a narrowed fetch refspec never download other branches) and match case-insensitively."""
    ref = resolve_branch(repo_dir, branch)
    if ref is not None:
        logger.debug("%s: '%s' resolved to %s", repo_dir, branch, ref)
        return ref
    logger.debug("%s: '%s' not found after fetch; trying explicit fetch", repo_dir, branch)
    if _fetch(repo_dir, f"+refs/heads/{branch}:refs/remotes/origin/{branch}", warn=False):
        ref = resolve_branch(repo_dir, branch)
        if ref is not None:
            return ref
    actual = _find_ref_ignoring_case(repo_dir, branch)
    if actual and actual != branch:
        logger.info("%s: using branch '%s' for requested '%s'", repo_dir, actual, branch)
        return resolve_branch(repo_dir, actual)
    logger.debug("%s: no branch '%s' (local or origin) even after explicit fetch", repo_dir, branch)
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

    with _BlobReader(repo_dir) as blobs:
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
                    detail(f"  skip  {rel_path}")
                    continue

                # Read file content from git without touching the working tree
                content = blobs.read(blob_sha)
                if content is None:
                    stats["errors"] += 1
                    detail(f"  error {rel_path}: blob {blob_sha} not found")
                    continue

                file_id = db.upsert_file(rel_path, 0.0, blob_sha, lang, repo)
                db.delete_symbols_for_file(file_id)

                symbols = parse_content(content, lang)
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
                if stats["files_indexed"] % _CHECKPOINT_EVERY == 0:
                    db.checkpoint()
                detail(f"  index {rel_path}  ({len(symbols)} symbols)")

            except Exception as e:
                stats["errors"] += 1
                detail(f"  error {rel_path}: {e}")
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

    # Fetch latest from origin before resolving any refs
    if single:
        _fetch(target_dir)
    else:
        # Network-bound and independent per repo, so fetch them concurrently
        with ThreadPoolExecutor(max_workers=min(8, len(repos))) as pool:
            list(pool.map(_fetch, repos.values()))

    # Decide which ref to read in each repo before touching the database
    plan: list[tuple[Optional[str], Path, str]] = []   # (repo name, repo dir, ref)
    fallbacks: dict[str, str] = {}
    unusable: list[str] = []
    if single:
        ref = _resolve_after_fetch(target_dir, branch)
        if ref is None:
            raise ValueError(f"Branch '{branch}' not found in {target_dir}")
        plan.append((None, target_dir, ref))
    else:
        for name, repo_dir in repos.items():
            ref = _resolve_after_fetch(repo_dir, branch)
            if ref is None:
                ref = default_branch(repo_dir)
                if ref is None:
                    unusable.append(name)  # e.g. a repo with no commits
                    continue
                fallbacks[name] = ref
                logger.warning("%s has no branch '%s'; falling back to %s", name, branch, ref)
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

    with db.batch():
        for name, repo_dir, ref in plan:
            label = name or repo_dir.name
            logger.debug("repo %s: indexing %s", label, ref)
            before = dict(stats)
            prefix = f"{name}/" if name else ""
            _index_repo_ref(db, repo_dir, ref, prefix, name, force, stats, seen_paths)
            logger.info("repo %s @ %s: %d indexed, %d skipped, %d error(s), %d symbols",
                        label, ref,
                        stats["files_indexed"] - before["files_indexed"],
                        stats["files_skipped"] - before["files_skipped"],
                        stats["errors"] - before["errors"],
                        stats["symbols_added"] - before["symbols_added"])

        # Remove stale records (files deleted from the branch, or repos that went away)
        stale = existing_paths - seen_paths
        for path_str in stale:
            db.delete_file(path_str)
            detail(f"  removed stale: {path_str}")

    logger.info("branch '%s': %d repo(s), %d indexed, %d skipped, %d removed (stale), %d error(s)",
                branch, len(plan), stats["files_indexed"], stats["files_skipped"], len(stale), stats["errors"])

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


def branch_index_infos(directory: Path) -> list[dict]:
    """list_indexes() entries for `directory`'s branch indexes, sorted by branch name."""
    key = hashlib.sha256(str(directory).encode()).hexdigest()[:16]
    return sorted((i for i in list_indexes() if i["name"].startswith(key + "_") and i["branch"]),
                  key=lambda i: i["branch"])


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
