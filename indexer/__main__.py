"""
CLI entry point for code-indexer.

Usage:
    python -m indexer index ./myproject
    python -m indexer search "parse authentication token"
    python -m indexer search "UserService" --mode fts
    python -m indexer status
"""

import functools
import json
import sys
from pathlib import Path

import click

from . import log as _log
from .indexer import index_directory, index_branch, index_default, get_db, db_path_for, resolve_index


def verbosity_options(f):
    """Add -v/--verbose (repeatable) and -q/--quiet to a command and apply them."""
    @click.option("--verbose", "-v", count=True,
                  help="More output: -v per-file progress, -vv debug (timings, paths, tracebacks). Logs go to stderr.")
    @click.option("--quiet", "-q", is_flag=True, help="Suppress progress and summary output; errors still show.")
    @functools.wraps(f)
    def wrapper(*args, verbose=0, quiet=False, **kwargs):
        _log.configure(verbose, quiet)
        return f(*args, **kwargs)
    return wrapper


def _use_index(target: Path, branch, directory, soft: bool = False):
    """Pick the index to query (see resolve_index); explain and exit if there is none."""
    branch, note, error = resolve_index(target, branch, directory)
    if note and _log.get_level() >= 0:
        click.echo(note, err=True)
    if error:
        click.echo(error, err=not soft)
        sys.exit(0 if soft else 1)
    return branch


@click.group()
@click.option("--verbose", "-v", count=True,
              help="More output: -v per-file progress, -vv debug. Also accepted after the subcommand.")
@click.option("--quiet", "-q", is_flag=True, help="Suppress progress and summary output; errors still show.")
def cli(verbose, quiet):
    """code-indexer — index and search your codebase locally."""
    _log.configure(verbose, quiet, from_group=True)


@cli.command()
@click.argument("directory", default=".", type=click.Path(exists=True, file_okay=False))
@click.option("--force", is_flag=True, help="Re-index all files even if unchanged.")
@click.option("--branch", default=None, help="Index a git branch (committed files) instead of the working tree. In a parent dir of repos, every repo is indexed on this branch; repos lacking it fall back to their default branch.")
@click.option("--worktree", is_flag=True, help="Index the working tree (uncommitted changes included) instead of the default branch.")
@verbosity_options
def index(directory, force, branch, worktree):
    """Index source files in DIRECTORY (default: current dir).

    Without --branch or --worktree, indexes the main branch (else master).
    """
    target = Path(directory).resolve()
    if branch and worktree:
        click.echo("Use either --branch or --worktree, not both.", err=True)
        sys.exit(1)

    quiet = _log.get_level() < 0
    if not branch and not worktree:
        if not quiet:
            click.echo(f"Indexing {target} (default branch) ...")
        try:
            stats, branch = index_default(target, force=force)
        except ValueError as e:
            click.echo(str(e), err=True)
            sys.exit(1)
    elif branch:
        if not quiet:
            click.echo(f"Indexing branch '{branch}' in {target} ...")
        try:
            stats = index_branch(target_dir=target, branch=branch, force=force)
        except ValueError as e:
            click.echo(str(e), err=True)
            sys.exit(1)
    else:
        if not quiet:
            click.echo(f"Indexing {target} ...")
        stats = index_directory(target_dir=target, force=force)

    if not quiet:
        click.echo("\nDone.")
        click.echo(f"  Files scanned:  {stats['files_scanned']}")
        click.echo(f"  Files indexed:  {stats['files_indexed']}")
        click.echo(f"  Files skipped:  {stats['files_skipped']} (unchanged)")
        click.echo(f"  Symbols added:  {stats['symbols_added']}")
        if stats.get("repos"):
            click.echo(f"  Repos found:    {stats['repos']}")
        for name, ref in stats.get("fallbacks", {}).items():
            click.echo(f"  note: {name} has no branch '{branch}'; indexed {ref} instead")
        for name in stats.get("skipped_repos", []):
            click.echo(f"  note: {name} has no commits; skipped")
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
@click.option("--repo", default=None, help="Restrict to one repo (when the directory holds several).")
@verbosity_options
def search(query, directory, mode, limit, as_json, branch, repo):
    """Search the index for QUERY."""
    target = Path(directory).resolve()
    branch = _use_index(target, branch, directory)

    from .searcher import fts_search

    results = fts_search(target, query, limit=limit, branch=branch, repo=repo)

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
@click.option("--repo", default=None, help="Restrict to one repo (when the directory holds several).")
@verbosity_options
def goto(name, kind, directory, as_json, branch, repo):
    """Jump to the definition(s) of symbol NAME."""
    target = Path(directory).resolve()
    branch = _use_index(target, branch, directory)

    from .searcher import goto_symbol
    results = goto_symbol(target, name, kind=kind, branch=branch, repo=repo)

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
@click.option("--repo", default=None, help="Restrict to one repo (when the directory holds several).")
@verbosity_options
def impls(name, directory, as_json, branch, repo):
    """Find all types that implement or extend NAME."""
    target = Path(directory).resolve()
    branch = _use_index(target, branch, directory)

    from .searcher import find_implementations
    results = find_implementations(target, name, branch=branch, repo=repo)

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
@click.option("--repo", default=None, help="Restrict to one repo (when the directory holds several).")
@verbosity_options
def refs(name, directory, limit, as_json, branch, repo):
    """Find symbols that reference or call NAME (approximate body search)."""
    target = Path(directory).resolve()
    branch = _use_index(target, branch, directory)

    from .searcher import find_usages
    results = find_usages(target, name, limit=limit, branch=branch, repo=repo)

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
@verbosity_options
def status(directory, branch):
    """Show index statistics for DIRECTORY."""
    target = Path(directory).resolve()
    branch = _use_index(target, branch, directory, soft=True)
    db_path = db_path_for(target, branch=branch)

    db = get_db(target, branch=branch)
    s = db.stats()
    db.close()

    click.echo(f"\nIndex: {db_path}")
    if branch:
        click.echo(f"Branch: {branch}")
    click.echo(f"  Files:    {s['files']}")
    click.echo(f"  Symbols:  {s['symbols']}")
    if s["repos"]:
        click.echo(f"  Repos:    {s['repos']}")
    if s["languages"]:
        click.echo("  Languages:")
        for lang, count in s["languages"]:
            click.echo(f"    {lang:<15} {count} files")


@cli.command("repos")
@click.option("--dir", "directory", default=".", type=click.Path(exists=True, file_okay=False),
              help="Parent directory whose index to inspect (default: current dir).")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.option("--branch", default=None, help="Inspect the index for a specific branch.")
@verbosity_options
def repos_cmd(directory, as_json, branch):
    """List the git repos recorded in the index for DIRECTORY."""
    target = Path(directory).resolve()
    branch = _use_index(target, branch, directory, soft=True)
    from .searcher import list_repos
    rows = list_repos(target, branch=branch)
    if as_json:
        click.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        click.echo("No repos recorded (single-repo index, or re-index needed).")
        return
    click.echo(f"\n{len(rows)} repo(s):\n")
    for r in rows:
        click.echo(f"  {r['repo']:<30} {r['files']} files, {r['symbols']} symbols")


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _print_indexes(infos: list[dict]):
    import datetime
    for i in infos:
        where = i["root"] or "(unknown origin - re-index to record it)"
        branch = f"  branch={i['branch']}" if i["branch"] else ""
        when = (datetime.datetime.fromtimestamp(i["indexed_at"] or i["modified"])
                .strftime("%Y-%m-%d %H:%M"))
        counts = f"{i['files']} files, {i['symbols']} symbols" if i["files"] is not None else "unreadable"
        click.echo(f"  {i['name']}  {where}{branch}")
        click.echo(f"    {counts}  |  {_fmt_size(i['size'])}  |  {when}")
        if i.get("stale_reason"):
            click.echo(f"    STALE: {i['stale_reason']}")


@cli.command("indexes")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@verbosity_options
def indexes_cmd(as_json):
    """List every index in the store, with size, origin and staleness."""
    from .indexer import list_indexes, _index_store_dir
    infos = list_indexes()
    if as_json:
        click.echo(json.dumps([{**i, "path": str(i["path"])} for i in infos], indent=2))
        return
    if not infos:
        click.echo(f"No indexes in {_index_store_dir()}.")
        return
    total = sum(i["size"] for i in infos)
    click.echo(f"\n{len(infos)} index(es) in {_index_store_dir()}  ({_fmt_size(total)} total)\n")
    _print_indexes(infos)


@cli.command()
@click.option("--dir", "directory", default=None, type=click.Path(file_okay=False),
              help="Remove the index for this directory (it need not exist any more).")
@click.option("--branch", default=None, help="With --dir: remove that branch's index instead.")
@click.option("--all-branches", is_flag=True, help="With --dir: remove the working-tree index and every branch index.")
@click.option("--stale", is_flag=True, help="Remove indexes whose directory or branch no longer exists, and unreadable ones.")
@click.option("--all", "everything", is_flag=True, help="Remove every index in the store.")
@click.option("--dry-run", is_flag=True, help="Show what would be removed without deleting anything.")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation for --stale / --all.")
@verbosity_options
def clean(directory, branch, all_branches, stale, everything, dry_run, yes):
    """Delete indexes. Choose one of --dir, --stale or --all."""
    from .indexer import clean_indexes

    scopes = [directory is not None, stale, everything]
    if sum(scopes) != 1:
        click.echo("Choose exactly one of --dir, --stale or --all.", err=True)
        sys.exit(2)
    if (branch or all_branches) and directory is None:
        click.echo("--branch / --all-branches only apply together with --dir.", err=True)
        sys.exit(2)
    if branch and all_branches:
        click.echo("Use either --branch or --all-branches, not both.", err=True)
        sys.exit(2)

    kwargs = dict(directory=Path(directory) if directory else None, branch=branch,
                  all_branches=all_branches, stale=stale, everything=everything)
    preview = clean_indexes(dry_run=True, **kwargs)
    if not preview:
        click.echo("Nothing to clean.")
        return

    quiet = _log.get_level() < 0 and not dry_run
    needs_confirm = (stale or everything) and not yes and not dry_run
    if not quiet or needs_confirm:  # never ask for confirmation without showing what is affected
        click.echo(f"\n{'Would remove' if dry_run else 'Removing'} {len(preview)} index(es):\n")
        _print_indexes(preview)
    if dry_run:
        click.echo(f"\nDry run - {_fmt_size(sum(i['size'] for i in preview))} would be freed.")
        return
    if (stale or everything) and not yes:
        click.confirm("\nProceed?", abort=True)

    done = clean_indexes(**kwargs)
    freed = sum(i["size"] for i in done if i["removed"])
    failed = [i for i in done if not i["removed"]]
    if not quiet:
        click.echo(f"\nRemoved {len(done) - len(failed)} index(es), freed {_fmt_size(freed)}.")
    for i in failed:
        click.echo(f"  could not remove {i['name']}: {i['error']}", err=True)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    cli()
