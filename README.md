# code-indexer — Setup Guide

## 1. Install

Clone or download the repo, then install it into your Python environment:

```bash
pip install -e C:\dev\code-indexer
```

This registers two commands available anywhere on your system:
- `code-indexer` — CLI for indexing and searching
- `code-indexer-mcp` — stdio MCP server for Claude

---

## 2. Index a project

```bash
code-indexer index C:\dev\myproject
```

This creates a `.code-index.db` file inside the target directory.

Options:
- `--force`  Re-index all files even if unchanged
- `-v`       Verbose per-file output

---

## 3. Search from the CLI

```bash
# Full-text keyword search
code-indexer search "UserService" --dir C:\dev\myproject

# JSON output (for scripting / MCP integration)
code-indexer search "database connection pool" --dir C:\dev\myproject --json
```

---

## 4. Connect to Claude Desktop (MCP)

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
| `run_index` | Trigger indexing of a directory from within Claude |

### Example Claude prompts once connected:

- *"What does the authentication flow look like in C:\dev\myproject?"*
- *"Find all functions related to database connections in C:\dev\dropkyck"*
- *"Show me all the symbols in src/api/auth.ts"*

---

## 5. Re-indexing

Re-run `code-indexer index <dir>` any time files change. Only modified files
are re-processed, so incremental re-indexes are fast.

For automatic re-indexing you could set up a file watcher or a scheduled task.
