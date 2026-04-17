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

    # Collect unique stock codes
    codes = list({p["code"] for p in predictions if p.get("code")})
    if not codes:
        return

    # Batch fetch via Sina (fast, no baostock login/logout per stock)
    from alpha_agents.data.market_data import get_realtime_quotes
    try:
        rt = get_realtime_quotes(codes) or {}
    except Exception as e:
        logger.warning("Failed to fetch quotes for prediction verification: %s", e)
        return

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

    # Check VPA pending signals — auto-confirm/deny/expire
    try:
        vpa_updates = await asyncio.to_thread(_check_vpa_signals, today)
        if vpa_updates:
            report += "\n\n" + vpa_updates
            logger.info("VPA signal check: %s", vpa_updates[:200])
    except Exception as e:
        logger.warning("VPA signal check failed: %s", e)

    return report


def _check_vpa_signals(today: str) -> str:
    """Check all pending VPA signals against today's K-line data.

    For each pending signal:
      - Expired? → mark expired
      - Confirmed? → re-analyze with LLM, let LLM judge
      - Still pending? → keep

    Returns text summary of changes for the report.
    """
    from alpha_agents.data.memory_store import (
        get_pending_vpa_signals, resolve_vpa_signal, expire_old_vpa_signals,
        get_pending_vpa_scenarios, expire_old_vpa_scenarios,
    )

    # Step 1: Expire old signals + scenarios (scenarios expire at 10 days,
    # signals at 3 — both use the same per-row expire_date set at create)
    expired_count = expire_old_vpa_signals(today)
    expired_scenarios = expire_old_vpa_scenarios(today)

    # Step 2: Check remaining pending signals
    pending = get_pending_vpa_signals()
    pending_scenarios = get_pending_vpa_scenarios()
    if not pending and not pending_scenarios and expired_count == 0 and expired_scenarios == 0:
        return ""

    lines = ["【VPA 信号追踪】"]
    if expired_count > 0:
        lines.append(f"• {expired_count} 个信号已过期（超过 3 天未确认）")
    if expired_scenarios > 0:
        lines.append(f"• {expired_scenarios} 个 scenario 已过期（超过 10 天未确认）")
    if pending_scenarios:
        lines.append(f"• {len(pending_scenarios)} 个 scenario 仍在追踪:")
        for sc in pending_scenarios[:5]:  # cap display
            sigs = "+".join(sc.get("signal_names") or [])
            lines.append(
                f"  - {sc['code']} {sc.get('name', '')}: {sc['scenario_name']} "
                f"({sc.get('phase', '')}) — 等 {sc.get('confirmation_criteria', '')[:30]}"
            )

    # Step 3: For each pending signal, check confirmation with code VPA (fast, no LLM save)
    if pending:
        from alpha_agents.tools.vpa import compute_vpa
        from alpha_agents.data.market_data import get_realtime_quotes
        for sig in pending[:10]:
            code = sig["code"]
            name = sig.get("name", "")
            try:
                # Use code-only VPA for quick check (no LLM, no DB writes)
                r = compute_vpa(code, name=name)
                if not r.get("ok"):
                    continue

                # Simple confirmation heuristic:
                # If signal was bearish and stock dropped today → confirmed
                # If signal was bearish and stock rose significantly → denied
                rt = get_realtime_quotes([code])
                today_chg = rt.get(code, {}).get("change_pct", 0) if rt else 0

                if sig["direction"] in ("偏空", "看空"):
                    if today_chg < -2:
                        resolve_vpa_signal(sig["id"], "confirmed",
                                          resolved_by=f"今日跌{today_chg:.1f}%")
                        lines.append(f"• ✅ {code} {name} [{sig['signal_type']}] → 已确认（今日{today_chg:+.1f}%）")
                    elif today_chg > 3:
                        resolve_vpa_signal(sig["id"], "denied",
                                          resolved_by=f"今日涨{today_chg:.1f}%否定看空")
                        lines.append(f"• ❌ {code} {name} [{sig['signal_type']}] → 已否定（今日{today_chg:+.1f}%）")
                    else:
                        lines.append(f"• ⏳ {code} {name} [{sig['signal_type']}] → 仍待确认（今日{today_chg:+.1f}%）")
                elif sig["direction"] in ("偏多", "看多"):
                    if today_chg > 2:
                        resolve_vpa_signal(sig["id"], "confirmed",
                                          resolved_by=f"今日涨{today_chg:.1f}%")
                        lines.append(f"• ✅ {code} {name} [{sig['signal_type']}] → 已确认（今日{today_chg:+.1f}%）")
                    elif today_chg < -3:
                        resolve_vpa_signal(sig["id"], "denied",
                                          resolved_by=f"今日跌{today_chg:.1f}%否定看多")
                        lines.append(f"• ❌ {code} {name} [{sig['signal_type']}] → 已否定（今日{today_chg:+.1f}%）")
                    else:
                        lines.append(f"• ⏳ {code} {name} [{sig['signal_type']}] → 仍待确认（今日{today_chg:+.1f}%）")
            except Exception as e:
                logger.debug("VPA signal check for %s failed: %s", code, e)

    return "\n".join(lines) if len(lines) > 1 else ""
