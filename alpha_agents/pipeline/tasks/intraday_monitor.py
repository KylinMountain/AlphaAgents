"""Intraday monitoring task — runs every 15 minutes during trading hours.

Pre-fetches sector ranking and anomaly data, then only invokes the
intraday agent if something unusual is detected. This avoids wasting
LLM calls on "nothing happened" cycles.
"""

import json
import logging
from datetime import datetime

from alpha_agents.data.memory_store import get_active_themes
from alpha_agents.config import DATA_DIR
from alpha_agents.tools.sector_ranking import get_sector_ranking_fn
from alpha_agents.tools.anomaly_detect import get_anomaly_stocks_fn
from alpha_agents.tools.market_breadth import get_market_breadth_fn
from alpha_agents.agents.intraday import run_intraday_analysis
from alpha_agents.notify import notify_all

logger = logging.getLogger(__name__)


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
            if best.get("change_pct", 0) > 2.0:
                signals.append(f"板块异动: {best['sector']} 涨{best['change_pct']:.1f}%, 资金净流入{best['net_flow_yi']:.1f}亿, 领涨股{best.get('leader', '')}")
        if top_losers:
            worst = top_losers[0]
            if worst.get("change_pct", 0) < -2.0:
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

    # Build full context for agent
    themes_context = _format_themes_for_monitoring(themes)
    parts = [themes_context, anomaly_context]
    if events_context:
        parts.append(events_context)
    full_context = "\n\n".join(parts)

    output = await run_intraday_analysis(full_context)

    if output and output.strip() != "无异动":
        logger.info("Intraday alert generated!")
        print(output)

        try:
            now = datetime.now().strftime("%H:%M")
            await asyncio.to_thread(
                notify_all,
                f"AlphaAgents 盘中提醒 | {now}",
                output[:500],
            )
        except Exception as e:
            logger.debug("Intraday notification failed: %s", e)

        return output

    logger.info("Intraday monitor: agent found no actionable anomaly")
    return None
