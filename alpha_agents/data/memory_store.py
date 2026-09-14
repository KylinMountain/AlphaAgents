"""Persistent memory for the analyst — theme lines, predictions, market cognition.

Follows the same pattern as report_store.py: thread-local SQLite connections,
plain functions, JSON for flexible fields.

Two things moved out on 2026-09-14, both because they are edited for different
reasons than the functions here: the DDL is ``data/memory_schema.py`` (re-exported
as ``_SCHEMA``), and the caches of what *other* systems produced — VPA analyses,
signals and financial statements — are ``data/vpa_store.py``. This module is the
trader's own record of its own decisions, and the connection those tables share.
"""

import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from alpha_agents.data.memory_schema import _SCHEMA  # re-exported; see memory_schema.py
from alpha_agents.config import MEMORY_DB_PATH

# A 2100-line store with no logger was where three failures went silent: two
# migration skips and a duplicate lesson. All three are genuinely ignorable, and
# all three were unreadable afterwards. Debug level, because a migration that was
# already applied is not news.
logger = logging.getLogger(__name__)


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
            # Phase 1 attribution: an order names the prediction it came from,
            # instead of the close path guessing it by stock and date.
            "ALTER TABLE virtual_portfolio ADD COLUMN prediction_id INTEGER",
            "ALTER TABLE virtual_portfolio ADD COLUMN legacy_realized_amount REAL",
            # Phase 1 attribution chain: the order names its thesis, and each
            # realised exit leg carries it too, so "which idea earned this"
            # survives both a re-opened position and a rewritten thesis.
            "ALTER TABLE virtual_portfolio ADD COLUMN thesis_id INTEGER",
            "ALTER TABLE position_exits ADD COLUMN thesis_id INTEGER",
        ):
            try:
                conn.execute(migration)
            except sqlite3.OperationalError as e:
                logger.debug("migration skipped, column already present: %s", e)
        # After the columns exist, not before — see the note in the schema.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_exits_thesis "
            "ON position_exits(thesis_id)"
        )
        conn.commit()
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

        # G1 migration: probabilistic forecasts and their scores.
        # A prediction states P(this beats the market over the horizon)
        # rather than a bare 看多/看空, so it can be graded with a proper
        # scoring rule — cumulative return needs years to reach
        # significance, Brier needs hundreds of observations. See
        # docs/self_improvement_roadmap.md G1.
        for migration in (
            "ALTER TABLE predictions ADD COLUMN prob REAL",
            "ALTER TABLE predictions ADD COLUMN brier REAL",
            "ALTER TABLE predictions ADD COLUMN log_score REAL",
            "ALTER TABLE predictions ADD COLUMN excess_return REAL",
            "ALTER TABLE predictions ADD COLUMN residual_alpha REAL",
            "ALTER TABLE predictions ADD COLUMN scored_at TEXT",
            "ALTER TABLE predictions ADD COLUMN created_at TEXT",
            # Phase 3 / T2: the horizon a forecast declared, and the day it
            # matures. Nullable on purpose — see the note in _SCHEMA.
            "ALTER TABLE predictions ADD COLUMN horizon_days INTEGER",
            "ALTER TABLE predictions ADD COLUMN deadline TEXT",
        ):
            try:
                conn.execute(migration)
            except sqlite3.OperationalError as e:
                logger.debug("migration skipped, column already present: %s", e)

        # Split theme strength into lifecycle vs today. The old single
        # `strength` was incremented on every intraday cycle — 48 times a
        # session — so a line with steady inflow saturated at 10 within
        # half an hour and a weak one floored at 0, and the number meant
        # "how many cycles in a row did the signal fire", not strength.
        # A pending order was cancelled on 金属铜 强度3 on a day that line
        # ran +2.0% vs the market on 55億 of inflow.
        # Sizing became a decision the agent makes, so the thesis carries
        # the share of the book it asked for. Declared in _SCHEMA too; this
        # is for databases created before that.
        for migration in (
            "ALTER TABLE theses ADD COLUMN entry_fraction REAL DEFAULT 0",
            "ALTER TABLE theses ADD COLUMN trader_id TEXT DEFAULT 'default'",
            "ALTER TABLE virtual_portfolio ADD COLUMN trader_id TEXT DEFAULT 'default'",
            "ALTER TABLE predictions ADD COLUMN trader_id TEXT DEFAULT 'default'",
            "ALTER TABLE theme_lines ADD COLUMN daily_score INTEGER DEFAULT 0",
            "ALTER TABLE theme_lines ADD COLUMN last_scored_date TEXT",
            # Today's cross-sectionally normalised theme strength. Null on every
            # pre-existing row, which is the honest value: those rows were never
            # scored on this scale, and reading null as 0 would retire them.
            "ALTER TABLE theme_lines ADD COLUMN trend_score REAL",
            # Consecutive daily closes the board could not name this theme. One
            # miss is a truncated frame (the endpoint returns 287–380 of ~387
            # boards, varying per call); several in a row are the theme. See
            # `theme_manager.mark_theme_unscored`.
            "ALTER TABLE theme_lines ADD COLUMN unmeasured_days INTEGER DEFAULT 0",
            # Indexes live here rather than in _SCHEMA: executescript runs
            # before the ALTERs above, so indexing a column an older
            # database has not gained yet fails the whole schema pass.
            "CREATE INDEX IF NOT EXISTS idx_portfolio_trader "
            "ON virtual_portfolio(trader_id)",
            "CREATE INDEX IF NOT EXISTS idx_theses_trader ON theses(trader_id)",
            # The evaluator's "what has matured" query, which reads the
            # declared deadline rather than a global horizon.
            "CREATE INDEX IF NOT EXISTS idx_pred_deadline "
            "ON predictions(deadline)",
        ):
            try:
                conn.execute(migration)
            except sqlite3.OperationalError as e:
                # Only the re-run case is expected. A bare `pass` here
                # would also swallow a locked or corrupt database and
                # leave the gate reading a column that does not exist.
                if "duplicate column name" not in str(e).lower():
                    raise
        conn.commit()

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
            "UPDATE custom_tasks SET last_run = datetime('now','localtime') WHERE id = ?",
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
            "UPDATE price_alerts SET status = 'triggered', triggered_at = datetime('now','localtime') WHERE id = ?",
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

def get_active_themes(min_status: str = "watching",
                      order_by: str = "strength") -> list[dict]:
    """Get all non-archived theme lines.

    ``order_by="strength"`` (the default) ranks by lifecycle position —
    how long the line has been confirmed. ``order_by="daily_score"`` ranks
    by today's raw signal, which is what "哪条主线今天最强" asks: a line
    running two weeks always out-accumulates one that broke out this
    morning, so strength cannot answer that question.
    """
    conn = _get_conn()
    order = ("daily_score DESC, strength DESC" if order_by == "daily_score"
             else "strength DESC, daily_score DESC")
    rows = conn.execute(
        f"SELECT * FROM theme_lines WHERE status != 'archived' ORDER BY {order}"
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
    daily_score: int | None = None,
    trend_score: float | None = None,
    unmeasured_days: int | None = None,
    last_scored_date: str | None = None,
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
            if daily_score is not None:
                sets.append("daily_score = ?"); vals.append(daily_score)
            if trend_score is not None:
                sets.append("trend_score = ?"); vals.append(trend_score)
            if unmeasured_days is not None:
                sets.append("unmeasured_days = ?"); vals.append(unmeasured_days)
            if last_scored_date is not None:
                sets.append("last_scored_date = ?"); vals.append(last_scored_date)
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
                "INSERT INTO theme_lines (name, status, strength, daily_score, trend_score, "
                "unmeasured_days, last_scored_date, catalyst, core_stocks, leader_code, notes, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name, status or "watching", strength or 0, daily_score or 0,
                 trend_score, unmeasured_days or 0, last_scored_date, catalyst,
                 json.dumps(core_stocks or [], ensure_ascii=False), leader_code, notes, now, now),
            )
            conn.commit()
            return cur.lastrowid


def clear_theme_score(name: str) -> None:
    """Clear ``trend_score`` back to "unmeasured" (NULL), never to zero.

    ``upsert_theme`` cannot express this — for every other column ``None`` means
    "leave it alone", which is exactly right for a partial update and exactly
    wrong here. A theme the board could not name this cycle has to lose the
    number, because the gate reads this column to decide whether money may go
    in, and yesterday's 0.2 is not evidence about today.
    """
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE theme_lines SET trend_score = NULL, updated_at = ? WHERE name = ?",
            (datetime.now().isoformat(), name),
        )
        conn.commit()


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

def _deadline_for(date: str, horizon_days: int | None) -> str | None:
    """The day a forecast with this declared horizon matures, or None.

    None when no horizon was declared. Returning the date + the global
    default instead would silently convert "the caller did not say" into
    "the caller said five days", and the whole point of storing the
    declaration is that those are different facts.
    """
    if horizon_days is None:
        return None
    try:
        days = int(horizon_days)
    except (TypeError, ValueError):
        return None
    if days <= 0:
        return None
    from datetime import timedelta
    return (datetime.strptime(str(date)[:10], "%Y-%m-%d")
            + timedelta(days=days)).strftime("%Y-%m-%d")


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
    prob: float | None = None,
    trader_id: str = "default",
    horizon_days: int | None = None,
) -> int:
    """Record a stock recommendation.

    ``features`` (Phase 1): optional dict of decision-time features used by the
    Playbook clustering in Phase 3. Serialized to ``features_json`` column.
    Pass None for legacy callers (stored as empty '{}').

    ``trader_id``: whose call this was. The uniqueness key includes it, so
    two traders recommending the same stock on the same day are two
    predictions to be graded separately rather than one overwriting the
    other.

    ``prob`` (G1): P(this beats the market over the scoring horizon). This
    is what makes a prediction gradable with a proper scoring rule —
    a bare 看多/看空 can only be scored on accuracy, which says nothing
    about confidence and needs years of P&L to reach significance. Legacy
    callers pass None and are simply never scored.

    ``horizon_days`` (Phase 3 / T2): how long this forecast gave itself to
    be right. It is a *declaration*, stored with the deadline it implies,
    because "when does this mature" is part of what the forecast asserted
    and a global constant cannot answer it for a per-trader horizon — the
    breakout book trades a 3-day horizon and the pullback book a 5-day
    one, and grading both at 5 measured neither. Callers that do not know
    leave it None, which is honest: the evaluator falls back to the global
    default for those and marks them as un-declared rather than pretending
    a horizon was stated.
    """
    features_json = json.dumps(features or {}, ensure_ascii=False)
    deadline = _deadline_for(date, horizon_days)
    with _write_lock:
        conn = _get_conn()
        # Intraday monitoring re-saves its whole top-5 every cycle, so a
        # single recommendation used to land dozens of times a day. That
        # inflates playbooks.total_trades by an order of magnitude and
        # turns the "hits >= 3" auto-create threshold into "one stock went
        # up once". One row per (date, code, report_type); later cycles
        # refresh it in place.
        existing = conn.execute(
            "SELECT id FROM predictions "
            "WHERE date = ? AND code = ? AND report_type = ? "
            "AND trader_id = ?",
            (date, code, report_type, trader_id),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE predictions SET name = ?, direction = ?, confidence = ?, "
                "theme_line = ?, entry_price = COALESCE(entry_price, ?), "
                "reason = ?, features_json = ?, prob = COALESCE(?, prob), "
                "horizon_days = COALESCE(?, horizon_days), "
                "deadline = COALESCE(?, deadline) "
                "WHERE id = ?",
                (name, direction, confidence, theme_line, entry_price,
                 reason, features_json, prob, horizon_days, deadline,
                 existing["id"]),
            )
            conn.commit()
            return existing["id"]

        # created_at is set on first insert only: it marks when the call
        # was made, and a later cycle refreshing the row must not move it.
        cur = conn.execute(
            "INSERT INTO predictions (date, report_type, code, name, direction, "
            "confidence, theme_line, entry_price, reason, features_json, prob, "
            "created_at, trader_id, horizon_days, deadline) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (date, report_type, code, name, direction, confidence, theme_line,
             entry_price, reason, features_json, prob,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"), trader_id,
             horizon_days, deadline),
        )
        conn.commit()
        return cur.lastrowid


def get_predictions_due_for_scoring(as_of: str, horizon_days: int = 5,
                                    limit: int = 200) -> list[dict]:
    """Probabilistic predictions whose horizon has elapsed but are unscored.

    Maturity is read from the row's **declared** ``deadline`` (Phase 3 /
    T2). ``horizon_days`` is now only the fallback for rows that pre-date
    the declaration and is reported as such through ``legacy_horizon``,
    so a caller can label those honestly instead of recording a
    declaration the forecast never made. Grading every book at one global
    horizon measured neither of them once two traders ran different ones.

    The ``deadline`` here is a **necessary, not sufficient** condition: it is
    a calendar count, and the window it stands for is measured in trading
    days, so the returned rows include forecasts the market has not traded
    far enough past yet. That is deliberate — the filter is an indexed
    pre-filter and the decisive test costs a query per row — but it means
    callers must not treat "returned by this function" as "gradeable".
    ``scoring.evidence_window_closed`` answers the second question, and
    ``outcome_labels.label_forecast`` will not censor a row whose window is
    still open.

    Only rows carrying a ``prob`` can be graded — legacy rows without one
    are skipped rather than back-filled with a guess.
    """
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, date, code, name, prob, horizon_days, deadline, "
        "(deadline IS NULL) AS legacy_horizon FROM predictions "
        "WHERE prob IS NOT NULL AND scored_at IS NULL "
        "AND COALESCE(deadline, date(date, ?)) <= ? ORDER BY date LIMIT ?",
        (f"+{int(horizon_days)} days", as_of, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def save_prediction_score(pred_id: int, score: dict) -> None:
    """Persist a graded prediction.

    ``hit`` is kept in sync with the scored outcome so the legacy hit-rate
    reports agree with the Brier numbers instead of drifting apart.
    """
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE predictions SET brier = ?, log_score = ?, "
            "excess_return = ?, residual_alpha = ?, scored_at = ?, hit = ? "
            "WHERE id = ?",
            (score.get("brier"), score.get("log_score"),
             score.get("excess_return"), score.get("residual_alpha"),
             score.get("scored_at"), 1 if score.get("outcome") else 0,
             pred_id),
        )
        conn.commit()


def get_scored_predictions(days: int = 30, report_type: str | None = None) -> list[dict]:
    """Graded predictions from the last ``days``, newest first."""
    conn = _get_conn()
    q = ["SELECT * FROM predictions WHERE scored_at IS NOT NULL "
         "AND date >= date('now', ?)"]
    params: list = [f"-{days} days"]
    if report_type:
        q.append("AND report_type = ?")
        params.append(report_type)
    q.append("ORDER BY date DESC")
    return [dict(r) for r in conn.execute(" ".join(q), params).fetchall()]


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
        "SELECT id, date, created_at, code, name, direction, confidence, "
        "theme_line, entry_price, reason, next_day_return, hit "
        "FROM predictions WHERE date = ? AND report_type LIKE 'intraday%' "
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
        except sqlite3.IntegrityError as e:
            logger.debug("daily lesson for %s already recorded, ignoring the duplicate: %s", date, e)


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

# ── VPA prior state (v7) ─────────────────────────────────────
# Read by the vpa package to carry the previous session's phase
# into the next analysis. Live mode requires the exact previous
# trading day; backtest mode takes the newest row strictly before
# the as-of date.

def get_prior_state_live(code: str) -> dict | None:
    """v7 spec §2.5: return the most recent VPA analysis row whose date
    equals the previous trading day. Used by live mode.

    Returns None if the most recent row is older than yesterday's trading day
    (treat as cold start).

    v7 review I#82: selected_candidate_id is now read from the persisted
    column (default None for legacy rows) rather than hardcoded to None.
    """
    from alpha_agents.tools.vpa import _prev_trading_day
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    expected = _prev_trading_day(today)
    if not expected:
        return None
    conn = _get_conn()
    row = conn.execute(
        """
        SELECT analysis_date, phase, verdict, signals_json, reason,
               selected_candidate_id
        FROM vpa_analysis_history
        WHERE code = ? AND analysis_date = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (code, expected),
    ).fetchone()
    if not row:
        return None
    return {
        "analysis_date": row["analysis_date"],
        "phase": row["phase"],
        "verdict": row["verdict"],
        "selected_candidate_id": row["selected_candidate_id"],
        "rationale": row["reason"] or "",
    }


def get_prior_state_backtest(code: str, as_of: str) -> dict | None:
    """v7 spec §2.5: return the largest-dated VPA analysis row strictly
    before ``as_of`` for ``code``. Used by backtest harness.

    Returns dict with keys {analysis_date, phase, verdict, selected_candidate_id,
    rationale} or None if no prior row exists.

    v7 review I#82: selected_candidate_id is now read from the persisted
    column (default None for legacy rows) rather than hardcoded to None.
    """
    conn = _get_conn()
    row = conn.execute(
        """
        SELECT analysis_date, phase, verdict, signals_json, reason,
               selected_candidate_id
        FROM vpa_analysis_history
        WHERE code = ? AND analysis_date < ?
        ORDER BY analysis_date DESC
        LIMIT 1
        """,
        (code, as_of[:10]),
    ).fetchone()
    if not row:
        return None
    return {
        "analysis_date": row["analysis_date"],
        "phase": row["phase"],
        "verdict": row["verdict"],
        "selected_candidate_id": row["selected_candidate_id"],
        "rationale": row["reason"] or "",
    }
