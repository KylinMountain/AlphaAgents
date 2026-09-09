"""Managing what is already open, while the session runs.

Split out of intraday_monitor when that file crossed the 1200-line
ceiling. The seam is real rather than arithmetic: the other half detects
anomalies, ranks sectors and scores candidates — it looks *outward* at the
market, once per cycle, shared by everyone. This half looks *inward* at
one trader's book: which of its orders filled, which of its theses broke,
whether it did what it said it would do.

They share the quotes and nothing else. That is why the market half runs
once and this one runs per trader.
"""

from __future__ import annotations

import asyncio
import logging

from alpha_agents.data.portfolio import (
    check_pending_orders, check_positions, get_open_positions,
    get_pending_orders,
)
from alpha_agents.notify import notify_all
from alpha_agents.pipeline.tasks import exit_decision, thesis_monitor

logger = logging.getLogger(__name__)


async def manage_book(trader, price_map: dict, today_str: str) -> None:
    """One trader's book: fills, exits, and the discipline check.

    Runs once per trader against the same quotes. The market half of the
    cycle — anomaly detection, sector ranking, theme scoring — happens
    once and is shared, because it is fact rather than strategy;
    duplicating it would multiply cost for nothing. Everything here is
    the part that differs.

    Failures are contained to one trader: a broken config or a wedged
    model call must not stop the other books from being managed.
    """
    pending = get_pending_orders(trader.id)
    open_pos = get_open_positions(trader.id)
    if not pending and not open_pos:
        return

    try:
        if pending:
            fill_alerts = check_pending_orders(realtime_prices=price_map,
                                               today=today_str,
                                               trader_id=trader.id)
            for alert in fill_alerts:
                msg = f"[{trader.name}] " + _format_order_alert(alert)
                logger.info("Order alert: %s", msg)
                try:
                    await asyncio.to_thread(notify_all, "AlphaAgents 订单提醒", msg)
                except Exception as e:
                    logger.warning("Notification failed: %s", e)

        if open_pos:
            # With the trading agent on, the rules run as a floor
            # only: they close what would breach the hard stop and
            # hand everything else to the agent as evidence. Off, they
            # close on every trigger as they always did.
            agent_exits = exit_decision.enabled()
            pos_alerts = check_positions(realtime_prices=price_map,
                                         today=today_str,
                                         hard_only=agent_exits,
                                         trader_id=trader.id)
            if agent_exits:
                # The morning's calls on this book, applied at the
                # first cycle after the open — that is when a real
                # price exists for them.
                morning_calls = exit_decision.pending_morning_calls(
                    today_str, trader.id)
                if morning_calls:
                    pos_alerts += exit_decision.apply(
                        morning_calls, open_pos, price_map)
                # Theses first: they are the agent's own stated plan,
                # evaluated in code, so they cost nothing and they run
                # before the hard floor gets a chance to close a
                # position the agent had already accounted for.
                result = await asyncio.to_thread(
                    thesis_monitor.check_all, price_map,
                    trader_id=trader.id)
                pos_alerts += result["closed"]
                # A position the floor already took, with nothing the
                # agent listed having fired — the blind spots.
                await asyncio.to_thread(thesis_monitor.settle_orphans,
                                        price_map, trader.id)
                # Discipline check, right where it can still be
                # acted on. A condition that is true now and a
                # position still open is a divergence between the
                # plan and the behaviour — worth catching at 10:35
                # rather than reading about it at the review.
                await asyncio.to_thread(_log_broken_promises, price_map,
                                        trader.id)
                # Only what genuinely needs judgement reaches a model.
                pos_alerts += await exit_decision.run(
                    price_map, pos_alerts,
                    narrative_due=result["narrative_due"],
                    trader_id=trader.id)
            # Split by notification importance:
            #   - stop_tightened: bearish pre-alert, log only (avoids spam;
            #     user sees it in the daily review / logs)
            #   - stopped/target_hit/expired/add_position: actual P/L event,
            #     push to notify channels
            for alert in pos_alerts:
                msg = f"[{trader.name}] " + _format_portfolio_alert(alert)
                logger.info("Portfolio alert: %s", msg)
                if alert.get("type") in ("stop_tightened", "signal"):
                    continue  # log only, don't spam push channels
                try:
                    await asyncio.to_thread(notify_all, "AlphaAgents 持仓提醒", msg)
                except Exception as e:
                    logger.warning("Notification failed: %s", e)
                    pass
    except Exception as e:
        logger.exception("交易员 %s 的组合管理失败，其余交易员不受影响: %s",
                         trader.id, e)


def _log_broken_promises(price_map: dict,
                         trader_id: str | None = None) -> None:
    """Say it out loud when the plan and the behaviour disagree.

    thesis_monitor should have closed anything whose condition fired, so
    a hit here means something upstream did not run — a failed close, a
    thesis unbound from its position, a condition the evaluator could not
    read. Silence would let the system look disciplined while it is not.
    """
    try:
        from alpha_agents.evolution.consistency import broken_promises
        for b in broken_promises(price_map, trader_id=trader_id):
            logger.warning("说了没做 [%s]: %s %s — %s（现价 %.2f，浮动 %+.1f%%）",
                           trader_id or "default",
                           b["code"], b["name"], b["condition"],
                           b["price"], b["return_pct"])
    except Exception as e:
        logger.debug("Consistency check unavailable: %s", e)


def _format_order_alert(alert: dict) -> str:
    """Format a pending order alert (filled or cancelled)."""
    code = alert["code"]
    name = alert.get("name", "")
    if alert["type"] == "filled":
        shares = alert.get("shares", 0)
        price = alert.get("fill_price", 0)
        cost = alert.get("cost", 0)
        return f"挂单成交 | {code} {name} {shares}股 @ {price:.2f}元 = {cost:,.0f}元"
    else:
        reason = alert.get("reason", "")
        return f"挂单取消 | {code} {name} — {reason}"


def _format_portfolio_alert(alert: dict) -> str:
    """Format a portfolio alert for notification."""
    code = alert["code"]
    name = alert.get("name", "")
    alert_type = alert.get("type", "")

    if alert_type == "add_position":
        add_shares = alert.get("add_shares", 0)
        add_price = alert.get("add_price", 0)
        new_avg = alert.get("new_avg_price", 0)
        total = alert.get("total_shares", 0)
        return (f"补仓 | {code} {name} +{add_shares}股 @ {add_price:.2f}元 "
                f"(均价{new_avg:.2f}, 共{total}股)")

    if alert_type == "stop_tightened":
        # Bearish signal pre-alert — stop raised, not yet a close
        price = alert.get("price", 0)
        old_stop = alert.get("old_stop", 0)
        new_stop = alert.get("new_stop", 0)
        cur_ret = alert.get("current_return", 0)
        reason = alert.get("reason", "")
        return (f"{reason} | {code} {name} 现价{price:.2f} ({cur_ret:+.1f}%) "
                f"止损 {old_stop:.2f}→{new_stop:.2f}")

    if alert_type == "signal":
        # A rule wanted out and the agent was given the call instead.
        # Logged, never pushed: nothing happened to the position.
        return (f"规则信号(未执行) | {code} {name} "
                f"{alert.get('current_return', 0):+.1f}% — {alert.get('reason', '')}")

    ret = alert.get("return_pct", 0)
    reason = alert.get("reason", "")
    close_price = alert.get("close_price", 0)
    sign = "盈" if ret >= 0 else "亏"
    return f"{reason} | {code} {name} 平仓价{close_price:.2f}元（{sign}{abs(ret):.1f}%）"
