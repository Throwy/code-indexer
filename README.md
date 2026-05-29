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
- `-v`                Verbose per-file output
- `--branch <name>`   Index a specific git branch instead of the working tree

```bash
# Index a specific branch
code-indexer index /path/to/myproject --branch feature/auth
```

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
```

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

All three commands accept `--branch <name>` and `--json` flags.

---

## 6. Index status

```bash
code-indexer status --dir /path/to/myproject

# Check stats for a specific branch index
code-indexer status --dir /path/to/myproject --branch feature/auth
```

---

## 7. Connect to Claude Desktop (MCP)

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
| `search_code` | Full-text keyword search across an indexed project |
| `index_status` | Check if a directory is indexed and see stats |
| `get_file_symbols` | List all functions/classes in a specific file |
| `goto_symbol` | Find the definition(s) of a symbol by exact name |
| `find_implementations` | Find all types that implement or extend a base class/interface |
| `find_usages` | Find symbols whose body references a given name (approximate) |
| `run_index` | Trigger indexing of a directory from within Claude |

### Example Claude prompts once connected:

- *"What does the authentication flow look like in ~/myproject?"*
- *"Find all functions related to database connections"*
- *"Show me all the symbols in src/api/auth.ts"*
- *"What classes implement IRepository?"*
- *"Where is parse_jwt defined?"*

---

## 8. Re-indexing

Re-run `code-indexer index <dir>` any time files change. Only modified files
are re-processed, so incremental re-indexes are fast.

For automatic re-indexing you could set up a file watcher or a scheduled task.
