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

CREATE TABLE IF NOT EXISTS daily_snapshots (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    data_type TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(date, data_type)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_date ON daily_snapshots(date);

CREATE TABLE IF NOT EXISTS virtual_portfolio (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL,
    name TEXT,
    theme TEXT,
    -- Pending order fields (set at recommendation time)
    order_date TEXT NOT NULL,             -- 挂单日
    entry_low REAL,                       -- 介入区间下限
    entry_high REAL,                      -- 介入区间上限
    stop_loss REAL,                       -- 止损价
    target_price REAL,                    -- 止盈目标价
    expire_days INTEGER DEFAULT 2,        -- 挂单有效天数
    -- Fill fields (set when order triggers)
    open_date TEXT,                        -- 实际建仓日
    open_price REAL,                       -- 实际建仓价
    shares INTEGER DEFAULT 0,
    -- Status: pending → open → stopped/target_hit/expired/cancelled
    status TEXT DEFAULT 'pending',
    close_date TEXT,
    close_price REAL,
    holding_days INTEGER DEFAULT 0,
    return_pct REAL,
    return_amount REAL,
    peak_return_pct REAL DEFAULT 0,
    max_drawdown_pct REAL DEFAULT 0,
    source TEXT,
    reason TEXT,
    close_reason TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_portfolio_status ON virtual_portfolio(status);
CREATE INDEX IF NOT EXISTS idx_portfolio_date ON virtual_portfolio(open_date);

CREATE TABLE IF NOT EXISTS custom_tasks (
    id INTEGER PRIMARY KEY,
    prompt TEXT NOT NULL,
    schedule_time TEXT,
    interval TEXT DEFAULT 'once',
    status TEXT DEFAULT 'active',
    last_run TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS price_alerts (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL,
    name TEXT,
    condition TEXT NOT NULL,
    target_price REAL NOT NULL,
    reason TEXT,
    status TEXT DEFAULT 'active',
    created_at TEXT DEFAULT (datetime('now')),
    triggered_at TEXT
);

CREATE TABLE IF NOT EXISTS sector_betas (
    id INTEGER PRIMARY KEY,
    concept TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    beta_20d REAL,
    beta_60d REAL,
    beta_120d REAL,
    beta_weighted REAL,
    avg_daily_amount REAL,
    updated_at TEXT,
    UNIQUE(concept, code)
);

CREATE TABLE IF NOT EXISTS sentiment_phase (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL UNIQUE,
    phase TEXT NOT NULL,
    phase_en TEXT,
    confidence REAL,
    indicators TEXT,
    strategy TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chat_memory (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    summary TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS vpa_analysis_history (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL,
    name TEXT,
    analysis_date TEXT NOT NULL,
    verdict TEXT,
    confidence REAL,
    phase TEXT,
    confirmed INTEGER DEFAULT 0,
    reason TEXT,
    report TEXT,
    signals_json TEXT,
    target_low REAL,                 -- v2.5: VPA推导目标价区间下限
    target_high REAL,                -- v2.5: VPA推导目标价区间上限
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_vpa_history_code_date ON vpa_analysis_history(code, analysis_date);

CREATE TABLE IF NOT EXISTS vpa_pending_signals (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL,
    name TEXT,
    signal_type TEXT NOT NULL,
    signal_date TEXT NOT NULL,
    direction TEXT NOT NULL,
    expected_confirmation TEXT,
    expected_denial TEXT,
    status TEXT DEFAULT 'pending',
    expire_date TEXT,
    source_analysis_id INTEGER,
    resolved_date TEXT,
    resolved_by TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (source_analysis_id) REFERENCES vpa_analysis_history(id)
);
CREATE INDEX IF NOT EXISTS idx_vpa_signals_status ON vpa_pending_signals(status);
CREATE INDEX IF NOT EXISTS idx_vpa_signals_code ON vpa_pending_signals(code);

CREATE TABLE IF NOT EXISTS financial_cache (
    code TEXT PRIMARY KEY,
    data TEXT NOT NULL,          -- JSON blob from get_financial_data_fn
    report_date TEXT,            -- 最新报告日 (e.g. "20241231")
    cached_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_financial_cached_at ON financial_cache(cached_at);
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
        # Schema migrations for older DBs — CREATE IF NOT EXISTS doesn't add
        # columns to existing tables. Each ALTER is wrapped in try/except to
        # ignore "duplicate column" errors on already-migrated DBs.
        for migration in (
            "ALTER TABLE vpa_analysis_history ADD COLUMN target_low REAL",
            "ALTER TABLE vpa_analysis_history ADD COLUMN target_high REAL",
        ):
            try:
                conn.execute(migration)
            except sqlite3.OperationalError:
                pass  # Column already exists
        _local.conn = conn
    return conn


# ── Custom Tasks ─────────────────────────────────────────────

def create_custom_task(prompt: str, schedule_time: str, interval: str = "once") -> int:
    """Create a custom scheduled task.

    Args:
        prompt: Natural language instruction for the agent
        schedule_time: Time to run, e.g. "14:00"
        interval: "once" / "daily" / "weekday" (trading days)
    """
    with _write_lock:
        conn = _get_conn()
        cursor = conn.execute(
            "INSERT INTO custom_tasks (prompt, schedule_time, interval) VALUES (?, ?, ?)",
            (prompt, schedule_time, interval),
        )
        conn.commit()
        return cursor.lastrowid


def get_active_custom_tasks() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM custom_tasks WHERE status = 'active' ORDER BY schedule_time"
    ).fetchall()
    return [dict(r) for r in rows]


def update_custom_task_last_run(task_id: int) -> None:
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE custom_tasks SET last_run = datetime('now') WHERE id = ?",
            (task_id,),
        )
        # If one-time task, mark as done
        conn.execute(
            "UPDATE custom_tasks SET status = 'done' WHERE id = ? AND interval = 'once'",
            (task_id,),
        )
        conn.commit()


def delete_custom_task(task_id: int) -> None:
    with _write_lock:
        conn = _get_conn()
        conn.execute("DELETE FROM custom_tasks WHERE id = ?", (task_id,))
        conn.commit()


# ── Price Alerts ─────────────────────────────────────────────

def create_price_alert(code: str, name: str, condition: str, target_price: float, reason: str = "") -> int:
    """Create a price alert. condition: 'above' or 'below'."""
    with _write_lock:
        conn = _get_conn()
        cursor = conn.execute(
            "INSERT INTO price_alerts (code, name, condition, target_price, reason) "
            "VALUES (?, ?, ?, ?, ?)",
            (code, name, condition, target_price, reason),
        )
        conn.commit()
        return cursor.lastrowid


def get_active_price_alerts() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM price_alerts WHERE status = 'active' ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def trigger_price_alert(alert_id: int) -> None:
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE price_alerts SET status = 'triggered', triggered_at = datetime('now') WHERE id = ?",
            (alert_id,),
        )
        conn.commit()


def delete_price_alert(alert_id: int) -> None:
    with _write_lock:
        conn = _get_conn()
        conn.execute("DELETE FROM price_alerts WHERE id = ?", (alert_id,))
        conn.commit()


# ── Chat Memory ─────────────────────────────────────────────

def save_chat_memory(summary: str) -> None:
    """Save chat session summary for cross-session memory."""
    from datetime import datetime
    with _write_lock:
        conn = _get_conn()
        today = datetime.now().strftime("%Y-%m-%d")
        # Upsert: one summary per day (latest wins)
        conn.execute(
            "INSERT INTO chat_memory (date, summary) VALUES (?, ?) ",
            (today, summary),
        )
        conn.commit()


def get_recent_chat_memories(days: int = 7) -> list[str]:
    """Get recent chat session summaries for context."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT summary FROM chat_memory ORDER BY id DESC LIMIT ?",
        (days,),
    ).fetchall()
    return [r["summary"] for r in rows]


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


def get_today_intraday_predictions() -> list[dict]:
    """Get today's intraday predictions for context continuity."""
    conn = _get_conn()
    today = datetime.now().strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT code, name, direction, confidence, theme_line, entry_price, reason "
        "FROM predictions WHERE date = ? AND report_type = 'intraday' "
        "ORDER BY id DESC",
        (today,),
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


# ── VPA Analysis History ──────────────────────────────────────

def save_vpa_analysis(code: str, name: str, analysis_date: str,
                      verdict: str, confidence: float, phase: str,
                      confirmed: bool, reason: str, report: str,
                      signals_json: str = "",
                      target_low: float | None = None,
                      target_high: float | None = None) -> int:
    """Save a VPA analysis report to history. Returns row id.

    target_low/target_high (v2.5): Optional VPA-derived target price zone,
    used by portfolio.py to set take-profit on positions opened from this
    analysis. Based on Wyckoff cause-and-effect: longer accumulation →
    larger target.
    """
    with _write_lock:
        conn = _get_conn()
        cur = conn.execute(
            "INSERT INTO vpa_analysis_history "
            "(code, name, analysis_date, verdict, confidence, phase, "
            " confirmed, reason, report, signals_json, target_low, target_high) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (code, name, analysis_date, verdict, confidence, phase,
             1 if confirmed else 0, reason, report, signals_json,
             target_low, target_high),
        )
        conn.commit()
        return cur.lastrowid


def get_latest_vpa_analysis(code: str) -> dict | None:
    """Get the most recent VPA analysis for a stock."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM vpa_analysis_history WHERE code = ? ORDER BY analysis_date DESC, id DESC LIMIT 1",
        (code,),
    ).fetchone()
    return dict(row) if row else None


def get_vpa_history(code: str, limit: int = 5) -> list[dict]:
    """Get recent VPA analyses for a stock."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM vpa_analysis_history WHERE code = ? ORDER BY analysis_date DESC LIMIT ?",
        (code, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ── VPA Pending Signals ──────────────────────────────────────

def save_vpa_signal(code: str, name: str, signal_type: str, signal_date: str,
                    direction: str, expected_confirmation: str = "",
                    expected_denial: str = "", expire_days: int = 3,
                    source_analysis_id: int | None = None) -> int:
    """Create a pending VPA signal to track. Returns row id. Deduplicates by (code, signal_type, signal_date)."""
    from datetime import datetime, timedelta
    # LLM may return "04-14" or "2026-04-14" — normalize
    if len(signal_date) <= 5:
        signal_date = f"{datetime.now().year}-{signal_date}"
    try:
        expire = (datetime.strptime(signal_date, "%Y-%m-%d") + timedelta(days=expire_days)).strftime("%Y-%m-%d")
    except ValueError:
        expire = (datetime.now() + timedelta(days=expire_days)).strftime("%Y-%m-%d")

    # Dedup: skip if same signal already pending for this stock
    conn = _get_conn()
    existing = conn.execute(
        "SELECT id FROM vpa_pending_signals WHERE code = ? AND signal_type = ? AND signal_date = ? AND status = 'pending'",
        (code, signal_type, signal_date),
    ).fetchone()
    if existing:
        return existing["id"]
    with _write_lock:
        conn = _get_conn()
        cur = conn.execute(
            "INSERT INTO vpa_pending_signals "
            "(code, name, signal_type, signal_date, direction, expected_confirmation, "
            " expected_denial, status, expire_date, source_analysis_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (code, name, signal_type, signal_date, direction,
             expected_confirmation, expected_denial, expire, source_analysis_id),
        )
        conn.commit()
        return cur.lastrowid


def get_pending_vpa_signals() -> list[dict]:
    """Get all pending (unresolved) VPA signals."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM vpa_pending_signals WHERE status = 'pending' ORDER BY signal_date",
    ).fetchall()
    return [dict(r) for r in rows]


def resolve_vpa_signal(signal_id: int, status: str, resolved_by: str = "") -> None:
    """Mark a VPA signal as confirmed/denied/expired."""
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE vpa_pending_signals SET status = ?, resolved_date = date('now'), resolved_by = ? WHERE id = ?",
            (status, resolved_by, signal_id),
        )
        conn.commit()


def expire_old_vpa_signals(today: str) -> int:
    """Expire signals past their expire_date. Returns count expired."""
    with _write_lock:
        conn = _get_conn()
        cur = conn.execute(
            "UPDATE vpa_pending_signals SET status = 'expired', resolved_date = ? "
            "WHERE status = 'pending' AND expire_date < ?",
            (today, today),
        )
        conn.commit()
        return cur.rowcount


# ── Financial data cache ────────────────────────────────────
# Quarterly financials rarely change — cache for 30 days to avoid
# hammering akshare on every cross-validation run.

def get_cached_financials(code: str, max_age_days: int = 30) -> dict | None:
    """Return cached financial data if fresh, else None.

    Args:
        code: 6-digit stock code
        max_age_days: TTL in days. Default 30 covers a quarterly cycle;
            new quarterly reports should prompt manual cache invalidation
            or natural expiry.
    """
    conn = _get_conn()
    row = conn.execute(
        "SELECT data, cached_at FROM financial_cache WHERE code = ?", (code,)
    ).fetchone()
    if not row:
        return None
    try:
        cached_at = datetime.fromisoformat(row["cached_at"])
    except (ValueError, TypeError):
        return None
    age_days = (datetime.now() - cached_at).days
    if age_days > max_age_days:
        return None
    try:
        return json.loads(row["data"])
    except (json.JSONDecodeError, TypeError):
        return None


def save_cached_financials(code: str, data: dict) -> None:
    """Save financial data to cache. Overwrites existing entry."""
    with _write_lock:
        conn = _get_conn()
        report_date = data.get("report_date", "") if isinstance(data, dict) else ""
        conn.execute(
            "INSERT OR REPLACE INTO financial_cache (code, data, report_date, cached_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            (code, json.dumps(data, ensure_ascii=False), report_date),
        )
        conn.commit()


# ── Theme strength snapshots (for velocity detection) ───────
# Snapshot daily at end of morning_scan so we can detect rapid theme decay
# (e.g., strength dropping from 9 → 5 in 2 days) which is a sell signal
# even if the current strength hasn't yet crossed the exit threshold.

def save_theme_snapshot(date: str, themes: list[dict]) -> None:
    """Snapshot current theme strengths for the given date.

    Args:
        date: "YYYY-MM-DD"
        themes: [{"name": "...", "strength": 8, "status": "..."}, ...]
    """
    with _write_lock:
        conn = _get_conn()
        payload = json.dumps(
            [{"name": t.get("name"), "strength": t.get("strength"), "status": t.get("status")}
             for t in themes],
            ensure_ascii=False,
        )
        conn.execute(
            "INSERT OR REPLACE INTO daily_snapshots (date, data_type, data) "
            "VALUES (?, 'theme_strengths', ?)",
            (date, payload),
        )
        conn.commit()


def get_theme_strength_history(theme_name: str, days: int = 5) -> list[dict]:
    """Get strength history for a theme over the last N days.

    Returns list sorted newest-first: [{"date": "...", "strength": N}, ...].
    Missing days are skipped (not padded).
    """
    conn = _get_conn()
    rows = conn.execute(
        "SELECT date, data FROM daily_snapshots "
        "WHERE data_type = 'theme_strengths' "
        "ORDER BY date DESC LIMIT ?",
        (days,),
    ).fetchall()
    history = []
    for r in rows:
        try:
            themes = json.loads(r["data"])
        except (json.JSONDecodeError, TypeError):
            continue
        for t in themes:
            if t.get("name") == theme_name and t.get("strength") is not None:
                history.append({"date": r["date"], "strength": t["strength"]})
                break
    return history


def get_recent_sentiment_phases(n: int = 2) -> list[dict]:
    """Get the N most recent sentiment phases, newest first.

    Returns: [{"date": "...", "phase": "..."}, ...]
    """
    conn = _get_conn()
    rows = conn.execute(
        "SELECT date, phase FROM sentiment_phase ORDER BY date DESC LIMIT ?",
        (n,),
    ).fetchall()
    return [dict(r) for r in rows]
