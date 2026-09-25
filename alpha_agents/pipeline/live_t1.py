"""Live pre-open adapter for the shared T+1 trading decision.

Morning research may discover and explain candidates, but it does not own the
trade. The final open/no-open decision is made by agents.t1_decider, the same
structured planner used by walk-forward replay, and submitted through the same
portfolio intent door.

This module deliberately does not fetch a second candidate universe. It turns
the morning research output into the planner's allowed panel, preserving the
research/decision boundary.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from alpha_agents.agents import t1_decider
from alpha_agents.data import clock, market_history as mh, opportunity_journal as OJ
from alpha_agents.data.portfolio_intent import create_pending_order
from alpha_agents.data.trader_session import namespace
from alpha_agents.evolution import feedback

logger = logging.getLogger(__name__)


def _previous_session(day: str) -> str | None:
    cut = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    return mh.get_latest_trading_day_at_or_before(cut)


def _panel(recommendations: list[dict], prev_day: str) -> list[dict]:
    """Freeze morning candidates into the same shape the T+1 planner reads."""
    out: list[dict] = []
    seen: set[str] = set()
    for rec in recommendations:
        code = str(rec.get("code") or "").strip()
        if not code or code in seen:
            continue
        history = mh.get_local_history(code, days=20, as_of=prev_day)
        if not history:
            logger.info("%s omitted from live T1 panel: no 20-session history", code)
            continue
        last = history[-1]
        volumes = [float(row.get("volume") or 0) for row in history]
        adv20 = sum(volumes) / len(volumes) if volumes and min(volumes) >= 0 else None
        close = float(last.get("close") or 0)
        if close <= 0 or not adv20:
            continue
        theme = str(rec.get("theme") or "").strip()
        out.append({
            "code": code,
            "name": str(rec.get("name") or ""),
            "close": close,
            "change_pct": round(float(last.get("change_pct") or 0), 2),
            "adv20": adv20,
            "turnover_rate": round(float(last.get("turnover_rate") or 0), 2),
            "consecutive_limits": None,
            "net_amount": None,
            "concepts": [theme] if theme else [],
            "_theme": theme,
        })
        seen.add(code)
    return out


def _portfolio_context(trader_id: str, prev_day: str) -> str:
    """Mark the live book with the same last-known-close convention as replay."""
    try:
        from alpha_agents.data.portfolio import get_open_positions
        price_map: dict[str, float] = {}
        for pos in get_open_positions(trader_id):
            code = pos.get("code")
            if not code:
                continue
            rows = mh.get_local_history(code, days=1, as_of=prev_day)
            if rows and rows[-1].get("close"):
                price_map[code] = float(rows[-1]["close"])
        return feedback.inject_portfolio(trader_id=trader_id, price_map=price_map)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Live T1 portfolio context unavailable: %s", exc)
        return ""


def _knowledge(exact_approved_knowledge: str) -> str:
    parts = []
    try:
        part = feedback.inject_sentiment()
        if part:
            parts.append(part)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Live T1 sentiment context unavailable: %s", exc)
    if exact_approved_knowledge:
        parts.append(exact_approved_knowledge)
    return "\n\n".join(parts)


def _research_packet(recommendations: list[dict], events_context: str) -> dict:
    """Only material the morning stage actually produced, with no inferred facts."""
    fields = (
        "code", "name", "theme", "reason", "confidence", "dims_passed",
        "entry_low", "entry_high", "stop_loss", "horizon_days",
    )
    return {
        "source": "live_morning_research",
        "events_context": events_context[:6000],
        "candidates": [
            {key: rec.get(key) for key in fields if rec.get(key) is not None}
            for rec in recommendations
        ],
    }


def _write_thesis(order: dict, row: dict, trader) -> int | None:
    from alpha_agents.data import thesis as T

    try:
        conditions = [T.Condition(**item)
                      for item in (order.get("invalidations") or [])]
        return T.create(T.Thesis(
            code=order["code"],
            name=row.get("name") or "",
            theme=row.get("_theme") or "",
            claim=order.get("reason") or "",
            horizon_days=int(order.get("horizon_days")
                             or trader.default_horizon_days),
            prob=float(order.get("prob", 0.5)),
            conviction=float(order.get("conviction", 0.5)),
            size_pct=float(order.get("size_pct") or 0.0),
            conditions=conditions,
            created_by="live_morning:t1_decider",
            trader_id=trader.id,
        ))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Live T1 thesis for %s not written: %s",
                       order.get("code"), exc)
        return None


def _record(day: str, trader, panel: list[dict], verdict: dict,
            research_packet: dict, prev_day: str) -> None:
    """Give production the opportunity journal replay already writes."""
    try:
        from alpha_agents.data import policy_registry
        OJ.record_decision(
            run_id=namespace(),
            trader_id=trader.id,
            day=day,
            phase="open",
            information_cutoff=f"{day} 09:00:00",
            panel=panel,
            orders=verdict.get("orders") or [],
            refusals=verdict.get("refused") or [],
            research={"source": "live_morning_research"},
            parse_error=verdict.get("parse_error"),
            raw=verdict.get("raw") or "",
            context={
                "origin": "live_morning",
                "ranking_day": prev_day,
                "policy_ref": policy_registry.active_ref(),
                "research_packet": research_packet,
                "frame_hash": verdict.get("frame_hash"),
                "decision_id": verdict.get("decision_id"),
                "decision_explanation": {
                    "status": verdict.get("decision_status"),
                    "no_trade_reason": verdict.get("no_trade_reason") or "",
                    "rejected": verdict.get("rejected") or [],
                },
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Live T1 opportunity journal failed: %s", exc)


async def plan_open(
    recommendations: list[dict],
    trader,
    *,
    prediction_ids: dict[str, int] | None = None,
    knowledge_block: str = "",
    events_context: str = "",
) -> dict:
    """Make and submit today's live pre-open plan through the replay planner."""
    day = clock.today()
    prev_day = _previous_session(day)
    if prev_day is None:
        return {"orders": [], "placed": [], "parse_error": "no previous session"}

    panel = _panel(recommendations, prev_day)
    if not panel:
        return {"orders": [], "placed": [], "parse_error": None,
                "decision_status": "abstained", "no_trade_reason": "empty panel"}

    research_packet = _research_packet(recommendations, events_context)
    try:
        from alpha_agents.tools.trader_tools import TRADER_TOOLS
        tools = TRADER_TOOLS
    except Exception as exc:  # noqa: BLE001
        logger.warning("Live T1 tools unavailable; planner runs tool-less: %s", exc)
        tools = []

    verdict = await t1_decider.propose(
        day=day,
        prev_day=prev_day,
        panel=panel,
        news=[],
        book=_portfolio_context(trader.id, prev_day),
        knowledge=_knowledge(knowledge_block),
        trader_note="\n\n".join(
            x for x in (trader.note, trader.extra_prompt) if x),
        picks=2,
        phase="open",
        tools=tools,
        research_packet=research_packet,
        trader_id=trader.id,
        run_id=namespace(),
        origin="live_morning",
    )
    _record(day, trader, panel, verdict, research_packet, prev_day)

    if verdict.get("parse_error"):
        logger.warning("Live T1 decision unreadable for %s: %s",
                       trader.id, verdict["parse_error"])
        verdict["placed"] = []
        return verdict

    by_code = {row["code"]: row for row in panel}
    pred_ids = prediction_ids or {}
    placed = []
    try:
        from alpha_agents.pipeline.tasks import exit_decision
        wake_agent = exit_decision.enabled()
    except Exception:  # noqa: BLE001
        wake_agent = False

    for order in verdict.get("orders") or []:
        row = by_code.get(order.get("code"))
        if row is None:
            continue
        theme = row.get("_theme") or ""
        if not theme:
            logger.info("Live T1 refused %s: morning research supplied no theme",
                        order.get("code"))
            continue
        thesis_id = _write_thesis(order, row, trader)
        order_id = create_pending_order(
            code=order["code"],
            name=row.get("name") or "",
            theme=theme,
            order_date=day,
            entry_low=order.get("entry_low"),
            entry_high=order.get("entry_high"),
            stop_loss=order.get("stop_loss"),
            target_price=order.get("target_price"),
            source="t1_live",
            reason=f"{t1_decider.DECIDER_NAME}: {order.get('reason') or ''}",
            trader_id=trader.id,
            prediction_id=pred_ids.get(order["code"]),
            thesis_id=thesis_id,
            wake_agent=wake_agent,
        )
        if order_id is not None:
            placed.append({
                "code": order["code"], "order_id": order_id,
                "thesis_id": thesis_id, "theme": theme,
                "reason": order.get("reason") or "",
            })

    verdict["placed"] = placed
    return verdict
