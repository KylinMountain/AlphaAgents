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


def _vpa_gate_for_candidate(code: str, name: str) -> tuple[str, str]:
    """Run LLM VPA (Anna Coulling) on an actionable candidate.

    Returns (verdict, note) where verdict maps Chinese to English:
      看多/偏多 → bullish, 看空/偏空 → bearish, 中性 → neutral

    Uses LLM full analysis with history context. Saves to DB automatically.
    """
    if not code or not code.strip().isdigit() or len(code.strip()) != 6:
        return ("unknown", "")
    try:
        from alpha_agents.tools.vpa import compute_vpa_with_llm
        r = compute_vpa_with_llm(code.strip(), name=name)
        if not r.get("ok"):
            return ("unknown", "")
        raw_v = r.get("llm_verdict", "中性")
        verdict_map = {"看多": "bullish", "偏多": "bullish",
                       "看空": "bearish", "偏空": "bearish", "中性": "neutral"}
        verdict = verdict_map.get(raw_v, "neutral")
        phase = r.get("llm_phase", "")
        reason = r.get("llm_reason", "")
        note = f"{phase}: {reason}" if phase else reason
        return (verdict, note)
    except Exception as e:
        logger.debug("VPA gate for %s failed: %s", code, e)
        return ("unknown", "")



def _detect_anomalies() -> tuple[bool, str]:
    """Detect anomalies with FUND FLOW FIRST, price second.

    V2 design principle #2: "资金行为优先于新闻叙事。看'谁在买卖'比看'发生了什么'更可靠。"
    Anna Coulling: "成交量是唯一不能被掩盖的真相。"

    Detection hierarchy:
      1. Fund flow anomalies (资金异动) — highest priority
      2. Volume-price divergence (量价背离) — Anna Coulling core
      3. Limit-up concentration (涨停板集中) — market structure signal
      4. Market breadth extremes (情绪极端) — context signal

    Returns (has_anomaly, context_text).
    """
    signals = []

    # ── 1. FUND FLOW: who is buying/selling and how much ──
    try:
        # Industry sectors
        ranking = json.loads(get_sector_ranking_fn(top_n=5))
        top_gainers = ranking.get("gainers", [])
        top_losers = ranking.get("losers", [])

        for sector in top_gainers[:3]:
            chg = sector.get("change_pct", 0)
            flow = sector.get("net_flow_yi", 0)

            # Case A: 涨 + 大资金流入 = 真异动（量价确认）
            if chg > 1.0 and flow > 5:
                signals.append(
                    f"🔴资金异动(量价确认): {sector['sector']} 涨{chg:.1f}% + 净流入{flow:.1f}亿 "
                    f"领涨{sector.get('leader', '')}"
                )
            # Case B: 涨 + 资金流出 = 量价背离（可能出货）
            elif chg > 2.0 and flow < -2:
                signals.append(
                    f"⚠️量价背离: {sector['sector']} 涨{chg:.1f}% 但资金净流出{abs(flow):.1f}亿 "
                    f"— 可能主力借涨出货"
                )
            # Case C: 不怎么涨但资金大幅流入 = 暗中吸筹
            elif chg < 1.0 and flow > 8:
                signals.append(
                    f"🔵暗流涌动: {sector['sector']} 仅涨{chg:.1f}% 但净流入{flow:.1f}亿 "
                    f"— 资金暗中布局"
                )

        for sector in top_losers[:2]:
            chg = sector.get("change_pct", 0)
            flow = sector.get("net_flow_yi", 0)

            # Case D: 跌 + 大资金流出 = 真下杀
            if chg < -1.5 and flow < -5:
                signals.append(
                    f"🔴资金出逃: {sector['sector']} 跌{abs(chg):.1f}% + 净流出{abs(flow):.1f}亿"
                )
            # Case E: 跌 + 资金流入 = 逆势吸筹
            elif chg < -2.0 and flow > 3:
                signals.append(
                    f"🔵逆势吸筹: {sector['sector']} 跌{abs(chg):.1f}% 但净流入{flow:.1f}亿 "
                    f"— 有人在接盘"
                )

        # Concept sectors — check for large fund flows
        try:
            concept_ranking = json.loads(get_concept_ranking_fn(top_n=5))
            for concept in concept_ranking.get("gainers", [])[:3]:
                flow = concept.get("net_flow_yi", 0)
                chg = concept.get("change_pct", 0)
                if flow > 10:
                    signals.append(
                        f"🔴概念资金涌入: {concept['concept']} 净流入{flow:.1f}亿 "
                        f"涨{chg:.1f}% 领涨{concept.get('leader', '')}"
                    )
        except Exception:
            pass

    except Exception as e:
        logger.debug("Sector ranking fetch failed: %s", e)

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
                for alert in pos_alerts:
                    msg = _format_portfolio_alert(alert)
                    logger.info("Portfolio alert: %s", msg)
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

        # Source 1: Today's anomaly sectors (real-time, changes every cycle)
        import re as _re2
        sector_match = _re2.search(r"板块异动:\s*(\S+)\s*涨", anomaly_context)
        if sector_match:
            s = sector_match.group(1)
            if s not in seen:
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
                "sector": stock.get("sector", ""),
            })
    except Exception as e:
        logger.warning("Signal detection failed: %s", e)

    # ── Step 2: Code selects actionable stocks, then parallel VPA filter ──
    # Phase A: Collect all non-limit-up candidates (fast, no LLM)
    raw_candidates = []
    for sector in candidate_sectors[:3]:
        try:
            if sector in sector_picks_cache:
                result = sector_picks_cache[sector]
            else:
                from alpha_agents.tools.sector_beta import get_sector_best_stocks_fn
                result = json.loads(get_sector_best_stocks_fn(sector, top_n=5))

            for s in result.get("top", []):
                if s.get("today_change_pct", 0) < 9.8:  # Not limit-up
                    raw_candidates.append((sector, s))
        except Exception as e:
            logger.debug("Sector best stocks failed for %s: %s", sector, e)

    # Phase B: Parallel VPA gate on all candidates (LLM calls run concurrently)
    actionable = []
    if raw_candidates:
        async def _vpa_check(sector, s):
            try:
                vpa_verdict, vpa_note = await asyncio.to_thread(
                    _vpa_gate_for_candidate, s["code"], s["name"]
                )
            except Exception:
                vpa_verdict, vpa_note = "unknown", ""
            return sector, s, vpa_verdict, vpa_note

        vpa_tasks = [_vpa_check(sector, s) for sector, s in raw_candidates]
        vpa_results = await asyncio.gather(*vpa_tasks, return_exceptions=True)

        for item in vpa_results:
            if isinstance(item, Exception):
                continue
            sector, s, vpa_verdict, vpa_note = item
            if vpa_verdict == "bearish":
                logger.info("Actionable filtered out by VPA: %s %s — %s",
                            s["code"], s["name"], vpa_note)
                continue

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
                "vpa_verdict": vpa_verdict,
                "vpa_note": vpa_note,
                "entry_low": entry_low,
                "entry_high": entry_high,
                "stop_loss": stop_loss_val,
            })

    # Sort by score, take top 5
    actionable.sort(key=lambda x: x["score"], reverse=True)
    actionable = actionable[:5]

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
            sector = f" [{s['sector']}]" if s.get("sector") else ""
            report_lines.append(f"• {s['code']} {s['name']} 涨停封板{board}{sector}")
        report_lines.append("")

    # Actionable stocks (code-generated, zero hallucination)
    report_lines.append("【可操作标的】")
    report_lines.append("| 代码 | 名称 | 现价 | 涨幅 | 评分 | VPA | 操作建议 |")
    report_lines.append("|------|------|------|------|------|-----|---------|")
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
            vpa_verdict = a.get("vpa_verdict", "unknown")
            vpa_note = a.get("vpa_note", "")
            vpa_cell = f"{vpa_verdict}"
            if vpa_note:
                vpa_cell = f"{vpa_verdict}({vpa_note})"
            report_lines.append(
                f"| {a['code']} | {a['name']} | {a['price']:.2f}元 | "
                f"{a['change_pct']:+.2f}% | {a['score']:.0f} | {vpa_cell} | "
                f"{action}{sl_str}{inst_str} |"
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

    # ── LLM VPA deep analysis for top actionable stocks ──
    # Code VPA does fast filtering; LLM VPA adds Anna Coulling narrative for user.
    if actionable:
        try:
            from alpha_agents.tools.vpa import compute_vpa_with_llm
            report_lines.append("【量价深度分析】（Anna Coulling VPA, 仅对可操作标的前 3 只）")
            for a in actionable[:3]:
                try:
                    vpa_r = await asyncio.to_thread(
                        compute_vpa_with_llm, a["code"], a["name"]
                    )
                    if vpa_r.get("ok") and vpa_r.get("llm_report"):
                        verdict = vpa_r.get("llm_verdict", "?")
                        conf = vpa_r.get("llm_confidence", 0)
                        phase = vpa_r.get("llm_phase", "?")
                        confirmed = "已确认" if vpa_r.get("llm_confirmed") else "待确认"
                        report_lines.append(
                            f"\n▶ {a['code']} {a['name']} [{verdict} 信心{conf} {phase} {confirmed}]"
                        )
                        # Truncate report to key sections (skip full table to save space)
                        llm_text = vpa_r["llm_report"]
                        if len(llm_text) > 1500:
                            llm_text = llm_text[:1500] + "\n...(完整报告请用 chat: vpa " + a["code"] + ")"
                        report_lines.append(llm_text)
                except Exception as e:
                    logger.debug("LLM VPA for %s failed: %s", a["code"], e)
            report_lines.append("")
        except Exception as e:
            logger.debug("LLM VPA section failed: %s", e)

    # Build RECOMMENDATIONS JSON (code-generated)
    recs_json = []
    for s in signals:
        board_note = f"({s['consecutive']}连板)" if s['consecutive'] >= 2 else ""
        recs_json.append({
            "code": s["code"], "name": s["name"],
            "theme": s.get("sector", ""), "type": "signal",
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
        })

    report_lines.append(f"<!--RECOMMENDATIONS\n{json.dumps(recs_json, ensure_ascii=False)}\nRECOMMENDATIONS-->")

    output = "\n".join(report_lines)
    logger.info("Code-driven intraday report generated: %d signals, %d actionable", len(signals), len(actionable))

    print(output)

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
            "   - 调用 web_search 搜索相关板块/个股的最新新闻，寻找催化事件（政策、业绩、行业事件）\n"
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
        tools=[web_search, get_lhb_detail, get_stock_fund_flow, get_sector_data],
    )

    try:
        result = await asyncio.wait_for(
            Runner.run(agent, f"以下是刚检测到的市场异动，请按思维链路追因分析：\n\n{context}"),
            timeout=60,  # More time — agent needs to call tools
        )
        return result.final_output
    except asyncio.TimeoutError:
        logger.warning("Cause analysis timed out (60s)")
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
