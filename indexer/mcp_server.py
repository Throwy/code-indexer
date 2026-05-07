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
) -> str:
    """
    Search an indexed codebase for symbols matching the query.

    Args:
        query:     Keyword query, e.g. "parse JWT token" or "UserService".
        directory: Absolute path to the project directory that has been indexed.
        limit:     Maximum number of results to return (default 10).

    Returns:
        Formatted list of matching symbols with file paths and line numbers.
    """
    from .searcher import fts_search
    target = _resolve(directory)
    results = fts_search(target, query, limit=limit)
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
        verbose=False,
    )

    lines = [
        f"Indexing complete for {target}",
        f"Files scanned:  {stats['files_scanned']}",
        f"Files indexed:  {stats['files_indexed']}",
        f"Files skipped:  {stats['files_skipped']} (unchanged)",
        f"Symbols added:  {stats['symbols_added']}",
    ]
    if stats.get("errors"):
        lines.append(f"Errors:         {stats['errors']}")
    return "\n".join(lines)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    mcp.run()


if __name__ == "__main__":
    main()
