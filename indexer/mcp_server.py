"""
MCP server for code-indexer.
Exposes search_code, index_directory, index_status, and get_file_symbols
as tools that Claude can call.

Run as a stdio MCP server:
    python -m indexer.mcp_server

Register in Claude Desktop config (see SETUP.md).
"""

from __future__ import annotations
import json
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "code-indexer",
    instructions=(
        "Use these tools to search and explore an indexed codebase. "
        "Always call index_status first to confirm an index exists before searching."
    ),
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _resolve(directory: str) -> Path:
    p = Path(directory).expanduser().resolve()
    if not p.is_dir():
        raise ValueError(f"Directory not found: {directory}")
    return p


def _fmt_results(results: list[dict]) -> str:
    if not results:
        return "No results found."
    lines = []
    for i, r in enumerate(results, 1):
        score = f" (score={r['score']:.3f})" if r.get("score") is not None else ""
        lines.append(f"{i}. [{r['kind']}] {r['name']}{score}")
        lines.append(f"   File: {r['path']}, lines {r['start_line']}–{r['end_line']}")
        if r.get("signature"):
            sig = r["signature"].replace("\n", " ")[:150]
            lines.append(f"   Signature: {sig}")
        if r.get("docstring"):
            doc = r["docstring"].replace("\n", " ")[:120]
            lines.append(f"   Docstring: {doc}")
        lines.append("")
    return "\n".join(lines)


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def search_code(
    query: str,
    directory: str,
    limit: int = 10,
    repo: Optional[str] = None,
) -> str:
    """
    Search an indexed codebase for symbols matching the query.

    Args:
        query:     Keyword query, e.g. "parse JWT token" or "UserService".
        directory: Absolute path to the project directory that has been indexed.
        limit:     Maximum number of results to return (default 10).
        repo:      Optional repo name to restrict results to (see list_repos).

    Returns:
        Formatted list of matching symbols with file paths and line numbers.
    """
    from .searcher import fts_search
    target = _resolve(directory)
    results = fts_search(target, query, limit=limit, repo=repo)
    return _fmt_results(results)


@mcp.tool()
def index_status(directory: str) -> str:
    """
    Return statistics about the code index for a project directory.
    Use this to confirm an index exists before searching.

    Args:
        directory: Absolute path to the project directory.

    Returns:
        Number of indexed files, symbols, embeddings, and language breakdown.
    """
    from .indexer import get_db, db_path_for
    target = _resolve(directory)
    db_path = db_path_for(target)

    if not db_path.exists():
        return (
            f"No index found for {target}. "
            f"Run `python -m indexer index {directory}` to create one."
        )

    db = get_db(target)
    s = db.stats()
    db.close()

    lines = [
        f"Index: {db_path}",
        f"Files:    {s['files']}",
        f"Symbols:  {s['symbols']}",
    ]
    if s["repos"]:
        lines.append(f"Repos:    {s['repos']}")
    if s["languages"]:
        lines.append("Languages:")
        for lang, count in s["languages"]:
            lines.append(f"  {lang}: {count} files")
    return "\n".join(lines)


@mcp.tool()
def get_file_symbols(file_path: str, directory: str) -> str:
    """
    List all indexed symbols (functions, classes, methods) in a specific file.

    Args:
        file_path: Path to the file, relative to the project directory.
        directory: Absolute path to the project directory.

    Returns:
        All symbols found in that file with their line ranges and signatures.
    """
    from .indexer import get_db, db_path_for
    target = _resolve(directory)
    db_path = db_path_for(target)

    if not db_path.exists():
        return f"No index found. Run `python -m indexer index {directory}` first."

    db = get_db(target)
    rows = db.conn.execute(
        """
        SELECT s.*, f.path FROM symbols s
        JOIN files f ON f.id = s.file_id
        WHERE f.path = ?
        ORDER BY s.start_line
        """,
        (file_path,),
    ).fetchall()
    db.close()

    if not rows:
        return f"No symbols found for '{file_path}'. Check the path is relative to the project root."

    lines = [f"Symbols in {file_path}:\n"]
    for r in rows:
        lines.append(f"  [{r['kind']}] {r['name']}  (lines {r['start_line']}–{r['end_line']})")
        if r["signature"]:
            sig = r["signature"].replace("\n", " ")[:120]
            lines.append(f"    {sig}")
    return "\n".join(lines)


@mcp.tool()
def goto_symbol(
    name: str,
    directory: str,
    kind: Optional[str] = None,
    repo: Optional[str] = None,
) -> str:
    """
    Find the definition(s) of a symbol by exact name.

    Args:
        name:      Exact symbol name, e.g. "UserService" or "parse_jwt".
        directory: Absolute path to the project directory that has been indexed.
        kind:      Optional filter: "function", "class", "method", "interface", or "struct".
        repo:      Optional repo name to restrict results to (see list_repos).

    Returns:
        File path(s) and line numbers where the symbol is defined.
    """
    from .searcher import goto_symbol as _goto
    target = _resolve(directory)
    results = _goto(target, name, kind=kind, repo=repo)
    if not results:
        return f"No definition found for '{name}'."
    lines = [f"{len(results)} definition(s) for '{name}':\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. [{r['kind']}] {r['name']}")
        lines.append(f"   File: {r['path']}, line {r['start_line']}")
        if r.get("signature"):
            sig = r["signature"].replace("\n", " ")[:150]
            lines.append(f"   Signature: {sig}")
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
def find_implementations(
    name: str,
    directory: str,
    repo: Optional[str] = None,
) -> str:
    """
    Find all classes or structs that implement or extend a given interface or base class.

    Args:
        name:      Name of the interface or base class, e.g. "IRepository" or "BaseHandler".
        directory: Absolute path to the project directory that has been indexed.
        repo:      Optional repo name to restrict results to (see list_repos).

    Returns:
        List of implementing types with their file paths and line numbers.
    """
    from .searcher import find_implementations as _impls
    target = _resolve(directory)
    results = _impls(target, name, repo=repo)
    if not results:
        return f"No implementations found for '{name}'."
    lines = [f"{len(results)} implementation(s) of '{name}':\n"]
    for i, r in enumerate(results, 1):
        bases = ", ".join(r["base_classes"]) if r["base_classes"] else ""
        lines.append(f"{i}. [{r['kind']}] {r['name']}  (extends/implements: {bases})")
        lines.append(f"   File: {r['path']}, line {r['start_line']}")
        if r.get("signature"):
            sig = r["signature"].replace("\n", " ")[:150]
            lines.append(f"   Signature: {sig}")
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
def find_usages(
    name: str,
    directory: str,
    limit: int = 20,
    repo: Optional[str] = None,
) -> str:
    """
    Find symbols (functions, methods) whose body references or calls NAME.
    This is an approximate search based on body text, not a full call-graph analysis.

    Args:
        name:      Symbol name to search for usages, e.g. "parse_jwt" or "UserService".
        directory: Absolute path to the project directory that has been indexed.
        limit:     Maximum number of results (default 20).
        repo:      Optional repo name to restrict results to (see list_repos).

    Returns:
        List of symbols that appear to reference NAME, with file paths and line numbers.
    """
    from .searcher import find_usages as _refs
    target = _resolve(directory)
    results = _refs(target, name, limit=limit, repo=repo)
    if not results:
        return f"No references found for '{name}'."
    lines = [f"{len(results)} reference(s) to '{name}':\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. [{r['kind']}] {r['name']}")
        lines.append(f"   File: {r['path']}, lines {r['start_line']}–{r['end_line']}")
        if r.get("signature"):
            sig = r["signature"].replace("\n", " ")[:150]
            lines.append(f"   Signature: {sig}")
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
def run_index(
    directory: str,
    force: bool = False,
) -> str:
    """
    Index (or re-index) a project directory. Only changed files are re-processed.

    Args:
        directory: Absolute path to the project directory to index.
        force:     If True, re-index all files even if unchanged.

    Returns:
        Summary of files scanned, indexed, and symbols added.
    """
    from .indexer import index_directory
    target = _resolve(directory)

    stats = index_directory(
        target_dir=target,
        force=force,
    )

    lines = [
        f"Indexing complete for {target}",
        f"Files scanned:  {stats['files_scanned']}",
        f"Files indexed:  {stats['files_indexed']}",
        f"Files skipped:  {stats['files_skipped']} (unchanged)",
        f"Symbols added:  {stats['symbols_added']}",
    ]
    if stats.get("repos"):
        lines.insert(1, f"Repos found:    {stats['repos']}")
    if stats.get("errors"):
        lines.append(f"Errors:         {stats['errors']}")
    return "\n".join(lines)


@mcp.tool()
def list_repos(directory: str) -> str:
    """
    List the git repos found under an indexed directory that contains several repos.
    Use the returned names as the `repo` filter in the other tools.

    Args:
        directory: Absolute path to the parent directory that has been indexed.

    Returns:
        Repo names with indexed file and symbol counts.
    """
    from .indexer import db_path_for
    from .searcher import list_repos as _repos
    target = _resolve(directory)
    if not db_path_for(target).exists():
        return f"No index found for {target}. Run run_index first."
    repos = _repos(target)
    if not repos:
        return "No repos recorded. Either this directory is itself a single repo, or it needs re-indexing."
    lines = [f"{len(repos)} repo(s) under {target}:\n"]
    for r in repos:
        lines.append(f"  {r['repo']}  ({r['files']} files, {r['symbols']} symbols)")
    return "\n".join(lines)


@mcp.tool()
def list_indexes() -> str:
    """
    List every code index in the index store, with its source directory, branch,
    size and whether it is stale (directory or branch deleted).

    Returns:
        One entry per index. Indexes created by an older version show an unknown
        origin until their directory is re-indexed.
    """
    from .indexer import list_indexes as _list
    infos = _list()
    if not infos:
        return "No indexes found."
    lines = [f"{len(infos)} index(es):\n"]
    for i in infos:
        where = i["root"] or "(unknown origin)"
        branch = f" [branch {i['branch']}]" if i["branch"] else ""
        counts = f"{i['files']} files, {i['symbols']} symbols" if i["files"] is not None else "unreadable"
        lines.append(f"  {where}{branch}: {counts}, {i['size'] // 1024} KB")
        if i.get("stale_reason"):
            lines.append(f"    STALE: {i['stale_reason']}")
    return "\n".join(lines)


@mcp.tool()
def clean_index(
    directory: str,
    branch: Optional[str] = None,
    all_branches: bool = False,
) -> str:
    """
    Delete the index for a project directory. It can be rebuilt with run_index.

    Args:
        directory:    Path of the project directory whose index to delete (need not exist any more).
        branch:       Delete that branch's index instead of the working-tree index.
        all_branches: Delete the working-tree index and all branch indexes for the directory.

    Returns:
        Which indexes were removed.
    """
    from .indexer import clean_indexes
    removed = clean_indexes(directory=Path(directory), branch=branch, all_branches=all_branches)
    if not removed:
        return f"No matching index found for {directory}."
    ok = [i for i in removed if i["removed"]]
    lines = [f"Removed {len(ok)} index(es) for {directory}."]
    for i in removed:
        if not i["removed"]:
            lines.append(f"Could not remove {i['name']}: {i['error']}")
    return "\n".join(lines)


@mcp.tool()
def clean_stale_indexes(dry_run: bool = True) -> str:
    """
    Remove indexes whose source directory or branch no longer exists (and unreadable ones).
    Defaults to a dry run that only reports what would be removed.

    Args:
        dry_run: If True (default), report only. Set False to actually delete.

    Returns:
        The stale indexes found and, unless dry_run, whether each was removed.
    """
    from .indexer import clean_indexes
    found = clean_indexes(stale=True, dry_run=dry_run)
    if not found:
        return "No stale indexes."
    verb = "Would remove" if dry_run else "Removed"
    lines = []
    for i in found:
        status = "" if dry_run or i["removed"] else f" (FAILED: {i['error']})"
        lines.append(f"{verb}: {i['root'] or i['name']}"
                     f"{' [branch ' + i['branch'] + ']' if i['branch'] else ''} - {i['stale_reason']}{status}")
    return "\n".join(lines)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    import argparse
    from . import log as _log

    parser = argparse.ArgumentParser(
        prog="code-indexer-mcp",
        description="code-indexer MCP server (stdio). Logs go to stderr.",
    )
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="-v per-file progress, -vv debug. Can also set CODE_INDEXER_VERBOSITY.")
    parser.add_argument("-q", "--quiet", action="store_true", help="Only log errors.")
    args, _ = parser.parse_known_args()
    _log.configure(args.verbose, args.quiet)
    _log.logger.debug("starting MCP server (verbosity=%d)", _log.get_level())
    mcp.run()


if __name__ == "__main__":
    main()
