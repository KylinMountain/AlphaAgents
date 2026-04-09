"""Intraday monitoring task — runs every 5 minutes during trading hours.

Pre-fetches sector ranking and anomaly data, then only invokes the
intraday agent if something unusual is detected. On anomaly detection,
boosts to 2-minute interval for faster follow-up.
"""

import json
import logging
import re
import time
from datetime import datetime

from alpha_agents.data.memory_store import get_active_themes, save_prediction, get_today_intraday_predictions
from alpha_agents.config import DATA_DIR
from alpha_agents.tools.sector_ranking import get_sector_ranking_fn
from alpha_agents.tools.anomaly_detect import get_anomaly_stocks_fn
from alpha_agents.tools.market_breadth import get_market_breadth_fn
from alpha_agents.tools.stock_quotes import get_stock_quotes_fn
from alpha_agents.agents.intraday import run_intraday_analysis
from alpha_agents.notify import notify_all
from alpha_agents.data.portfolio import (
    create_pending_order, check_pending_orders, check_positions,
    get_open_positions, get_pending_orders,
    parse_entry_zone, parse_stop_loss,
)
from alpha_agents.data.market_data import get_realtime_quotes

logger = logging.getLogger(__name__)

# Scheduler reference for boost — set by main.py before scheduler starts
_scheduler = None

def set_scheduler(scheduler) -> None:
    """Allow main to inject the scheduler so we can boost on anomaly."""
    global _scheduler
    _scheduler = scheduler


def _format_themes_for_monitoring(themes: list[dict]) -> str:
    """Format themes into monitoring context."""
    if not themes:
        return "无活跃主线"
    lines = []
    for t in themes:
        stocks = json.loads(t["core_stocks"]) if t["core_stocks"] else []
        stock_list = ", ".join(f"{s['code']} {s['name']}" for s in stocks[:10])
        lines.append(
            f"主线: {t['name']}（强度 {t['strength']}/10, {t['status']}）\n"
            f"  龙头: {t.get('leader_code', '无')}\n"
            f"  监控标的: {stock_list}"
        )
    return "\n".join(lines)


def _detect_anomalies() -> tuple[bool, str]:
    """Pre-fetch market data and check for anomalies before calling LLM.

    Returns (has_anomaly, context_text).
    """
    signals = []

    # 1. Sector ranking — which sectors are surging/plunging?
    try:
        ranking = json.loads(get_sector_ranking_fn(top_n=5))
        top_gainers = ranking.get("gainers", [])
        top_losers = ranking.get("losers", [])
        if top_gainers:
            best = top_gainers[0]
            if best.get("change_pct", 0) > 1.5:
                signals.append(f"板块异动: {best['sector']} 涨{best['change_pct']:.1f}%, 资金净流入{best['net_flow_yi']:.1f}亿, 领涨股{best.get('leader', '')}")
        if top_losers:
            worst = top_losers[0]
            if worst.get("change_pct", 0) < -1.5:
                signals.append(f"板块下杀: {worst['sector']} 跌{abs(worst['change_pct']):.1f}%")
    except Exception as e:
        logger.debug("Sector ranking fetch failed: %s", e)

    # 2. Anomaly stocks — limit up concentration
    try:
        anomalies = json.loads(get_anomaly_stocks_fn())
        summary = anomalies.get("summary", {})
        limit_up = summary.get("limit_up_count", 0)
        consecutive = summary.get("consecutive_limit_stocks", [])
        top_sector = summary.get("top_sector", "")

        if limit_up > 30:
            signals.append(f"涨停板活跃: {limit_up}家涨停, 集中在{top_sector}")
        if consecutive:
            names = ", ".join(f"{s['name']}({s['consecutive_limits']}板)" for s in consecutive[:5])
            signals.append(f"连板股: {names}")
    except Exception as e:
        logger.debug("Anomaly detection failed: %s", e)

    # 3. Market breadth
    try:
        breadth = json.loads(get_market_breadth_fn())
        ad_ratio = breadth.get("advance_decline_ratio", 1)
        sentiment = breadth.get("sentiment", "")
        if ad_ratio > 5 or ad_ratio < 0.3:
            signals.append(f"市场情绪极端: 涨跌比{ad_ratio}, {sentiment}")
    except Exception as e:
        logger.debug("Market breadth failed: %s", e)

    if not signals:
        return False, ""

    context = "【实时市场数据异动】\n" + "\n".join(f"• {s}" for s in signals)
    return True, context


async def run_intraday_monitor() -> str | None:
    """Execute one intraday monitoring cycle.

    1. Print current themes being watched
    2. Pre-fetch market data to detect anomalies (no LLM cost)
    3. Only call intraday agent if anomaly detected
    4. Push notification on alert

    Returns alert text if anomaly found, None otherwise.
    """
    import asyncio

    # Skip during lunch break (11:30-13:00) — market is closed, data is stale
    now = datetime.now()
    hm = now.hour * 100 + now.minute
    if 1130 <= hm < 1300:
        logger.info("Intraday monitor: lunch break, skipping")
        return None

    # ── Portfolio monitoring: check pending orders + open positions ──
    today_str = now.strftime("%Y-%m-%d")
    pending = get_pending_orders()
    open_pos = get_open_positions()
    all_codes = list({p["code"] for p in pending + open_pos})

    if all_codes:
        rt_prices = await asyncio.to_thread(get_realtime_quotes, all_codes)
        if rt_prices:
            price_map = {code: data["price"] for code, data in rt_prices.items()}

            # Check pending orders — fill if price hits entry zone
            if pending:
                fill_alerts = check_pending_orders(realtime_prices=price_map, today=today_str)
                for alert in fill_alerts:
                    msg = _format_order_alert(alert)
                    logger.info("Order alert: %s", msg)
                    try:
                        await asyncio.to_thread(notify_all, "AlphaAgents 订单提醒", msg)
                    except Exception:
                        pass

            # Check open positions — stop loss / take profit / expiry
            if open_pos:
                pos_alerts = check_positions(realtime_prices=price_map, today=today_str)
                for alert in pos_alerts:
                    msg = _format_portfolio_alert(alert)
                    logger.info("Portfolio alert: %s", msg)
                    try:
                        await asyncio.to_thread(notify_all, "AlphaAgents 持仓提醒", msg)
                    except Exception:
                        pass

    themes = get_active_themes()
    if not themes:
        logger.info("Intraday monitor: no active themes to watch")
        return None

    # Print what we're watching
    for t in themes:
        stocks = json.loads(t["core_stocks"]) if t["core_stocks"] else []
        leader = next((s["name"] for s in stocks if s.get("role") == "龙头"), "无")
        logger.info("  主线: %s（强度 %d/10, %s）龙头: %s",
                     t["name"], t["strength"], t["status"], leader)

    # Pre-fetch data to check for anomalies (cheap, no LLM)
    has_anomaly, anomaly_context = await asyncio.to_thread(_detect_anomalies)

    if not has_anomaly:
        logger.info("Intraday monitor: no anomaly detected (checked sectors + limit-up + breadth)")
        return None

    logger.info("Intraday monitor: anomaly detected, calling agent for analysis...")
    logger.info(anomaly_context)

    # Boost to 2-min interval for faster follow-up
    if _scheduler:
        _scheduler.boost_task("intraday_monitor", minutes=15)

    # Read today's events from morning scan (if available)
    events_context = ""
    try:
        import time as _time
        cache_path = DATA_DIR / "today_events.json"
        if cache_path.exists():
            with open(cache_path, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if cached.get("date") == _time.strftime("%Y-%m-%d"):
                event_lines = []
                for e in cached.get("events", [])[:5]:
                    event_lines.append(f"  [{e.get('category', '?')}] {e.get('event', '?')} (重要性{e.get('importance', 0)}/5)")
                if event_lines:
                    events_context = "【今日已知事件（晨扫识别）】\n" + "\n".join(event_lines)
                    logger.info("Intraday: loaded %d events from morning scan", len(event_lines))
    except Exception:
        pass

    # Build context for prior intraday recommendations (continuity)
    prior_context = ""
    try:
        prior_recs = get_today_intraday_predictions()
        if prior_recs:
            lines = []
            for r in prior_recs:
                price_str = f" @ {r['entry_price']:.2f}元" if r.get("entry_price") else ""
                lines.append(f"  {r['code']} {r['name']}{price_str} ({r['confidence']}) — {r.get('reason', '')}")
            prior_context = "【今日已推荐标的（保持一致性，如需改变判断请明确说明原因）】\n" + "\n".join(lines)
    except Exception:
        pass

    # Build full context for agent
    themes_context = _format_themes_for_monitoring(themes)
    parts = [themes_context, anomaly_context]
    if prior_context:
        parts.append(prior_context)
    if events_context:
        parts.append(events_context)
    full_context = "\n\n".join(parts)

    output = await run_intraday_analysis(full_context)

    if output and output.strip() != "无异动":
        logger.info("Intraday alert generated!")
        print(output)

        # Save intraday recommendations with real-time prices
        _save_intraday_recommendations(output)

        try:
            now_str = datetime.now().strftime("%H:%M")
            await asyncio.to_thread(
                notify_all,
                f"AlphaAgents 盘中提醒 | {now_str}",
                output[:500],
            )
        except Exception as e:
            logger.debug("Intraday notification failed: %s", e)

        return output

    logger.info("Intraday monitor: agent found no actionable anomaly")
    return None


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
    ret = alert.get("return_pct", 0)
    reason = alert.get("reason", "")
    close_price = alert.get("close_price", 0)
    sign = "盈" if ret >= 0 else "亏"
    return f"{reason} | {code} {name} 平仓价{close_price:.2f}元（{sign}{abs(ret):.1f}%）"


def _save_intraday_recommendations(report: str) -> None:
    """Extract recommendations from intraday report and save with real-time prices."""
    match = re.search(r"<!--RECOMMENDATIONS\s*(.*?)\s*RECOMMENDATIONS-->", report, re.DOTALL)
    if not match:
        return
    try:
        from json_repair import repair_json
        recs = repair_json(match.group(1), return_objects=True)
        if not isinstance(recs, list):
            return
    except Exception:
        return

    today = time.strftime("%Y-%m-%d")
    valid_recs = [r for r in recs if re.match(r"^\d{6}$", r.get("code", ""))]
    if not valid_recs:
        return

    # Batch fetch real-time prices
    prices = {}
    try:
        codes = ",".join(r["code"] for r in valid_recs)
        result = json.loads(get_stock_quotes_fn(codes=codes))
        for q in result.get("quotes", []):
            prices[q["code"]] = q.get("price") or q.get("latest_close")
    except Exception as e:
        logger.debug("Failed to fetch prices for intraday recs: %s", e)

    saved = 0
    for r in valid_recs:
        code = r["code"]
        entry_price = prices.get(code)
        rec_type = r.get("type", "actionable")
        # signal = 涨停确认股, actionable = 可操作标的
        report_type = "intraday_signal" if rec_type == "signal" else "intraday"
        confidence = r.get("confidence", "medium") if rec_type != "signal" else "signal"
        try:
            save_prediction(
                date=today,
                report_type=report_type,
                code=code,
                name=r.get("name", ""),
                direction="bullish",
                confidence=confidence,
                theme_line=r.get("theme", ""),
                entry_price=entry_price,
                reason=r.get("reason", "")[:100],
            )
            saved += 1
            tag = "signal" if rec_type == "signal" else r.get("confidence", "medium")
            logger.info("  Saved intraday %s: %s %s @ %.2f",
                        tag, code, r.get("name", ""), entry_price or 0)
            # Create pending order for actionable recommendations (not signals)
            if rec_type != "signal":
                try:
                    # Prefer structured JSON fields, fallback to regex
                    entry_low = r.get("entry_low")
                    entry_high = r.get("entry_high")
                    stop_loss_val = r.get("stop_loss")
                    if entry_low is None and entry_high is None:
                        entry_low, entry_high = parse_entry_zone(r.get("action", ""))
                    if stop_loss_val is None:
                        stop_loss_val = parse_stop_loss(r.get("action", ""))
                    create_pending_order(
                        code=code,
                        name=r.get("name", ""),
                        theme=r.get("theme", ""),
                        order_date=today,
                        entry_low=entry_low,
                        entry_high=entry_high,
                        stop_loss=stop_loss_val,
                        source="intraday",
                        reason=r.get("reason", "")[:100],
                    )
                except Exception as e:
                    logger.debug("Failed to create pending order for %s: %s", code, e)
        except Exception as e:
            logger.debug("Failed to save intraday prediction for %s: %s", code, e)

    if saved:
        logger.info("Saved %d intraday predictions (signal + actionable)", saved)
