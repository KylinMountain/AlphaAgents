"""Post-market review task — runs at 15:30 after market close.

Verifies today's predictions, updates theme line strengths,
updates market cognition, and generates review report.
"""

import json
import logging
from datetime import datetime, timedelta

from alpha_agents.data.memory_store import (
    get_active_themes, get_pending_predictions, update_prediction_result,
    get_prediction_stats, upsert_cognition,
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


def _verify_predictions() -> None:
    """Verify yesterday's predictions against today's actual prices.

    Fetches yesterday's unverified predictions, gets today's close prices,
    calculates returns, and writes back hit/next_day_return to the DB.
    """
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    predictions = get_pending_predictions(yesterday)
    if not predictions:
        logger.info("No pending predictions from %s to verify", yesterday)
        return

    # Collect all stock codes to fetch quotes
    codes = list({p["code"] for p in predictions if p.get("code")})
    if not codes:
        return

    try:
        quotes_raw = get_stock_quotes_fn(",".join(codes))
        quotes_data = json.loads(quotes_raw)
        # Build lookup: code -> quote dict
        quote_lookup = {q["code"]: q for q in quotes_data.get("quotes", []) if "error" not in q}
    except Exception as e:
        logger.warning("Failed to fetch quotes for prediction verification: %s", e)
        return

    verified = 0
    for pred in predictions:
        code = pred.get("code")
        if not code or code not in quote_lookup:
            continue

        quote = quote_lookup[code]
        today_close = quote.get("price")
        if not today_close:
            continue

        # Use entry_price as baseline; fall back to yesterday's close if unavailable
        entry_price = pred.get("entry_price")
        if not entry_price:
            # Approximate: today_close / (1 + change_pct/100) gives yesterday's close
            change_pct = quote.get("change_pct", 0)
            if change_pct and change_pct != 0:
                entry_price = today_close / (1 + change_pct / 100)
            else:
                continue

        return_pct = round((today_close - entry_price) / entry_price * 100, 2) if entry_price else 0
        direction = pred.get("direction", "")
        is_bullish = direction in ("看多", "bullish", "买入", "long")
        if is_bullish:
            hit_val = 1 if return_pct > 0 else 0
        else:
            hit_val = 1 if return_pct < 0 else 0

        update_prediction_result(pred["id"], next_day_return=return_pct, hit=hit_val)
        verified += 1
        logger.debug("Prediction #%d %s: return=%.2f%%, hit=%d",
                      pred["id"], code, return_pct, hit_val)

    logger.info("Verified %d/%d predictions from %s", verified, len(predictions), yesterday)


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
            logger.debug("Failed to upsert cognition for %s: %s", name, e)

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

    # Deduplicate by code — keep FIRST occurrence (earliest entry_price)
    seen = set()
    unique = []
    for p in predictions:
        if p["code"] not in seen:
            seen.add(p["code"])
            unique.append(p)

    # Batch fetch today's actual close prices via Sina
    codes = [p["code"] for p in unique]
    from alpha_agents.data.market_data import get_realtime_quotes
    prices = get_realtime_quotes(codes) or {}

    # Build verification table
    lines = ["| 代码 | 名称 | 方向 | 推荐价 | 收盘价 | 推荐→收盘 | 结果 | 主线 |",
             "|------|------|------|-------|-------|----------|------|------|"]

    hits, misses, neutral = 0, 0, 0
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

        # Determine hit/miss based on return from entry
        if direction == "bullish":
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

    total_verified = hits + misses
    hit_rate = hits / total_verified * 100 if total_verified > 0 else 0

    summary = f"命中率: {hits}/{total_verified} ({hit_rate:.0f}%) | 中性{neutral}只 | 共{len(unique)}只\n"
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
            f"- {t['name']}（强度 {t['strength']}/10, {t['status']}）\n"
            f"  龙头: {leader} ({t.get('leader_code', '?')})"
        )
    return "\n".join(lines)


def _format_stats(stats: dict) -> str:
    """Format prediction stats."""
    total = stats.get("total", 0)
    if total == 0:
        return "暂无预测记录"
    return f"近7天命中率: {stats.get('hit_rate', 0):.1f}% ({stats.get('hits', 0)}/{total})"


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

    # 3. Format contexts — Agent gets pre-computed results, doesn't need to call tools for verification
    themes_ctx = _format_themes(themes)
    stats_ctx = _format_stats(stats)

    # Portfolio context
    portfolio_ctx = ""
    try:
        pos_summary = get_open_positions_summary()
        changes_summary = get_today_changes_summary(today)
        perf_stats = format_portfolio_stats(get_portfolio_stats(days=7))
        portfolio_ctx = (
            f"【虚拟持仓状态】\n{pos_summary}\n\n"
            f"【今日持仓变动】\n{changes_summary}\n\n"
            f"【近7天策略表现】\n{perf_stats}"
        )
    except Exception as e:
        logger.debug("Failed to build portfolio context: %s", e)

    # 4. Run review agent (append portfolio context to stats)
    full_stats_ctx = stats_ctx
    if portfolio_ctx:
        full_stats_ctx = stats_ctx + "\n\n" + portfolio_ctx
    report = await run_review_analysis(pred_ctx, themes_ctx, full_stats_ctx)

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
            logger.debug("Review notification failed: %s", e)

    # Archive today's market data for future backtesting
    try:
        await asyncio.to_thread(run_daily_archive)
    except Exception as e:
        logger.warning("Daily archive failed: %s", e)

    print(report)
    return report
