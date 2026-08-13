## Following Along (YouTube Tutorial)

This project follows [MCP Tutorial: Build Your First MCP Server](https://www.youtube.com/watch?v=jLM6n4mdRuA) — steps below mirror that walkthrough, adapted for `uv` and the current v2 SDK.

### Step 1: Initialize the project and install the MCP SDK

From an empty repo, in your terminal:

```bash
uv init
uv add "mcp[cli]"
```

This creates `pyproject.toml`, `uv.lock`, and a `.venv`, with `mcp[cli]`
installed as a dependency.

### Step 2: Write the server

Create `main.py` in the project root:
(paste the code above)

This mirrors the PyPI quickstart skeleton (`from mcp.server import MCPServer`, `@mcp.tool()` decorators), swapped in with the standup journal's own tools:

- `log_task(task_description, status="done")` — log what you did.
- `get_tasks_by_date(log_date)` — see everything logged on a given day.
- `generate_standup_report()` — pull yesterday's done items and current
  blockers into a Slack-ready bulleted message.

**Note on the video:** the tutorial uses `FastMCP` from `mcp.server.fastmcp`(the v1.x SDK API from April 2025). `uv add "mcp[cli]"` today installs the v2 SDK, where `FastMCP` was renamed `MCPServer` and moved to `mcp.server`. The concepts and decorators (`@mcp.tool()`, `@mcp.resource()`) are unchanged; only the import path and class name differ.

Data lives in a local SQLite file at `~/.standup-journal/standups.db`
(override with `STANDUP_DB_PATH`), created automatically on first run.

### Step 3: Install the server into Claude Desktop

From the project root:

```bash
uv run mcp install main.py
```

This imports `main.py`, reads the server's name (`"standup-journal"`, set
via `MCPServer("standup-journal")`), and writes the launch command directly
into Claude Desktop's config file for you — no manual JSON editing needed.

Example output:

\`\`\`
INFO     Added server 'standup-journal' to Claude config
INFO     Successfully installed standup-journal in Claude app
\`\`\`

**Fully quit Claude Desktop (not just the window) and reopen it** for the new server to show up. Your tools — `log_task`, `update_task_status`, `get_tasks_by_date`, `get_tasks_between`, `search_tasks`, `list_tags`, `generate_standup_report`, `generate_exec_summary`, `generate_weekly_summary` — should then be available in chat.

Under the hood, `mcp install` writes an entry that launches your server via `uv run`, pinned to the exact `mcp` version installed in your project, so Claude Desktop always runs it in the right environment, without you needing `standup-journal` on your system `PATH`.

### Step 4: Verify the tools are visible in Claude Desktop

`mcp install` edits the config file automatically, but if you ever need to check or edit it by hand (or just confirm the entry is there), the config lives under Claude Desktop's **Developer** settings.

1. Open Claude Desktop.
2. Open the app menu (Windows/Linux: hamburger menu; macOS: the app name in the system menu bar) and select **Settings**.
3. Go to the **Developer** tab.
4. Click **Edit Config** — this opens `claude_desktop_config.json` in your default text editor. You should see a `standup-journal` entry under `mcpServers`, written there by `mcp install` in Step 3.
5. Save the file (even with no changes) and **fully quit and reopen Claude Desktop** to load the tools.

Once it restarts, click the **`+`** (or paperclip) icon in the chat box and select **Connectors** — `standup-journal` should be listed there, with your tools available.

## Testing in Claude Desktop

Once `standup-journal` shows as connected under **Connectors** (Step 4), test it with natural chat requests. Claude decides which tool to call based on what you ask and each tool's docstring.

### Try this sequence

1. **Log a completed task:**
   > "Log that I finished the login screen styling."

Claude should call `log_task`. The first time, you'll get a permission prompt, approve it. Look for an expandable tool-call block in the chat showing the tool name and arguments used.
![Permission Prompt](mcppermissions.png)

2. **Log a blocker:**
   > "Log that I'm blocked on the AWS deployment because permissions are broken."

3. **Check what's logged today:**
   > "What have I logged for today?"
![Log Example1](logmcpexample.png)
![Log Example2](logmcpexample1.png)

Should trigger `get_tasks_by_date` with today's date.

4. **Generate the standup report:**
   > "Generate my standup report."

   Should call `generate_standup_report` and return a bulleted, Slack-ready summary of yesterday's done items, today's in-progress items, and open blockers.

### If a tool doesn't get called

Claude decides *whether* to call a tool based on phrasing. Try being more explicit ("use the standup journal to log...") if it's not picking it up.

Verify the data actually landed by checking the SQLite file directly:

\`\`\`bash
sqlite3 ~/.standup-journal/standups.db "SELECT * FROM tasks;"
\`\`\`

(adjust the path if you set `STANDUP_DB_PATH`)

### If the connector won't connect or shows an error

Claude Desktop keeps a log per server:

- **macOS:** `~/Library/Logs/Claude/mcp-server-standup-journal.log`
- **Windows:** `%APPDATA%\Claude\logs\`

Check there for a Python traceback if `main.py` failed to start.

Usually the issue is anything printed to stdout before the server starts. Stdio *is* the protocol channel for this transport, so a stray `print()` statement corrupts the connection. Use `logging` (which writes to stderr) instead of `print()` if you need to debug inside the server.

Sources: [MCP docs — Connect local servers](https://modelcontextprotocol.io/docs/2026-07-28/develop/connect-local-servers), [Claude docs — MCP Apps troubleshooting](https://claude.com/docs/connectors/building/mcp-apps/troubleshooting), [PyPi MCP Docs](https://pypi.org/project/mcp/)

---

## Additional tools (added after Step 2)

- `update_task_status(task_id, status)` — resolve a blocker or change any logged task's status.
- `log_task(..., tag?)` — optional free-text category, inferred from phrasing like "tag this as X" / "log under X", or set explicitly. No predefined list — invent tags as you go. Each tag gets a consistent royal-themed emoji (👑💎🦋🕯️ etc.), assigned automatically per tag name.
- `get_tasks_by_date` / `get_tasks_between(start_date, end_date)` — both accept an optional `tag` filter.
- `search_tasks(keyword)` — find past entries by keyword, any date/status.
- `list_tags()` — all tags currently in use, with open task counts.
- `generate_exec_summary(days=7)` — quick counts (done/in progress/blocked) plus open blockers.
- `generate_weekly_summary(week_start?)` — 7-day rollup for retros or 1:1s.

Talking naturally works across all of these — e.g. "Finished the login screen, still working on the API integration, tag it acme-client, and I'm blocked on AWS permissions" gets split into separate logged entries automatically.
