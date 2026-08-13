import os
import sqlite3
from datetime import date, timedelta

from mcp.server import MCPServer

mcp = MCPServer("standup-journal")

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
            tag TEXT
        )
        """
    )
    # Migration for DBs created before the `tag` column existed.
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    if "tag" not in existing_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN tag TEXT")
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
    if not tag:
        return ""
    return f"{_tag_emoji(tag)} {tag}"


@mcp.tool()
def log_task(task_description: str, status: str = "done", tag: str | None = None) -> str:
    """Log a single piece of work: what you did, are doing, or are blocked on.

    If the user gives a rambling, multi-item update in one message (e.g.
    "finished the login screen, still working on the API integration, and
    I'm blocked on AWS permissions"), call this tool ONCE PER DISTINCT ITEM
    rather than logging it as one combined entry.

    Infer `status` from the language used for each item:
    - "done" -> finished, shipped, completed, past tense ("fixed", "wrote")
    - "in_progress" -> still working on, in the middle of, ongoing
    - "blocked" -> blocked, stuck, waiting on, can't proceed until

    `tag` is an optional project/category label (e.g. "frontend", "acme-client",
    "customer-advisor", "infra"). Set it whenever the user's phrasing implies
    a category, using any of these patterns:
    - "log ___ under/as/for [category]"
    - "tag this as [category]"
    - "categorize/sort this as [category]"
    - simply mentioning a known project/client name in the task description
    Each distinct tag automatically gets a consistent colored-circle emoji
    in every response, so tasks stay visually grouped by category over time.
    If no category is stated or inferable from context, leave `tag` unset.

    If `status` is "blocked" and there's already an open blocker with a very
    similar description, this updates that existing entry's date instead of
    creating a duplicate — so a recurring blocker doesn't pile up as
    multiple rows across days.
    """
    today = date.today().isoformat()
    conn = get_db()

    if status == "blocked":
        existing = _find_matching_open_blocker(conn, task_description)
        if existing is not None:
            conn.execute(
                "UPDATE tasks SET log_date = ?, description = ?, tag = ? WHERE id = ?",
                (today, task_description, tag or existing["tag"], existing["id"]),
            )
            conn.commit()
            conn.close()
            return (
                f"Existing blocker #{existing['id']} still open, refreshed to "
                f"{today}: {task_description}"
            )

    cur = conn.execute(
        "INSERT INTO tasks (description, status, log_date, tag) VALUES (?, ?, ?, ?)",
        (task_description, status, today, tag),
    )
    conn.commit()
    conn.close()
    tag_note = f" [{_tag_label(tag)}]" if tag else ""
    return f"Logged #{cur.lastrowid} ({status}){tag_note} on {today}: {task_description}"


@mcp.tool()
def update_task_status(task_id: int, status: str) -> str:
    """Update the status of an existing logged task (e.g. unblock it).

    Use this when the user says a previously logged item has changed state
    — e.g. a blocker just got resolved, or something in progress is now
    done. `status` must be one of: "done", "in_progress", "blocked".
    """
    conn = get_db()
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
def search_tasks(keyword: str) -> str:
    """Search all logged tasks (any date, any status) for a keyword in the description.

    Useful for "when did I last work on X" style questions.
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM tasks WHERE description LIKE ? ORDER BY log_date DESC",
        (f"%{keyword}%",),
    ).fetchall()
    conn.close()
    if not rows:
        return f"No tasks found matching '{keyword}'."
    lines = [
        f"- {r['log_date']} #{r['id']} [{r['status']}]" + (f" ({_tag_label(r['tag'])})" if r["tag"] else "") + f" {r['description']}"
        for r in rows
    ]
    return f"Tasks matching '{keyword}':\n" + "\n".join(lines)


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


@mcp.tool()
def generate_exec_summary(days: int = 7) -> str:
    """One-paragraph summary of activity over the last N days (default 7):
    counts of done / in_progress / blocked, and a list of currently open blockers.

    Good for pasting at the top of a longer update, or a quick self-check.
    """
    end = date.today()
    start = end - timedelta(days=days - 1)

    conn = get_db()
    counts = conn.execute(
        """
        SELECT status, COUNT(*) as n FROM tasks
        WHERE log_date BETWEEN ? AND ?
        GROUP BY status
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    blockers = conn.execute(
        "SELECT description FROM tasks WHERE status = 'blocked'"
    ).fetchall()
    conn.close()

    count_map = {row["status"]: row["n"] for row in counts}
    done_n = count_map.get("done", 0)
    in_progress_n = count_map.get("in_progress", 0)
    blocked_n = count_map.get("blocked", 0)

    summary = (
        f"*Exec Summary ({start.isoformat()} to {end.isoformat()}):* "
        f"{done_n} task(s) done, {in_progress_n} in progress, "
        f"{blocked_n} blocker(s) open."
    )
    if blockers:
        summary += "\nOpen blockers: " + "; ".join(r["description"] for r in blockers)
    return summary


@mcp.tool()
def generate_weekly_summary(week_start: str | None = None) -> str:
    """Generate a bulleted weekly rollup: all done items and any blockers
    across a 7-day window. Good for Friday retros or manager 1:1s.

    Args:
        week_start: ISO date (YYYY-MM-DD) for the first day of the window.
            Defaults to 7 days ago through today.
    """
    end = date.today()
    if week_start:
        start = date.fromisoformat(week_start)
        end = start + timedelta(days=6)
    else:
        start = end - timedelta(days=6)

    conn = get_db()
    done_rows = conn.execute(
        """
        SELECT * FROM tasks
        WHERE log_date BETWEEN ? AND ? AND status = 'done'
        ORDER BY log_date ASC
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    blockers = conn.execute(
        "SELECT description FROM tasks WHERE status = 'blocked'"
    ).fetchall()
    conn.close()

    lines = [f"*Weekly Summary — {start.isoformat()} to {end.isoformat()}*", "", "*Completed:*"]
    lines += [f"• {r['description']}" for r in done_rows] or ["• Nothing logged."]
    lines += ["", "*Open Blockers:*"]
    lines += [f"• {r['description']}" for r in blockers] or ["• None 🎉"]
    return "\n".join(lines)


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
def generate_standup_report() -> str:
    """Generate a clean, bulleted Slack standup message from yesterday and today.

    Automatically groups items by tag/project if any logged tasks have one.
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
    conn.close()

    lines = [f"*Standup — {today}*", "", "*Yesterday:*"]
    lines += _group_by_tag_lines(done_yesterday) or ["• Nothing logged."]
    lines += ["", "*Today:*"]
    lines += _group_by_tag_lines(in_progress_today) or ["• Nothing logged yet."]
    lines += ["", "*Blockers:*"]
    lines += _group_by_tag_lines(blockers) or ["• None 🎉"]
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
