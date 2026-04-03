"""Report persistence — store analysis reports and predictions in SQLite.

Schema:
- reports: full analysis reports with events and output
- predictions: extracted bullish/bearish sector predictions for daily review
- reviews: next-day review results comparing predictions vs actual
- events: event nodes for the event graph
- event_links: causal relationships between events
"""

import json
import sqlite3
import threading
import time
from pathlib import Path

from alpha_agents.config import DATA_DIR

REPORTS_DB_PATH = DATA_DIR / "reports.db"

_lock = threading.Lock()
_local = threading.local()

_REPORTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle INTEGER,
    timestamp REAL NOT NULL,
    event_count INTEGER NOT NULL DEFAULT 0,
    categories TEXT,          -- JSON: {"政策": 2, "地缘": 1}
    events_json TEXT,         -- JSON array of digested events
    report_text TEXT,         -- full agent output
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL,
    date TEXT NOT NULL,        -- YYYY-MM-DD
    direction TEXT NOT NULL,   -- bullish / bearish
    sector TEXT NOT NULL,      -- 板块名
    strength INTEGER,          -- 1-5
    reason TEXT,
    category TEXT,             -- 政策/地缘/宏观/行业/市场
    event_summary TEXT,        -- 关联事件摘要
    FOREIGN KEY (report_id) REFERENCES reports(id)
);

CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,         -- review 的目标日期 YYYY-MM-DD
    predictions_count INTEGER,
    correct_count INTEGER,
    accuracy REAL,
    review_text TEXT,           -- LLM 生成的回顾分析
    market_data TEXT,           -- JSON: 当日实际行情数据
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    category TEXT,
    importance INTEGER,
    timestamp REAL,
    summary TEXT,
    report_id INTEGER,
    FOREIGN KEY (report_id) REFERENCES reports(id)
);

CREATE TABLE IF NOT EXISTS event_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_event_id INTEGER NOT NULL,
    target_event_id INTEGER NOT NULL,
    relation TEXT NOT NULL,     -- causes / amplifies / mitigates / relates_to
    confidence REAL DEFAULT 0.5,
    reason TEXT DEFAULT '',     -- LLM explanation of why this relationship exists
    FOREIGN KEY (source_event_id) REFERENCES events(id),
    FOREIGN KEY (target_event_id) REFERENCES events(id)
);

CREATE INDEX IF NOT EXISTS idx_predictions_date ON predictions(date);
CREATE INDEX IF NOT EXISTS idx_reviews_date ON reviews(date);
CREATE INDEX IF NOT EXISTS idx_events_title ON events(title);
CREATE INDEX IF NOT EXISTS idx_events_category ON events(category);
CREATE INDEX IF NOT EXISTS idx_event_links_source ON event_links(source_event_id);
CREATE INDEX IF NOT EXISTS idx_event_links_target ON event_links(target_event_id);
"""


def _get_conn() -> sqlite3.Connection:
    """Get a thread-local SQLite connection (reused within the same thread)."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except sqlite3.ProgrammingError:
            conn = None
    REPORTS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(REPORTS_DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    conn.executescript(_REPORTS_SCHEMA)
    _local.conn = conn
    return conn


def save_report(
    cycle: int,
    timestamp: float,
    events: list[dict],
    categories: dict,
    report_text: str,
) -> int:
    """Persist an analysis report and return its ID."""
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            "INSERT INTO reports (cycle, timestamp, event_count, categories, events_json, report_text) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (cycle, timestamp, len(events), json.dumps(categories, ensure_ascii=False),
             json.dumps(events, ensure_ascii=False), report_text),
        )
        report_id = cur.lastrowid
        conn.commit()
        return report_id


def save_predictions(report_id: int, date: str, predictions: list[dict]) -> None:
    """Save extracted predictions for a given report."""
    with _lock:
        conn = _get_conn()
        for p in predictions:
            conn.execute(
                "INSERT INTO predictions (report_id, date, direction, sector, strength, reason, category, event_summary) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (report_id, date, p["direction"], p["sector"],
                 p.get("strength", 0), p.get("reason", ""),
                 p.get("category", ""), p.get("event_summary", "")),
            )
        conn.commit()


def get_predictions_by_date(date: str) -> list[dict]:
    """Get all predictions for a given date."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM predictions WHERE date = ? ORDER BY direction, strength DESC",
        (date,),
    ).fetchall()
    return [dict(r) for r in rows]


def save_review(date: str, predictions_count: int, correct_count: int,
                accuracy: float, review_text: str, market_data: dict) -> int:
    """Save a daily review result."""
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            "INSERT INTO reviews (date, predictions_count, correct_count, accuracy, review_text, market_data) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (date, predictions_count, correct_count, accuracy,
             review_text, json.dumps(market_data, ensure_ascii=False)),
        )
        conn.commit()
        return cur.lastrowid


def get_recent_reports(limit: int = 20) -> list[dict]:
    """Get most recent reports."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, cycle, timestamp, event_count, categories, report_text, created_at "
        "FROM reports ORDER BY timestamp DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_recent_reviews(limit: int = 10) -> list[dict]:
    """Get most recent reviews."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM reviews ORDER BY date DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


# --- Event Graph ---

def save_event(title: str, category: str, importance: int,
               timestamp: float, summary: str, report_id: int | None = None) -> int:
    """Save an event node and return its ID."""
    with _lock:
        conn = _get_conn()
        cur = conn.execute(
            "INSERT INTO events (title, category, importance, timestamp, summary, report_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (title, category, importance, timestamp, summary, report_id),
        )
        conn.commit()
        return cur.lastrowid


def link_events(source_id: int, target_id: int, relation: str,
                confidence: float = 0.5, reason: str = "") -> None:
    """Create a causal link between two events."""
    with _lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO event_links (source_event_id, target_event_id, relation, confidence, reason) "
            "VALUES (?, ?, ?, ?, ?)",
            (source_id, target_id, relation, confidence, reason),
        )
        conn.commit()


def get_event_graph(limit: int = 50) -> dict:
    """Get recent events and their links for graph visualization."""
    conn = _get_conn()
    events = conn.execute(
        "SELECT * FROM events ORDER BY timestamp DESC LIMIT ?", (limit,)
    ).fetchall()
    event_ids = [e["id"] for e in events]
    if not event_ids:
        return {"events": [], "links": []}

    placeholders = ",".join("?" * len(event_ids))
    links = conn.execute(
        f"SELECT * FROM event_links WHERE source_event_id IN ({placeholders}) "
        f"OR target_event_id IN ({placeholders})",
        event_ids + event_ids,
    ).fetchall()
    return {
        "events": [dict(e) for e in events],
        "links": [dict(lnk) for lnk in links],
    }


def find_related_events(event_title: str, limit: int = 10) -> list[dict]:
    """Find events with similar titles (for linking)."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM events WHERE title LIKE ? ORDER BY timestamp DESC LIMIT ?",
        (f"%{event_title[:20]}%", limit),
    ).fetchall()
    return [dict(r) for r in rows]
