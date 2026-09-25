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
from alpha_agents.data.clock import today as clock_today
from alpha_agents.data.theme_state import concept_state
from alpha_agents.tools.market_breadth import get_market_breadth_fn
from alpha_agents.pipeline.theme_manager import (
    evaluate_theme_signals, maybe_discover_theme, refresh_theme_scores, update_theme_strength,
)
from alpha_agents.tools.stock_quotes import get_stock_quotes_fn
from alpha_agents.notify import notify_all
from alpha_agents.data import clock
from alpha_agents.data.portfolio import (
    create_pending_order, check_pending_orders, check_positions,
    get_open_positions, get_pending_orders,
)
from alpha_agents.data.market_data import get_realtime_quotes
from alpha_agents.data.decision_context import build_decision_context, merge_features
from alpha_agents.data.scoring import confidence_to_prob
from alpha_agents.data.thesis import from_recommendation
from alpha_agents.data.trader import load_traders
from alpha_agents.pipeline.tasks import (
    safe_active_themes, safe_market_regime, safe_sentiment_phase,
)
from alpha_agents.pipeline.tasks import exit_decision, thesis_monitor
from alpha_agents.pipeline.tasks.book_manager import manage_book
from alpha_agents.pipeline.tasks.anomaly_scan import (
    _detect_anomalies, _detect_style_rotation, _news_for_sectors,
    _refresh_theme_strengths,
)
# Report post-processing — real prices in the table, the cause-analysis LLM pass,
# and the empty-actionable fallback — lives in its own module: they all operate
# on the report text and share none of this module's state, and the file was at
# its line ceiling. Imported back here because this is where they are called.
from alpha_agents.pipeline.tasks.intraday_report import (
    _auto_fill_actionable, _fix_prices_in_report, _get_cause_analysis,
)
from alpha_agents.pipeline.tasks.session_memory import (
    note_decline, note_undecided, recall_decline,
)

logger = logging.getLogger(__name__)

# How long an intraday pick gives itself to be right. Named once because it is
# declared twice for the same decision — the forecast's own horizon and the
# thesis derived from it — and two literals that must agree is how they come to
# disagree. It used to be a bare ``3`` in the ``from_recommendation`` call while
# the forecast it belongs to declared nothing, so every intraday prediction was
# graded on the global 5-day fallback (and labelled ``legacy_horizon``) while
# the thesis for the same decision said three days.
INTRADAY_HORIZON_DAYS = 3




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
            f"主线: {t['name']}（累计强度 {t['strength']}/10, 今日 {t.get('daily_score', 0):+d}, {t['status']}）\n"
            f"  龙头: {t.get('leader_code', '无')}\n"
            f"  监控标的: {stock_list}"
        )
    return "\n".join(lines)













def _market_view() -> dict:
    """Sector rank, sector flow and market breadth, for evaluating theses.

    Three of the eleven invalidation kinds read these — theme_rank_worse_than,
    theme_flow_negative and breadth_below. Nothing ever passed them, so
    ``MarketView`` carried None for all three and ``evaluate`` correctly
    declined to fire on data it did not have. The result was silent: the
    vocabulary offered the agent three conditions, the agent wrote them
    into real theses, and the monitor could never check them.

    "I could not measure it" must not read as "the thesis broke", so the
    fix is to supply the measurement rather than to loosen the check.

    Returns {} on failure, which restores exactly the previous behaviour —
    those conditions simply do not fire this cycle.
    """
    out: dict = {}
    try:
        # Not today's ranking. The concept flow ranking has a day-over-day
        # Spearman of -0.004 — a theme leading today is no likelier than
        # chance to lead tomorrow — so a thesis checked against one session
        # is checked against noise. concept_state accumulates five sessions,
        # which is also what the replay now reads: the same condition has to
        # mean the same thing in both, or a backtest measures a rule that
        # production does not run.
        ranks, flows = concept_state(clock_today())
        if ranks:
            out["sector_ranks"] = ranks
            out["sector_flows"] = flows
        else:
            logger.warning("板块排名/资金流为空，本轮 theme_rank / theme_flow "
                           "类条件无法判定")
    except Exception as e:
        logger.warning("板块排名/资金流不可用，本轮 theme_rank / theme_flow "
                       "类条件无法判定: %s", e)
    try:
        breadth = json.loads(get_market_breadth_fn())
        # Not ad_ratio. That is advances/declines — unbounded, 999 on a day
        # with no decliners — while the replay feeds advances/total, a 0–1
        # fraction. Same condition, two different quantities: a thesis that
        # said "breadth_below 0.35" would exit at 26% up in production and
        # at 35% up in the backtest. One definition, and it is the bounded
        # one, because that is the one a threshold can be reasoned about on.
        advances = breadth.get("advances")
        total = breadth.get("total")
        if advances is not None and total:
            out["breadth_ratio"] = round(float(advances) / float(total), 4)
        else:
            logger.warning("市场宽度缺少 advances/total，本轮 breadth_below "
                           "条件无法判定: %s", sorted(breadth))
    except Exception as e:
        logger.warning("市场宽度不可用，本轮 breadth_below 条件无法判定: %s", e)
    return out


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
    #
    # The business date comes from the kernel clock, not from ``now``.
    # Everything downstream of this line — the fill date, the exit date,
    # the T+1 settle date — is dated inside the window being simulated,
    # and under replay a wall-clock date would put a September fill on a
    # March order. The kernel refuses that (clock.LookAheadError) rather
    # than writing it, so this is not merely tidier: it is what keeps a
    # replayed cycle working at all. ``now`` above stays the wall clock:
    # the lunch check asks whether the market is open *here and now*,
    # which is a scheduling fact rather than a simulated one.
    today_str = clock.today()
    from alpha_agents.data.memory_store import get_active_price_alerts, trigger_price_alert

    # Quotes are fetched once for every trader's book: the market is
    # shared, only the decisions are not.
    pending = get_pending_orders()
    open_pos = get_open_positions()
    price_alerts = get_active_price_alerts()

    # Persistent WAIT plans are risk-relevant even on a quiet market cycle.
    # They must be checked before the anomaly early-return below.
    from alpha_agents.data import trader_state_store
    from alpha_agents.data.trader_session import namespace as run_namespace
    runtime_traders = load_traders()
    watch_codes: set[str] = set()
    for _trader in runtime_traders:
        try:
            state = trader_state_store.load_latest(
                run_id=run_namespace(), trader_id=_trader.id)
            if state is not None:
                watch_codes.update(
                    item.code for item in state.watchlist
                    if item.status.value in {"watching", "triggered"})
        except Exception as e:
            logger.warning(
                "Trader Runtime state unavailable for %s: %s", _trader.id, e)

    all_codes = list(
        {p["code"] for p in pending + open_pos}
        | {a["code"] for a in price_alerts}
        | watch_codes)

    if all_codes:
        rt_prices = await asyncio.to_thread(get_realtime_quotes, all_codes)
        if rt_prices:
            price_map = {code: data["price"] for code, data in rt_prices.items()}
            trader_prices = {}
            for code, data in rt_prices.items():
                trader_prices[code] = data["price"]
                trader_prices[code + "_chg"] = data.get("change_pct", 0)
            world = await asyncio.to_thread(_market_view)

            for _trader in runtime_traders:
                await manage_book(_trader, price_map, today_str, world)
                # A watch crossing its condition wakes the same Trader even
                # when the market as a whole has no anomaly.
                try:
                    result = await plan_intraday(
                        [], _trader, prices=trader_prices,
                        market_view=world)
                    if result.get("decisions"):
                        logger.info(
                            "Trader Runtime watch recheck [%s]: %s",
                            _trader.id, result.get("status"))
                except Exception as e:
                    logger.exception(
                        "Trader Runtime watch check [%s] failed: %s",
                        _trader.id, e)

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

    # ── Today's cross-sectional theme score — what the theme gate reads ──
    # Refreshed every cycle, unlike `strength`: it answers "how strong is this
    # line today", and it is the number admission and cancellation compare
    # against. It fetches the board again rather than reusing the pass above,
    # which is worth the second call: the endpoint returns a partial list each
    # time, so two draws see more of the board than one.
    await asyncio.to_thread(refresh_theme_scores)

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
    except Exception as e:
        logger.debug("Intraday: morning event context unavailable: %s", e)

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
    except Exception as e:
        logger.debug("Intraday: prior-recommendation context unavailable: %s", e)

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
    except Exception as e:
        logger.debug("Intraday: sentiment-cycle block unavailable: %s", e)

    # ── Pre-fetch best stocks for anomaly sectors (code-level, not LLM-dependent) ──
    candidate_sectors = []  # Must be defined before try block
    sector_picks_cache = {}  # Cache results to avoid duplicate API calls
    sector_picks_ctx = ""
    # Initialised outside the try for the same reason as the line above:
    # it is read much further down, and an early failure in the block
    # would otherwise raise NameError there instead of degrading.
    news_ctx = ""
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
        except Exception as e:
            logger.debug("Intraday: sector extraction unavailable: %s", e)

        # Source 3: Active themes with strength >= 6 (stable but may not change daily)
        for t in themes:
            if t.get("strength", 0) >= 6:
                s = t["name"]
                if s not in seen:
                    candidate_sectors.append(s)
                    seen.add(s)

        # ── News behind the move (the 追因 half of 资金 + 新闻归因) ──
        # The agent has a search_news tool, but leaving it to decide when
        # to use it means an attribution that depends on the model
        # remembering. The sectors are already identified here, by code —
        # so look them up and hand the evidence over with the anomaly.
        news_ctx = _news_for_sectors(candidate_sectors[:3])

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
    #
    # No prices here. This loop selects — it says "the money is buying this
    # name in this sector" — and selection is all a scoring function can
    # honestly do. Where to buy and where to give up are judgements, and
    # they are made in _price_entries below by a model that can look at
    # the stock's actual levels. Filling them in here with price * 0.97 is
    # what produced 109 identical 3% bands, on stocks whose average daily
    # range was 6%.
    actionable = []
    for sector, s in raw_candidates:
        actionable.append({
            "code": s["code"],
            "name": s["name"],
            "price": s.get("price", 0),
            "change_pct": s.get("today_change_pct", 0),
            "score": s.get("score", 0),
            "beta": s.get("beta_weighted", 0),
            "note": s.get("note", ""),
            "theme": sector,
            "institutional": s.get("institutional", ""),
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
                # With its core stocks, as a discovered theme gets them —
                # otherwise it stays an empty line until someone fills it.
                from alpha_agents.pipeline.tasks.morning_scan import _fill_theme_stocks
                await asyncio.to_thread(_fill_theme_stocks, theme)

    # ── Step 3: Code computes theme changes ──
    theme_changes = []
    for t in themes:
        # Strength already updated by _refresh_theme_strengths
        theme_changes.append(f"• {t['name']}: 累计强度 {t['strength']}/10, 今日 {t.get('daily_score', 0):+d} ({t['status']})")

    # ── Step 4: LLM only writes cause analysis (50-100 words) ──
    cause_text = ""
    try:
        cause_context = anomaly_context
        if events_context:
            cause_context += "\n" + events_context
        # The flashes behind the sectors that moved. Without them the
        # model was asked to explain a move from price and flow alone and
        # had to invent a reason or reach for a web search that, on this
        # host, times out.
        if news_ctx:
            cause_context += "\n" + news_ctx
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
    await _save_intraday_recommendations(output)

    try:
        await asyncio.to_thread(
            notify_all,
            f"AlphaAgents 盘中提醒 | {now_str}",
            output[:500],
        )
    except Exception as e:
        logger.warning("Intraday notification failed: %s", e)

    return output


def _worth_asking(recs: list[dict], prices: dict) -> list[dict]:
    """Drop only what no judgement can rescue, and remember the rest.

    Two filters remain, and both are about orders that *cannot be placed*
    rather than about ideas that are probably bad:

      * the theme is not one the system tracks — order creation refuses it
      * the theme is below the admission bar — order creation refuses it

    Pricing a candidate whose order would be rejected is not a judgement
    call being taken away; it is work with no possible outcome. 博敏电子
    was priced twice today on 存储芯片 and 先进封装, and both orders died
    at creation for an untracked theme.

    Both filters read the same gate order creation reads (``data.theme_gate``),
    and that is the point of that module. A filter one bar tighter than
    creation silently drops candidates creation would have taken; one bar
    looser queues up work that dies a cycle later. The bar itself is no longer
    ``strength >= 4`` — it is today's cross-sectional theme score, so a line
    qualifies on money arriving *today* rather than on how many sessions it has
    been confirmed, which is what let one theme's low count kill four
    candidates on 2026-09-14.

    **A recent decline is no longer a filter.** It travels with the
    candidate as ``prior_view`` instead, because the problem it was
    solving was never too much freedom — it was no memory. See
    ``recall_decline``. The theme's score travels the same way, as
    ``theme_note``, so the model prices with the number in front of it.
    """
    from alpha_agents.data.theme_gate import resolve_theme, theme_admits, theme_score_note

    out, dropped = [], []
    for r in recs:
        code = r["code"]
        theme = r.get("theme", "")
        resolved = resolve_theme(theme)
        if resolved is None:
            dropped.append(f"{code}(主线'{theme}'不在跟踪列表，下单必被拒)")
            continue
        weak = theme_admits(resolved)
        if weak:
            dropped.append(f"{code}({weak})")
            continue
        item = {**r, "theme": resolved}
        note = theme_score_note(resolved)
        if note:
            item["theme_note"] = note
        out.append(item)

    if dropped:
        logger.info("定价前剔除 %d 个（订单必被拒，不是判断问题）: %s",
                    len(dropped), ", ".join(dropped[:6]))
    return out


def _dedupe_actionable_by_code(recs: list[dict]) -> list[dict]:
    """One security is one Trader subject, even when several themes surface it."""
    chosen: dict[str, dict] = {}
    themes: dict[str, set[str]] = {}
    for rec in recs:
        code = str(rec.get("code") or "")
        if not code:
            continue
        theme = str(rec.get("theme") or "")
        themes.setdefault(code, set())
        if theme:
            themes[code].add(theme)
        current = chosen.get(code)
        if current is None or float(rec.get("score") or 0) > float(
                current.get("score") or 0):
            chosen[code] = dict(rec)
    out = []
    for code, rec in chosen.items():
        rec["alternate_themes"] = sorted(
            theme for theme in themes.get(code, set())
            if theme != rec.get("theme"))
        out.append(rec)
    return out


async def _save_intraday_recommendations(report: str) -> None:
    """Extract recommendations from intraday report and save with real-time prices."""
    import asyncio

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

    # The kernel clock, not the wall clock: this date becomes the order's
    # ``order_date`` and the information cutoff frozen against it.
    today = clock.today()
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

    # G6: one context per cycle, shared by the picks it produced. The
    # knowledge hash is what makes "this note was in front of the agent"
    # checkable instead of inferred — see decision_context.knowledge_hash.
    from alpha_agents.evolution.context_builder import knowledge_in_force
    _intraday_ctx = build_decision_context(
        task="intraday_monitor",
        themes=safe_active_themes(),
        market_regime=safe_market_regime(),
        sentiment_phase=safe_sentiment_phase(),
        knowledge=knowledge_in_force(),
        extra={"has_anomaly": True},
    )

    # Only traders allowed to open new positions get an order here;
    # the legacy default manages what it holds and buys nothing.
    traders = load_traders(scanning=True)
    saved = 0

    # A 'signal' row is an observation about the market — a stock hit its
    # limit — not a call anyone made. Recorded once, with no trader and no
    # order behind it: doing it per trader would multiply one fact by the
    # number of books and inflate every per-trader count with rows nobody
    # decided.
    for r in valid_recs:
        if r.get("type") == "actionable":
            continue
        _record_intraday_pick(None, r, r["code"], today, "intraday_signal",
                              "signal", prices.get(r["code"]), prices,
                              _intraday_ctx, "signal")
        saved += 1

    buys = [r for r in valid_recs if r.get("type", "actionable") == "actionable"
            and prices.get(r["code"])]
    # Everything code can settle for free, settled before a model is asked.
    #
    # It was the other way round for one morning: 335 pricing calls, 1.6M
    # tokens, zero orders. The agent priced 博敏电子 twice on themes
    # (存储芯片, 先进封装) the system does not track, and the rejection
    # came at the *order*, after the thinking was paid for. 301511 was
    # priced nineteen times and declined nineteen times with the same
    # sentence, because nothing remembered the last eighteen.
    buys = _dedupe_actionable_by_code(_worth_asking(buys, prices))
    if not buys:
        if saved:
            logger.info("Saved %d intraday predictions", saved)
        return

    # Research records candidates; the continuous Trader Runtime owns the
    # decision. The old entry_pricing path is kept below as a migration helper
    # but production no longer asks a second model to create an independent
    # order outside TraderState.
    from alpha_agents.evolution.context_builder import knowledge_in_force
    knowledge_block = knowledge_in_force()
    for trader in traders:
        prediction_ids = {}
        for r in buys:
            confidence = "high" if r.get("score", 0) >= 70 else "medium"
            pred_id = _record_intraday_pick(
                trader, r, r["code"], today, "intraday",
                confidence, prices.get(r["code"]), prices,
                _intraday_ctx, "actionable", place_order=False)
            if pred_id is not None:
                prediction_ids[r["code"]] = pred_id
                saved += 1
        try:
            plan = await plan_intraday(
                buys, trader, prices=prices,
                prediction_ids=prediction_ids,
                research_context=report,
                knowledge_block=knowledge_block)
            logger.info(
                "Trader Runtime intraday [%s]: %s, %d order(s), recheck=%s",
                trader.id, plan.get("status"), len(plan.get("placed") or []),
                plan.get("reevaluate_subjects") or [])
        except Exception as e:
            logger.exception(
                "Trader Runtime intraday [%s] failed — no legacy order fallback: %s",
                trader.id, e)

    if saved:
        logger.info("Saved %d intraday predictions (signal + actionable)", saved)


def _order_fields(d: dict) -> dict:
    """The trader's own numbers, in the shape the order path reads."""
    return {"entry_low": d["entry_low"], "entry_high": d["entry_high"],
            "stop_loss": d["stop_loss"], "size_pct": d.get("size_pct"),
            "confidence": d["confidence"],
            "reason": d["reason"]}


async def _price_for(trader, candidates: list[dict],
                     prices: dict) -> dict[str, dict]:
    """One trader's entry decisions, or nothing at all.

    Nothing at all is a real answer here. Before this, every candidate
    became an order at a fixed 3% band whether or not anyone thought the
    price was right; a cycle that places no order because the levels were
    unattractive is the system working, not failing.
    """
    from alpha_agents.pipeline.tasks import entry_pricing

    if not entry_pricing.enabled():
        logger.info("AGENT_ENTRY_PRICING 已关闭 — 盘中不下单")
        return {}
    candidates = _with_prior_views(candidates, prices, trader_id=trader.id)
    try:
        return await entry_pricing.price(candidates, trader)
    except Exception as e:
        logger.warning("交易员 %s 定价异常（%s）— 本轮不下单", trader.id, e)
        return {}


def _with_prior_views(candidates: list[dict], prices: dict, *, trader_id: str) -> list[dict]:
    # Candidates are shared; beliefs are not. Strip stale cross-book annotations.
    out = []
    for candidate in candidates:
        item = {k: v for k, v in candidate.items() if k != "prior_view"}
        prior = recall_decline(item["code"], prices.get(item["code"]) or 0,
                               trader_id=trader_id)
        if prior:
            item["prior_view"] = prior
        out.append(item)
    return out


def _record_intraday_pick(trader, r: dict, code: str, today: str,
                          report_type: str, confidence: str,
                          entry_price, prices: dict, ctx: dict,
                          rec_type: str, *, place_order: bool = True) -> int | None:
    """Save one code-generated pick as one trader's prediction and order.

    The candidate list is shared — it comes from a scoring function, not a
    model, so every trader sees the same names. What differs is where each
    is willing to buy them, which is exactly the variable the cancellation
    record says decides whether an order ever becomes a position.

    ``trader=None`` records a market observation with no order behind it.
    """
    from alpha_agents.data.trader import DEFAULT_TRADER

    trader_id = trader.id if trader else DEFAULT_TRADER
    try:
        # Phase 1: capture decision-time features for Playbook clustering
        # (Phase 3). Fields match available_fields in the evolution spec.
        #
        # `change_pct_band` is the third clustering dimension, and it is
        # recorded rather than derived at read time so the grouping (SQL), the
        # generated pattern condition and the matcher all read one value. The
        # boundary table lives in `evolution.playbook.change_band`, which owns
        # it the same way it owns the condition it produces.
        from alpha_agents.evolution.playbook import change_band

        features = merge_features({
            "theme": r.get("theme", ""),
            "score": r.get("score", 0),
            "change_pct": r.get("change_pct", 0),
            "change_pct_band": change_band(r.get("change_pct")),
            "institutional": r.get("institutional", ""),
            "playbook_id": r.get("playbook_id"),
            "playbook_name": r.get("playbook_name", ""),
            "playbook_matched": bool(r.get("playbook_id")),
            "rec_type": rec_type,  # 'signal' vs 'actionable'
            "trader": trader_id,
        }, ctx)
        pred_id = save_prediction(
            date=today,
            report_type=report_type,
            code=code,
            name=r.get("name", ""),
            direction="bullish",
            confidence=confidence,
            theme_line=r.get("theme", ""),
            entry_price=entry_price,
            reason=r.get("reason", ""),
            features=features,
            # G1: gradable on Brier. A limit-up 'signal' row is an
            # observation, not a forecast, so only 'actionable' picks
            # carry a probability.
            prob=(confidence_to_prob(confidence)
                  if rec_type == "actionable" else None),
            # The horizon this decision already declares. Not a default chosen
            # here: it is the same number the thesis for this *same* decision
            # gets two paragraphs down, and grading the forecast on 3 days
            # while grading its own thesis on the global 5 measures the pick
            # against a deadline it never had. A 'signal' row carries no prob
            # and so has no forecast to mature — None is the honest answer
            # there, not 3.
            horizon_days=(INTRADAY_HORIZON_DAYS
                          if rec_type == "actionable" else None),
            trader_id=trader_id,
        )
        tag = "signal" if rec_type == "signal" else r.get("confidence", "medium")
        logger.info("  Saved intraday %s [%s]: %s %s @ %.2f",
                    tag, trader_id, code, r.get("name", ""), entry_price or 0)
    except Exception as e:
        logger.debug("Failed to save intraday prediction for %s: %s", code, e)
        return None

    # Production T4 passes place_order=False: this function is now a research
    # recorder. The direct order branch remains temporarily for compatibility
    # tests/callers until the migration is complete.
    if not place_order:
        return pred_id

    # Legacy compatibility: create pending order for actionable recommendations.
    price_chg = prices.get(code + "_chg", 0)
    if trader is None or rec_type == "signal" or price_chg >= 9.8:
        return pred_id
    try:
        # Prefer structured JSON fields, fallback to regex
        entry_low = r.get("entry_low")
        entry_high = r.get("entry_high")
        stop_loss_val = r.get("stop_loss")
        # These came from the trader's own pricing pass. No fallback:
        # a missing level means the trader declined or never answered, and
        # inventing one here would put the 3% constant back in through the
        # side door — which is the entire thing this path replaced.
        if entry_low is None or entry_high is None or stop_loss_val is None:
            logger.debug("%s 缺少交易员定价 — 不下单", code)
            return pred_id
        # Thesis before order, same as the morning path. The pick came from
        # a scoring function but the *price* came from the trader, so its
        # reason is on record and from_recommendation derives the exit
        # conditions from the signals that selected the stock.
        thesis_id = from_recommendation(
            {**r, "stop_loss": stop_loss_val,
             "horizon_days": INTRADAY_HORIZON_DAYS},
            code, created_by="intraday", trader_id=trader.id)
        create_pending_order(
            code=code,
            name=r.get("name", ""),
            theme=r.get("theme", ""),
            order_date=today,
            entry_low=entry_low,
            entry_high=entry_high,
            stop_loss=stop_loss_val,
            source="intraday",
            reason=r.get("reason", ""),
            trader_id=trader.id,
            # Names its own forecast, same as the morning path: the close
            # cannot attribute a result without an explicit link.
            prediction_id=pred_id,
            # And names the idea, for the same reason: the monitor has to
            # know which thesis this fill belongs to, and the result has to
            # land on that thesis rather than on whichever one is nearest.
            thesis_id=thesis_id,
            # The theme lifecycle advises; the agent that wrote the thesis
            # decides — see order_review.
            wake_agent=exit_decision.enabled(),
        )
    except Exception as e:
        logger.debug("Failed to create pending order for %s: %s", code, e)
    return pred_id
