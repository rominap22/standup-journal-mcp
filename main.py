import json
import logging
import os
import sqlite3
import threading
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

from mcp.server import MCPServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("standup-journal")

mcp = MCPServer("standup-journal")

CHECKLIST_PORT = int(os.environ.get("CHECKLIST_PORT", "9249"))

DB_PATH = os.environ.get(
    "STANDUP_DB_PATH",
    os.path.join(os.path.expanduser("~"), ".standup-journal", "standups.db"),
)


def get_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            description TEXT NOT NULL,
            status TEXT NOT NULL,
            log_date TEXT NOT NULL,
            tag TEXT,
            due_date TEXT
        )
        """
    )
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    if "tag" not in existing_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN tag TEXT")
    if "due_date" not in existing_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN due_date TEXT")
    conn.commit()
    return conn


_STOPWORDS = {
    "the", "a", "an", "on", "in", "is", "are", "still", "blocked",
    "blocking", "issue", "with", "and", "to", "of", "for", "i'm", "im",
    "am", "waiting", "stuck", "due", "because", "by",
}


def _significant_words(text: str) -> set[str]:
    words = {w.strip(".,!?").lower() for w in text.split()}
    return {w for w in words if w and w not in _STOPWORDS and len(w) > 2}


def _find_matching_open_blocker(conn: sqlite3.Connection, description: str):
    """Loose match: does this description share most of its meaningful
    keywords with an existing open blocker? Used to avoid duplicate blocker
    entries piling up across days when the same issue keeps coming up in
    standup, even when it's phrased differently each time.

    This is a simple keyword-overlap heuristic, not true fuzzy matching —
    it can miss genuinely different rephrasings or occasionally merge two
    unrelated blockers that happen to share several words. Good enough for
    a personal log; not meant to be bulletproof.
    """
    new_words = _significant_words(description)
    if not new_words:
        return None

    rows = conn.execute("SELECT * FROM tasks WHERE status = 'blocked'").fetchall()
    for row in rows:
        existing_words = _significant_words(row["description"])
        if not existing_words:
            continue
        overlap = new_words & existing_words
        smaller = min(len(new_words), len(existing_words))
        if smaller and len(overlap) / smaller >= 0.5:
            return row
    return None


_TAG_EMOJIS = [
    "👑", "💎", "💄", "🦋", "🍾", "🥂", "🏰", "🕯️", "🪞", "🔮",
    "🎭", "🖋️", "📿", "🧿", "🪩", "🎀", "🥀", "🌹", "🦢", "🐆",
    "🦚", "🧵", "🪆", "🗝️", "⚜️", "🏺", "🪄", "🎇", "🍇", "🍓",
]


def _tag_emoji(tag: str) -> str:
    """Deterministically map a tag name to an emoji from a royal/luxe
    palette, so the same tag always renders with the same emoji across
    every response — a rich, easy-to-scan visual marker per category,
    chosen automatically, no hardcoded category list required. You define
    tags freely just by naming them in conversation.
    """
    if not tag:
        return ""
    index = sum(ord(c) for c in tag) % len(_TAG_EMOJIS)
    return _TAG_EMOJIS[index]


def _tag_label(tag: str | None) -> str:
    if not tag or tag.startswith("subtask:"):
        return ""
    return f"{_tag_emoji(tag)} {tag}"


def _normalize_status(status: str) -> str:
    s = status.lower().strip()
    if s in ("done", "complete", "completed", "finished"):
        return "done"
    if s in ("blocked", "stuck", "waiting"):
        return "blocked"
    return "in_progress"


@mcp.tool()
def log_task(task_description: str, status: str = "done", tag: str | None = None, due_date: str | None = None) -> str:
    """Log a single piece of work: what you did, are doing, or are blocked on.

    If the user gives a rambling, multi-item update in one message (e.g.
    "finished the login screen, still working on the API integration, and
    I'm blocked on AWS permissions"), call this tool ONCE PER DISTINCT ITEM
    rather than logging it as one combined entry.

    Infer `status` from the language used for each item:
    - "done" -> finished, shipped, completed, past tense ("fixed", "wrote")
    - "in_progress" -> still working on, in the middle of, ongoing, todo, to do, need to, planning to
    - "blocked" -> blocked, stuck, waiting on, can't proceed until

    IMPORTANT: Always normalize status to exactly one of: "done", "in_progress", "blocked".
    Never use "todo", "to do", "in progress" (with space), or any other variant.

    `tag` is an optional project/category label (e.g. "frontend", "acme-client",
    "customer-advisor", "infra", "github"). Set it whenever the user's phrasing implies
    a category, using any of these patterns:
    - "log ___ under/as/for [category]"
    - "tag this as [category]"
    - "categorize/sort this as [category]"
    - simply mentioning a known project/client name in the task description
    Each distinct tag automatically gets a consistent colored-circle emoji
    in every response, so tasks stay visually grouped by category over time.
    If no category is stated or inferable from context, leave `tag` unset.

    `due_date` is an optional ISO date (YYYY-MM-DD) deadline. Set it whenever
    the user mentions a due date, deadline, or "by [date]" in their message.
    Parse natural language dates (e.g. "due Friday", "by next week", "due Aug 21")
    into ISO format.

    If `status` is "blocked" and there's already an open blocker with a very
    similar description, this updates that existing entry's date instead of
    creating a duplicate — so a recurring blocker doesn't pile up as
    multiple rows across days.
    """
    today = date.today().isoformat()
    conn = get_db()
    status = _normalize_status(status)

    if status == "blocked":
        existing = _find_matching_open_blocker(conn, task_description)
        if existing is not None:
            conn.execute(
                "UPDATE tasks SET log_date = ?, description = ?, tag = ?, due_date = ? WHERE id = ?",
                (today, task_description, tag or existing["tag"], due_date or existing["due_date"], existing["id"]),
            )
            conn.commit()
            conn.close()
            return (
                f"Existing blocker #{existing['id']} still open, refreshed to "
                f"{today}: {task_description}"
            )

    cur = conn.execute(
        "INSERT INTO tasks (description, status, log_date, tag, due_date) VALUES (?, ?, ?, ?, ?)",
        (task_description, status, today, tag, due_date),
    )
    conn.commit()
    conn.close()
    tag_note = f" [{_tag_label(tag)}]" if tag else ""
    due_note = f" (due {due_date})" if due_date else ""
    return f"Logged #{cur.lastrowid} ({status}){tag_note}{due_note} on {today}: {task_description}"


@mcp.tool()
def update_task_status(task_id: int, status: str) -> str:
    """Update the status of an existing logged task (e.g. unblock it).

    Use this when the user says a previously logged item has changed state
    — e.g. a blocker just got resolved, or something in progress is now
    done. `status` must be one of: "done", "in_progress", "blocked".
    """
    conn = get_db()
    status = _normalize_status(status)
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        conn.close()
        return f"No task found with id {task_id}."
    conn.execute(
        "UPDATE tasks SET status = ? WHERE id = ?",
        (status, task_id),
    )
    conn.commit()
    conn.close()
    return f"Task #{task_id} updated to '{status}': {row['description']}"


@mcp.tool()
def get_tasks_by_date(log_date: str, tag: str | None = None) -> str:
    """Retrieve all logged tasks for a given ISO date (YYYY-MM-DD).

    Optionally filter to a single `tag`/project.
    """
    conn = get_db()
    if tag:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE log_date = ? AND tag = ?", (log_date, tag)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE log_date = ?", (log_date,)
        ).fetchall()
    conn.close()
    if not rows:
        return f"No tasks logged for {log_date}" + (f" tagged '{tag}'." if tag else ".")
    return "\n".join(
        f"- #{r['id']} [{r['status']}]" + (f" ({_tag_label(r['tag'])})" if r["tag"] else "") + f" {r['description']}"
        for r in rows
    )


@mcp.tool()
def get_tasks_between(start_date: str, end_date: str, tag: str | None = None) -> str:
    """Retrieve all logged tasks within an inclusive date range (YYYY-MM-DD each).

    Use this for "what did I do last week" style questions instead of
    calling get_tasks_by_date multiple times. Optionally filter to a single
    `tag`/project.
    """
    conn = get_db()
    if tag:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE log_date BETWEEN ? AND ? AND tag = ? ORDER BY log_date ASC",
            (start_date, end_date, tag),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE log_date BETWEEN ? AND ? ORDER BY log_date ASC",
            (start_date, end_date),
        ).fetchall()
    conn.close()
    if not rows:
        return f"No tasks logged between {start_date} and {end_date}" + (f" tagged '{tag}'." if tag else ".")
    lines = [
        f"- {r['log_date']} #{r['id']} [{r['status']}]" + (f" ({_tag_label(r['tag'])})" if r["tag"] else "") + f" {r['description']}"
        for r in rows
    ]
    return f"Tasks from {start_date} to {end_date}:\n" + "\n".join(lines)


@mcp.tool()
def list_tags() -> str:
    """List all distinct tags/projects currently in use, with open task counts."""
    conn = get_db()
    rows = conn.execute(
        """
        SELECT tag, COUNT(*) as n FROM tasks
        WHERE tag IS NOT NULL AND status != 'done'
        GROUP BY tag ORDER BY n DESC
        """
    ).fetchall()
    conn.close()
    if not rows:
        return "No tags in use yet."
    return "\n".join(f"- {_tag_label(r['tag'])}: {r['n']} open task(s)" for r in rows)


def _group_by_tag_lines(rows) -> list[str]:
    """Render rows as bullets, grouped under a sub-header per tag if any
    row has one, otherwise as a flat bullet list."""
    if not rows:
        return []
    if not any(r["tag"] for r in rows):
        return [f"• {r['description']}" for r in rows]

    grouped: dict[str, list[str]] = {}
    for r in rows:
        grouped.setdefault(r["tag"] or "Other", []).append(r["description"])
    lines: list[str] = []
    for tag_name, descs in grouped.items():
        header = _tag_label(tag_name) if tag_name != "Other" else "Other"
        lines.append(f"  _{header}:_")
        lines.extend(f"  • {d}" for d in descs)
    return lines


@mcp.tool()
def generate_standup_report(include_weekly: bool = False, range_days: int = 7) -> str:
    """Generate a clean, bulleted Slack standup message from yesterday and today.

    Automatically groups items by tag/project if any logged tasks have one.
    Set include_weekly=True to append a rollup covering the last `range_days`
    days (default 7). The rollup follows the standard three-question standup
    format — Done / In Progress / Blocked — for that whole period, not just
    completed items, so it works for a weekly retro, a 1:1, or any custom
    stretch of time by adjusting range_days.
    """
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    conn = get_db()
    done_yesterday = conn.execute(
        "SELECT * FROM tasks WHERE log_date = ? AND status = 'done'",
        (yesterday,),
    ).fetchall()
    in_progress_today = conn.execute(
        "SELECT * FROM tasks WHERE log_date = ? AND status = 'in_progress'",
        (today,),
    ).fetchall()
    blockers = conn.execute(
        "SELECT * FROM tasks WHERE status = 'blocked'"
    ).fetchall()

    lines = [f"*Standup — {today}*", "", "*Yesterday:*"]
    lines += _group_by_tag_lines(done_yesterday) or ["• Nothing logged."]
    lines += ["", "*Today:*"]
    lines += _group_by_tag_lines(in_progress_today) or ["• Nothing logged yet."]
    lines += ["", "*Blockers:*"]
    lines += _group_by_tag_lines(blockers) or ["• None 🎉"]

    if include_weekly:
        start = (date.today() - timedelta(days=range_days - 1)).isoformat()

        done_range = conn.execute(
            "SELECT * FROM tasks WHERE log_date BETWEEN ? AND ? AND status = 'done' ORDER BY log_date ASC",
            (start, today),
        ).fetchall()
        in_progress_range = conn.execute(
            "SELECT * FROM tasks WHERE log_date BETWEEN ? AND ? AND status = 'in_progress' ORDER BY log_date ASC",
            (start, today),
        ).fetchall()
        blocked_range = conn.execute(
            "SELECT * FROM tasks WHERE log_date BETWEEN ? AND ? AND status = 'blocked' ORDER BY log_date ASC",
            (start, today),
        ).fetchall()

        lines += ["", f"*Rollup ({start} → {today}):*", "", "_Done:_"]
        lines += _group_by_tag_lines(done_range) or ["• Nothing completed in this range."]
        lines += ["", "_In Progress:_"]
        lines += _group_by_tag_lines(in_progress_range) or ["• Nothing in progress in this range."]
        lines += ["", "_Blocked:_"]
        lines += _group_by_tag_lines(blocked_range) or ["• None 🎉"]

    conn.close()
    return "\n".join(lines)


# --- Shared checklist helpers (used by both MCP tools and HTTP API) ---

def _add_item(title: str, parent_id: int | None = None) -> tuple[int, str | None]:
    """Insert a task/subtask. Returns (new_id, error_msg_or_None)."""
    conn = get_db()
    if parent_id:
        parent = conn.execute("SELECT id FROM tasks WHERE id = ?", (parent_id,)).fetchone()
        if not parent:
            conn.close()
            return (-1, f"Parent task #{parent_id} not found.")
    tag = f"subtask:{parent_id}" if parent_id else None
    cur = conn.execute(
        "INSERT INTO tasks (description, status, log_date, tag) VALUES (?, ?, ?, ?)",
        (title, "in_progress", date.today().isoformat(), tag),
    )
    conn.commit()
    conn.close()
    return (cur.lastrowid, None)


def _delete_item(item_id: int) -> tuple[str | None, int]:
    """Recursively delete a task and all descendants. Returns (description_or_None, count_deleted)."""
    conn = get_db()
    row = conn.execute("SELECT description FROM tasks WHERE id = ?", (item_id,)).fetchone()
    if not row:
        conn.close()
        return (None, 0)
    to_delete = [item_id]
    i = 0
    while i < len(to_delete):
        children = conn.execute("SELECT id FROM tasks WHERE tag = ?", (f"subtask:{to_delete[i]}",)).fetchall()
        to_delete.extend(r["id"] for r in children)
        i += 1
    for tid in to_delete:
        conn.execute("DELETE FROM tasks WHERE id = ?", (tid,))
    conn.commit()
    conn.close()
    return (row["description"], len(to_delete))


def _toggle_item(item_id: int) -> tuple[str | None, str | None]:
    """Toggle done/in_progress. Returns (new_status, description) or (None, None) if not found."""
    conn = get_db()
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (item_id,)).fetchone()
    if not row:
        conn.close()
        return (None, None)
    new_status = "done" if row["status"] != "done" else "in_progress"
    conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (new_status, item_id))
    conn.commit()
    conn.close()
    return (new_status, row["description"])


# --- Checklist MCP Tools ---

@mcp.tool()
def add_checklist_item(title: str, parent_id: int | None = None) -> str:
    """Add a task to the checklist. Set parent_id to nest it as a subtask under an existing task."""
    new_id, err = _add_item(title, parent_id)
    if err:
        return err
    kind = "Subtask" if parent_id else "Task"
    return f"{kind} #{new_id} added: {title}"


@mcp.tool()
def delete_checklist_item(item_id: int) -> str:
    """Delete a task from the checklist by ID, including all nested subtasks."""
    desc, count = _delete_item(item_id)
    if not desc:
        return f"No task #{item_id} found."
    return f"Deleted #{item_id}: {desc} (and {count-1} subtask(s))"


@mcp.tool()
def toggle_checklist_item(item_id: int) -> str:
    """Toggle a task between done and not done."""
    new_status, desc = _toggle_item(item_id)
    if not new_status:
        return f"No task #{item_id} found."
    mark = "✅" if new_status == "done" else "⬜"
    return f"{mark} #{item_id}: {desc}"


@mcp.tool()
def get_checklist() -> str:
    """Get all tasks as a checklist view, grouped by status."""
    conn = get_db()
    rows = conn.execute("SELECT * FROM tasks ORDER BY log_date DESC, id DESC").fetchall()
    conn.close()
    if not rows:
        return "No tasks logged yet."
    lines = []
    for r in rows:
        mark = "✅" if r["status"] == "done" else ("🚫" if r["status"] == "blocked" else "⬜")
        tag_str = f" ({_tag_label(r['tag'])})" if r["tag"] and not (r["tag"] or "").startswith("subtask:") else ""
        lines.append(f"{mark} #{r['id']} {r['description']}{tag_str} [{r['log_date']}]")
    return "\n".join(lines)


@mcp.tool()
def open_checklist_dashboard() -> str:
    """Launch the interactive task checklist in the browser.

    Use this whenever the user wants to view, manage, or work through
    their tasks interactively — whether they're starting their day,
    checking what's left, or asking about what to do next.

    Returns a localhost URL. Data persists across sessions.
    """
    _ensure_http_server()
    return f"Checklist dashboard running at http://localhost:{CHECKLIST_PORT}\nOpen this URL in your browser to manage tasks and subtasks interactively."


# --- Local HTTP server for the interactive HTML dashboard ---

_server_started = False


def _ensure_http_server():
    global _server_started
    if _server_started:
        return
    _server_started = True
    t = threading.Thread(target=_run_http_server, daemon=True)
    t.start()


def _run_http_server():
    server = HTTPServer(("", CHECKLIST_PORT), _ChecklistHandler)
    logger.info(f"Checklist dashboard running at http://localhost:{CHECKLIST_PORT}")
    server.serve_forever()


class _ChecklistHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # suppress stdout to avoid corrupting MCP stdio transport

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json_response(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/" or path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(_CHECKLIST_HTML.encode())
        elif path == "/api/summary":
            end = date.today()
            start = end - timedelta(days=6)
            conn = get_db()
            counts = conn.execute(
                "SELECT status, COUNT(*) as n FROM tasks WHERE log_date BETWEEN ? AND ? GROUP BY status",
                (start.isoformat(), end.isoformat()),
            ).fetchall()
            blockers = conn.execute("SELECT description FROM tasks WHERE status = 'blocked'").fetchall()
            conn.close()
            count_map = {r["status"]: r["n"] for r in counts}
            self._json_response({
                "done": count_map.get("done", 0),
                "in_progress": count_map.get("in_progress", 0),
                "blocked": count_map.get("blocked", 0),
                "blockers": [r["description"] for r in blockers],
                "start": start.isoformat(),
                "end": end.isoformat(),
            })
        elif path == "/api/checklist":
            conn = get_db()
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY "
                "CASE WHEN due_date IS NOT NULL AND status != 'done' THEN 0 ELSE 1 END, "
                "due_date ASC, log_date DESC, id DESC"
            ).fetchall()
            conn.close()
            self._json_response([dict(r) for r in rows])
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        if path == "/api/checklist":
            title = body.get("title", "").strip()
            if not title:
                self._json_response({"error": "title required"}, 400)
                return
            parent_id = body.get("parent_id")
            new_id, err = _add_item(title, parent_id)
            if err:
                self._json_response({"error": err}, 400)
                return
            self._json_response({"id": new_id})

        elif path == "/api/checklist/toggle":
            item_id = body.get("id")
            new_status, _ = _toggle_item(item_id)
            if not new_status:
                self._json_response({"error": "not found"}, 404)
                return
            self._json_response({"ok": True})

        elif path == "/api/checklist/delete":
            item_id = body.get("id")
            desc, _ = _delete_item(item_id)
            if not desc:
                self._json_response({"error": "not found"}, 404)
                return
            self._json_response({"ok": True})
        else:
            self.send_response(404)
            self.end_headers()


_CHECKLIST_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Standup Journal — Checklist</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,sans-serif;max-width:600px;margin:40px auto;padding:0 16px;background:#1a1a2e;color:#e0e0e0}
h1{margin-bottom:16px;color:#fff}
.summary{background:#16213e;border-radius:8px;padding:12px 16px;margin-bottom:20px;font-size:14px;line-height:1.6}
.summary .counts span{margin-right:12px}
.summary .blockers{margin-top:6px;opacity:.8;font-size:13px}
.progress-bar{width:100%;height:8px;background:#333;border-radius:4px;margin:8px 0;overflow:hidden}
.progress-bar .done{height:100%;background:linear-gradient(90deg,#2ecc71,#1abc9c);border-radius:4px;transition:width 1.0s ease}
.add-form{display:flex;gap:8px;margin-bottom:24px}
.add-form input{flex:1;padding:8px 12px;border-radius:6px;border:1px solid #333;background:#16213e;color:#e0e0e0}
.add-form button{padding:8px 16px;border:none;border-radius:6px;background:#0f3460;color:#fff;cursor:pointer}
.add-form button:hover{background:#1a5276}
.item{display:flex;align-items:center;gap:8px;padding:8px;border-radius:6px;margin-bottom:4px}
.item:hover{background:#16213e}
.item.done .title{text-decoration:line-through;opacity:.5}
.item .title{flex:1;cursor:pointer}
.item .actions{display:flex;gap:15px}
.item .actions button{background:none;border:none;cursor:pointer;font-size:14px;opacity:.6;color:#e0e0e0}
.item .actions button:hover{opacity:1}
.check{cursor:pointer;font-size:18px}
.subtasks{margin-left:28px}
.sub-add{display:flex;gap:4px;margin:4px 0 4px 28px}
.sub-add input{flex:1;padding:4px 8px;border-radius:4px;border:1px solid #333;background:#16213e;color:#e0e0e0;font-size:13px}
.sub-add button{padding:4px 10px;border:none;border-radius:4px;background:#0f3460;color:#fff;cursor:pointer;font-size:13px}
.due{font-size:11px;margin-left:4px;color:#f39c12;font-weight:600}
.due.overdue{color:#e74c3c}
.tag-badge{font-size:11px;margin-left:4px;opacity:.7}
</style>
</head>
<body>
<h1>📋 Checklist</h1>
<div id="summary" class="summary"></div>
<div class="add-form"><input id="newItem" placeholder="New task…" /><button onclick="addItem()">Add</button></div>
<div id="list"></div>
<script>
const BASE='http://localhost:'+location.port;
const TAG_EMOJIS=(function(){const e=['👑','💎','💄','🦋','🍾','🥂','🏰','🕯️','🪞','🔮','🎭','🖋️','📿','🧿','🪩','🎀','🥀','🌹','🦢','🐆','🦚','🧵','🪆','🗝️','⚜️','🏺','🪄','🎇','🍇','🍓'];return new Proxy({},{get:(_,tag)=>{let i=0;for(const c of tag)i+=c.charCodeAt(0);return e[i%e.length]}})})();
async function loadSummary(){
  const res=await fetch(BASE+'/api/summary');const s=await res.json();
  const total=s.done+s.in_progress+s.blocked;
  const pDone=total?((s.done/total)*100):0;
  const bar=document.getElementById('progress-fill');
  if(bar){bar.style.width=pDone+'%';}else{
    let html=`<div class="progress-bar"><div class="done" id="progress-fill" style="width:${pDone}%"></div></div>`;
    html+=`<div id="summary-text"></div>`;
    document.getElementById('summary').innerHTML=html;
  }
  let txt=`<div class="counts"><span>✅ ${s.done} done</span><span>🔄 ${s.in_progress} in progress</span><span>🚫 ${s.blocked} blocked</span></div>`;
  txt+=`<div style="opacity:.5;font-size:12px">${s.start} → ${s.end}</div>`;
  if(s.blockers.length) txt+=`<div class="blockers">Blockers: ${s.blockers.map(b=>'• '+b).join('<br>')}</div>`;
  document.getElementById('summary-text').innerHTML=txt;
}
async function load(){
  const res=await fetch(BASE+'/api/checklist');const tasks=await res.json();
  const today=new Date().toISOString().slice(0,10);
  function getChildren(parentId){return tasks.filter(t=>t.tag==='subtask:'+parentId);}
  function renderTree(items,depth){
    let html='';
    items.forEach(t=>{
      const done=t.status==='done';
      const blocked=t.status==='blocked';
      const cls=done?'item done':'item';
      const mark=done?'✅':(blocked?'🚫':'⬜');
      const tag=t.tag&&!t.tag.startsWith('subtask:')?`<span class="tag-badge">${TAG_EMOJIS[t.tag]||''} ${t.tag}</span>`:'';
      const due=t.due_date?`<span class="due${t.due_date<today&&!done?' overdue':''}">${t.due_date}</span>`:'';
      html+=`<div class="${cls}"><span class="check" onclick="toggle(${t.id})">${mark}</span><span class="title" onclick="toggle(${t.id})">${esc(t.description)}${tag}${due}</span>${depth===0?`<small style="opacity:.5;margin-right:4px">${t.log_date}</small>`:''}<span class="actions"><button onclick="showSubAdd(${t.id})" title="Add subtask">+</button><button onclick="del(${t.id})">🗑️</button></span></div>`;
      const children=getChildren(t.id);
      if(children.length) html+='<div class="subtasks">'+renderTree(children,depth+1)+'</div>';
      html+=`<div class="sub-add" id="sub-add-${t.id}" style="display:none"><input id="sub-inp-${t.id}" placeholder="Subtask…" onkeydown="if(event.key==='Enter')addSub(${t.id})"/><button onclick="addSub(${t.id})">Add</button></div>`;
    });
    return html;
  }
  const roots=tasks.filter(t=>!t.tag||!t.tag.startsWith('subtask:'));
  document.getElementById('list').innerHTML=renderTree(roots,0)||'<p style="opacity:.5">No tasks yet.</p>';
}
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;')}
function showSubAdd(id){const el=document.getElementById('sub-add-'+id);el.style.display=el.style.display==='none'?'flex':'none';}
async function addItem(){
  const inp=document.getElementById('newItem');const t=inp.value.trim();if(!t)return;
  await fetch(BASE+'/api/checklist',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:t})});
  inp.value='';load();loadSummary();
}
async function addSub(parentId){
  const inp=document.getElementById('sub-inp-'+parentId);const t=inp.value.trim();if(!t)return;
  await fetch(BASE+'/api/checklist',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:t,parent_id:parentId})});
  inp.value='';load();loadSummary();
}
async function toggle(id){await fetch(BASE+'/api/checklist/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});load();loadSummary()}
window.del=async function(id){if(!confirm('Delete this task and its subtasks?'))return;await fetch(BASE+'/api/checklist/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});load();loadSummary()}
document.getElementById('newItem').addEventListener('keydown',e=>{if(e.key==='Enter')addItem()});
load();loadSummary();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    _ensure_http_server()
    mcp.run()