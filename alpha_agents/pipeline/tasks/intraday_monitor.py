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
from alpha_agents.tools.sector_ranking import get_sector_ranking_fn, get_concept_ranking_fn
from alpha_agents.tools.anomaly_detect import get_anomaly_stocks_fn
from alpha_agents.tools.market_breadth import get_market_breadth_fn
from alpha_agents.pipeline.theme_manager import evaluate_theme_signals, update_theme_strength, maybe_discover_theme
from alpha_agents.tools.stock_quotes import get_stock_quotes_fn
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


def _refresh_theme_strengths() -> None:
    """Lightweight theme strength update using current sector ranking data.

    Runs every intraday cycle (5 min). No LLM, just data + rules.
    Also discovers new themes from top gainers.
    """
    try:
        ranking = json.loads(get_concept_ranking_fn(top_n=10))
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

        # Try to discover new themes from top gainers
        for gainer in ranking.get("gainers", [])[:5]:
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

    # Capture whole-market spot to all_quote_snapshots — enables per-minute
    # replay fidelity across ALL A-shares regardless of later strategy changes.
    # Runs in a worker thread (~10s Tencent batch) to keep the main loop async;
    # any failure is swallowed and logged at debug, so the rest of the monitor
    # never blocks on this.
    try:
        from alpha_agents.data.snapshot_store import capture_market_snapshot
        n_captured = await asyncio.to_thread(capture_market_snapshot)
        if n_captured:
            logger.info("Captured %d whole-market quotes to snapshot DB", n_captured)
    except Exception as e:
        logger.debug("market snapshot capture failed: %s", e)

    # ── Portfolio monitoring: check pending orders + open positions ──
    today_str = now.strftime("%Y-%m-%d")
    from alpha_agents.data.memory_store import get_active_price_alerts, trigger_price_alert

    pending = get_pending_orders()
    open_pos = get_open_positions()
    price_alerts = get_active_price_alerts()
    all_codes = list({p["code"] for p in pending + open_pos} | {a["code"] for a in price_alerts})

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
                    except Exception as e:
                        logger.warning("Notification failed: %s", e)
                        pass

            # Check open positions — stop loss / take profit / expiry
            if open_pos:
                pos_alerts = check_positions(realtime_prices=price_map, today=today_str)
                # Split by notification importance:
                #   - stop_tightened: bearish pre-alert, log only (avoids spam;
                #     user sees it in the daily review / logs)
                #   - stopped/target_hit/expired/add_position: actual P/L event,
                #     push to notify channels
                for alert in pos_alerts:
                    msg = _format_portfolio_alert(alert)
                    logger.info("Portfolio alert: %s", msg)
                    if alert.get("type") == "stop_tightened":
                        continue  # log only, don't spam push channels
                    try:
                        await asyncio.to_thread(notify_all, "AlphaAgents 持仓提醒", msg)
                    except Exception as e:
                        logger.warning("Notification failed: %s", e)
                        pass

            # Check price alerts
            if price_alerts:
                for alert in price_alerts:
                    code = alert["code"]
                    price = price_map.get(code)
                    if not price:
                        continue
                    triggered = False
                    if alert["condition"] == "above" and price >= alert["target_price"]:
                        triggered = True
                    elif alert["condition"] == "below" and price <= alert["target_price"]:
                        triggered = True
                    if triggered:
                        trigger_price_alert(alert["id"])
                        direction = "涨到" if alert["condition"] == "above" else "跌到"
                        msg = (f"价格提醒 | {code} {alert.get('name', '')} "
                               f"{direction}{price:.2f}元 (目标{alert['target_price']:.2f})")
                        if alert.get("reason"):
                            msg += f" — {alert['reason']}"
                        logger.info("Price alert: %s", msg)
                        try:
                            await asyncio.to_thread(notify_all, "AlphaAgents 价格提醒", msg)
                        except Exception as e:
                            logger.warning("Notification failed: %s", e)

    # ── Lightweight theme strength refresh (uses sector ranking, no LLM) ──
    await asyncio.to_thread(_refresh_theme_strengths)

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

    # ── Sentiment cycle context ──
    sentiment_ctx = ""
    try:
        from alpha_agents.data.sentiment_cycle import get_sentiment_cycle
        cycle = get_sentiment_cycle()
        phase = cycle.get("phase", "?")
        strat = cycle.get("strategy", {})
        sentiment_ctx = (
            f"【市场情绪周期: {phase}】\n"
            f"  买入策略: {strat.get('buy_style', '')}\n"
            f"  卖出策略: {strat.get('sell_style', '')}"
        )
    except Exception:
        pass

    # ── Pre-fetch best stocks for anomaly sectors (code-level, not LLM-dependent) ──
    candidate_sectors = []  # Must be defined before try block
    sector_picks_cache = {}  # Cache results to avoid duplicate API calls
    sector_picks_ctx = ""
    try:
        from alpha_agents.tools.sector_beta import get_sector_best_stocks_fn
        # ── Build candidate sectors from 3 sources (not just themes) ──
        candidate_sectors = []
        seen = set()

        # Source 1: Today's anomaly concepts (real-time, changes every cycle).
        # Parse concept names out of the concept-driven signals emitted by
        # _detect_anomalies. Matches patterns like:
        #   "🔴资金异动(量价确认): <name> 涨..."
        #   "⚠️量价背离: <name> 涨..."
        #   "🔵暗流涌动: <name> 仅涨..."
        #   "🔴资金出逃: <name> 跌..."
        #   "🔵逆势吸筹: <name> 跌..."
        # Style-rotation signals (🧭) are intentionally skipped — those are
        # industry-level, members aren't in concept_stocks mapping.
        import re as _re2
        for m in _re2.finditer(
            r"(?:🔴资金异动.*?|⚠️量价背离|🔵暗流涌动|🔴资金出逃|🔵逆势吸筹)[:：]\s*"
            r"([^\s涨跌仅]+)\s+(?:涨|跌|仅涨)",
            anomaly_context,
        ):
            s = m.group(1).strip()
            if s and s not in seen:
                candidate_sectors.append(s)
                seen.add(s)

        # Source 2: Today's top concept ranking gainers (real-time)
        try:
            ranking = json.loads(get_concept_ranking_fn(top_n=5))
            for gainer in ranking.get("gainers", [])[:3]:
                s = gainer.get("concept", "")
                if s and s not in seen and gainer.get("change_pct", 0) > 1.5:
                    candidate_sectors.append(s)
                    seen.add(s)
        except Exception:
            pass

        # Source 3: Active themes with strength >= 6 (stable but may not change daily)
        for t in themes:
            if t.get("strength", 0) >= 6:
                s = t["name"]
                if s not in seen:
                    candidate_sectors.append(s)
                    seen.add(s)

        pick_lines = []
        for sector in candidate_sectors[:3]:  # Max 3 sectors to avoid slowness
            try:
                result = json.loads(get_sector_best_stocks_fn(sector, top_n=5))
                sector_picks_cache[sector] = result  # Cache for Step 2 reuse
                top = result.get("top", [])
                if top:
                    stocks = ", ".join(
                        f"{s['code']} {s['name']}(score={s['score']}, beta={s['beta_weighted']}, {s['today_change_pct']:+.1f}%)"
                        for s in top if s.get("today_change_pct", 0) < 9.8
                    )
                    if stocks:
                        pick_lines.append(f"  {sector}: {stocks}")
            except Exception as e:
                logger.warning("Sector picks failed for %s: %s", sector, e)
        if pick_lines:
            sector_picks_ctx = "【板块高beta候选股（系统预选，优先从这里选可操作标的）】\n" + "\n".join(pick_lines)
            logger.info("Pre-fetched sector picks for %d sectors", len(pick_lines))
    except Exception as e:
        logger.debug("Sector picks pre-fetch failed: %s", e)

    # ══════════════════════════════════════════════════════════
    # CODE-DRIVEN REPORT: code does selection, LLM only writes cause
    # ══════════════════════════════════════════════════════════

    now_str = datetime.now().strftime("%H:%M")

    # ── Step 1: Code selects signal stocks (涨停确认) ──
    # Show all 连板 (consecutive >= 2) + top first-board stocks for theme confirmation
    signals = []
    total_limit_up = 0
    consecutive_count = 0
    try:
        anomaly_data = json.loads(get_anomaly_stocks_fn())
        all_limit_up = anomaly_data.get("limit_up", [])
        total_limit_up = len(all_limit_up)

        # Split by board count
        consecutive_stocks = [s for s in all_limit_up if s.get("consecutive_limits", 1) >= 2]
        first_board_stocks = [s for s in all_limit_up if s.get("consecutive_limits", 1) < 2]
        consecutive_count = len(consecutive_stocks)

        # Sort 连板 by board count desc (5连板 > 3连板 > 2连板)
        consecutive_stocks.sort(key=lambda x: x.get("consecutive_limits", 1), reverse=True)

        # Take all 连板 + top 15 first-board (already sorted by seal strength from akshare)
        selected = consecutive_stocks + first_board_stocks[:15]

        for stock in selected:
            signals.append({
                "code": stock.get("code", ""),
                "name": stock.get("name", ""),
                "change_pct": stock.get("change_pct", 0),
                "consecutive": stock.get("consecutive_limits", 1),
                # 涨停原因（Kaipanla lu_desc）— 真实主题标签（"算力"/"锂电池"）
                # 若 Tushare 数据缺失回退到 industry 结构标签
                "lu_desc": stock.get("lu_desc", "") or stock.get("industry", ""),
                "industry": stock.get("industry", ""),
                "theme": stock.get("theme", ""),
                "board_status": stock.get("board_status", ""),
            })
    except Exception as e:
        logger.warning("Signal detection failed: %s", e)

    # ── Step 2: Code selects actionable stocks ──
    # Phase A: Collect candidates that match the fund-flow anomaly direction.
    #
    # V2 principle #2: 资金行为优先. Intraday anomaly = sector with fund inflow.
    # Within such a sector, the right picks are stocks THE MONEY IS BUYING —
    # meaning change_pct > 0. Falling stocks (change_pct < 0) contradict the
    # anomaly story: money is selling those, not buying. We reject them
    # regardless of beta/liquidity score, otherwise the system will recommend
    # "best of the worst" when a sector lacks good gainers.
    #
    # Threshold: change_pct > 0.3% to filter out noise (stocks sitting at 0).
    # Upper bound 9.8% excludes limit-up (can't buy).
    raw_candidates = []
    for sector in candidate_sectors[:3]:
        try:
            if sector in sector_picks_cache:
                result = sector_picks_cache[sector]
            else:
                from alpha_agents.tools.sector_beta import get_sector_best_stocks_fn
                result = json.loads(get_sector_best_stocks_fn(sector, top_n=5))

            for s in result.get("top", []):
                chg = s.get("today_change_pct", 0)
                # Must be rising (money buying) but not yet limit-up
                if 0.3 < chg < 9.8:
                    raw_candidates.append((sector, s))
                else:
                    logger.debug("Rejected %s %s: chg=%.2f%% not in (0.3, 9.8)",
                                 s.get("code"), s.get("name"), chg)
        except Exception as e:
            logger.debug("Sector best stocks failed for %s: %s", sector, e)

    # Phase B: build actionable entries from candidates
    actionable = []
    for sector, s in raw_candidates:
        price = s.get("price", 0)
        entry_high = round(price, 2) if price else None
        entry_low = round(price * 0.97, 2) if price else None
        stop_loss_val = round(price * 0.93, 2) if price else None

        actionable.append({
            "code": s["code"],
            "name": s["name"],
            "price": price,
            "change_pct": s.get("today_change_pct", 0),
            "score": s.get("score", 0),
            "beta": s.get("beta_weighted", 0),
            "note": s.get("note", ""),
            "theme": sector,
            "institutional": s.get("institutional", ""),
            "entry_low": entry_low,
            "entry_high": entry_high,
            "stop_loss": stop_loss_val,
        })

    # Phase 3: apply playbook weights (regime-aware — returns None if <2 active)
    try:
        from alpha_agents.evolution import match_playbook
        for a in actionable:
            pb = match_playbook(a)
            if pb:
                a["playbook"] = pb["name"]
                a["playbook_id"] = pb.get("id")
                a["playbook_name"] = pb.get("name", "")
                a["playbook_weight"] = pb.get("weight", 1.0)
                a["score"] = a.get("score", 0) * pb.get("weight", 1.0)
    except Exception as e:
        logger.debug("Playbook matching failed (non-fatal): %s", e)

    # Sort by score, take top 5
    actionable.sort(key=lambda x: x["score"], reverse=True)
    actionable = actionable[:5]

    # ── Step 2b: Ensure actionable stocks' themes exist in theme_lines ──
    # Intraday anomaly may discover new sectors not yet in theme_lines.
    # Without this, pending orders get cancelled 2 min later ("主线不存在").
    if actionable:
        from alpha_agents.data.memory_store import get_theme_by_name, upsert_theme
        for a in actionable:
            theme = a.get("theme", "")
            if theme and not get_theme_by_name(theme):
                upsert_theme(theme, catalyst=f"盘中异动发现 {now_str}")
                logger.info("Auto-created theme '%s' from intraday anomaly", theme)

    # ── Step 3: Code computes theme changes ──
    theme_changes = []
    for t in themes:
        # Strength already updated by _refresh_theme_strengths
        theme_changes.append(f"• {t['name']}: 强度 {t['strength']}/10 ({t['status']})")

    # ── Step 4: LLM only writes cause analysis (50-100 words) ──
    cause_text = ""
    try:
        cause_context = anomaly_context
        if events_context:
            cause_context += "\n" + events_context
        cause_text = await _get_cause_analysis(cause_context)
    except Exception as e:
        logger.warning("LLM cause analysis failed: %s", e)
        cause_text = anomaly_context  # Fallback to raw anomaly data

    # ── Step 5: Code assembles the final report ──
    report_lines = [f"=== 盘中提醒 | {now_str} ===", ""]

    # Cause analysis (from LLM)
    report_lines.append(cause_text)
    report_lines.append("")

    # Signal stocks (code-generated)
    if signals:
        header = "【信号确认】（已涨停，不可买入，仅作主线强度参考）"
        if total_limit_up > 0:
            first_board = total_limit_up - consecutive_count
            header += f" — 今日涨停 {total_limit_up} 家（{consecutive_count} 连板，{first_board} 首板）"
        report_lines.append(header)
        for s in signals:
            board = f"({s['consecutive']}连板)" if s['consecutive'] >= 2 else ""
            # Prefer Kaipanla concept reason (lu_desc) over static industry
            label_parts = []
            if s.get("lu_desc"):
                label_parts.append(f"涨停原因:{s['lu_desc']}")
            elif s.get("industry"):
                label_parts.append(f"行业:{s['industry']}")
            if s.get("theme") and s["theme"] != s.get("lu_desc", ""):
                label_parts.append(f"题材:{s['theme']}")
            label = f" [{' | '.join(label_parts)}]" if label_parts else ""
            report_lines.append(f"• {s['code']} {s['name']} 涨停封板{board}{label}")
        report_lines.append("")

    # Actionable stocks (code-generated, zero hallucination)
    report_lines.append("【可操作标的】")
    report_lines.append("| 代码 | 名称 | 现价 | 涨幅 | 评分 | 操作建议 |")
    report_lines.append("|------|------|------|------|------|---------|")
    if actionable:
        for a in actionable:
            el = a.get("entry_low")
            eh = a.get("entry_high")
            sl = a.get("stop_loss")
            if el and eh:
                action = f"介入{el:.2f}-{eh:.2f}"
            elif eh:
                action = f"回调至{eh:.2f}可介入"
            elif el:
                action = f"突破{el:.2f}跟进"
            else:
                action = ""
            sl_str = f" 止损{sl:.2f}" if sl else ""
            inst = a.get("institutional", "")
            inst_str = f" [{inst}]" if inst and inst != "无" else ""
            pb_name = a.get("playbook", "")
            pb_bit = f" [{pb_name}×{a.get('playbook_weight', 1.0):.1f}]" if pb_name else ""
            report_lines.append(
                f"| {a['code']} | {a['name']} | {a['price']:.2f}元 | "
                f"{a['change_pct']:+.2f}% | {a['score']:.0f} | "
                f"{action}{sl_str}{inst_str}{pb_bit} |"
            )
    else:
        report_lines.append("| — | 暂无符合条件的候选 | — | — | — | — |")
    report_lines.append("")

    # Theme changes (code-generated)
    if theme_changes:
        report_lines.append("【主线状态变化】")
        report_lines.extend(theme_changes)
        report_lines.append("")

    # Sentiment context
    if sentiment_ctx:
        report_lines.append(sentiment_ctx)
        report_lines.append("")

    # Build RECOMMENDATIONS JSON (code-generated)
    recs_json = []
    for s in signals:
        board_note = f"({s['consecutive']}连板)" if s['consecutive'] >= 2 else ""
        # Theme preference: Kaipanla concept reason > industry fallback
        theme = s.get("lu_desc") or s.get("industry", "")
        recs_json.append({
            "code": s["code"], "name": s["name"],
            "theme": theme, "type": "signal",
            "reason": f"涨停封板{board_note}",
        })
    for a in actionable:
        recs_json.append({
            "code": a["code"], "name": a["name"],
            "theme": a.get("theme", ""), "type": "actionable",
            "reason": a.get("note", ""),
            "confidence": "high" if a["score"] >= 70 else "medium",
            "action": "",
            "entry_low": a.get("entry_low"),
            "entry_high": a.get("entry_high"),
            "stop_loss": a.get("stop_loss"),
            # Phase 1: decision features flow through to save_prediction
            "score": a.get("score", 0),
            "change_pct": a.get("change_pct", 0),
            "institutional": a.get("institutional", ""),
            "playbook_id": a.get("playbook_id"),
            "playbook_name": a.get("playbook_name", ""),
            "playbook_matched": bool(a.get("playbook_id")),
        })

    report_lines.append(f"<!--RECOMMENDATIONS\n{json.dumps(recs_json, ensure_ascii=False)}\nRECOMMENDATIONS-->")

    output = "\n".join(report_lines)
    logger.info("Code-driven intraday report generated: %d signals, %d actionable", len(signals), len(actionable))

    # Save recommendations
    _save_intraday_recommendations(output)

    try:
        await asyncio.to_thread(
            notify_all,
            f"AlphaAgents 盘中提醒 | {now_str}",
            output[:500],
        )
    except Exception as e:
        logger.warning("Intraday notification failed: %s", e)

    return output


def _fix_prices_in_report(report: str) -> str:
    """Replace hallucinated prices with real Sina data.

    Also moves stocks that are at limit-up (>=9.8%) from 可操作标的 to 信号确认,
    since you can't buy a stock that's already at the daily limit.
    """
    # Extract all stock codes mentioned in table rows
    codes_in_report = re.findall(r"\|\s*(\d{6})\s*\|", report)
    if not codes_in_report:
        return report

    rt = get_realtime_quotes(list(set(codes_in_report)))
    if not rt:
        return report

    # Collect stocks that are actually at limit-up but listed as actionable
    limit_up_moves = []

    lines = report.split("\n")
    fixed_lines = []
    in_actionable_table = False

    for line in lines:
        # Detect we're in the actionable table
        if "【可操作标的】" in line:
            in_actionable_table = True
            fixed_lines.append(line)
            continue
        if in_actionable_table and line.strip() and not line.strip().startswith("|"):
            in_actionable_table = False

        match = re.match(r"\|\s*(\d{6})\s*\|([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)\|", line)
        if match and in_actionable_table:
            code = match.group(1)
            try:
                name = match.group(2).strip()
                action = match.group(5).strip()

                if code in rt:
                    real = rt[code]
                    price = real.get("price", 0)
                    change_pct = real.get("change_pct", 0)
                    # If at limit-up (>=9.8%), remove from actionable, add to signals
                    if change_pct >= 9.8:
                        limit_up_moves.append(
                            f"• {code} {name} 涨停封板({change_pct:+.2f}%) "
                            f"— 实际已涨停，从可操作移至信号确认"
                        )
                        continue  # Skip this row from actionable table

                    fixed_lines.append(
                        f"| {code} | {name} | {price:.2f}元 | "
                        f"{change_pct:+.2f}% | {action} |"
                    )
                    continue
            except Exception as e:
                logger.warning("Failed to fix price for %s in report: %s", code, e)

        fixed_lines.append(line)

    # Append limit-up stocks to signal section
    if limit_up_moves:
        result = "\n".join(fixed_lines)
        insert_text = "\n".join(limit_up_moves)
        # Try to append after existing signal section
        if "【信号确认】" in result:
            # Find last line of signal section and append
            signal_idx = result.index("【信号确认】")
            # Find next section after signal
            next_section = None
            for marker in ["【可操作标的】", "【主线状态变化】"]:
                pos = result.find(marker, signal_idx + 10)
                if pos > 0:
                    next_section = pos
                    break
            if next_section:
                result = result[:next_section] + insert_text + "\n\n" + result[next_section:]
            else:
                result += "\n" + insert_text
        else:
            # No signal section exists, add one before actionable
            result = result.replace("【可操作标的】",
                                    "【信号确认】（已涨停，系统自动识别）\n" + insert_text + "\n\n【可操作标的】")
        return result

    return "\n".join(fixed_lines)


async def _get_cause_analysis(context: str) -> str:
    """V2 anomaly tracing: observe → trace cause → judge persistence.

    Restored from V2 spec design. LLM has tools to search for catalysts
    and check fund flow sources. Code handles stock selection and pricing.

    Tools given:
      - web_search: find news catalysts (policy, earnings, events)
      - get_lhb_detail: check institutional vs hot money seats
      - get_stock_fund_flow: check main force vs retail flow direction
      - get_sector_data: check related sector linkage
    """
    import asyncio
    from agents import Agent, Runner
    from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
    from openai import AsyncOpenAI
    from alpha_agents.config import AGENT_API_KEY, AGENT_BASE_URL, AGENT_MODEL
    from alpha_agents.tools.registry import (
        web_search, get_lhb_detail, get_stock_fund_flow, get_sector_data,
        get_cls_telegraph, get_news,
    )

    client = AsyncOpenAI(api_key=AGENT_API_KEY, base_url=AGENT_BASE_URL)
    model = OpenAIChatCompletionsModel(model=AGENT_MODEL or "qwen-plus", openai_client=client)
    agent = Agent(
        name="cause_analyst",
        instructions=(
            "你是盘中异动追因分析师。检测到市场异动后，你需要追溯原因并判断持续性。\n\n"
            "## 思维链路（严格按步骤执行）\n\n"
            "1. **观察**：阅读传入的异动数据，识别核心异动（哪个板块、什么类型的资金异动）\n"
            "2. **追因**：\n"
            "   - **优先**调用 get_cls_telegraph（财联社电报）搜索最新快讯，关键词用板块名或龙头股名\n"
            "   - 如果财联社没有，调用 get_news（东方财富新闻）搜索\n"
            "   - 如果国内源都没有，再调用 web_search 搜索（注意：web_search 对中文新闻效果有限）\n"
            "   - 调用 get_lhb_detail 查看龙虎榜，判断资金来源是机构还是游资\n"
            "   - 如果有明确的龙头股，调用 get_stock_fund_flow 查看主力资金方向\n"
            "   - 调用 get_sector_data 查看关联板块是否联动\n"
            "3. **判断**：基于追因结果判断——\n"
            "   - 机构资金 + 明确政策/产业催化 + 多板块联动 → **持续行情**\n"
            "   - 游资席位 + 无明确催化 + 单一个股 → **一日游，不追**\n"
            "   - 资金异动但无新闻催化 → **主力提前布局，密切关注**\n\n"
            "## 输出格式\n\n"
            "对每个异动板块/方向输出：\n"
            "【异动】{板块名} {异动类型}\n"
            "• 催化: {找到的新闻原因，没找到就写'未发现明确催化，可能是资金先行'}\n"
            "• 资金来源: {机构/游资/主力/不明}\n"
            "• 关联板块: {是否有联动}\n"
            "• 判断: {一日游/持续行情/主力提前布局}\n"
            "• 失效条件: {什么情况下判断不成立}\n\n"
            "## 重要原则\n"
            "- 不要推荐股票，不要给操作建议，不要生成表格\n"
            "- 资金行为优先于新闻叙事——如果资金在流入但没有新闻，不要说'没有异动'\n"
            "- 没找到新闻催化不代表没有原因，可能是主力提前知道了什么\n"
            "- 每个工具最多调用一次，不要重复调用"
        ),
        model=model,
        tools=[get_cls_telegraph, get_news, web_search, get_lhb_detail, get_stock_fund_flow, get_sector_data],
    )

    try:
        result = await asyncio.wait_for(
            Runner.run(agent, f"以下是刚检测到的市场异动，请按思维链路追因分析：\n\n{context}",
                       max_turns=30),
            timeout=90,
        )
        output = result.final_output or ""

        # Clean up LLM tool call tags that leak into output (LongCat compatibility)
        import re as _re_clean
        output = _re_clean.sub(r'</?longcat_tool_call>', '', output)
        output = _re_clean.sub(r'</?tool_call>', '', output)
        output = _re_clean.sub(r'\{"name":\s*"[^"]+",\s*"arguments":\s*\{[^}]*\}\}', '', output)
        output = output.strip()

        if not output or len(output) < 20:
            logger.warning("Cause analysis returned empty/garbage, using fallback")
            return context

        return output
    except asyncio.TimeoutError:
        logger.warning("Cause analysis timed out (90s)")
        return context
    except Exception as e:
        logger.warning("Cause analysis failed: %s", e)
        return context


def _auto_fill_actionable(report: str, sectors: list[str]) -> str:
    """If the actionable table is empty, auto-fill it from sector beta picks.

    LLM sometimes only recommends limit-up stocks. This fallback ensures
    the report always has non-limit-up candidates.
    """
    # Check if actionable table is empty (only header/separator, no data rows)
    lines = report.split("\n")
    in_table = False
    has_data_rows = False
    for line in lines:
        if "【可操作标的】" in line:
            in_table = True
            continue
        if in_table and line.strip().startswith("|") and "代码" not in line and "---" not in line:
            # Found a data row
            has_data_rows = True
            break
        if in_table and line.strip() and not line.strip().startswith("|"):
            break  # End of table section

    if has_data_rows:
        return report  # Table already has data, don't override

    # Fetch beta picks and build table rows
    try:
        from alpha_agents.tools.sector_beta import get_sector_best_stocks_fn
        from alpha_agents.data.market_data import get_realtime_quotes

        all_picks = []
        for sector in sectors:
            result = json.loads(get_sector_best_stocks_fn(sector, top_n=3))
            for s in result.get("top", []):
                if s.get("today_change_pct", 0) < 9.8:  # Not limit-up
                    all_picks.append(s)

        if not all_picks:
            return report

        # Build table rows
        table_rows = []
        for s in all_picks[:5]:
            code = s["code"]
            name = s["name"]
            price = s.get("price", 0)
            chg = s.get("today_change_pct", 0)
            beta = s.get("beta_weighted", 0)
            note = s.get("note", "")
            table_rows.append(
                f"| {code} | {name} | {price:.2f}元 | {chg:+.2f}% | "
                f"高beta={beta:.2f}, {note} (系统预选) |"
            )

        if table_rows:
            # Insert after the table header
            new_lines = []
            inserted = False
            for line in lines:
                new_lines.append(line)
                if not inserted and "可操作标的" in line:
                    # Skip to after header/separator
                    pass
                if not inserted and line.strip().startswith("|---"):
                    new_lines.extend(table_rows)
                    inserted = True
            if inserted:
                report = "\n".join(new_lines)
                logger.info("Auto-filled %d actionable stocks from sector beta picks", len(table_rows))
    except Exception as e:
        logger.debug("Auto-fill actionable failed: %s", e)

    return report


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

    # Batch fetch real-time prices (try Sina realtime first, then stock_quotes fallback)
    prices = {}
    try:
        code_list = [r["code"] for r in valid_recs]
        rt = get_realtime_quotes(code_list)
        if rt:
            for code, data in rt.items():
                if data.get("price", 0) > 0:
                    prices[code] = data["price"]
                    prices[code + "_chg"] = data.get("change_pct", 0)

        # Fallback for any missing
        missing = [r["code"] for r in valid_recs if r["code"] not in prices]
        if missing:
            result = json.loads(get_stock_quotes_fn(codes=",".join(missing)))
            for q in result.get("quotes", []):
                if q.get("price", 0) > 0:
                    prices[q["code"]] = q["price"]
                    prices[q["code"] + "_chg"] = q.get("change_pct", 0)
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
            # Phase 1: capture decision-time features for Playbook clustering (Phase 3).
            # Fields match the available_fields list in the evolution spec.
            features = {
                "theme": r.get("theme", ""),
                "score": r.get("score", 0),
                "change_pct": r.get("change_pct", 0),
                "institutional": r.get("institutional", ""),
                "playbook_id": r.get("playbook_id"),
                "playbook_name": r.get("playbook_name", ""),
                "playbook_matched": bool(r.get("playbook_id")),
                "rec_type": rec_type,  # 'signal' vs 'actionable'
            }
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
                features=features,
            )
            saved += 1
            tag = "signal" if rec_type == "signal" else r.get("confidence", "medium")
            logger.info("  Saved intraday %s: %s %s @ %.2f",
                        tag, code, r.get("name", ""), entry_price or 0)
            # Create pending order for actionable recommendations (not signals, not at limit-up)
            price_chg = prices.get(code + "_chg", 0)
            if rec_type != "signal" and price_chg < 9.8:
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
