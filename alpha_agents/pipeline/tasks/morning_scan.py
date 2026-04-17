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
from alpha_agents.tools.futures_quotes import get_futures_quotes_fn
from alpha_agents.tools.stock_quotes import get_stock_quotes_fn
from alpha_agents.data.memory_store import upsert_theme
from alpha_agents.agents.morning import run_morning_analysis
# cross_validate agent replaced by code-driven 5-dimension check (v2.3)
from alpha_agents.notify import notify_all
from alpha_agents.config import DATA_DIR
from alpha_agents.data.portfolio import create_pending_order, parse_entry_zone, parse_stop_loss

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
            leader_change = gainer.get("leader_change_pct", gainer.get("change_pct", 0))
            signals = evaluate_theme_signals(
                sector_name=concept_name,
                sector_change_pct=gainer.get("change_pct", 0),
                sector_fund_flow=gainer.get("net_flow_yi", 0) * 1e8,
                market_change_pct=0,
                leader_hit_limit=leader_change >= 9.5,
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
    if not events and not themes:
        logger.info("Morning scan: no significant events and no active themes")
        return None
    if not events:
        logger.info("Morning scan: no significant events, but %d active themes — continuing", len(themes))

    # Cache today's events for intraday agent to read
    try:
        cache_path = DATA_DIR / "today_events.json"
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump({"date": time.strftime("%Y-%m-%d"), "events": events}, f, ensure_ascii=False)
    except Exception as e:
        logger.warning("Failed to write event cache: %s", e)

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
        # Add commodity prices (oil, gold)
        try:
            futures = json.loads(await asyncio.to_thread(get_futures_quotes_fn, "原油,沪金", 2))
            for q in futures.get("quotes", []):
                lines.append(f"  {q['name']}: {q['latest_close']} ({q['change_pct']:+.2f}%)")
        except Exception:
            pass

        global_ctx = "【全球市场】\n" + "\n".join(lines) if lines else ""
    except Exception as e:
        logger.debug("Morning scan: global overview failed: %s", e)

    # 5. Sentiment cycle context
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

    # 6. Run morning agent with context
    themes_ctx = _format_themes(themes)
    stats_ctx = _format_stats(stats)
    events_ctx = _format_events(events)
    if global_ctx:
        events_ctx = global_ctx + "\n\n" + events_ctx
    if sentiment_ctx:
        events_ctx = sentiment_ctx + "\n\n" + events_ctx

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
            logger.warning("Morning notification failed: %s", e)

    # 6. Extract recommendations, cross-validate, and save as predictions
    if report and not report.startswith("["):
        recs = _extract_json_recommendations(report)
        if not recs:
            recs = _extract_table_recommendations(report)
        if recs:
            recs = await _cross_validate_recommendations(recs)
            _save_recommendations_list(recs)

            # 7. LLM VPA analysis for each recommended stock (Anna Coulling deep analysis)
            try:
                from alpha_agents.tools.vpa import compute_vpa_with_llm
                vpa_lines = ["\n\n【量价深度分析】（Anna Coulling VPA）"]
                for r in recs[:5]:  # Top 5 recommendations
                    code = r.get("code", "")
                    name = r.get("name", "")
                    if not code:
                        continue
                    try:
                        vpa_r = await asyncio.to_thread(compute_vpa_with_llm, code, name)
                        if vpa_r.get("ok") and vpa_r.get("llm_report"):
                            verdict = vpa_r.get("llm_verdict", "?")
                            conf = vpa_r.get("llm_confidence", 0)
                            phase = vpa_r.get("llm_phase", "?")
                            confirmed = "已确认" if vpa_r.get("llm_confirmed") else "待确认"
                            vpa_lines.append(
                                f"\n▶ {code} {name} [{verdict} 信心{conf} {phase} {confirmed}]"
                            )
                            llm_text = vpa_r["llm_report"]
                            if len(llm_text) > 1500:
                                llm_text = llm_text[:1500] + f"\n...(完整报告: vpa {code})"
                            vpa_lines.append(llm_text)
                    except Exception as e:
                        logger.debug("Morning VPA for %s failed: %s", code, e)
                if len(vpa_lines) > 1:
                    report += "\n".join(vpa_lines)
                    logger.info("Added VPA analysis for %d stocks to morning report", len(vpa_lines) - 1)
            except Exception as e:
                logger.debug("Morning VPA section failed: %s", e)

    return report


async def _cross_validate_recommendations(recs: list[dict]) -> list[dict]:
    """Code-driven 5-dimension cross-validation (V2 模式4 upgrade).

    Replaces the old LLM+regex approach with deterministic rules + VPA.

    5 dimensions:
      1. 资金面: 主力资金方向（get_stock_fund_flow_fn）
      2. 基本面: ROE / 净利润增长 / 负债率（get_financial_data_fn, 30d 缓存）
      3. 位置面: 近5日涨幅（get_stock_quotes_fn）
      4. 情绪面: 大盘涨跌比（get_market_breadth_fn）
      5. VPA面: Anna Coulling 量价分析（compute_vpa_with_llm）

    Scoring: 4+/5 pass → high, 3/5 → medium, ≤2 → removed.

    **Strict mode** (fixed 2026-04-15): Data-unavailable dimensions count as
    "unknown" (?) and DO NOT contribute to passes. Previously they were
    default-pass, which meant a stock with all 5 tool failures could score
    5/5 → high confidence (hallucination amplifier). VPA "中性" also no
    longer counts as pass — must be explicit 看多/偏多 to contribute.
    """
    import asyncio

    if not recs:
        return recs

    # ── Dimension 4: 情绪面 (shared across all stocks) ──
    # If tool fails, we genuinely don't know market sentiment — don't grant
    # a free pass to every stock in the batch.
    emotion_known = False
    emotion_pass = False
    try:
        from alpha_agents.tools.market_breadth import get_market_breadth_fn
        breadth = json.loads(await asyncio.to_thread(get_market_breadth_fn))
        ad_ratio = breadth.get("advance_decline_ratio", 1)
        emotion_pass = ad_ratio > 0.8  # V2 原始标准
        emotion_known = True
    except Exception:
        pass  # emotion_known stays False → dim marked as "?"

    validated = []
    for r in recs:
        code = r.get("code", "")
        name = r.get("name", "")
        if not re.match(r"^\d{6}$", code):
            continue

        passes = 0
        dims = []

        # ── Dim 1: 资金面 ──
        try:
            from alpha_agents.tools.fund_flow import get_stock_fund_flow_fn
            ff = json.loads(await asyncio.to_thread(get_stock_fund_flow_fn, code))
            consecutive_in = ff.get("consecutive_inflow_days", 0)
            trend = ff.get("trend", "")
            if consecutive_in >= 1 or "流入" in trend:
                passes += 1
                dims.append("资金✅")
            else:
                dims.append("资金❌")
        except Exception:
            dims.append("资金?")  # No default pass — unknown stays unknown

        # ── Dim 2: 基本面 (real financials, 30d cached) ──
        # Pass: 优质 (ROE>15 + 低杠杆) or 一般. Fail: 盈利能力弱 (ROE<5).
        # The "一般" bucket passes because A-share median ROE sits ~8%, so
        # requiring "优质" for every recommendation would remove most picks.
        # "盈利能力弱" catches the real dog stocks (ST candidates, 亏损股).
        try:
            from alpha_agents.tools.financial_data import get_financial_data_fn
            fd = json.loads(await asyncio.to_thread(get_financial_data_fn, code))
            if fd.get("error"):
                dims.append("基本面?")
            else:
                quality = fd.get("quality_flag", "一般")
                roe = fd.get("roe_pct")
                np_growth = fd.get("net_profit_growth_pct")
                roe_str = f"ROE{roe:.1f}%" if roe is not None else "ROE?"
                if quality == "优质":
                    passes += 1
                    dims.append(f"基本面✅({roe_str})")
                elif quality == "盈利能力弱":
                    dims.append(f"基本面❌({roe_str})")
                else:  # 一般 — acceptable median
                    passes += 1
                    dims.append(f"基本面~({roe_str})")
                # Hard override: 净利润同比下滑>50% 直接判定为基本面差
                if np_growth is not None and np_growth < -50:
                    # Retract the pass if we granted one
                    if dims[-1].startswith("基本面✅") or dims[-1].startswith("基本面~"):
                        passes -= 1
                    dims[-1] = f"基本面❌({roe_str} 净利{np_growth:.0f}%)"
        except Exception:
            dims.append("基本面?")

        # ── Dim 3: 位置面 ──
        try:
            from alpha_agents.tools.stock_quotes import get_stock_quotes_fn
            quotes = json.loads(await asyncio.to_thread(get_stock_quotes_fn, code))
            q = quotes.get("quotes", [{}])[0]
            week_chg = q.get("week_change_pct", 0)
            if week_chg < 15:  # V2 标准: 5日涨幅<15%
                passes += 1
                dims.append(f"位置✅({week_chg:+.1f}%)")
            else:
                dims.append(f"位置❌(追高{week_chg:+.1f}%)")
        except Exception:
            dims.append("位置?")

        # ── Dim 4: 情绪面 ──
        if not emotion_known:
            dims.append("情绪?")
        elif emotion_pass:
            passes += 1
            dims.append("情绪✅")
        else:
            dims.append("情绪❌")

        # ── Dim 5: VPA 量价分析 ──
        # Only 看多/偏多 contributes. 中性 = no signal = no pass.
        try:
            from alpha_agents.tools.vpa import compute_vpa_with_llm
            vpa_r = await asyncio.to_thread(compute_vpa_with_llm, code, name)
            if vpa_r.get("ok"):
                verdict = vpa_r.get("llm_verdict", "中性")
                if verdict in ("看多", "偏多"):
                    passes += 1
                    dims.append(f"VPA✅({verdict})")
                elif verdict in ("看空", "偏空"):
                    dims.append(f"VPA❌({verdict})")
                else:
                    dims.append(f"VPA~({verdict})")  # 中性 no pass
            else:
                dims.append("VPA?")
        except Exception:
            dims.append("VPA?")

        # ── Scoring ──
        dims_str = " ".join(dims)
        if passes >= 4:
            r["confidence"] = "high"
            validated.append(r)
            logger.info("  CV: %s %s → high (%d/5) [%s]", code, name, passes, dims_str)
        elif passes >= 3:
            r["confidence"] = "medium"
            validated.append(r)
            logger.info("  CV: %s %s → medium (%d/5) [%s]", code, name, passes, dims_str)
        else:
            logger.info("  CV: %s %s → removed (%d/5) [%s]", code, name, passes, dims_str)

    if not validated:
        logger.info("Cross-validation removed all recommendations")
    else:
        logger.info("Cross-validation: %d/%d passed", len(validated), len(recs))
    return validated



def _save_recommendations_list(recs: list[dict]) -> None:
    """Save pre-validated recommendations as predictions, fetching entry prices."""
    today = time.strftime("%Y-%m-%d")
    if not recs:
        return

    # Batch-fetch latest close prices for all recommendation codes
    entry_prices: dict[str, float | None] = {}
    valid_codes = [r.get("code", "") for r in recs if re.match(r"^\d{6}$", r.get("code", ""))]
    if valid_codes:
        try:
            quotes_raw = get_stock_quotes_fn(codes=",".join(valid_codes))
            quotes_data = json.loads(quotes_raw)
            for q in quotes_data.get("quotes", []):
                if "price" in q and q["price"]:
                    entry_prices[q["code"]] = q["price"]
        except Exception as e:
            logger.debug("Failed to fetch entry prices: %s", e)

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
                entry_price=entry_prices.get(code),
                reason=r.get("reason", "")[:100],
            )
            saved += 1
            logger.info("  Saved prediction: %s %s (%s, entry=%.2f)",
                        code, r.get("name", ""), r.get("confidence", ""),
                        entry_prices.get(code, 0) or 0)
            # Create pending order (挂单，等价格回调到介入区间再建仓)
            try:
                # Prefer structured JSON fields, fallback to regex parsing
                entry_low = r.get("entry_low")
                entry_high = r.get("entry_high")
                stop_loss_val = r.get("stop_loss")
                if entry_low is None and entry_high is None:
                    entry_low, entry_high = parse_entry_zone(r.get("action", ""))
                if stop_loss_val is None:
                    stop_loss_val = parse_stop_loss(r.get("action", ""))
                # P0.2: pull VPA-derived take-profit target if available
                from alpha_agents.data.portfolio import get_vpa_target_for_code
                target_price = get_vpa_target_for_code(code)
                create_pending_order(
                    code=code,
                    name=r.get("name", ""),
                    theme=r.get("theme", ""),
                    order_date=today,
                    entry_low=entry_low,
                    entry_high=entry_high,
                    stop_loss=stop_loss_val,
                    target_price=target_price,
                    source="morning",
                    reason=r.get("reason", "")[:100],
                )
            except Exception as e:
                logger.debug("Failed to create pending order for %s: %s", code, e)
        except Exception as e:
            logger.debug("Failed to save prediction for %s: %s", code, e)

    if saved:
        logger.info("Saved %d predictions from morning report", saved)


def _save_recommendations(report: str) -> None:
    """Legacy wrapper — extract and save recommendations from report text.

    Primary: parse <!--RECOMMENDATIONS ... RECOMMENDATIONS--> JSON block.
    Fallback: regex parse the markdown table.
    """
    recs = _extract_json_recommendations(report)
    if not recs:
        recs = _extract_table_recommendations(report)
    if not recs:
        return
    _save_recommendations_list(recs)


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
