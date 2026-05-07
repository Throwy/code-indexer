"""
CLI entry point for code-indexer.

Usage:
    python -m indexer index ./myproject
    python -m indexer search "parse authentication token"
    python -m indexer search "UserService" --mode fts
    python -m indexer status
"""

import json
import sys
from pathlib import Path

import click

from .indexer import index_directory, get_db, db_path_for


@click.group()
def cli():
    """code-indexer — index and search your codebase locally."""
    pass


@cli.command()
@click.argument("directory", default=".", type=click.Path(exists=True, file_okay=False))
@click.option("--force", is_flag=True, help="Re-index all files even if unchanged.")
@click.option("--verbose", "-v", is_flag=True, help="Print per-file progress.")
def index(directory, force, verbose):
    """Index source files in DIRECTORY (default: current dir)."""
    target = Path(directory).resolve()
    click.echo(f"Indexing {target} ...")

    stats = index_directory(
        target_dir=target,
        force=force,
        verbose=verbose,
    )

    click.echo("\nDone.")
    click.echo(f"  Files scanned:  {stats['files_scanned']}")
    click.echo(f"  Files indexed:  {stats['files_indexed']}")
    click.echo(f"  Files skipped:  {stats['files_skipped']} (unchanged)")
    click.echo(f"  Symbols added:  {stats['symbols_added']}")
    if stats["errors"]:
        click.echo(f"  Errors:         {stats['errors']}", err=True)


@cli.command()
@click.argument("query")
@click.option("--dir", "directory", default=".", type=click.Path(exists=True, file_okay=False),
              help="Directory whose index to search (default: current dir).")
@click.option("--mode", type=click.Choice(["fts"]), default="fts",
              help="Search mode.")
@click.option("--limit", "-n", default=10, help="Max results (default: 10).")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def search(query, directory, mode, limit, as_json):
    """Search the index for QUERY."""
    target = Path(directory).resolve()
    db_path = db_path_for(target)

    if not db_path.exists():
        click.echo(f"No index found for {target}. Run `python -m indexer index {directory}` first.", err=True)
        sys.exit(1)

    from .searcher import fts_search

    results = fts_search(target, query, limit=limit)

    if as_json:
        click.echo(json.dumps(results, indent=2))
        return

    if not results:
        click.echo("No results found.")
        return

    click.echo(f"\n{len(results)} result(s) for '{query}'\n")
    for i, r in enumerate(results, 1):
        score_str = f"  score={r['score']:.3f}" if r["score"] is not None else ""
        click.echo(f"{i:>2}. [{r['kind']}] {r['name']}{score_str}")
        click.echo(f"    {r['path']}:{r['start_line']}-{r['end_line']}")
        if r.get("signature"):
            sig = r["signature"].replace("\n", " ")[:120]
            click.echo(f"    {sig}")
        if r.get("docstring"):
            doc = r["docstring"].replace("\n", " ")[:100]
            click.echo(f"    \"{doc}\"")
        click.echo()


@cli.command()
@click.option("--dir", "directory", default=".", type=click.Path(exists=True, file_okay=False),
              help="Directory to inspect.")
def status(directory):
    """Show index statistics for DIRECTORY."""
    target = Path(directory).resolve()
    db_path = db_path_for(target)

    if not db_path.exists():
        click.echo(f"No index found for {target}.")
        return

    db = get_db(target)
    s = db.stats()
    db.close()

    click.echo(f"\nIndex: {db_path}")
    click.echo(f"  Files:    {s['files']}")
    click.echo(f"  Symbols:  {s['symbols']}")
    if s["languages"]:
        click.echo("  Languages:")
        for lang, count in s["languages"]:
            click.echo(f"    {lang:<15} {count} files")


if __name__ == "__main__":
    cli()
