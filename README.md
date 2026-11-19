# code-indexer — Setup Guide

## 1. Install

Clone or download the repo, then install it into your Python environment:

```bash
pip install -e /path/to/code-indexer
```

This registers two commands available anywhere on your system:
- `code-indexer` — CLI for indexing and searching
- `code-indexer-mcp` — stdio MCP server for Claude

---

## 2. Update (already installed)

If you've pulled new changes and want to pick them up without reinstalling:

```bash
pip install -e /path/to/code-indexer --upgrade
```

Because the package is installed in editable mode (`-e`), Python code changes
take effect immediately — no reinstall needed for those. Re-running the command
above is only required when `pyproject.toml` changes (new dependencies, new
entry points, etc.).

---

## 3. Index a project

```bash
code-indexer index /path/to/myproject
```

This creates a `.code-index.db` file inside the target directory.

Options:
- `--force`           Re-index all files even if unchanged
- `-v`                Per-repo/branch summaries (`-vv` per-file, `--debug` for debug; see [Verbosity](#11-verbosity-and-logging))
- `--branch <name>`   Index a specific git branch instead of the default (`main`, else `master`)
- `--worktree`        Index the working tree (uncommitted changes included) instead of a branch

With neither flag, a git repo (or parent folder of repos) is indexed on `main`
(else `master`); a folder with no git repos is indexed as a plain working tree.

```bash
# Default: index main (else master)
code-indexer index /path/to/myproject

# Index a specific branch
code-indexer index /path/to/myproject --branch feature/auth

# Index the working tree, uncommitted changes included
code-indexer index /path/to/myproject --worktree
```

Searching needs no flag for the default: with no `--branch`, queries use the
working-tree index if you made one, otherwise `main`/`master` (see
[Querying without `--branch`](#querying-without---branch)).

---

## 4. Search from the CLI

```bash
# Full-text keyword search
code-indexer search "UserService" --dir /path/to/myproject

# JSON output (for scripting / MCP integration)
code-indexer search "database connection pool" --dir /path/to/myproject --json

# Limit results
code-indexer search "parse token" --dir /path/to/myproject -n 20

# Search a specific branch's index
code-indexer search "UserService" --dir /path/to/myproject --branch feature/auth

# Prefix (wildcard) search
code-indexer search "User*" --dir /path/to/myproject
```

### Query syntax

- **Terms are OR-ed.** A symbol matches if it contains *any* of the terms in its
  name, signature, docstring, or body. Symbols that match more terms rank higher,
  so `"parse JWT token"` puts symbols containing all three at the top.
- **Trailing `*` matches a prefix.** `User*` matches `UserService`, `UserRepo`,
  etc. Only a trailing `*` counts; a `*` elsewhere is searched as literal text,
  and a bare `*` is ignored.
- **Punctuation is safe.** Each term is quoted before it reaches SQLite FTS5, so
  terms like `foo-bar` or `a:b` are searched as text instead of being parsed as
  FTS5 operators. This also means raw FTS5 syntax (`AND`, `NEAR`, `-term`,
  column filters) isn't supported; those are searched as ordinary words.

The MCP `search_code` tool uses the same syntax.

---

## 5. Symbol navigation

### Go to definition

```bash
code-indexer goto UserService --dir /path/to/myproject

# Filter by symbol kind: function, class, method, interface, struct
code-indexer goto parse_jwt --dir /path/to/myproject --kind function
```

### Find implementations

```bash
# Find all classes that extend or implement an interface/base class
code-indexer impls IRepository --dir /path/to/myproject
```

### Find usages / references

```bash
# Find all symbols whose body references a given name
code-indexer refs parse_jwt --dir /path/to/myproject

# Limit results
code-indexer refs UserService --dir /path/to/myproject -n 30
```

All three commands accept `--branch <name>`, `--repo <name>` and `--json` flags.

---

## 6. Multiple repos in one directory

Point the indexer at a parent folder that contains several git repos and it
builds a single index across all of them:

```bash
code-indexer index ~/dev
code-indexer repos --dir ~/dev          # list discovered repos with file/symbol counts
```

- Repos are discovered up to 3 levels below the folder (hidden dirs and ignored
  dirs like `node_modules` are skipped; the walk doesn't descend into a repo
  once found). Each file is tagged with its repo, named by its path relative to
  the parent (e.g. `api` or `group/beta`).
- Paths in results include the repo prefix, e.g. `api/src/auth.py`.
- Restrict any search to one repo with `--repo`:

```bash
code-indexer search "UserService" --dir ~/dev --repo api
code-indexer goto UserService --dir ~/dev --repo group/beta
code-indexer impls IRepository --dir ~/dev --repo api
code-indexer refs parse_jwt --dir ~/dev --repo api
```

- If the folder you index is itself a git repo, nothing changes: it is indexed
  as a single repo with no repo tags.
- Indexes created before this feature upgrade automatically; unchanged files
  just get their repo tag filled in on the next `index` run.
- Each repo's `.gitignore` (plus `.git/info/exclude` and your global excludes
  file) is respected, along with the built-in ignore list.
- Indexing a parent folder with no flags indexes every repo on its own `main`
  (else `master`); `--worktree` indexes the working trees instead.
- `--branch` works on a parent folder too: every repo is indexed on that branch
  (committed files; working trees are untouched), and `index` / `search` / `repos`
  etc. take the same `--branch` to query it. A repo that lacks the branch falls
  back to its default branch (origin's HEAD, else `main`/`master`) and the
  summary lists which repos fell back. A branch that exists in no repo is an
  error.
- `--branch main` also matches `master`: a repo that only has `master` is
  indexed from it (this applies to single repos too, and isn't reported as a
  fallback). The reverse (`--branch master` matching `main`) does not apply.
- A branch that exists only as `origin/<branch>` is used if there is no local one.
<a id="querying-without---branch"></a>
- **Querying without `--branch`:** the working-tree index (from `--worktree`)
  is used if there is one. If there isn't, but the directory has exactly one branch index, that
  branch is used automatically (the CLI prints a note to stderr; MCP tools
  prefix their answer with it). If there are several branch indexes, `main`
  is used (else `master`); if neither is indexed, the command lists them and
  asks you to pass `--branch`. The same applies to the
  MCP tools' `branch` parameter.

```bash
code-indexer index ~/dev --branch feature/auth
code-indexer search "UserService" --dir ~/dev --branch feature/auth
```

---

## 7. Index status

```bash
code-indexer status --dir /path/to/myproject

# Check stats for a specific branch index
code-indexer status --dir /path/to/myproject --branch feature/auth
```

---

## 8. Connect to Claude Desktop (MCP)

Add the following to your Claude Desktop config file.

**Config file location:**
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- macOS:   `~/Library/Application Support/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "code-indexer": {
      "command": "code-indexer-mcp"
    }
  }
}
```

No `cwd` required — the command works from any directory after installation.

Restart Claude Desktop after editing. You should see "code-indexer" in the MCP tools panel.

### Tools Claude will have access to:

| Tool | Description |
|------|-------------|
| `search_code` | Full-text keyword search across an indexed project (optional `repo` filter) |
| `index_status` | Check if a directory is indexed and see stats |
| `get_file_symbols` | List all functions/classes in a specific file |
| `goto_symbol` | Find the definition(s) of a symbol by exact name (optional `repo` filter) |
| `find_implementations` | Find all types that implement or extend a base class/interface (optional `repo` filter) |
| `find_usages` | Find symbols whose body references a given name (approximate; optional `repo` filter) |
| `run_index` | Trigger indexing of a directory from within Claude (optional `branch`, or `worktree` to index the working tree; defaults to main/master) |
| `list_repos` | List the git repos found under an indexed parent directory |
| `list_indexes` | List every stored index with its directory, branch, size and staleness |
| `clean_index` | Delete the index for a directory (or one branch / all branches) |
| `clean_stale_indexes` | Remove indexes whose directory or branch is gone (dry run by default) |

Every search/navigation tool, `index_status`, `get_file_symbols` and `list_repos`
also accept an optional `branch` to query that branch's index.

### Example Claude prompts once connected:

- *"What does the authentication flow look like in ~/myproject?"*
- *"Find all functions related to database connections"*
- *"Show me all the symbols in src/api/auth.ts"*
- *"What classes implement IRepository?"*
- *"Where is parse_jwt defined?"*
- *"Index ~/dev and find UserService in the api repo"*

---

## 9. Re-indexing

Re-run `code-indexer index <dir>` any time files change. Only modified files
are re-processed, so incremental re-indexes are fast.

For automatic re-indexing you could set up a file watcher or a scheduled task.

---

## 10. Cleaning up indexes

Indexes live in one store (`%LOCALAPPDATA%\code-indexer` on Windows,
`$XDG_CACHE_HOME/code-indexer` or `~/.cache/code-indexer` elsewhere), outside
your projects. They are safe to delete; re-running `index` rebuilds them.

```bash
# See everything in the store: origin, branch, size, and which are stale
code-indexer indexes

# Delete the working-tree index for a directory (it need not exist any more)
code-indexer clean --dir /path/to/myproject

# Delete one branch's index, or the working-tree index plus every branch index
code-indexer clean --dir /path/to/myproject --branch feature/auth
code-indexer clean --dir /path/to/myproject --all-branches

# Delete indexes whose directory or branch no longer exists (and unreadable ones)
code-indexer clean --stale

# Delete everything
code-indexer clean --all
```

- Add `--dry-run` to any `clean` to preview what would be removed and the space
  freed. `--stale` and `--all` ask for confirmation unless you pass `-y`.
- Choose exactly one of `--dir`, `--stale` or `--all`.
- Staleness is judged from the directory and branch recorded in each index.
  Indexes created before this feature show an "unknown origin" and are never
  treated as stale; re-run `index` on that directory to record it (or remove
  them with `clean --dir` / `clean --all`).
- If an index is open in another process (e.g. the MCP server mid-search),
  Windows may refuse to delete it; the command reports it and you can retry.

---

## 11. Verbosity and logging

Every command accepts `-v` / `--verbose` (repeat it for more), `--debug` and `-q` / `--quiet`.
The flag works before or after the subcommand: `code-indexer -v index .` and
`code-indexer index . -v` are the same.

| Flag | Level | What you see |
|------|-------|--------------|
| `-q` | quiet | Errors only; `index` and `clean` skip their summaries |
| *(none)* | default | Warnings and errors, plus each command's normal output |
| `-v` | verbose | Per-repo / per-branch summaries: files indexed, skipped, errors, stale removals, symbols |
| `-vv` | detail | Also per-file progress: `index`, `skip`, `error`, stale removals |
| `--debug` | debug | Everything above plus database paths, discovered repos, file counts, git details, query timings, index/clean decisions, and tracebacks for files that fail to parse |

```bash
code-indexer index ~/dev -v            # per-repo summaries
code-indexer index ~/dev -vv           # + per-file progress
code-indexer index ~/dev --debug       # + repos found, timings, DB path
code-indexer search "UserService" --dir ~/dev --debug     # + query timing and row counts
code-indexer clean --stale --dry-run --debug              # + why each index counts as stale
```

- Logs go to **stderr**; results and summaries stay on stdout, so
  `code-indexer search ... --json --debug > out.json` still produces clean JSON.
- Set a default with the `CODE_INDEXER_VERBOSITY` environment variable
  (`quiet`, `warning`, `info`, `detail`, `debug`, or `-1` to `3`). Flags override it, and
  `-q` always wins.
- The MCP server takes the same flags. Claude Desktop shows its stderr in the
  MCP log, so this is the way to see what it is doing:

```json
{
  "mcpServers": {
    "code-indexer": {
      "command": "code-indexer-mcp",
      "args": ["--debug"]
    }
  }
}
```

(or set `"env": { "CODE_INDEXER_VERBOSITY": "debug" }` instead of `args`).
