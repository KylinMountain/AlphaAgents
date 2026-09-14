"""Caches of what other systems produced: VPA analyses, signals, financials.

Moved out of ``memory_store.py`` as one subject. What these tables have in common
is that they are **not the trader's own memory** — they are what
``tools/vpa`` and ``tools/financial_data.py`` wrote down so they need not re-ask
an LLM or a data vendor. That is a different responsibility from theme lines,
predictions and lessons, which are the trader's record of its own decisions.

The connection still belongs to ``memory_store``: importing it from here is
downward, and ``memory_store`` does not import this module back, so there is no
cycle. Callers import from here now.
"""

import json
import logging
from datetime import datetime

from alpha_agents.data.memory_store import _get_conn, _write_lock

logger = logging.getLogger(__name__)


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
            "VALUES (?, ?, ?, datetime('now','localtime'))",
            (code, json.dumps(data, ensure_ascii=False), report_date),
        )
        conn.commit()

