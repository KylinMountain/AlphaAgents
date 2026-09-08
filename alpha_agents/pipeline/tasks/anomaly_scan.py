"""Anomaly detection for the intraday monitor — the 资金 half, and the
news lookup that explains it.

Split out of intraday_monitor.py when that file crossed the 1200-line
lint ceiling. These four functions form one stage and share no state with
the rest of the task: they read market data, decide whether anything is
worth an agent call, and gather the evidence for it. Everything after
them consumes their two return values.

The split matters beyond the line count: this stage is what runs every
five minutes whether or not an agent is ever invoked, so it is the part
worth reading on its own when the monitor is quiet and should not be.
"""

import json
import logging
import re
from datetime import datetime

from alpha_agents.data.memory_store import get_active_themes
from alpha_agents.pipeline.theme_manager import (
    evaluate_theme_signals, maybe_discover_theme, update_theme_strength,
)
from alpha_agents.tools.anomaly_detect import get_anomaly_stocks_fn
from alpha_agents.tools.market_breadth import get_market_breadth_fn
from alpha_agents.tools.sector_ranking import (
    get_concept_ranking_fn, get_sector_ranking_fn,
)

logger = logging.getLogger(__name__)


def _refresh_theme_strengths() -> None:
    """Lightweight theme strength update using current sector ranking data.

    Runs every intraday cycle (5 min). No LLM, just data + rules.
    Also discovers new themes from top gainers.
    """
    try:
        ranking = json.loads(get_concept_ranking_fn(top_n=20))
        all_concepts = ranking.get("gainers", []) + ranking.get("losers", [])
        concept_lookup = {c.get("concept", ""): c for c in all_concepts}

        themes = get_active_themes()
        for theme in themes:
            if theme["name"] in concept_lookup:
                c = concept_lookup[theme["name"]]
                signals = evaluate_theme_signals(
                    sector_name=theme["name"],
                    sector_change_pct=c.get("change_pct", 0),
                    sector_fund_flow=c.get("net_flow_yi", 0) * 1e8,
                    market_change_pct=0,
                )
                update_theme_strength(theme["name"], signals)

        # Top 10, not top 5: the cut sat above 天然气 (8th by inflow) on a
        # day the digest called the energy shock the most important event
        # of the session.
        for gainer in ranking.get("gainers", [])[:10]:
            concept_name = gainer.get("concept", "")
            if not concept_name:
                continue
            signals = evaluate_theme_signals(
                sector_name=concept_name,
                sector_change_pct=gainer.get("change_pct", 0),
                sector_fund_flow=gainer.get("net_flow_yi", 0) * 1e8,
                market_change_pct=0,
            )
            maybe_discover_theme(
                concept_name, signals,
                catalyst=f"盘中发现: 涨{gainer.get('change_pct', 0):.1f}%, 净流入{gainer.get('net_flow_yi', 0):.1f}亿",
            )
    except Exception as e:
        logger.debug("Theme strength refresh failed: %s", e)
def _detect_style_rotation() -> list[str]:
    """Industry-level fund flow → style rotation signals (aux context).

    Industry aggregates ~500 stocks each, so industry signals lag concept
    signals but carry different information: STYLE CHANGE. Example — 银行
    + 证券 today see >20亿 inflow each = defensive rotation starting.

    This is context for LLM, NOT a trade trigger.
    """
    out = []
    try:
        ranking = json.loads(get_sector_ranking_fn(top_n=5))
        # Flag industry-level inflows/outflows above a coarse threshold.
        for ind in ranking.get("gainers", [])[:3]:
            chg = ind.get("change_pct", 0)
            flow = ind.get("net_flow_yi", 0)
            if chg > 1.5 and flow > 15:
                out.append(
                    f"🧭风格切换: {ind['sector']} 行业级流入 {flow:.1f}亿 涨{chg:.1f}% "
                    f"— 可能启动宏观风格轮动"
                )
        for ind in ranking.get("losers", [])[:2]:
            chg = ind.get("change_pct", 0)
            flow = ind.get("net_flow_yi", 0)
            if chg < -1.5 and flow < -15:
                out.append(
                    f"🧭风格退潮: {ind['sector']} 行业级流出 {abs(flow):.1f}亿 "
                    f"跌{abs(chg):.1f}% — 资金离场"
                )
    except Exception as e:
        logger.debug("Style rotation detect failed: %s", e)
    return out
# How far back an intraday attribution looks. A sector moving now is
# explained by this session's flashes, not yesterday's — a wider window
# mostly surfaces the previous day's version of the same story and reads
# as confirmation.
_NEWS_LOOKBACK_HOURS = 6
_NEWS_PER_SECTOR = 3
def _news_for_sectors(sectors: list[str]) -> str:
    """Recent flashes explaining these sectors, as prompt context.

    Returns "" when nothing clears the similarity floor — an attribution
    with no news behind it should say so, not be handed the loosest match
    in the corpus and treat it as a cause.
    """
    if not sectors:
        return ""
    try:
        from alpha_agents.data.news_index import search_news
    except Exception as e:
        logger.debug("News index unavailable: %s", e)
        return ""

    blocks = []
    for sector in sectors:
        try:
            hits = search_news(sector, hours=_NEWS_LOOKBACK_HOURS,
                               top_k=_NEWS_PER_SECTOR)
        except Exception as e:
            logger.warning("News lookup failed for %s: %s", sector, e)
            continue
        if not hits:
            blocks.append(f"  {sector}: 近{_NEWS_LOOKBACK_HOURS}小时无相关快讯"
                          f"（资金异动可能先于新闻，或属于纯资金行为）")
            continue
        lines = [f"  {sector}:"]
        for h in hits:
            stamp = (h.get("time") or "")[11:16]
            lines.append(f"    [{stamp} {h.get('source', '')}] "
                         f"{h.get('title', '')[:70]}")
        blocks.append("\n".join(lines))

    if not blocks:
        return ""
    return ("\n【异动板块的近期快讯】（本地实时快讯库语义检索，"
            f"{_NEWS_LOOKBACK_HOURS}小时内）\n" + "\n".join(blocks))
def _detect_anomalies() -> tuple[bool, str]:
    """Detect anomalies with FUND FLOW FIRST, price second.

    V2 design principle #2: "资金行为优先于新闻叙事。看'谁在买卖'比看'发生了什么'更可靠。"
    Anna Coulling: "成交量是唯一不能被掩盖的真相。"

    Detection is CONCEPT-driven (fine granularity), NOT industry-driven:
      - Concept boards (~30-50 members each) react fast and sharp to
        smart-money moves. Industry boards (~500 members each) average
        out and lag, so they're only useful for style rotation signals.
      - Case A-E (inflow confirmation, divergence, hidden accumulation,
        outflow, counter-trend) all run on concept ranking.
      - Industry ranking is used separately in _detect_style_rotation()
        for macro style-change context.

    Detection hierarchy:
      1. Concept fund flow anomalies (Case A-E) — PRIMARY trade triggers
      2. Industry-level style rotation — macro context (aux)
      3. Limit-up concentration — market structure
      4. Market breadth extremes — sentiment context

    Returns (has_anomaly, context_text).
    """
    signals = []

    # ── 1. CONCEPT FUND FLOW (primary anomaly detection) ──
    # Thresholds calibrated for concept scale (concept ~30-50 members per
    # board, so inflow figures are roughly 1/3 of industry for same signal).
    try:
        ranking = json.loads(get_concept_ranking_fn(top_n=10))
        top_gainers = ranking.get("gainers", [])
        top_losers = ranking.get("losers", [])

        for concept in top_gainers[:5]:
            name = concept.get("concept", "")
            chg = concept.get("change_pct", 0)
            flow = concept.get("net_flow_yi", 0)
            leader = concept.get("leader", "")

            # Case A: 涨 + 大资金流入 = 真异动（量价确认）
            if chg > 1.0 and flow > 3:
                signals.append(
                    f"🔴资金异动(量价确认): {name} 涨{chg:.1f}% + 净流入{flow:.1f}亿 "
                    f"领涨{leader}"
                )
            # Case B: 涨 + 资金流出 = 量价背离（可能出货）
            elif chg > 2.0 and flow < -1:
                signals.append(
                    f"⚠️量价背离: {name} 涨{chg:.1f}% 但资金净流出{abs(flow):.1f}亿 "
                    f"— 可能主力借涨出货"
                )
            # Case C: 不怎么涨但资金大幅流入 = 暗中吸筹
            elif chg < 1.0 and flow > 5:
                signals.append(
                    f"🔵暗流涌动: {name} 仅涨{chg:.1f}% 但净流入{flow:.1f}亿 "
                    f"— 资金暗中布局"
                )

        for concept in top_losers[:3]:
            name = concept.get("concept", "")
            chg = concept.get("change_pct", 0)
            flow = concept.get("net_flow_yi", 0)

            # Case D: 跌 + 大资金流出 = 真下杀
            if chg < -1.5 and flow < -3:
                signals.append(
                    f"🔴资金出逃: {name} 跌{abs(chg):.1f}% + 净流出{abs(flow):.1f}亿"
                )
            # Case E: 跌 + 资金流入 = 逆势吸筹
            elif chg < -2.0 and flow > 2:
                signals.append(
                    f"🔵逆势吸筹: {name} 跌{abs(chg):.1f}% 但净流入{flow:.1f}亿 "
                    f"— 有人在接盘"
                )
    except Exception as e:
        logger.debug("Concept ranking fetch failed: %s", e)

    # ── 1b. INDUSTRY STYLE ROTATION (aux context) ──
    signals.extend(_detect_style_rotation())

    # ── 2. LIMIT-UP concentration (market structure) ──
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

    # ── 3. MARKET BREADTH (context) ──
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
