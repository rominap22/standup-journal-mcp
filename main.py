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
            log_date TEXT NOT NULL
        )
        """
    )
    return conn


@mcp.tool()
def log_task(task_description: str, status: str = "done") -> str:
    """Log a piece of work: what you did, are doing, or are blocked on."""
    today = date.today().isoformat()
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO tasks (description, status, log_date) VALUES (?, ?, ?)",
        (task_description, status, today),
    )
    conn.commit()
    conn.close()
    return f"Logged #{cur.lastrowid} ({status}) on {today}: {task_description}"


@mcp.tool()
def get_tasks_by_date(log_date: str) -> str:
    """Retrieve all logged tasks for a given ISO date (YYYY-MM-DD)."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM tasks WHERE log_date = ?", (log_date,)
    ).fetchall()
    conn.close()
    if not rows:
        return f"No tasks logged for {log_date}."
    return "\n".join(f"- [{r['status']}] {r['description']}" for r in rows)


@mcp.tool()
def generate_standup_report() -> str:
    """Generate a clean, bulleted Slack standup message from yesterday and today."""
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    conn = get_db()
    done_yesterday = conn.execute(
        "SELECT description FROM tasks WHERE log_date = ? AND status = 'done'",
        (yesterday,),
    ).fetchall()
    blockers = conn.execute(
        "SELECT description FROM tasks WHERE status = 'blocked'"
    ).fetchall()
    conn.close()

    lines = [f"*Standup — {today}*", "", "*Yesterday:*"]
    lines += [f"• {r['description']}" for r in done_yesterday] or ["• Nothing logged."]
    lines += ["", "*Blockers:*"]
    lines += [f"• {r['description']}" for r in blockers] or ["• None 🎉"]
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()