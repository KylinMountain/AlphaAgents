"""Persistent memory for the analyst — theme lines, predictions, market cognition.

Follows the same pattern as report_store.py: thread-local SQLite connections,
plain functions, JSON for flexible fields.
"""

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from alpha_agents.config import MEMORY_DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS theme_lines (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    status TEXT DEFAULT 'watching',
    strength INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT,
    catalyst TEXT,
    core_stocks TEXT,
    leader_code TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    report_type TEXT,
    code TEXT NOT NULL,
    name TEXT,
    direction TEXT,
    confidence TEXT,
    theme_line TEXT,
    entry_price REAL,
    reason TEXT,
    next_day_return REAL,
    week_return REAL,
    hit INTEGER,
    review_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_pred_date ON predictions(date);
CREATE INDEX IF NOT EXISTS idx_pred_code ON predictions(code);

CREATE TABLE IF NOT EXISTS market_cognition (
    id INTEGER PRIMARY KEY,
    sector TEXT NOT NULL,
    date TEXT NOT NULL,
    position TEXT,
    fund_trend TEXT,
    pe_percentile REAL,
    recent_events TEXT,
    assessment TEXT,
    UNIQUE(sector, date)
);
CREATE INDEX IF NOT EXISTS idx_cognition_sector ON market_cognition(sector);

CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    type TEXT NOT NULL,              -- success / mistake / insight
    category TEXT,                   -- theme / timing / signal / risk / market_regime
    theme_line TEXT,                 -- related theme line, nullable
    description TEXT NOT NULL,       -- what happened
    market_context TEXT,             -- market regime when this was observed
    actionable TEXT,                 -- concrete rule derived
    times_confirmed INTEGER DEFAULT 1,
    last_confirmed TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_lessons_type ON lessons(type);
CREATE INDEX IF NOT EXISTS idx_lessons_category ON lessons(category);
"""

_local = threading.local()
_write_lock = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    """Get or create a thread-local connection to memory.db."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        MEMORY_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(MEMORY_DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        _local.conn = conn
    return conn


# ── Theme Lines ──────────────────────────────────────────────

def get_active_themes(min_status: str = "watching") -> list[dict]:
    """Get all non-archived theme lines, ordered by strength desc."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM theme_lines WHERE status != 'archived' ORDER BY strength DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_theme_by_name(name: str) -> dict | None:
    """Get a single theme line by name."""
    conn = _get_conn()
    row = conn.execute("SELECT * FROM theme_lines WHERE name = ?", (name,)).fetchone()
    return dict(row) if row else None


def upsert_theme(
    name: str,
    *,
    status: str | None = None,
    strength: int | None = None,
    catalyst: str | None = None,
    core_stocks: list[dict] | None = None,
    leader_code: str | None = None,
    notes: str | None = None,
) -> int:
    """Create or update a theme line. Returns the theme id."""
    now = datetime.now().isoformat()
    with _write_lock:
        conn = _get_conn()
        existing = conn.execute(
            "SELECT id FROM theme_lines WHERE name = ?", (name,)
        ).fetchone()

        if existing:
            sets, vals = [], []
            if status is not None:
                sets.append("status = ?"); vals.append(status)
            if strength is not None:
                sets.append("strength = ?"); vals.append(strength)
            if catalyst is not None:
                sets.append("catalyst = ?"); vals.append(catalyst)
            if core_stocks is not None:
                sets.append("core_stocks = ?"); vals.append(json.dumps(core_stocks, ensure_ascii=False))
            if leader_code is not None:
                sets.append("leader_code = ?"); vals.append(leader_code)
            if notes is not None:
                sets.append("notes = ?"); vals.append(notes)
            sets.append("updated_at = ?"); vals.append(now)
            vals.append(existing["id"])
            conn.execute(f"UPDATE theme_lines SET {', '.join(sets)} WHERE id = ?", vals)
            conn.commit()
            return existing["id"]
        else:
            cur = conn.execute(
                "INSERT INTO theme_lines (name, status, strength, catalyst, core_stocks, leader_code, notes, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name, status or "watching", strength or 0, catalyst,
                 json.dumps(core_stocks or [], ensure_ascii=False), leader_code, notes, now, now),
            )
            conn.commit()
            return cur.lastrowid


def archive_theme(name: str) -> None:
    """Archive a theme line (soft delete)."""
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE theme_lines SET status = 'archived', updated_at = ? WHERE name = ?",
            (datetime.now().isoformat(), name),
        )
        conn.commit()


# ── Predictions ──────────────────────────────────────────────

def save_prediction(
    date: str,
    report_type: str,
    code: str,
    name: str,
    direction: str,
    confidence: str,
    theme_line: str,
    entry_price: float | None,
    reason: str,
) -> int:
    """Record a stock recommendation."""
    with _write_lock:
        conn = _get_conn()
        cur = conn.execute(
            "INSERT INTO predictions (date, report_type, code, name, direction, confidence, theme_line, entry_price, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (date, report_type, code, name, direction, confidence, theme_line, entry_price, reason),
        )
        conn.commit()
        return cur.lastrowid


def update_prediction_result(pred_id: int, *, next_day_return: float | None = None,
                              week_return: float | None = None, hit: int | None = None,
                              review_note: str | None = None) -> None:
    """Fill in backtesting results for a prediction."""
    with _write_lock:
        conn = _get_conn()
        sets, vals = [], []
        if next_day_return is not None:
            sets.append("next_day_return = ?"); vals.append(next_day_return)
        if week_return is not None:
            sets.append("week_return = ?"); vals.append(week_return)
        if hit is not None:
            sets.append("hit = ?"); vals.append(hit)
        if review_note is not None:
            sets.append("review_note = ?"); vals.append(review_note)
        if sets:
            vals.append(pred_id)
            conn.execute(f"UPDATE predictions SET {', '.join(sets)} WHERE id = ?", vals)
            conn.commit()


def get_pending_predictions(date: str) -> list[dict]:
    """Get predictions that haven't been reviewed yet for a given date."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM predictions WHERE date = ? AND hit IS NULL", (date,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_prediction_stats(days: int = 7) -> dict:
    """Get hit rate statistics for recent predictions."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT direction, confidence, hit FROM predictions "
        "WHERE hit IS NOT NULL ORDER BY date DESC LIMIT ?",
        (days * 20,),
    ).fetchall()
    if not rows:
        return {"total": 0, "hits": 0, "hit_rate": 0.0, "by_confidence": {}}
    total = len(rows)
    hits = sum(1 for r in rows if r["hit"] == 1)
    by_conf = {}
    for r in rows:
        c = r["confidence"] or "unknown"
        by_conf.setdefault(c, {"total": 0, "hits": 0})
        by_conf[c]["total"] += 1
        if r["hit"] == 1:
            by_conf[c]["hits"] += 1
    for v in by_conf.values():
        v["hit_rate"] = round(v["hits"] / v["total"] * 100, 1) if v["total"] else 0
    return {"total": total, "hits": hits, "hit_rate": round(hits / total * 100, 1), "by_confidence": by_conf}


# ── Market Cognition ─────────────────────────────────────────

def upsert_cognition(sector: str, date: str, *, position: str | None = None,
                      fund_trend: str | None = None, pe_percentile: float | None = None,
                      recent_events: list[str] | None = None, assessment: str | None = None) -> None:
    """Update the analyst's understanding of a sector."""
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO market_cognition (sector, date, position, fund_trend, pe_percentile, recent_events, assessment) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(sector, date) DO UPDATE SET "
            "position=COALESCE(excluded.position, position), "
            "fund_trend=COALESCE(excluded.fund_trend, fund_trend), "
            "pe_percentile=COALESCE(excluded.pe_percentile, pe_percentile), "
            "recent_events=COALESCE(excluded.recent_events, recent_events), "
            "assessment=COALESCE(excluded.assessment, assessment)",
            (sector, date, position, fund_trend, pe_percentile,
             json.dumps(recent_events or [], ensure_ascii=False) if recent_events else None, assessment),
        )
        conn.commit()


def get_cognition(sector: str) -> dict | None:
    """Get the latest cognition for a sector."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM market_cognition WHERE sector = ? ORDER BY date DESC LIMIT 1", (sector,)
    ).fetchone()
    return dict(row) if row else None


def get_all_cognition_latest() -> list[dict]:
    """Get latest cognition for all tracked sectors."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT m1.* FROM market_cognition m1 "
        "INNER JOIN (SELECT sector, MAX(date) as max_date FROM market_cognition GROUP BY sector) m2 "
        "ON m1.sector = m2.sector AND m1.date = m2.max_date "
        "ORDER BY m1.sector"
    ).fetchall()
    return [dict(r) for r in rows]


# ── Lessons ─────────────────────────────────────────────────

def save_lesson(
    date: str,
    type: str,
    description: str,
    *,
    category: str | None = None,
    theme_line: str | None = None,
    market_context: str | None = None,
    actionable: str | None = None,
) -> int:
    """Save a structured lesson from review.

    Before inserting, checks for semantically similar existing lessons
    (same type + category + overlapping keywords). If a match is found,
    increments times_confirmed instead of creating a duplicate.

    Returns the lesson id (new or existing).
    """
    now = datetime.now().isoformat()
    with _write_lock:
        conn = _get_conn()

        # Try to find a similar existing lesson to merge with
        existing = _find_similar_lesson(conn, type, category, description)
        if existing:
            conn.execute(
                "UPDATE lessons SET times_confirmed = times_confirmed + 1, "
                "last_confirmed = ? WHERE id = ?",
                (date, existing["id"]),
            )
            conn.commit()
            return existing["id"]

        cur = conn.execute(
            "INSERT INTO lessons (date, type, category, theme_line, description, "
            "market_context, actionable, times_confirmed, last_confirmed, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (date, type, category, theme_line, description,
             market_context, actionable, date, now),
        )
        conn.commit()
        return cur.lastrowid


def _find_similar_lesson(
    conn: sqlite3.Connection, type: str, category: str | None, description: str
) -> dict | None:
    """Find an existing lesson that is semantically similar enough to merge.

    Uses keyword overlap: if 50%+ of the significant words match an existing
    lesson of the same type+category, treat them as the same lesson.
    """
    rows = conn.execute(
        "SELECT * FROM lessons WHERE type = ? AND category IS ? AND times_confirmed > 0",
        (type, category),
    ).fetchall()
    if not rows:
        return None

    new_words = set(description)
    # Use character bigrams for Chinese text matching
    if len(description) >= 2:
        new_words = {description[i:i+2] for i in range(len(description) - 1)}

    best_match, best_ratio = None, 0.0
    for row in rows:
        existing_desc = row["description"] or ""
        if len(existing_desc) < 2:
            continue
        existing_words = {existing_desc[i:i+2] for i in range(len(existing_desc) - 1)}
        if not new_words or not existing_words:
            continue
        overlap = len(new_words & existing_words)
        ratio = overlap / min(len(new_words), len(existing_words))
        if ratio > best_ratio:
            best_ratio = ratio
            best_match = row

    if best_ratio >= 0.5 and best_match is not None:
        return dict(best_match)
    return None


def get_lessons(
    limit: int = 20,
    type: str | None = None,
    min_confirmed: int = 1,
) -> list[dict]:
    """Get lessons ordered by confirmation count (most validated first).

    Args:
        limit: Max lessons to return.
        type: Filter by type (success/mistake/insight). None for all.
        min_confirmed: Minimum times_confirmed threshold.
    """
    conn = _get_conn()
    conditions = ["times_confirmed >= ?"]
    params: list = [min_confirmed]
    if type:
        conditions.append("type = ?")
        params.append(type)
    where = " AND ".join(conditions)
    params.append(limit)
    rows = conn.execute(
        f"SELECT * FROM lessons WHERE {where} "
        "ORDER BY times_confirmed DESC, last_confirmed DESC LIMIT ?",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def format_lessons_context(limit: int = 10) -> str:
    """Format top lessons into a readable context string for agent prompts.

    Returns a concise summary sorted by confirmation count.
    """
    lessons = get_lessons(limit=limit)
    if not lessons:
        return "暂无历史经验"

    type_labels = {"success": "成功", "mistake": "错误", "insight": "洞察"}
    lines = []
    for l in lessons:
        label = type_labels.get(l["type"], l["type"])
        confirmed = l["times_confirmed"]
        desc = l["description"]
        actionable = l.get("actionable") or ""
        entry = f"- [{label}×{confirmed}] {desc}"
        if actionable:
            entry += f" → 规则: {actionable}"
        lines.append(entry)
    return "\n".join(lines)
