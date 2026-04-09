"""Opening reminder task — runs at 09:15 before market open.

Compares morning predictions against auction/opening prices and pushes
a brief summary of which predictions are aligning and which show
expectation gaps (预期差).

This is a rule-based task — no LLM agent required.
"""

import asyncio
import json
import logging
import time

from alpha_agents.data.memory_store import get_pending_predictions
from alpha_agents.tools.stock_quotes import get_stock_quotes_fn
from alpha_agents.tools.market_breadth import get_market_breadth_fn
from alpha_agents.notify import notify_all

logger = logging.getLogger(__name__)


async def run_opening_reminder() -> str | None:
    """09:15 pre-open check: validate morning predictions against auction data."""
    today = time.strftime("%Y-%m-%d")
    logger.info("Opening reminder starting for %s ...", today)

    # 1. Fetch today's morning predictions
    predictions = get_pending_predictions(today)
    if not predictions:
        logger.info("Opening reminder: no predictions for today, skipping.")
        return None

    # 2. Get current/auction prices for predicted stocks
    codes = [p["code"] for p in predictions if p.get("code")]
    if not codes:
        return None

    try:
        quotes_raw = await asyncio.to_thread(
            get_stock_quotes_fn, ",".join(codes)
        )
        quotes_data = json.loads(quotes_raw)
    except Exception as e:
        logger.warning("Opening reminder: failed to fetch quotes: %s", e)
        return None

    # Build code -> quote lookup
    quote_map: dict[str, dict] = {}
    for q in quotes_data.get("quotes", []):
        if "error" not in q:
            quote_map[q["code"]] = q

    # 3. Get market breadth for overall sentiment
    sentiment = "未知"
    try:
        breadth_raw = await asyncio.to_thread(get_market_breadth_fn)
        breadth = json.loads(breadth_raw)
        if not breadth.get("error"):
            sentiment = breadth.get("sentiment", "未知")
            ad_ratio = breadth.get("advance_decline_ratio", 0)
            advances = breadth.get("advances", 0)
            declines = breadth.get("declines", 0)
            sentiment = f"{sentiment}（涨{advances}/跌{declines}, 比值{ad_ratio}）"
    except Exception as e:
        logger.debug("Opening reminder: breadth failed: %s", e)

    # 4. Compare predictions against opening prices
    aligning = []      # predictions matching the open
    expectation_gap = []  # predictions showing 预期差

    for pred in predictions:
        code = pred.get("code", "")
        name = pred.get("name", code)
        direction = pred.get("direction", "bullish")
        quote = quote_map.get(code)
        if not quote:
            continue

        change_pct = quote.get("change_pct", 0)

        if direction == "bullish":
            if change_pct > 0.5:
                aligning.append(f"  ✓ {name}({code}) 高开{change_pct:+.1f}%，符合看多预期")
            elif change_pct < -0.5:
                expectation_gap.append(f"  ✗ {name}({code}) 低开{change_pct:+.1f}%，预期差！看多但开盘偏弱")
            else:
                aligning.append(f"  ~ {name}({code}) 平开{change_pct:+.1f}%，待观察")
        else:  # bearish
            if change_pct < -0.5:
                aligning.append(f"  ✓ {name}({code}) 低开{change_pct:+.1f}%，符合看空预期")
            elif change_pct > 0.5:
                expectation_gap.append(f"  ✗ {name}({code}) 高开{change_pct:+.1f}%，预期差！看空但开盘偏强")
            else:
                aligning.append(f"  ~ {name}({code}) 平开{change_pct:+.1f}%，待观察")

    # 5. Format brief opening reminder (3-5 lines)
    lines = [f"【开盘提醒 {time.strftime('%H:%M')}】"]

    if aligning:
        lines.append("符合预期:")
        lines.extend(aligning)
    if expectation_gap:
        lines.append("预期差:")
        lines.extend(expectation_gap)
    if not aligning and not expectation_gap:
        lines.append("今日预测标的暂无行情数据")

    lines.append(f"市场情绪: {sentiment}")

    report = "\n".join(lines)
    logger.info("Opening reminder:\n%s", report)

    # 6. Push notification
    try:
        await asyncio.to_thread(
            notify_all,
            f"AlphaAgents 开盘提醒 | {time.strftime('%m-%d')}",
            report,
        )
    except Exception as e:
        logger.debug("Opening notification failed: %s", e)

    print(report)
    return report
