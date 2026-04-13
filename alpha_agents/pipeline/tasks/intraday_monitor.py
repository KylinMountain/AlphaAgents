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
    sector_picks_ctx = ""
    try:
        from alpha_agents.tools.sector_beta import get_sector_best_stocks_fn
        # Extract sector names from anomaly context and active themes
        candidate_sectors = []
        for t in themes:
            if t.get("strength", 0) >= 6:
                candidate_sectors.append(t["name"])
        # Also try to extract sector from anomaly text (e.g. "板块异动: 电池 涨3.5%")
        import re as _re2
        sector_match = _re2.search(r"板块异动:\s*(\S+)\s*涨", anomaly_context)
        if sector_match:
            anomaly_sector = sector_match.group(1)
            if anomaly_sector not in candidate_sectors:
                candidate_sectors.insert(0, anomaly_sector)

        pick_lines = []
        for sector in candidate_sectors[:3]:  # Max 3 sectors to avoid slowness
            try:
                result = json.loads(get_sector_best_stocks_fn(sector, top_n=5))
                top = result.get("top", [])
                if top:
                    stocks = ", ".join(
                        f"{s['code']} {s['name']}(score={s['score']}, beta={s['beta_weighted']}, {s['today_change_pct']:+.1f}%)"
                        for s in top if s.get("today_change_pct", 0) < 9.8  # Exclude limit-up
                    )
                    if stocks:
                        pick_lines.append(f"  {sector}: {stocks}")
            except Exception:
                pass
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
    signals = []
    try:
        anomaly_data = json.loads(get_anomaly_stocks_fn())
        for stock in anomaly_data.get("limit_up", [])[:10]:
            signals.append({
                "code": stock.get("code", ""),
                "name": stock.get("name", ""),
                "change_pct": stock.get("change_pct", 0),
                "consecutive": stock.get("consecutive_limits", 1),
            })
    except Exception:
        pass

    # ── Step 2: Code selects actionable stocks (beta + institutional) ──
    actionable = []
    candidate_sectors_used = candidate_sectors[:3] if candidate_sectors else []
    for sector in candidate_sectors_used:
        try:
            from alpha_agents.tools.sector_beta import get_sector_best_stocks_fn
            result = json.loads(get_sector_best_stocks_fn(sector, top_n=5))
            for s in result.get("top", []):
                if s.get("today_change_pct", 0) < 9.8:  # Not limit-up
                    # Get institutional analysis for entry/stop
                    try:
                        from alpha_agents.tools.institutional_position import get_institutional_position_fn
                        inst = json.loads(get_institutional_position_fn(s["code"]))
                        action_info = inst.get("action", {})
                        entry_zone = action_info.get("entry_zone", "")
                        stop_loss = action_info.get("stop_loss", "")
                        recommendation = action_info.get("recommendation", "观望")
                        if recommendation in ("回避", "观望") and inst.get("signal_summary", {}).get("score", 0) < 0:
                            continue  # Skip negative scores
                    except Exception:
                        entry_zone = ""
                        stop_loss = ""
                        recommendation = ""

                    actionable.append({
                        "code": s["code"],
                        "name": s["name"],
                        "price": s.get("price", 0),
                        "change_pct": s.get("today_change_pct", 0),
                        "score": s.get("score", 0),
                        "beta": s.get("beta_weighted", 0),
                        "note": s.get("note", ""),
                        "theme": sector,
                        "entry_zone": entry_zone,
                        "stop_loss": stop_loss,
                        "recommendation": recommendation,
                    })
        except Exception as e:
            logger.debug("Sector best stocks failed for %s: %s", sector, e)

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
        report_lines.append("【信号确认】（已涨停，不可买入，仅作主线强度参考）")
        for s in signals[:5]:
            board = f"({s['consecutive']}连板)" if s['consecutive'] >= 2 else ""
            report_lines.append(f"• {s['code']} {s['name']} 涨停封板{board}")
        report_lines.append("")

    # Actionable stocks (code-generated, zero hallucination)
    report_lines.append("【可操作标的】")
    report_lines.append("| 代码 | 名称 | 现价 | 涨幅 | 评分 | 操作建议 |")
    report_lines.append("|------|------|------|------|------|---------|")
    if actionable:
        for a in actionable:
            action = a.get("entry_zone", "") or a.get("recommendation", "")
            sl = f"止损{a['stop_loss']}" if a.get("stop_loss") else ""
            report_lines.append(
                f"| {a['code']} | {a['name']} | {a['price']:.2f}元 | "
                f"{a['change_pct']:+.2f}% | {a['score']:.0f} | "
                f"{action} {sl} |"
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
    for s in signals[:5]:
        recs_json.append({
            "code": s["code"], "name": s["name"],
            "theme": "", "type": "signal",
            "reason": f"涨停封板",
        })
    for a in actionable:
        entry_high = None
        entry_low = None
        sl = None
        try:
            if a.get("stop_loss"):
                sl = float(str(a["stop_loss"]).replace("元", ""))
            ez = a.get("entry_zone", "")
            if "-" in str(ez):
                parts_ez = ez.split("-")
                entry_low = float(parts_ez[0].strip().replace("元", ""))
                entry_high = float(parts_ez[1].strip().replace("元", ""))
            elif a.get("price"):
                entry_high = round(a["price"] * 0.97, 2)  # Default: 3% below current
        except (ValueError, IndexError):
            pass

        recs_json.append({
            "code": a["code"], "name": a["name"],
            "theme": a.get("theme", ""), "type": "actionable",
            "reason": a.get("note", ""),
            "confidence": "high" if a["score"] >= 70 else "medium",
            "action": a.get("entry_zone", ""),
            "entry_low": entry_low, "entry_high": entry_high,
            "stop_loss": sl,
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
    """LLM's ONLY job: explain WHY the anomaly happened in 2-3 sentences."""
    import asyncio
    from agents import Agent, Runner
    from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
    from openai import AsyncOpenAI
    from alpha_agents.config import AGENT_API_KEY, AGENT_BASE_URL, AGENT_MODEL

    client = AsyncOpenAI(api_key=AGENT_API_KEY, base_url=AGENT_BASE_URL)
    model = OpenAIChatCompletionsModel(model=AGENT_MODEL or "qwen-plus", openai_client=client)
    agent = Agent(
        name="cause_analyst",
        instructions=(
            "你是盘中异动追因分析师。你只需要做一件事：用2-3句话解释异动的原因。"
            "不要推荐股票，不要给操作建议，不要生成表格。只写原因分析。"
            "格式：【异动】{板块名} {描述}\n• 催化: {原因}\n• 判断: {一日游/持续行情}\n• 失效条件: {什么情况下判断不成立}"
        ),
        model=model,
        tools=[],  # No tools — just analyze the context we give it
    )

    try:
        result = await asyncio.wait_for(
            Runner.run(agent, f"请分析以下异动的原因:\n\n{context}"),
            timeout=30,
        )
        return result.final_output
    except Exception as e:
        logger.warning("Cause analysis failed: %s", e)
        return context  # Fallback to raw anomaly data


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
