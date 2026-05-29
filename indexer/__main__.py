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

from .indexer import index_directory, index_branch, get_db, db_path_for


@click.group()
def cli():
    """code-indexer — index and search your codebase locally."""
    pass


@cli.command()
@click.argument("directory", default=".", type=click.Path(exists=True, file_okay=False))
@click.option("--force", is_flag=True, help="Re-index all files even if unchanged.")
@click.option("--verbose", "-v", is_flag=True, help="Print per-file progress.")
@click.option("--branch", default=None, help="Index a specific git branch instead of the working tree.")
def index(directory, force, verbose, branch):
    """Index source files in DIRECTORY (default: current dir)."""
    target = Path(directory).resolve()

    if branch:
        click.echo(f"Indexing branch '{branch}' in {target} ...")
        try:
            stats = index_branch(target_dir=target, branch=branch, force=force, verbose=verbose)
        except ValueError as e:
            click.echo(str(e), err=True)
            sys.exit(1)
    else:
        click.echo(f"Indexing {target} ...")
        stats = index_directory(target_dir=target, force=force, verbose=verbose)

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
@click.option("--branch", default=None, help="Search the index for a specific branch.")
def search(query, directory, mode, limit, as_json, branch):
    """Search the index for QUERY."""
    target = Path(directory).resolve()
    db_path = db_path_for(target, branch=branch)

    if not db_path.exists():
        hint = f" --branch {branch}" if branch else ""
        click.echo(f"No index found for {target}{' branch ' + branch if branch else ''}. Run `python -m indexer index{hint} {directory}` first.", err=True)
        sys.exit(1)

    from .searcher import fts_search

    results = fts_search(target, query, limit=limit, branch=branch)

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
@click.argument("name")
@click.option("--kind", type=click.Choice(["function", "class", "method", "interface", "struct"]),
              default=None, help="Restrict to a specific symbol kind.")
@click.option("--dir", "directory", default=".", type=click.Path(exists=True, file_okay=False),
              help="Directory whose index to search (default: current dir).")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.option("--branch", default=None, help="Search the index for a specific branch.")
def goto(name, kind, directory, as_json, branch):
    """Jump to the definition(s) of symbol NAME."""
    target = Path(directory).resolve()
    db_path = db_path_for(target, branch=branch)
    if not db_path.exists():
        hint = f" --branch {branch}" if branch else ""
        click.echo(f"No index found for {target}{' branch ' + branch if branch else ''}. Run `python -m indexer index{hint} {directory}` first.", err=True)
        sys.exit(1)

    from .searcher import goto_symbol
    results = goto_symbol(target, name, kind=kind, branch=branch)

    if as_json:
        click.echo(json.dumps(results, indent=2))
        return

    if not results:
        click.echo(f"No definition found for '{name}'.")
        return

    click.echo(f"\n{len(results)} definition(s) for '{name}'\n")
    for i, r in enumerate(results, 1):
        click.echo(f"{i:>2}. [{r['kind']}] {r['name']}")
        click.echo(f"    {r['path']}:{r['start_line']}")
        if r.get("signature"):
            sig = r["signature"].replace("\n", " ")[:120]
            click.echo(f"    {sig}")
        click.echo()


@cli.command()
@click.argument("name")
@click.option("--dir", "directory", default=".", type=click.Path(exists=True, file_okay=False),
              help="Directory whose index to search (default: current dir).")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.option("--branch", default=None, help="Search the index for a specific branch.")
def impls(name, directory, as_json, branch):
    """Find all types that implement or extend NAME."""
    target = Path(directory).resolve()
    db_path = db_path_for(target, branch=branch)
    if not db_path.exists():
        hint = f" --branch {branch}" if branch else ""
        click.echo(f"No index found for {target}{' branch ' + branch if branch else ''}. Run `python -m indexer index{hint} {directory}` first.", err=True)
        sys.exit(1)

    from .searcher import find_implementations
    results = find_implementations(target, name, branch=branch)

    if as_json:
        click.echo(json.dumps(results, indent=2))
        return

    if not results:
        click.echo(f"No implementations found for '{name}'.")
        return

    click.echo(f"\n{len(results)} implementation(s) of '{name}'\n")
    for i, r in enumerate(results, 1):
        bases = ", ".join(r["base_classes"]) if r["base_classes"] else ""
        click.echo(f"{i:>2}. [{r['kind']}] {r['name']}  (extends/implements: {bases})")
        click.echo(f"    {r['path']}:{r['start_line']}")
        if r.get("signature"):
            sig = r["signature"].replace("\n", " ")[:120]
            click.echo(f"    {sig}")
        click.echo()


@cli.command()
@click.argument("name")
@click.option("--dir", "directory", default=".", type=click.Path(exists=True, file_okay=False),
              help="Directory whose index to search (default: current dir).")
@click.option("--limit", "-n", default=20, help="Max results (default: 20).")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.option("--branch", default=None, help="Search the index for a specific branch.")
def refs(name, directory, limit, as_json, branch):
    """Find symbols that reference or call NAME (approximate body search)."""
    target = Path(directory).resolve()
    db_path = db_path_for(target, branch=branch)
    if not db_path.exists():
        hint = f" --branch {branch}" if branch else ""
        click.echo(f"No index found for {target}{' branch ' + branch if branch else ''}. Run `python -m indexer index{hint} {directory}` first.", err=True)
        sys.exit(1)

    from .searcher import find_usages
    results = find_usages(target, name, limit=limit, branch=branch)

    if as_json:
        click.echo(json.dumps(results, indent=2))
        return

    if not results:
        click.echo(f"No references found for '{name}'.")
        return

    click.echo(f"\n{len(results)} reference(s) to '{name}'\n")
    for i, r in enumerate(results, 1):
        click.echo(f"{i:>2}. [{r['kind']}] {r['name']}")
        click.echo(f"    {r['path']}:{r['start_line']}-{r['end_line']}")
        if r.get("signature"):
            sig = r["signature"].replace("\n", " ")[:120]
            click.echo(f"    {sig}")
        click.echo()


@cli.command()
@click.option("--dir", "directory", default=".", type=click.Path(exists=True, file_okay=False),
              help="Directory to inspect.")
@click.option("--branch", default=None, help="Show stats for a specific branch index.")
def status(directory, branch):
    """Show index statistics for DIRECTORY."""
    target = Path(directory).resolve()
    db_path = db_path_for(target, branch=branch)

    if not db_path.exists():
        msg = f"No index found for {target}"
        if branch:
            msg += f" branch '{branch}'"
        click.echo(msg + ".")
        return

    db = get_db(target, branch=branch)
    s = db.stats()
    db.close()

    click.echo(f"\nIndex: {db_path}")
    if branch:
        click.echo(f"Branch: {branch}")
    click.echo(f"  Files:    {s['files']}")
    click.echo(f"  Symbols:  {s['symbols']}")
    if s["languages"]:
        click.echo("  Languages:")
        for lang, count in s["languages"]:
            click.echo(f"    {lang:<15} {count} files")


if __name__ == "__main__":
    cli()
