# code-indexer — Setup Guide

## 1. Install dependencies

```bash
cd C:\dev\code-indexer
pip install -r requirements.txt
```

---

## 2. Index a project

```bash
python -m indexer index C:\dev\myproject
```

This creates a `.code-index.db` file inside the target directory.

Options:
- `--force`  Re-index all files even if unchanged
- `-v`       Verbose per-file output

---

## 3. Search from the CLI

```bash
# Full-text keyword search
python -m indexer search "UserService" --dir C:\dev\myproject

# JSON output (for scripting / MCP integration)
python -m indexer search "database connection pool" --dir C:\dev\myproject --json
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
      "command": "python",
      "args": ["-m", "indexer.mcp_server"],
      "cwd": "C:\\dev\\code-indexer"
    }
  }
}
```

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

Re-run `python -m indexer index <dir>` any time files change. Only modified files
are re-processed, so incremental re-indexes are fast.

For automatic re-indexing you could set up a file watcher or a scheduled task.
