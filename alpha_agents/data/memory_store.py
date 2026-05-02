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

-- v2.5 (problem 7): Scenario layer — a Wyckoff story told by multiple
-- signals together. Confirmation happens at scenario level (holistic price
-- action + signal consensus), not per individual bar.
CREATE TABLE IF NOT EXISTS vpa_scenarios (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL,
    name TEXT,                       -- 股票名
    scenario_name TEXT NOT NULL,     -- 如"终极派发"/"初期吸筹"
    phase TEXT,                      -- 对应 Wyckoff 阶段
    signal_names TEXT,               -- JSON array: 组成该 scenario 的底层信号名
    confirmation_criteria TEXT,      -- scenario整体确认条件
    denial_criteria TEXT,            -- scenario整体否定条件
    status TEXT DEFAULT 'pending',   -- pending/confirmed/denied/expired
    scenario_date TEXT NOT NULL,     -- 首次提出日期
    resolved_date TEXT,
    resolved_by TEXT,
    expire_date TEXT,
    source_analysis_id INTEGER,
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (source_analysis_id) REFERENCES vpa_analysis_history(id)
);
CREATE INDEX IF NOT EXISTS idx_vpa_scenarios_status ON vpa_scenarios(status);
CREATE INDEX IF NOT EXISTS idx_vpa_scenarios_code ON vpa_scenarios(code);

CREATE TABLE IF NOT EXISTS financial_cache (
    code TEXT PRIMARY KEY,
    data TEXT NOT NULL,          -- JSON blob from get_financial_data_fn
    report_date TEXT,            -- 最新报告日 (e.g. "20241231")
    cached_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_financial_cached_at ON financial_cache(cached_at);

CREATE TABLE IF NOT EXISTS daily_lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    lesson_type TEXT NOT NULL,
    theme TEXT,
    content TEXT NOT NULL,
    source TEXT DEFAULT 'review',
    relevance_tags TEXT DEFAULT '',
    consolidated_into INTEGER,
    UNIQUE(date, content)
);
CREATE INDEX IF NOT EXISTS idx_lessons_date ON daily_lessons(date);

CREATE TABLE IF NOT EXISTS trading_principles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    principle TEXT NOT NULL UNIQUE,
    pattern_description TEXT NOT NULL,
    category TEXT NOT NULL,
    action_guidance TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '[]',
    evidence_count INTEGER DEFAULT 1,
    win_rate REAL,
    first_learned TEXT NOT NULL,
    last_reinforced TEXT NOT NULL,
    status TEXT DEFAULT 'active'
);
CREATE INDEX IF NOT EXISTS idx_principles_status ON trading_principles(status);
CREATE INDEX IF NOT EXISTS idx_principles_category ON trading_principles(category);

CREATE TABLE IF NOT EXISTS playbooks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    pattern_json TEXT NOT NULL,
    created_date TEXT NOT NULL,
    last_updated TEXT NOT NULL,
    status TEXT DEFAULT 'active',
    weight REAL DEFAULT 1.0,
    total_trades INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    hit_rate REAL DEFAULT 0.0,
    avg_return REAL DEFAULT 0.0,
    annotation TEXT DEFAULT '',
    annotation_date TEXT DEFAULT '',
    version_history TEXT DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_playbooks_status ON playbooks(status);

CREATE TABLE IF NOT EXISTS evolution_metrics (
    date TEXT PRIMARY KEY,
    intraday_hit_rate_7d REAL,
    intraday_count_7d INTEGER,
    matched_hit_rate_7d REAL,
    matched_count_7d INTEGER,
    unmatched_hit_rate_7d REAL,
    unmatched_count_7d INTEGER,
    active_principles INTEGER DEFAULT 0,
    weakened_principles INTEGER DEFAULT 0,
    active_playbooks INTEGER DEFAULT 0,
    degraded_playbooks INTEGER DEFAULT 0,
    lessons_count_7d INTEGER DEFAULT 0
);
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
        # Phase 1 migration: add features_json to predictions (idempotent).
        # Used by Playbook clustering (Phase 3) to group predictions by decision features
        # (vpa_verdict, theme_strength, institutional, score, etc.).
        try:
            conn.execute(
                "ALTER TABLE predictions ADD COLUMN features_json TEXT DEFAULT '{}'"
            )
            conn.commit()
        except sqlite3.OperationalError as e:
            # Column already exists — expected on every restart after first migration.
            if "duplicate column name" not in str(e).lower():
                raise
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
    features: dict | None = None,
) -> int:
    """Record a stock recommendation.

    ``features`` (Phase 1): optional dict of decision-time features used by the
    Playbook clustering in Phase 3. Serialized to ``features_json`` column.
    Pass None for legacy callers (stored as empty '{}').
    """
    features_json = json.dumps(features or {}, ensure_ascii=False)
    with _write_lock:
        conn = _get_conn()
        cur = conn.execute(
            "INSERT INTO predictions (date, report_type, code, name, direction, "
            "confidence, theme_line, entry_price, reason, features_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (date, report_type, code, name, direction, confidence, theme_line,
             entry_price, reason, features_json),
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


def get_pending_prediction_dates(before_date: str, days: int = 14) -> list[str]:
    """Recent prediction dates before ``before_date`` still awaiting review."""
    from datetime import datetime as _dt, timedelta as _td
    try:
        cutoff = (_dt.strptime(before_date, "%Y-%m-%d") - _td(days=days)).strftime("%Y-%m-%d")
    except ValueError:
        cutoff = "0000-00-00"
    conn = _get_conn()
    rows = conn.execute(
        "SELECT DISTINCT date FROM predictions "
        "WHERE hit IS NULL AND date < ? AND date >= ? "
        "ORDER BY date",
        (before_date, cutoff),
    ).fetchall()
    return [r["date"] for r in rows]


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


def get_latest_vpa_analysis(code: str, as_of: str | None = None) -> dict | None:
    """Get the most recent VPA analysis for a stock.

    ``as_of`` (YYYY-MM-DD): only return analyses dated strictly BEFORE as_of
    — used by backtest replay to prevent future-data leakage.
    """
    conn = _get_conn()
    if as_of:
        row = conn.execute(
            "SELECT * FROM vpa_analysis_history WHERE code = ? AND analysis_date < ? "
            "ORDER BY analysis_date DESC, id DESC LIMIT 1",
            (code, as_of),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM vpa_analysis_history WHERE code = ? "
            "ORDER BY analysis_date DESC, id DESC LIMIT 1",
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


# ── VPA Scenarios (problem 7: signal-group-level confirmation) ─────

def save_vpa_scenario(code: str, name: str, scenario_name: str,
                      phase: str, signal_names: list[str],
                      confirmation: str, denial: str,
                      scenario_date: str,
                      source_analysis_id: int | None = None,
                      expire_days: int = 10) -> int | None:
    """Record a Wyckoff scenario hypothesis for later confirmation.

    Scenarios are DEDUPLICATED on (code, scenario_name, status='pending') —
    re-running VPA on the same day for the same stock that re-proposes the
    same scenario is a no-op, not a duplicate row. Expiry defaults to 10
    days (scenarios tell longer stories than per-bar signals).

    Returns row id, or None if duplicate.
    """
    from datetime import datetime as _dt, timedelta as _td
    try:
        expire = (_dt.strptime(scenario_date, "%Y-%m-%d") + _td(days=expire_days)).strftime("%Y-%m-%d")
    except ValueError:
        expire = scenario_date

    with _write_lock:
        conn = _get_conn()
        # Dedup: same stock + same scenario_name still pending
        existing = conn.execute(
            "SELECT id FROM vpa_scenarios "
            "WHERE code = ? AND scenario_name = ? AND status = 'pending'",
            (code, scenario_name),
        ).fetchone()
        if existing:
            return None
        cur = conn.execute(
            "INSERT INTO vpa_scenarios "
            "(code, name, scenario_name, phase, signal_names, "
            " confirmation_criteria, denial_criteria, scenario_date, "
            " source_analysis_id, expire_date, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
            (code, name, scenario_name, phase,
             json.dumps(signal_names, ensure_ascii=False),
             confirmation, denial, scenario_date,
             source_analysis_id, expire),
        )
        conn.commit()
        return cur.lastrowid


def get_pending_vpa_scenarios(code: str = "") -> list[dict]:
    """Get pending scenarios. If code is given, filter to that code."""
    conn = _get_conn()
    if code:
        rows = conn.execute(
            "SELECT * FROM vpa_scenarios WHERE code = ? AND status = 'pending' "
            "ORDER BY scenario_date DESC",
            (code,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM vpa_scenarios WHERE status = 'pending' "
            "ORDER BY scenario_date DESC",
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["signal_names"] = json.loads(d.get("signal_names") or "[]")
        except (json.JSONDecodeError, TypeError):
            d["signal_names"] = []
        out.append(d)
    return out


def resolve_vpa_scenario(scenario_id: int, status: str, resolved_by: str = "") -> None:
    """Mark a scenario as confirmed/denied/expired."""
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE vpa_scenarios SET status = ?, resolved_date = date('now'), "
            "resolved_by = ? WHERE id = ?",
            (status, resolved_by, scenario_id),
        )
        conn.commit()


def expire_old_vpa_scenarios(today: str) -> int:
    """Expire scenarios past their expire_date. Returns count expired."""
    with _write_lock:
        conn = _get_conn()
        cur = conn.execute(
            "UPDATE vpa_scenarios SET status = 'expired', resolved_date = ? "
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


# ── Phase 2: daily lessons ────────────────────────────────────
def insert_daily_lesson(date: str, lesson_type: str, theme: str | None,
                        content: str, tags: str = "", source: str = "review") -> None:
    """Insert a lesson; silently skips if (date, content) already exists."""
    with _write_lock:
        conn = _get_conn()
        try:
            conn.execute(
                "INSERT INTO daily_lessons (date, lesson_type, theme, content, source, relevance_tags) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (date, lesson_type, theme, content, source, tags),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            pass  # duplicate (date, content)


def get_recent_daily_lessons(days: int = 7, themes: list[str] | None = None) -> list[dict]:
    """Return lessons from last N days. If themes given, ONLY matching ones."""
    from datetime import datetime, timedelta
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    q = "SELECT * FROM daily_lessons WHERE date >= ?"
    params: list = [cutoff]
    if themes:
        placeholders = ",".join("?" * len(themes))
        q += f" AND theme IN ({placeholders})"
        params.extend(themes)
    q += " ORDER BY date DESC, id DESC"
    return [dict(r) for r in _get_conn().execute(q, params).fetchall()]


def get_historical_lessons_by_themes(themes: list[str], older_than_days: int = 7,
                                      limit: int = 10) -> list[dict]:
    """For filtering old lessons by currently active themes (budget-aware injection)."""
    if not themes:
        return []
    from datetime import datetime, timedelta
    cutoff = (datetime.now() - timedelta(days=older_than_days)).strftime("%Y-%m-%d")
    placeholders = ",".join("?" * len(themes))
    q = (f"SELECT * FROM daily_lessons WHERE date < ? AND theme IN ({placeholders}) "
         f"ORDER BY date DESC LIMIT ?")
    return [dict(r) for r in _get_conn().execute(q, [cutoff, *themes, limit]).fetchall()]


# ── Phase 2: trading principles ───────────────────────────────
def create_trading_principle(*, principle: str, pattern_description: str,
                              category: str, action_guidance: str,
                              evidence: list[dict], today: str,
                              win_rate: float | None = None) -> int:
    with _write_lock:
        conn = _get_conn()
        cur = conn.execute(
            "INSERT INTO trading_principles "
            "(principle, pattern_description, category, action_guidance, "
            " evidence, evidence_count, win_rate, first_learned, last_reinforced, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')",
            (principle, pattern_description, category, action_guidance,
             json.dumps(evidence, ensure_ascii=False), len(evidence),
             win_rate, today, today),
        )
        conn.commit()
        return cur.lastrowid


def reinforce_trading_principle(principle_id: int, *, today: str,
                                 new_case: dict | None = None,
                                 win_rate: float | None = None) -> None:
    with _write_lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT evidence, evidence_count FROM trading_principles WHERE id = ?",
            (principle_id,),
        ).fetchone()
        if not row:
            return
        evidence = json.loads(row["evidence"] or "[]")
        if new_case:
            evidence.append(new_case)
        updates = [
            "evidence = ?",
            "evidence_count = ?",
            "last_reinforced = ?",
            "status = 'active'",
        ]
        params: list = [json.dumps(evidence, ensure_ascii=False),
                        len(evidence), today]
        if win_rate is not None:
            updates.append("win_rate = ?")
            params.append(win_rate)
        params.append(principle_id)
        conn.execute(
            f"UPDATE trading_principles SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        conn.commit()


def set_principle_status(principle_id: int, status: str) -> None:
    """status ∈ {'active', 'weakened', 'retired'}"""
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE trading_principles SET status = ? WHERE id = ?",
            (status, principle_id),
        )
        conn.commit()


def get_active_principles() -> list[dict]:
    rows = _get_conn().execute(
        "SELECT * FROM trading_principles WHERE status = 'active' "
        "ORDER BY evidence_count DESC, last_reinforced DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_all_principles_including_weakened() -> list[dict]:
    rows = _get_conn().execute(
        "SELECT * FROM trading_principles WHERE status IN ('active', 'weakened') "
        "ORDER BY status, evidence_count DESC"
    ).fetchall()
    return [dict(r) for r in rows]


# ── Phase 3: playbooks ────────────────────────────────────────
def create_playbook(*, name: str, pattern_json: dict, today: str,
                    status: str = "active", weight: float = 1.0) -> int:
    with _write_lock:
        conn = _get_conn()
        cur = conn.execute(
            "INSERT INTO playbooks (name, pattern_json, created_date, last_updated, "
            " status, weight, version_history) "
            "VALUES (?, ?, ?, ?, ?, ?, '[]')",
            (name, json.dumps(pattern_json, ensure_ascii=False),
             today, today, status, weight),
        )
        conn.commit()
        return cur.lastrowid


def update_playbook_status(playbook_id: int, *, status: str, weight: float,
                            reason: str, hit_rate_at_change: float,
                            today: str) -> None:
    with _write_lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT status, version_history FROM playbooks WHERE id = ?",
            (playbook_id,),
        ).fetchone()
        if not row:
            return
        old_status = row["status"]
        history = json.loads(row["version_history"] or "[]")
        history.append({
            "date": today,
            "old_status": old_status,
            "new_status": status,
            "reason": reason,
            "hit_rate_at_change": hit_rate_at_change,
        })
        conn.execute(
            "UPDATE playbooks SET status = ?, weight = ?, last_updated = ?, "
            "version_history = ? WHERE id = ?",
            (status, weight, today,
             json.dumps(history, ensure_ascii=False), playbook_id),
        )
        conn.commit()


def record_playbook_trade(playbook_id: int, *, hit: bool,
                           return_pct: float) -> None:
    """Called when a prediction matched to a playbook is verified."""
    with _write_lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT total_trades, wins, avg_return FROM playbooks WHERE id = ?",
            (playbook_id,),
        ).fetchone()
        if not row:
            return
        total = row["total_trades"] + 1
        wins = row["wins"] + (1 if hit else 0)
        prev_avg = row["avg_return"] or 0.0
        new_avg = (prev_avg * (total - 1) + return_pct) / total
        conn.execute(
            "UPDATE playbooks SET total_trades = ?, wins = ?, hit_rate = ?, "
            "avg_return = ? WHERE id = ?",
            (total, wins, wins / total, new_avg, playbook_id),
        )
        conn.commit()


def set_playbook_annotation(playbook_id: int, *, annotation: str,
                             today: str) -> None:
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE playbooks SET annotation = ?, annotation_date = ? "
            "WHERE id = ?",
            (annotation, today, playbook_id),
        )
        conn.commit()


def get_active_playbooks() -> list[dict]:
    rows = _get_conn().execute(
        "SELECT * FROM playbooks WHERE status = 'active' "
        "ORDER BY weight DESC, hit_rate DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_all_playbooks() -> list[dict]:
    rows = _get_conn().execute(
        "SELECT * FROM playbooks ORDER BY status, weight DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_active_or_degraded_playbooks() -> list[dict]:
    rows = _get_conn().execute(
        "SELECT * FROM playbooks WHERE status IN ('active', 'degraded') "
        "ORDER BY status, weight DESC"
    ).fetchall()
    return [dict(r) for r in rows]


# ── Phase 4: evolution metrics ────────────────────────────────
def upsert_evolution_metrics(date: str, fields: dict) -> None:
    """Insert or replace a daily metrics row. `fields` maps column names to values."""
    cols = [
        "intraday_hit_rate_7d", "intraday_count_7d",
        "matched_hit_rate_7d", "matched_count_7d",
        "unmatched_hit_rate_7d", "unmatched_count_7d",
        "active_principles", "weakened_principles",
        "active_playbooks", "degraded_playbooks",
        "lessons_count_7d",
    ]
    values = [fields.get(c) for c in cols]
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO evolution_metrics "
            f"(date, {', '.join(cols)}) VALUES (?, {', '.join('?' * len(cols))})",
            (date, *values),
        )
        conn.commit()


def get_evolution_metrics_trend(days: int = 30) -> list[dict]:
    from datetime import datetime, timedelta
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = _get_conn().execute(
        "SELECT * FROM evolution_metrics WHERE date >= ? ORDER BY date",
        (cutoff,),
    ).fetchall()
    return [dict(r) for r in rows]
