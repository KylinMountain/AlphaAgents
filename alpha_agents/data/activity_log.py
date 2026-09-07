"""Cross-process activity stream for the dashboard.

The scheduler and the web UI run as separate containers, so the in-memory
event bus in ``server/events.py`` cannot carry V2 activity to the browser —
each process has its own. They do share ``data/`` though, so the stream
goes through SQLite: the scheduler appends, the web reads.

Rows are small and capped by MAX_ROWS; this is a live feed, not an audit
log — reports and reviews are already persisted separately.
"""

import json
import logging
import sqlite3
import threading
from datetime import datetime

from alpha_agents.config import DATA_DIR

logger = logging.getLogger(__name__)

DB_PATH = DATA_DIR / "activity.db"
MAX_ROWS = 2000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,        -- task_start / task_done / task_failed / signal / trade
    task TEXT,
    status TEXT,               -- running / ok / failed / timeout
    message TEXT,
    detail_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_activity_ts ON activity(ts DESC);
"""

_local = threading.local()
_write_lock = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        # WAL lets the web container read while the scheduler writes.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        conn.commit()
        _local.conn = conn
    return conn


def log_activity(kind: str, *, task: str = "", status: str = "",
                 message: str = "", detail: dict | None = None) -> None:
    """Append one row. Never raises — the feed must not break a task."""
    try:
        with _write_lock:
            conn = _get_conn()
            conn.execute(
                "INSERT INTO activity (ts, kind, task, status, message, detail_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (datetime.now().isoformat(timespec="seconds"), kind, task, status,
                 message[:2000], json.dumps(detail or {}, ensure_ascii=False)),
            )
            # Trim opportunistically rather than on a timer.
            conn.execute(
                "DELETE FROM activity WHERE id < "
                "(SELECT MAX(id) - ? FROM activity)", (MAX_ROWS,),
            )
            conn.commit()
    except Exception as e:
        logger.debug("activity log write failed: %s", e)


def get_activity(limit: int = 100, since_id: int | None = None) -> list[dict]:
    """Newest rows first, or everything after ``since_id`` for polling."""
    try:
        conn = _get_conn()
        if since_id is not None:
            rows = conn.execute(
                "SELECT * FROM activity WHERE id > ? ORDER BY id DESC LIMIT ?",
                (since_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM activity ORDER BY id DESC LIMIT ?", (limit,),
            ).fetchall()
    except Exception as e:
        logger.debug("activity log read failed: %s", e)
        return []

    out = []
    for r in rows:
        d = dict(r)
        try:
            d["detail"] = json.loads(d.pop("detail_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            d["detail"] = {}
        out.append(d)
    return out
