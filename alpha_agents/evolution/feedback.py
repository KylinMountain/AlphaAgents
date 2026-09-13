"""L1 Feedback — query-and-format helpers for data that exists but wasn't injected.

What reaches the prompt is gated on approval, not on a row's ``status``. See
:func:`in_force_snapshot_id` for the reason the gate reads the policy pointer
rather than the newest approved snapshot.
"""

from __future__ import annotations

import logging
import sqlite3

from alpha_agents.data import policy_registry
from alpha_agents.data import knowledge_snapshots
from alpha_agents.data.sentiment_cycle import get_sentiment_cycle
from alpha_agents.data.memory_store import (
    get_all_cognition_latest,
    get_recent_daily_lessons,
)

logger = logging.getLogger(__name__)


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


def _query_vpa_signals_for_code(
    code: str, days: int = 14, as_of: str | None = None
) -> list[dict]:
    """Return VPA signals for this code within last N days, newest first.

    Pulled out so tests can mock this without a real DB.
    """
    from datetime import datetime, timedelta
    from alpha_agents.data.memory_store import _get_conn
    if as_of:
        try:
            anchor = datetime.strptime(as_of, "%Y-%m-%d")
        except ValueError:
            anchor = datetime.now()
            as_of = None
    else:
        anchor = datetime.now()
    cutoff = (anchor - timedelta(days=days)).strftime("%Y-%m-%d")

    sql = (
        "SELECT signal_type, signal_date, direction, status, resolved_by, resolved_date "
        "FROM vpa_pending_signals "
        "WHERE code = ? AND signal_date >= ? "
    )
    params: list[str] = [code, cutoff]
    if as_of:
        # Point-in-time guard: replay/backtest cannot see same-day/future signals.
        sql += "AND signal_date < ? "
        params.append(as_of)
    sql += "ORDER BY signal_date DESC LIMIT 8"
    rows = _get_conn().execute(sql, tuple(params)).fetchall()
    return [dict(r) for r in rows]


_SIGNAL_STATUS_ICON = {
    "confirmed": "✅已确认",
    "denied": "❌已否定",
    "expired": "⌛已过期",
    "pending": "⏳待确认",
}
_VPA_SIGNAL_BUDGET = 400


def inject_vpa_signal_history(code: str, as_of: str | None = None) -> str:
    """Format this stock's recent VPA signal history for the VPA LLM prompt.

    Closes the feedback loop: signals predicted by prior VPA analyses are
    shown as confirmed/denied based on market outcomes, so the LLM can
    learn from its own track record.
    """
    if not code or not code.isdigit() or len(code) != 6:
        return ""
    signals = _query_vpa_signals_for_code(code, as_of=as_of)
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

# Shown when the gate is off. Not an empty string: "" says "there is no
# knowledge", and "nothing is in force" is a different statement that must not
# wear the same shape. A silent degradation is the one thing this slice is
# required not to have.
_NOT_IN_FORCE = "（未生效：没有任何已批准的知识快照在生效）"


def in_force_snapshot_id() -> int | None:
    """The approved knowledge snapshot the policy in force names, or None.

    Read from the **pointer** rather than from "the newest approved snapshot".
    The second is the tempting implementation and it is the one §14 forbids:
    it would be a second answer to "what is in force", and two answers become
    two truths the moment they disagree. The policy version in force carries a
    ``knowledge`` source naming exactly one snapshot; that is the answer, and
    there is only one of it.
    """
    version = policy_registry.active_version()
    if version is None:
        return None
    sources = policy_registry.sources_of(version["id"]) or {}
    snapshot_id = (sources.get("knowledge") or {}).get("snapshot_id")
    return int(snapshot_id) if snapshot_id else None


def _approved_rows(entity_type: str) -> tuple[list[dict], str | None]:
    """One pointer read, verified payloads, and an explicit fail-closed marker.

    No snapshot, a corrupt snapshot or edited knowledge must not fall back to
    status-based retrieval. Read once so a concurrent promotion cannot cause
    the preliminary check and the actual selection to use different versions.
    """
    try:
        snapshot_id = in_force_snapshot_id()
        if snapshot_id is None:
            raise ValueError("no approved knowledge snapshot is in force")
        return knowledge_snapshots.verified_rows(snapshot_id, entity_type), None
    except (ValueError, TypeError, KeyError, sqlite3.DatabaseError) as exc:
        logger.warning("%s retrieval unavailable: %s", entity_type, exc)
        return [], _NOT_IN_FORCE


def inject_principles() -> str:
    """Render the principles that are in force as an Anna-Coulling-style
    manual, grouped by category. Budget: 800 chars.

    Selection is by **approval**, not by ``status``. §10's "must stay outside
    production decision retrieval until approved" used to hold for candidates
    and not for principle rows: an ``active`` row that had never been inside
    any approved snapshot was rendered anyway, so approval was a record kept
    afterwards rather than a switch thrown beforehand. This is the switch.
    """
    rows, marker = _approved_rows("principle")
    if marker:
        return f"【交易经验手册】{marker}"
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
            tag = "[weakened] " if r.get("status") == "weakened" else ""
            # Counts and win rates are not approved rule fields. They must
            # neither enter the prompt nor change budget-based truncation.
            line = f"• {tag}{r['principle']} → {r.get('action_guidance', '')}"
            if sum(len(x) for x in lines) + len(line) > _PRINCIPLES_BUDGET:
                return "\n".join(lines)
            lines.append(line)
    return "\n".join(lines)


def inject_recent_lessons(days: int = 7, *, purpose: str = "decision") -> str:
    """Render raw lessons only for explicit research use. Budget: 600 chars.

    Raw LLM suggestions are not approved rules. Default/unknown purposes
    return empty without querying storage, including for legacy callers.
    """
    if purpose != "research":
        return ""
    rows = get_recent_daily_lessons(days=days)
    if not rows:
        return ""
    lines = [
        "[Recent lessons — unverified research material]",
        "Raw LLM suggestions, not approved. Do not apply directly as trading rules.",
    ]
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


def inject_portfolio(trader_id: str | None = None, days: int = 5) -> str:
    """What the agent is currently holding, and how its last exits went.

    The morning and intraday agents recommended stocks without ever being
    shown their own book. That is not a cosmetic gap: an agent that cannot
    see its positions cannot avoid recommending what it already owns,
    cannot suggest adding to a thesis that is working, and — the reason
    this matters most — cannot connect "I bought this for X" to "X did not
    happen and I lost 4%". Recent closes are included for exactly that:
    the losses are the part worth reading before picking again.

    ``trader_id`` scopes it to one book. Showing a trader the pooled
    portfolio would be worse than showing it nothing: it would size
    against money it does not have and reason about positions it never
    took.
    """
    from alpha_agents.data.portfolio import (
        get_closed_positions, get_open_positions_summary,
    )

    sections = [f"【我的持仓】\n{get_open_positions_summary(trader_id)}"]

    try:
        closed = get_closed_positions(limit=8, trader_id=trader_id)
    except Exception:
        closed = []
    if closed:
        lines = [f"【最近 {min(len(closed), 8)} 笔平仓】"]
        for c in closed:
            ret = c.get("return_pct") or 0
            lines.append(
                f"• {c.get('code', '')} {c.get('name', '')} "
                f"{ret:+.1f}% 持仓{c.get('holding_days', 0)}天 — "
                f"{c.get('close_reason', '') or '原因未记录'}"
            )
        sections.append("\n".join(lines))

    return "\n\n".join(sections)


_PLAYBOOKS_BUDGET = 400


def inject_playbooks() -> str:
    """Render the playbooks that are in force, as rule fields only.

    Excluding mutable outcome measurements is Phase 1's boundary and still
    holds: ``hit_rate`` / ``wins`` / ``total_trades`` are what the model
    produced, and feeding them back lets it reason from feedback it generated.

    Selection is by **approval**, not by ``status`` — an ``active`` row that
    has never been inside an approved snapshot is not in force, and used to be
    rendered anyway. Same gate as :func:`inject_principles`.
    """
    rows, marker = _approved_rows("playbook")
    if marker:
        return f"【活跃 Playbook】{marker}"
    if not rows:
        return ""
    lines = [f"【活跃 Playbook】（{len(rows)}条）"]
    for pb in rows:
        w = pb.get("weight", 1.0)
        ann = pb.get("annotation", "")
        ann_bit = f" — {ann}" if ann else ""
        line = (f"• {pb['name']} — status={pb['status']} "
                f"weight={w:.1f}{ann_bit}")
        if sum(len(x) for x in lines) + len(line) > _PLAYBOOKS_BUDGET:
            break
        lines.append(line)
    return "\n".join(lines)
