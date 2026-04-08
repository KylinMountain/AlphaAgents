"""Morning scan task — runs at 06:30 before market open.

Fetches overnight news, reads memory context, and runs the morning
analyst agent to produce a daily briefing.
"""

import json
import logging
import time

import re

from alpha_agents.data.memory_store import (
    get_active_themes, get_prediction_stats, get_all_cognition_latest,
    save_prediction,
)
from alpha_agents.pipeline.digest import digest_news
from alpha_agents.pipeline.monitor import NEWS_SOURCES
from alpha_agents.pipeline.theme_manager import evaluate_theme_signals, maybe_discover_theme
from alpha_agents.tools.sector_ranking import get_concept_ranking_fn
from alpha_agents.tools.stock_search import search_stocks_fn
from alpha_agents.tools.stock_filter import filter_stocks_fn
from alpha_agents.tools.global_market import get_global_overview_fn
from alpha_agents.data.memory_store import upsert_theme
from alpha_agents.agents.morning import run_morning_analysis
from alpha_agents.notify import notify_all

logger = logging.getLogger(__name__)


def _fill_theme_stocks(theme_name: str, leader_name: str = "") -> None:
    """Fill core stocks for a newly discovered theme.

    Uses the concept ranking's leader as the confirmed leader,
    then searches for related stocks via semantic + keyword matching.
    """
    try:
        # Search for stocks related to this concept
        result = json.loads(search_stocks_fn(keyword=theme_name))
        all_codes = []
        for concept in result.get("matches", []):
            for stock in concept.get("stocks", [])[:5]:
                all_codes.append({"code": stock["code"], "name": stock["name"]})

        # If search found nothing, try to at least record the leader
        if not all_codes and leader_name:
            # Search by leader name
            leader_result = json.loads(search_stocks_fn(keyword=leader_name))
            for concept in leader_result.get("matches", []):
                for stock in concept.get("stocks", [])[:3]:
                    all_codes.append({"code": stock["code"], "name": stock["name"]})

        if not all_codes:
            logger.debug("No stocks found for theme '%s'", theme_name)
            return

        # Deduplicate
        seen = set()
        unique = []
        for s in all_codes:
            if s["code"] not in seen:
                seen.add(s["code"])
                unique.append(s)

        # Filter out ST/suspended
        codes = [s["code"] for s in unique[:20]]
        filtered = json.loads(filter_stocks_fn(stock_codes=codes))
        kept = filtered.get("stocks", [])

        core_stocks = []
        for i, s in enumerate(kept[:10]):
            core_stocks.append({
                "code": s["code"],
                "name": s["name"],
                "role": "龙头" if i == 0 else "核心",
            })

        leader_code = core_stocks[0]["code"] if core_stocks else None
        upsert_theme(theme_name, core_stocks=core_stocks, leader_code=leader_code)
        logger.info("  Filled %d stocks for '%s', leader=%s",
                     len(core_stocks), theme_name,
                     core_stocks[0]["name"] if core_stocks else "无")
    except Exception as e:
        logger.debug("Failed to fill stocks for '%s': %s", theme_name, e)


def _format_themes(themes: list[dict]) -> str:
    """Format active themes into a readable context string."""
    if not themes:
        return "无活跃主线"
    lines = []
    for t in themes:
        stocks = json.loads(t["core_stocks"]) if t["core_stocks"] else []
        leader = next((s["name"] for s in stocks if s.get("role") == "龙头"), "无")
        stock_names = ", ".join(s["name"] for s in stocks[:5])
        lines.append(
            f"- {t['name']}（强度 {t['strength']}/10, {t['status']}）\n"
            f"  龙头: {leader} | 标的: {stock_names}\n"
            f"  催化: {t.get('catalyst', '无')}"
        )
    return "\n".join(lines)


def _format_stats(stats: dict) -> str:
    """Format prediction stats into readable text."""
    total = stats.get("total", 0)
    if total == 0:
        return "暂无预测记录"
    hit_rate = stats.get("hit_rate", 0)
    hits = stats.get("hits", 0)
    by_conf = stats.get("by_confidence", {})
    lines = [f"近7天命中率: {hit_rate:.1f}% ({hits}/{total})"]
    for conf, data in by_conf.items():
        lines.append(f"  {conf}信心: {data['hit_rate']:.1f}% ({data['hits']}/{data['total']})")
    return "\n".join(lines)


def _format_events(events: list[dict]) -> str:
    """Format digested events into readable text."""
    if not events:
        return "无重要事件"
    lines = []
    for e in events:
        lines.append(
            f"- [{e.get('category', '?')}] {e.get('event', '?')} "
            f"(重要性 {e.get('importance', 0)}/5, {e.get('credibility', '?')})\n"
            f"  摘要: {e.get('summary', '')[:100]}"
        )
    return "\n".join(lines)


async def run_morning_scan() -> str | None:
    """Execute the morning scan task.

    1. Fetch overnight news from all sources
    2. Read active theme lines, stats, cognition from memory
    3. Digest news into events
    4. Run morning analyst agent with full context
    5. Push notification

    Returns the morning report text, or None if nothing significant.
    """
    import asyncio

    logger.info("Morning scan starting...")

    # 1. Fetch news from all sources
    news_items = []
    for source_id, name, fetch_fn_factory in NEWS_SOURCES:
        try:
            raw = await asyncio.to_thread(fetch_fn_factory)
            data = json.loads(raw)
            items = data.get("news", [])
            news_items.extend(items)
        except Exception as e:
            logger.debug("Morning scan: %s unavailable: %s", name, e)

    if not news_items:
        logger.info("Morning scan: no news items")
        return None

    # 2. Auto-discover themes from current sector data
    try:
        ranking = json.loads(await asyncio.to_thread(get_concept_ranking_fn, 10))
        for gainer in ranking.get("gainers", [])[:8]:
            concept_name = gainer.get("concept", "")
            if not concept_name:
                continue
            signals = evaluate_theme_signals(
                sector_name=concept_name,
                sector_change_pct=gainer.get("change_pct", 0),
                sector_fund_flow=gainer.get("net_flow_yi", 0) * 1e8,
                market_change_pct=0,
            )
            leader = gainer.get("leader", "")
            if maybe_discover_theme(
                concept_name, signals,
                catalyst=f"概念涨{gainer.get('change_pct', 0):.1f}%, 净流入{gainer.get('net_flow_yi', 0):.1f}亿, 领涨{leader}",
            ):
                logger.info("Morning scan: discovered theme '%s', searching for stocks...", concept_name)
                _fill_theme_stocks(concept_name, leader_name=leader)
    except Exception as e:
        logger.debug("Morning scan theme discovery failed: %s", e)

    # 3. Read memory
    themes = get_active_themes()
    stats = get_prediction_stats(days=7)
    cognition = get_all_cognition_latest()

    logger.info("Morning scan: %d news items, %d active themes", len(news_items), len(themes))

    # 3. Digest news
    events = await digest_news(news_items)
    if not events:
        logger.info("Morning scan: no significant events")
        return None

    # 4. Pre-fetch global market data
    global_ctx = ""
    try:
        overview = json.loads(await asyncio.to_thread(get_global_overview_fn))
        lines = []
        for idx in overview.get("us_indices", []):
            lines.append(f"  {idx['name']}: {idx['close']} ({idx['change_pct']:+.2f}%)")
        bonds = overview.get("bond_yields", {})
        if bonds.get("us_10y"):
            lines.append(f"  美债10Y: {bonds['us_10y']}%")
        if bonds.get("cn_us_spread"):
            lines.append(f"  中美利差: {bonds['cn_us_spread']}%")
        for sig in overview.get("signals", []):
            lines.append(f"  信号: {sig}")
        global_ctx = "【全球市场】\n" + "\n".join(lines) if lines else ""
    except Exception as e:
        logger.debug("Morning scan: global overview failed: %s", e)

    # 5. Run morning agent with context
    themes_ctx = _format_themes(themes)
    stats_ctx = _format_stats(stats)
    events_ctx = _format_events(events)
    if global_ctx:
        events_ctx = global_ctx + "\n\n" + events_ctx

    report = await run_morning_analysis(events_ctx, themes_ctx, stats_ctx)

    # 5. Push notification
    if report and not report.startswith("["):
        try:
            await asyncio.to_thread(
                notify_all,
                f"AlphaAgents 晨报 | {time.strftime('%m-%d')}",
                report[:500],
            )
        except Exception as e:
            logger.debug("Morning notification failed: %s", e)

    # 6. Extract recommendations and save as predictions
    if report and not report.startswith("["):
        _save_recommendations(report)

    print(report)
    return report


def _save_recommendations(report: str) -> None:
    """Extract recommendations from report and save as predictions.

    Primary: parse <!--RECOMMENDATIONS ... RECOMMENDATIONS--> JSON block.
    Fallback: regex parse the markdown table.
    """
    today = time.strftime("%Y-%m-%d")
    recs = _extract_json_recommendations(report)
    if not recs:
        recs = _extract_table_recommendations(report)
    if not recs:
        return

    saved = 0
    for r in recs:
        code = r.get("code", "")
        if not re.match(r"^\d{6}$", code):
            continue
        try:
            save_prediction(
                date=today,
                report_type="morning",
                code=code,
                name=r.get("name", ""),
                direction="bullish",
                confidence=r.get("confidence", "medium"),
                theme_line=r.get("theme", ""),
                entry_price=None,
                reason=r.get("reason", "")[:100],
            )
            saved += 1
            logger.info("  Saved prediction: %s %s (%s)", code, r.get("name", ""), r.get("confidence", ""))
        except Exception as e:
            logger.debug("Failed to save prediction for %s: %s", code, e)

    if saved:
        logger.info("Saved %d predictions from morning report", saved)


def _extract_json_recommendations(report: str) -> list[dict]:
    """Extract structured JSON from <!--RECOMMENDATIONS ... RECOMMENDATIONS--> block."""
    match = re.search(r"<!--RECOMMENDATIONS\s*(.*?)\s*RECOMMENDATIONS-->", report, re.DOTALL)
    if not match:
        return []
    try:
        from json_repair import repair_json
        data = repair_json(match.group(1), return_objects=True)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def _extract_table_recommendations(report: str) -> list[dict]:
    """Fallback: parse the markdown table for recommendations."""
    results = []
    lines = report.split("\n")
    in_table = False

    for line in lines:
        line = line.strip()
        if "推荐关注" in line:
            in_table = True
            continue
        if in_table and line and not line.startswith("|"):
            in_table = False
            continue
        if not in_table or not line.startswith("|"):
            continue
        if "代码" in line or "---" in line:
            continue

        cells = [c.strip() for c in line.split("|") if c.strip()]
        if len(cells) < 5:
            continue

        code = cells[0]
        if not re.match(r"^\d{6}$", code):
            continue

        conf_raw = cells[4]
        confidence = "high" if "高" in conf_raw else "medium" if "中" in conf_raw else "low"

        results.append({
            "code": code,
            "name": cells[1],
            "theme": cells[2],
            "reason": cells[3],
            "confidence": confidence,
        })

    return results
