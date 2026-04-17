"""L1 Feedback — query-and-format helpers for data that exists but wasn't injected."""

from __future__ import annotations

from alpha_agents.data.sentiment_cycle import get_sentiment_cycle
from alpha_agents.data.memory_store import get_all_cognition_latest


def inject_sentiment() -> str:
    """Format today's sentiment cycle phase for agent context.

    Returns empty string if no cycle is saved yet (first-run edge case).
    """
    cycle = get_sentiment_cycle()
    if not cycle:
        return ""
    phase = cycle.get("phase", "")
    strategy = cycle.get("strategy", "")
    if not phase:
        return ""
    # Truncate strategy to stay within ~50 char budget
    if len(strategy) > 40:
        strategy = strategy[:40].rstrip() + "…"
    return f"【情绪周期】{phase}" + (f" — {strategy}" if strategy else "")


_POSITION_MAP = {"high": "高位", "mid": "中位", "low": "低位"}
_TREND_MAP = {"inflow": "资金流入", "outflow": "资金流出", "neutral": "资金中性"}
_COGNITION_BUDGET = 300


def inject_cognition() -> str:
    """Format latest market cognition (per sector) for agent context.

    Reads the `market_cognition` table via get_all_cognition_latest. The
    review task writes one row per active theme every day; we surface the
    latest. Truncates to stay within budget, dropping lowest-priority
    (alphabetical last) sectors first.
    """
    rows = get_all_cognition_latest()
    if not rows:
        return ""
    lines = ["【市场认知】"]
    for r in rows:
        sector = r.get("sector", "")
        pos = _POSITION_MAP.get(r.get("position", ""), r.get("position", ""))
        trend = _TREND_MAP.get(r.get("fund_trend", ""), r.get("fund_trend", ""))
        assessment = r.get("assessment", "")
        line = f"• {sector}: {pos} + {trend} — \"{assessment}\""
        if sum(len(x) for x in lines) + len(line) > _COGNITION_BUDGET:
            break
        lines.append(line)
    return "\n".join(lines)


def _query_vpa_signals_for_code(code: str, days: int = 14) -> list[dict]:
    """Return VPA signals for this code within last N days, newest first.

    Pulled out so tests can mock this without a real DB.
    """
    from datetime import datetime, timedelta
    from alpha_agents.data.memory_store import _get_conn
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = _get_conn().execute(
        "SELECT signal_type, signal_date, direction, status, resolved_by, resolved_date "
        "FROM vpa_pending_signals "
        "WHERE code = ? AND signal_date >= ? "
        "ORDER BY signal_date DESC LIMIT 8",
        (code, cutoff),
    ).fetchall()
    return [dict(r) for r in rows]


_SIGNAL_STATUS_ICON = {
    "confirmed": "✅已确认",
    "denied": "❌已否定",
    "expired": "⌛已过期",
    "pending": "⏳待确认",
}
_VPA_SIGNAL_BUDGET = 400


def inject_vpa_signal_history(code: str) -> str:
    """Format this stock's recent VPA signal history for the VPA LLM prompt.

    Closes the feedback loop: signals predicted by prior VPA analyses are
    shown as confirmed/denied based on market outcomes, so the LLM can
    learn from its own track record.
    """
    if not code or not code.isdigit() or len(code) != 6:
        return ""
    signals = _query_vpa_signals_for_code(code)
    if not signals:
        return ""
    lines = ["【该股VPA信号历史】"]
    for s in signals:
        icon = _SIGNAL_STATUS_ICON.get(s.get("status", "pending"), "⏳待确认")
        date = s.get("signal_date", "")[5:]  # "2026-04-14" → "04-14"
        stype = s.get("signal_type", "")
        direction = s.get("direction", "")
        resolved = s.get("resolved_by", "")
        tail = f"（{resolved}）" if resolved else ""
        line = f"• {date} {stype}({direction}) → {icon}{tail}"
        if sum(len(x) for x in lines) + len(line) > _VPA_SIGNAL_BUDGET:
            break
        lines.append(line)
    return "\n".join(lines)
