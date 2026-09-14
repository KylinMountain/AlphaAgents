"""The daily snapshot store: write a blob by (date, data_type), read it back.

This is the storage half of what used to be one module. The half that *fetches*
— `run_daily_archive`, which pulls from four `tools/` modules and the Tushare
storers — lives in ``pipeline/tasks/daily_archive.py``, because ``data/`` may not
import ``tools/``.

The split is a layering fact rather than a preference, and it is the opposite of
what the tech-debt entry proposed: moving the whole module up would have made
``data/market_history.py``, ``data/market_data.py`` and ``data/sentiment_cycle.py``
import ``pipeline/``, which is a backwards import in the layer that is supposed to
have none. Four violations traded for three worse ones.

So the store stays here, where its readers are, and only the orchestration moved.
"""

import json
import logging

from alpha_agents.data.memory_store import _get_conn, _write_lock

logger = logging.getLogger(__name__)


def save_snapshot(date: str, data_type: str, data: dict) -> None:
    """Save a data snapshot. Upserts on (date, data_type)."""
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO daily_snapshots (date, data_type, data) VALUES (?, ?, ?) "
            "ON CONFLICT(date, data_type) DO UPDATE SET data = excluded.data, "
            "created_at = datetime('now','localtime')",
            (date, data_type, json.dumps(data, ensure_ascii=False)),
        )
        conn.commit()


def get_snapshot(date: str, data_type: str) -> dict | None:
    """Read a snapshot back. Returns parsed JSON or None."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT data FROM daily_snapshots WHERE date = ? AND data_type = ?",
        (date, data_type),
    ).fetchone()
    if row:
        return json.loads(row["data"])
    return None
