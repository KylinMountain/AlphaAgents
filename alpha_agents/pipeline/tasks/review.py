"""Post-market review task — runs at 15:30 after market close.

Verifies today's predictions, updates theme line strengths,
updates market cognition, and generates review report.
"""

import logging
import json
from datetime import datetime

from alpha_agents.data.memory_store import (
    get_active_themes, get_pending_predictions, update_prediction_result,
    get_prediction_stats, upsert_cognition, get_pending_prediction_dates,
)
from alpha_agents.pipeline.theme_manager import (
    evaluate_theme_signals, update_theme_strength, maybe_discover_theme,
    retire_stale_themes, check_leader_health,
)
from alpha_agents.tools.sector_ranking import get_sector_ranking_fn, get_concept_ranking_fn
from alpha_agents.tools.market_breadth import get_market_breadth_fn
from alpha_agents.tools.stock_quotes import get_stock_quotes_fn
from alpha_agents.tools.anomaly_detect import get_anomaly_stocks_fn
from alpha_agents.agents.review_agent import run_review_analysis
from alpha_agents.notify import notify_all
from alpha_agents.data.daily_archive import run_daily_archive
from alpha_agents.data.portfolio import (
    get_open_positions_summary, get_today_changes_summary, get_portfolio_stats,
    format_portfolio_stats,
)

logger = logging.getLogger(__name__)


def _market_return_pct(rt: dict) -> float:
    """Today's market move, as the median change across quoted stocks.

    Grading a pick on raw direction makes every stock a "hit" on a day the
    whole market rose 1.5%, so the recorded hit rate tracks beta rather
    than skill. Median, not mean, because A-share daily returns are
    right-skewed. Returns 0.0 when there is too little to judge, which
    degrades to the old absolute-return behaviour.
    """
    changes = [
        q.get("change_pct") for q in rt.values()
        if isinstance(q.get("change_pct"), (int, float))
    ]
    if len(changes) < 5:
        return 0.0
    changes.sort()
    mid = len(changes) // 2
    if len(changes) % 2:
        return float(changes[mid])
    return float((changes[mid - 1] + changes[mid]) / 2)


def _verify_prediction_date(pred_date: str, rt: dict) -> int:
    """Verify one prior prediction date against today's actual prices."""
    predictions = get_pending_predictions(pred_date)
    if not predictions:
        logger.info("No pending predictions from %s to verify", pred_date)
        return 0

    market_pct = _market_return_pct(rt)
    verified = 0
    for pred in predictions:
        code = pred.get("code")
        if not code or code not in rt:
            continue

        today_close = rt[code].get("price")
        if not today_close or today_close <= 0:
            continue

        # Use entry_price as baseline; fall back to deriving from Sina's change_pct
        entry_price = pred.get("entry_price")
        if not entry_price:
            change_pct = rt[code].get("change_pct", 0)
            prev_close = rt[code].get("prev_close", 0)
            if prev_close and prev_close > 0:
                entry_price = prev_close
            else:
                continue

        return_pct = round((today_close - entry_price) / entry_price * 100, 2) if entry_price else 0
        # Grade on excess return: beating the market is the claim a stock
        # pick makes. Absolute direction would score the whole book as
        # hits on an up day and misses on a down day.
        excess_pct = round(return_pct - market_pct, 2)
        direction = pred.get("direction", "")
        is_bullish = direction in ("看多", "bullish", "买入", "long")
        if is_bullish:
            hit_val = 1 if excess_pct > 0 else 0
        else:
            hit_val = 1 if excess_pct < 0 else 0

        update_prediction_result(pred["id"], next_day_return=return_pct, hit=hit_val)

        # Phase 3: if this prediction matched an active playbook, record the trade outcome
        try:
            import json as _json
            from alpha_agents.data.memory_store import record_playbook_trade
            features = _json.loads(pred.get("features_json") or "{}")
            playbook_id = features.get("playbook_id")
            if playbook_id:
                record_playbook_trade(int(playbook_id), hit=bool(hit_val),
                                      return_pct=return_pct)
        except Exception as e:
            logger.debug("Playbook trade recording failed for pred #%d: %s",
                         pred["id"], e)

        verified += 1
        logger.debug("Prediction #%d %s: return=%.2f%%, hit=%d",
                      pred["id"], code, return_pct, hit_val)

    logger.info("Verified %d/%d predictions from %s", verified, len(predictions), pred_date)
    return verified


def _verify_predictions() -> None:
    """Verify recent unreviewed prediction dates against today's prices.

    Uses prediction dates, not calendar yesterday, so Monday/holiday reviews
    still close out the prior trading day's recommendations.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    dates = get_pending_prediction_dates(today)
    if not dates:
        logger.info("No pending predictions before %s to verify", today)
        return

    codes = set()
    for pred_date in dates:
        for pred in get_pending_predictions(pred_date):
            if pred.get("code"):
                codes.add(pred["code"])
    if not codes:
        return

    from alpha_agents.data.market_data import get_realtime_quotes
    try:
        rt = get_realtime_quotes(list(codes)) or {}
    except Exception as e:
        logger.warning("Failed to fetch quotes for prediction verification: %s", e)
        return

    total = 0
    for pred_date in dates:
        total += _verify_prediction_date(pred_date, rt)
    logger.info("Verified %d predictions across %d prior dates", total, len(dates))


def _update_market_cognition(themes: list[dict], today: str) -> None:
    """Write market cognition entries for each active theme.

    Derives position from strength and fund_trend from recent strength changes.
    """
    for theme in themes:
        name = theme["name"]
        strength = theme.get("strength", 0) or 0

        # Derive position from strength
        if strength >= 7:
            position = "high"
        elif strength >= 4:
            position = "mid"
        else:
            position = "low"

        # Derive fund_trend from strength change direction
        # Compare current strength with what we know: if theme was recently updated
        # we check status transitions as a proxy for trend
        status = theme.get("status", "watching")
        if status in ("active", "peak"):
            fund_trend = "inflow"
        elif status == "declining":
            fund_trend = "outflow"
        else:
            fund_trend = "neutral"

        assessment = f"{name} 强度{strength}/10, {status}"

        try:
            upsert_cognition(
                sector=name,
                date=today,
                position=position,
                fund_trend=fund_trend,
                assessment=assessment,
            )
        except Exception as e:
            logger.warning("Failed to upsert cognition for %s: %s", name, e)

    logger.info("Updated market cognition for %d themes", len(themes))


def _update_themes_from_market_data(existing_themes: list[dict]) -> None:
    """Use real sector ranking data to discover new themes and update existing ones.

    This runs synchronously (called via to_thread).
    """
    try:
        # Get market breadth for context
        breadth = json.loads(get_market_breadth_fn())
        market_change = 0.0  # Approximate from breadth
        ad_ratio = breadth.get("advance_decline_ratio", 1)

        # Get concept ranking (matches DB concept names)
        ranking = json.loads(get_concept_ranking_fn(top_n=10))
        all_concepts = ranking.get("gainers", []) + ranking.get("losers", [])

        # Build lookup by concept name
        concept_lookup = {c.get("concept", ""): c for c in all_concepts}

        # Get anomaly data — check which stocks hit limit up today
        limit_up_codes: set[str] = set()
        try:
            anomaly_raw = get_anomaly_stocks_fn()
            anomaly_data = json.loads(anomaly_raw)
            for stock in anomaly_data.get("limit_up", []):
                code = stock.get("code", "")
                if code:
                    limit_up_codes.add(code)
        except Exception as e:
            logger.debug("Anomaly fetch for theme update failed: %s", e)

        # Update existing themes
        existing_names = {t["name"] for t in existing_themes}
        for theme in existing_themes:
            if theme["name"] in concept_lookup:
                c = concept_lookup[theme["name"]]
                # Check if this theme's leader hit limit up
                leader_code = theme.get("leader_code", "")
                leader_hit = leader_code in limit_up_codes if leader_code else False
                # Check if leader is breaking down (price < 5-day MA)
                leader_down = check_leader_health(theme)
                signals = evaluate_theme_signals(
                    sector_name=theme["name"],
                    sector_change_pct=c.get("change_pct", 0),
                    sector_fund_flow=c.get("net_flow_yi", 0) * 1e8,
                    market_change_pct=market_change,
                    leader_hit_limit=leader_hit,
                    leader_breaking_down=leader_down,
                )
                update_theme_strength(theme["name"], signals)

        # Discover new themes from top-performing concepts
        for gainer in ranking.get("gainers", [])[:8]:
            concept = gainer.get("concept", "")
            if not concept or concept in existing_names:
                continue
            signals = evaluate_theme_signals(
                sector_name=concept,
                sector_change_pct=gainer.get("change_pct", 0),
                sector_fund_flow=gainer.get("net_flow_yi", 0) * 1e8,
                market_change_pct=market_change,
            )
            maybe_discover_theme(
                concept,
                signals,
                catalyst=f"概念涨{gainer.get('change_pct', 0):.1f}%, 净流入{gainer.get('net_flow_yi', 0):.1f}亿, 领涨{gainer.get('leader', '')}",
            )

        logger.info("Theme update: %d existing updated, checked top 5 for discovery",
                     len(existing_themes))
    except Exception as e:
        logger.warning("Theme update from market data failed: %s", e)


def _verify_today_predictions(predictions: list[dict]) -> str:
    """Batch-verify today's predictions: entry_price → close_price.

    All computation done in Python — no LLM calls needed.
    Uses: first recommendation's entry_price vs today's actual close price.
    """
    if not predictions:
        return "今日无待验证预测"

    # Filter out signal-type predictions (涨停确认股 — not actionable recommendations)
    actionable = [p for p in predictions if p.get("report_type") not in ("intraday_signal",)]

    # Deduplicate by code — keep FIRST occurrence (earliest entry_price)
    seen = set()
    unique = []
    for p in actionable:
        if p["code"] not in seen:
            seen.add(p["code"])
            unique.append(p)

    logger.info("Verify predictions: %d total, %d actionable, %d unique",
                len(predictions), len(actionable), len(unique))

    # Batch fetch today's actual close prices via Sina
    codes = [p["code"] for p in unique]
    from alpha_agents.data.market_data import get_realtime_quotes
    prices = get_realtime_quotes(codes) or {}

    # Build verification table
    lines = ["| 代码 | 名称 | 方向 | 推荐价 | 收盘价 | 推荐→收盘 | 结果 | 主线 |",
             "|------|------|------|-------|-------|----------|------|------|"]

    hits, misses, neutral, unreachable = 0, 0, 0, 0
    by_theme: dict[str, dict] = {}

    for p in unique:
        code = p["code"]
        name = p.get("name", "?")
        direction = p.get("direction", "bullish")
        entry = p.get("entry_price")
        theme = p.get("theme_line", "?")
        rt = prices.get(code)

        if not rt or rt["price"] <= 0:
            lines.append(f"| {code} | {name} | {direction} | {entry or '—'} | — | — | 无数据 | {theme} |")
            continue

        close = rt["price"]

        # Calculate return: entry_price → close (not yesterday → today)
        if entry and entry > 0:
            change_pct = round((close - entry) / entry * 100, 2)
        else:
            # No entry price recorded — use today's change as fallback
            change_pct = rt["change_pct"]
            entry = None

        # Determine hit/miss based on return from entry.
        # Limit-up stocks (>=9.5% from entry) are likely unfillable in practice —
        # mark separately and exclude from hit/miss stats so the rate reflects
        # actually-tradeable outcomes.
        is_limit_up_unreachable = (direction == "bullish" and change_pct >= 9.5)
        is_limit_down_unreachable = (direction == "bearish" and change_pct <= -9.5)

        if is_limit_up_unreachable or is_limit_down_unreachable:
            result = "涨停未入场" if is_limit_up_unreachable else "跌停未入场"
            unreachable += 1  # Not counted in hit/miss — not actually tradeable
        elif direction == "bullish":
            if change_pct > 0:
                result = "命中"
                hits += 1
            elif change_pct < -2:
                result = "未命中"
                misses += 1
            else:
                result = "中性"
                neutral += 1
        else:
            if change_pct < 0:
                result = "命中"
                hits += 1
            elif change_pct > 2:
                result = "未命中"
                misses += 1
            else:
                result = "中性"
                neutral += 1

        lines.append(
            f"| {code} | {name} | {direction} | "
            f"{f'{entry:.2f}' if entry else '—'} | {close:.2f} | "
            f"{change_pct:+.2f}% | {result} | {theme} |"
        )

        t = by_theme.setdefault(theme, {"hits": 0, "total": 0})
        t["total"] += 1
        if result == "命中":
            t["hits"] += 1

    total_verified = hits + misses + neutral
    hit_rate = hits / total_verified * 100 if total_verified > 0 else 0

    summary_parts = [f"命中率: {hits}/{total_verified} ({hit_rate:.0f}%)", f"中性{neutral}只"]
    if unreachable > 0:
        summary_parts.append(f"涨停/跌停未入场{unreachable}只（已从命中率分母剔除）")
    summary_parts.append(f"共{len(unique)}只")
    summary = " | ".join(summary_parts) + "\n"
    summary += "按主线:\n"
    for theme, data in by_theme.items():
        tr = data["hits"] / data["total"] * 100 if data["total"] else 0
        summary += f"  {theme}: {data['hits']}/{data['total']} ({tr:.0f}%)\n"

    return "\n".join(lines) + "\n" + summary


def _format_themes(themes: list[dict]) -> str:
    """Format active themes for the review agent."""
    if not themes:
        return "无活跃主线"
    lines = []
    for t in themes:
        stocks = json.loads(t["core_stocks"]) if t["core_stocks"] else []
        leader = next((s["name"] for s in stocks if s.get("role") == "龙头"), "无")
        lines.append(
            f"- {t['name']}（累计强度 {t['strength']}/10, 今日 {t.get('daily_score', 0):+d}, {t['status']}）\n"
            f"  龙头: {leader} ({t.get('leader_code', '?')})"
        )
    return "\n".join(lines)


def _format_stats(stats: dict) -> str:
    """Format prediction stats."""
    total = stats.get("total", 0)
    if total == 0:
        return "暂无预测记录"
    return f"近7天命中率: {stats.get('hit_rate', 0):.1f}% ({stats.get('hits', 0)}/{total})"


def _score_due_predictions() -> str:
    """Grade probabilistic predictions whose horizon has elapsed.

    This is the G1 signal: Brier plus factor-residual alpha, both from
    market data. Unlike the next-day hit flag it is a proper scoring rule
    and it is neutralised for style exposure, so it measures picking
    rather than beta — and it reaches useful precision in hundreds of
    observations instead of years.
    """
    from datetime import datetime as _dt
    from alpha_agents.data.memory_store import (
        get_predictions_due_for_scoring, save_prediction_score,
        get_scored_predictions,
    )
    from alpha_agents.data.scoring import (
        score_prediction, summarize_scores, DEFAULT_HORIZON_DAYS,
    )

    today = _dt.now().strftime("%Y-%m-%d")
    due = get_predictions_due_for_scoring(today, DEFAULT_HORIZON_DAYS)
    scored = 0
    for pred in due:
        try:
            result = score_prediction(pred["code"], pred["date"], pred["prob"],
                                      DEFAULT_HORIZON_DAYS)
        except Exception as e:
            logger.debug("Scoring failed for #%s: %s", pred["id"], e)
            continue
        if result:
            save_prediction_score(pred["id"], result)
            scored += 1

    if scored:
        logger.info("Scored %d/%d due predictions", scored, len(due))

    summary = summarize_scores(get_scored_predictions(days=30))
    if not summary.get("n"):
        return ""

    skill = summary["brier_skill"]
    verdict = ("概率带信息" if skill > 0.02
               else "概率无信息" if skill > -0.02 else "概率反向")
    lines = [
        "【预测质量】(近30天，市场数据评分)",
        f"• 样本 {summary['n']} 条 | Brier {summary['brier']:.3f} "
        f"(基准0.25) | 技能分 {skill:+.3f} → {verdict}",
        f"• 跑赢市场比例 {summary['hit_rate']*100:.0f}% | "
        f"中位超额 {summary['median_excess']:+.2f}%"
        if summary.get("median_excess") is not None else "",
    ]
    if summary.get("median_residual_alpha") is not None:
        lines.append(
            f"• 中位残差alpha {summary['median_residual_alpha']:+.2f}% "
            f"(剔除动量/反转/波动/换手后，n={summary['n_with_residual']})"
        )
    return "\n".join(x for x in lines if x)


def _exposure_note(today: str) -> str:
    """State how much of the day's risk was actually taken.

    A day with no fills exercised no stop, no target and no theme exit, so
    nothing about the exit logic was tested. Left unsaid, the agent reads
    a quiet P&L as evidence its risk management works.
    """
    from alpha_agents.data.portfolio import get_open_positions
    from alpha_agents.data.memory_store import _get_conn

    try:
        conn = _get_conn()
        filled = conn.execute(
            "SELECT COUNT(*) FROM virtual_portfolio WHERE open_date = ? "
            "AND status IN ('open', 'stopped', 'target_hit', 'expired')",
            (today,)).fetchone()[0]
        closed = conn.execute(
            "SELECT COUNT(*) FROM virtual_portfolio WHERE close_date = ?",
            (today,)).fetchone()[0]
        held = len(get_open_positions())
    except Exception as e:
        logger.debug("Exposure note unavailable: %s", e)
        return ""

    if filled == 0 and held == 0:
        return ("【今日风险敞口】0 笔成交、0 笔持仓。\n"
                "→ 因此今天**没有任何退出规则被检验过**：没有止损可触发，"
                "没有止盈可触发，没有主线退出可触发。\n"
                "→ 不要把「没有亏损」写成风控有效或挂单价格合理——"
                "什么都没发生和风控起作用是两回事。今日的经验只能来自"
                "选股与主线判断，不能来自持仓管理。")
    return (f"【今日风险敞口】成交 {filled} 笔、平仓 {closed} 笔、"
            f"当前持仓 {held} 笔。持仓管理相关的结论只对这些仓位成立。")


def _risk_note(today: str) -> str:
    """Portfolio-level risk: the account curve, the drawdown, correlation.

    Recorded here rather than intraday because the mark that matters is
    the close. Without a daily mark there is no curve at all, so drawdown
    and any time-weighted comparison to the market stay uncomputable no
    matter how long the system runs.
    """
    parts = []
    try:
        from alpha_agents.data.portfolio_risk import (
            current_drawdown, describe_clusters, record_equity_mark,
        )
        mark = record_equity_mark(today)
        parts.append(f"【账户】{mark['equity']:,.0f}元 "
                     f"（现金 {mark['cash']:,.0f} + 持仓市值 {mark['market_value']:,.0f}），"
                     f"累计 {mark['return_pct']:+.2f}%")

        dd = current_drawdown()
        if dd:
            line = f"【组合回撤】距高点 {dd['drawdown_pct']:.2f}%（{dd['marks']} 个记录点）"
            if dd["blocked"]:
                line += "，**已触及上限，暂停开新仓**"
            parts.append(line)

        clusters = describe_clusters()
        if clusters:
            parts.append(clusters)
    except Exception as e:
        logger.warning("Risk note unavailable: %s", e)
    return "\n".join(parts)


async def run_review() -> str | None:
    """Execute the post-market review task.

    1. Get pending predictions and format for agent
    2. Get active themes and format for agent
    3. Run review agent to verify and analyze
    4. Retire stale themes
    5. Push notification

    Returns the review report text.
    """
    import asyncio

    today = datetime.now().strftime("%Y-%m-%d")
    logger.info("Review starting for %s...", today)

    # 0. Verify yesterday's predictions against actual prices
    await asyncio.to_thread(_verify_predictions)

    # G1: grade probabilistic forecasts on Brier + residual alpha.
    try:
        score_block = await asyncio.to_thread(_score_due_predictions)
    except Exception as e:
        logger.warning("Prediction scoring failed: %s", e)
        score_block = ""

    # 1. Verify today's predictions in Python (batch, no LLM needed)
    pending = get_pending_predictions(today)
    themes = get_active_themes()
    stats = get_prediction_stats(days=7)

    logger.info("Review: %d predictions, %d themes", len(pending), len(themes))

    pred_ctx = await asyncio.to_thread(_verify_today_predictions, pending)
    logger.info("Prediction verification done (Python batch)")

    # 2. Auto-discover/update themes from real sector data
    await asyncio.to_thread(_update_themes_from_market_data, themes)

    # Re-read themes after update
    themes = get_active_themes()

    # 2b. Snapshot theme strengths for velocity detection (end-of-day state)
    # Used by portfolio.check_positions to detect rapid theme decay
    # (e.g., strength dropping 9→5 in 2 days triggers a bearish stop tighten).
    # INSERT OR REPLACE on (date, data_type) makes this idempotent within a day.
    try:
        from alpha_agents.data.memory_store import save_theme_snapshot
        await asyncio.to_thread(save_theme_snapshot, today, themes)
        logger.info("Theme snapshot saved: %d themes", len(themes))
    except Exception as e:
        logger.warning("Failed to save theme snapshot: %s", e)

    # 3. Format contexts — Agent gets pre-computed results, doesn't need to call tools for verification
    themes_ctx = _format_themes(themes)
    stats_ctx = _format_stats(stats)

    # Portfolio context — each call independent so failures are isolated
    pos_summary = ""
    changes_summary = ""
    perf_stats = ""
    try:
        pos_summary = get_open_positions_summary()
    except Exception as e:
        logger.warning("Failed to get positions summary: %s", e)
    try:
        changes_summary = get_today_changes_summary(today)
    except Exception as e:
        logger.warning("Failed to get today changes: %s", e)
    try:
        perf_stats = format_portfolio_stats(get_portfolio_stats(days=7))
    except Exception as e:
        logger.warning("Failed to get portfolio stats: %s", e)

    portfolio_ctx = ""
    if pos_summary or changes_summary or perf_stats:
        parts = []
        if pos_summary:
            parts.append(f"【虚拟持仓状态】\n{pos_summary}")
        if changes_summary:
            parts.append(f"【今日持仓变动】\n{changes_summary}")
        if perf_stats:
            parts.append(f"【近7天策略表现】\n{perf_stats}")
        portfolio_ctx = "\n\n".join(parts)

    # Say plainly when nothing was tested. The agent read "今日无持仓变动"
    # and wrote the lesson "挂单价格设置合理，所有5笔挂单均未触发止损，
    # 有效控制了下行风险" — on a day when nothing filled, so no stop could
    # have triggered and no exit rule was ever exercised. It scored the
    # absence of events as a risk-management success and wrote that into
    # long-term memory. Absence of evidence has to arrive labelled.
    portfolio_ctx = (portfolio_ctx + "\n\n" + _exposure_note(today)
                     + "\n\n" + _risk_note(today)).strip()

    # 4. Run review agent. The learning context goes in too: post_review
    # runs after this call, so without it the agent writing the report
    # cannot see the principles and lessons it has already produced, and
    # re-derives them every session.
    full_stats_ctx = stats_ctx
    if portfolio_ctx:
        full_stats_ctx = stats_ctx + "\n\n" + portfolio_ctx
    try:
        from alpha_agents.evolution import build_review_context
        learned = build_review_context()
        if learned:
            full_stats_ctx += "\n\n" + learned
    except Exception as e:
        logger.warning("Review learning context unavailable: %s", e)
    report = await run_review_analysis(pred_ctx, themes_ctx, full_stats_ctx)

    # Prepend the market-scored quality block. It goes above the LLM's own
    # narrative deliberately: these numbers come from price data, so they
    # are the part of the report that cannot be talked into looking good.
    if score_block:
        report = score_block + "\n\n" + report

    # 5. Retire stale themes
    retired = retire_stale_themes()
    if retired:
        logger.info("Review: retired themes: %s", retired)

    # 6. Update market cognition for all active themes
    active_themes = get_active_themes()
    _update_market_cognition(active_themes, today)

    # 7. Push notification
    if report and not report.startswith("["):
        try:
            await asyncio.to_thread(
                notify_all,
                f"AlphaAgents 复盘 | {today}",
                report[:500],
            )
        except Exception as e:
            logger.warning("Review notification failed: %s", e)

    # Archive today's market data for future backtesting
    try:
        await asyncio.to_thread(run_daily_archive)
    except Exception as e:
        logger.warning("Daily archive failed: %s", e)

    # Update local market history DB with today's data
    try:
        from alpha_agents.data.market_history import update_daily
        updated = await asyncio.to_thread(update_daily)
        logger.info("Market history daily update: %d rows", updated)
    except Exception as e:
        logger.warning("Market history update failed: %s", e)

    # Compute and save tomorrow's sentiment phase (uses today's archived data)
    try:
        from alpha_agents.data.sentiment_cycle import compute_and_save_sentiment
        cycle = await asyncio.to_thread(compute_and_save_sentiment)
        logger.info("Tomorrow's sentiment: %s (confidence %.0f%%)",
                     cycle.get("phase", "?"), cycle.get("confidence", 0) * 100)
    except Exception as e:
        logger.warning("Sentiment cycle computation failed: %s", e)

    # Phase 2: extract lessons + consolidate principles
    try:
        from alpha_agents.evolution import post_review
        evolution_report = await post_review(today, report)
        if evolution_report:
            report += "\n\n" + evolution_report
    except Exception as e:
        logger.warning("Evolution post_review failed: %s", e)

    return report
