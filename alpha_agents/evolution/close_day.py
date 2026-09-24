"""The trader's close of day, in the order a trader does it.

1. Review each trade it closed (``trade_review``) — with the handbook that
   was in force while it held them.
2. Review the market (``market_review``): what the day was, which boards
   moved and why, whether it had them, a watchlist for tomorrow — with its
   exposure against the market and the grade of its earlier watchlists.
3. Rewrite its handbook (``handbook``) from every trade review **and** from
   what it missed: its exposure against the market, the boards its recent
   market reviews say it was not in, and how its watchlists did. A rewrite
   that only reads losses learns only to stay out; the 2026-01 replay with a
   handbook did exactly that (18 buys → 9, exposure 6.7% → 3.2%, in a market
   up 6.8%).

Replay and live share this; they differ only in how the day's facts, the
exposure line and the "what did I do with each board" map are assembled.
``sealed`` is the first date the step must not read — the day after the
session — because a replay's market history holds the future.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, timedelta

logger = logging.getLogger(__name__)


def day_after(day: str) -> str:
    return (date.fromisoformat(day) + timedelta(days=1)).isoformat()


async def review_day(conn: sqlite3.Connection, hist: sqlite3.Connection, *,
                     trader_id: str, trader, day: str, model, facts_text: str,
                     exposure_text: str = "",
                     handbook_before: str | None = None,
                     watch: bool = True) -> dict:
    """Run all three steps for one trader. Never raises; returns counts."""
    from alpha_agents.evolution import handbook, market_review, trade_review

    sealed = day_after(day)
    counts = {"trade_reviews": 0, "market_review": 0, "handbook": 0}
    try:
        counts["trade_reviews"] = await trade_review.review_closed(
            conn, hist, trader_id=trader_id, as_of=day, model=model,
            trader=trader, handbook_before=handbook_before)
    except Exception as e:                            # noqa: BLE001
        logger.warning("%s: trade reviews failed for %s: %s", day, trader_id, e)

    grade = ""
    try:
        grade = market_review.grade_line(
            market_review.grade(conn, hist, trader_id, before=sealed))
    except Exception as e:                            # noqa: BLE001
        logger.warning("%s: watchlist grading failed: %s", day, e)

    if watch and facts_text:
        context = "\n\n".join(x for x in (
            exposure_text, grade,
            handbook.load(trader_id, before=handbook_before)) if x)
        if await market_review.write(conn, trader_id=trader_id, date=day,
                                     facts=facts_text, model=model,
                                     context=context):
            counts["market_review"] = 1

    opportunity = "\n".join(x for x in (
        exposure_text, market_review.missed(conn, trader_id, up_to=day), grade) if x)
    if counts["trade_reviews"] or counts["market_review"]:
        if await handbook.consolidate(conn, trader_id, as_of=day, model=model,
                                      trader=trader, opportunity=opportunity):
            counts["handbook"] = 1
    return counts
