"""The trader's close of day, in the order a trader does it.

1. Review each trade it closed (``trade_review``) — with the handbook that
   was in force while it held them.
2. Review the day (``market_review``): the market's numbers beside the
   trader's own record of the day (``day_record``) — what it chose at the
   open and why, what it passed on, its orders, fills and holdings — board
   by board as missed / walked into / turned under it / got right. It is a
   diagnosis of the day, not tomorrow's shortlist.
3. Rewrite its handbook (``handbook``) from every trade review **and** from
   those diagnoses and its exposure against the market. A rewrite
   that only reads losses learns only to stay out; the 2026-01 replay with a
   handbook did exactly that (18 buys → 9, exposure 6.7% → 3.2%, in a market
   up 6.8%).

Replay and live share this; they differ only in how the day's facts, the
exposure line and the "what did I do with each board" map are assembled.
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)


async def review_day(conn: sqlite3.Connection, hist: sqlite3.Connection, *,
                     trader_id: str, trader, day: str, model, facts_text: str,
                     exposure_text: str = "", record_text: str = "",
                     handbook_before: str | None = None,
                     review_market: bool = True) -> dict:
    """Run all three steps for one trader. Never raises; returns counts."""
    from alpha_agents.evolution import handbook, market_review, trade_review

    counts = {"trade_reviews": 0, "market_review": 0, "handbook": 0}
    try:
        counts["trade_reviews"] = await trade_review.review_closed(
            conn, hist, trader_id=trader_id, as_of=day, model=model,
            trader=trader, handbook_before=handbook_before)
    except Exception as e:                            # noqa: BLE001
        logger.warning("%s: trade reviews failed for %s: %s", day, trader_id, e)

    if review_market and facts_text:
        context = "\n\n".join(x for x in (
            exposure_text,
            handbook.load(trader_id, before=handbook_before)) if x)
        if await market_review.write(conn, trader_id=trader_id, date=day,
                                     facts=facts_text, model=model,
                                     record=record_text, context=context):
            counts["market_review"] = 1

    opportunity = "\n".join(x for x in (
        exposure_text, market_review.missed(conn, trader_id, up_to=day)) if x)
    if counts["trade_reviews"] or counts["market_review"]:
        if await handbook.consolidate(conn, trader_id, as_of=day, model=model,
                                      trader=trader, opportunity=opportunity):
            counts["handbook"] = 1
    return counts
