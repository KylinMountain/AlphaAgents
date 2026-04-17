"""L1 Feedback — query-and-format helpers for data that exists but wasn't injected."""

from __future__ import annotations

from alpha_agents.data.sentiment_cycle import get_sentiment_cycle
from alpha_agents.data.memory_store import (
    get_all_cognition_latest,
    get_all_principles_including_weakened,
    get_recent_daily_lessons,
)


def inject_sentiment() -> str:
    """Format today's sentiment cycle phase for agent context.

    Returns empty string if no cycle is saved yet (first-run edge case).
    """
    cycle = get_sentiment_cycle()
    if not cycle:
        return ""
    phase = cycle.get("phase", "")
    if not phase:
        return ""
    # ``strategy`` may be a string (legacy) or a dict with buy_style/sell_style
    # (current schema from sentiment_cycle.py). Normalize to a string.
    raw_strategy = cycle.get("strategy", "")
    if isinstance(raw_strategy, dict):
        strategy = raw_strategy.get("buy_style", "") or raw_strategy.get("sell_style", "")
    else:
        strategy = raw_strategy or ""
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


_PRINCIPLES_BUDGET = 800
_LESSONS_BUDGET = 600

_CATEGORY_HEADERS = {
    "vpa_signal": "VPA信号",
    "theme_timing": "主线择时",
    "entry": "入场",
    "exit": "出场",
    "risk": "风控",
}


def inject_principles() -> str:
    """Render active (and weakened) trading principles as an Anna-Coulling-style
    manual, grouped by category. Budget: 800 chars."""
    rows = get_all_principles_including_weakened()
    if not rows:
        return ""
    by_cat: dict[str, list[dict]] = {}
    for r in rows:
        by_cat.setdefault(r.get("category", "insight"), []).append(r)

    lines = [f"【交易经验手册】（{len(rows)}条）"]
    for cat, items in by_cat.items():
        header = _CATEGORY_HEADERS.get(cat, cat)
        lines.append(f"■ {header}")
        for r in items:
            wr = r.get("win_rate")
            ec = r.get("evidence_count", 0)
            tag = "⚠️" if r.get("status") == "weakened" else ""
            wr_str = f"胜率{wr*100:.0f}%" if wr is not None else ""
            meta = f"（{wr_str}, {ec}例）" if wr_str else f"（{ec}例）"
            line = f"• {tag}{r['principle']}{meta} → {r.get('action_guidance', '')}"
            if sum(len(x) for x in lines) + len(line) > _PRINCIPLES_BUDGET:
                return "\n".join(lines)
            lines.append(line)
    return "\n".join(lines)


def inject_recent_lessons(days: int = 7) -> str:
    """Render last N days of daily_lessons. Budget: 600 chars."""
    rows = get_recent_daily_lessons(days=days)
    if not rows:
        return ""
    lines = ["【近期教训】"]
    for r in rows:
        date = r.get("date", "")[5:]  # "04-17"
        ltype = r.get("lesson_type", "")
        theme = r.get("theme", "") or ""
        content = r.get("content", "")
        theme_bit = f"[{theme}] " if theme else ""
        line = f"• [{date}] {ltype}: {theme_bit}{content}"
        if sum(len(x) for x in lines) + len(line) > _LESSONS_BUDGET:
            break
        lines.append(line)
    return "\n".join(lines)
