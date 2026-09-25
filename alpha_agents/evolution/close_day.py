"""The trader's close of day, in the order a trader does it.

1. Review each trade it closed (``trade_review``) — with the legacy handbook
   that was in force while it held them.
2. Review every persisted TraderDecision and quarantine any concrete
   LessonCandidate; WAIT/HOLD/REJECT are reviewed even without a trade.
3. Review the day (``market_review``): the market's numbers beside the
   trader's own record of the day (``day_record``) — what it chose at the
   open and why, what it passed on, its orders, fills and holdings — board
   by board as missed / walked into / turned under it / got right. It is a
   diagnosis of the day, not tomorrow's shortlist.
4. Stop at evidence. A close review may create LessonCandidates, but it does
   **not** rewrite an active handbook/rule. Candidate→Lesson→Rule is a separate
   evidence pipeline, so one review can no longer become a permanent veto.

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
    """Return completion counts; temporal-integrity faults propagate."""
    from alpha_agents.evolution import handbook, market_review, trade_review
    from alpha_agents.evolution import trader_review
    from alpha_agents.evolution import trader_learning

    # The *_failed counts are what a replay's report checks before it shows a
    # number: a day whose review timed out is a day the trader did not learn.
    counts = {
        "trade_reviews": 0, "market_review": 0,
        # Retained for report/backward compatibility. New close-day processing
        # never rewrites the legacy handbook.
        "handbook": 0, "handbook_failed": 0,
        "decision_reviews": 0, "lesson_candidates": 0,
        "lessons_created": 0, "rules_created": 0,
        "market_review_failed": 0,
    }
    review_stats = {}
    try:
        counts["trade_reviews"] = await trade_review.review_closed(
            conn, hist, trader_id=trader_id, as_of=day, model=model,
            trader=trader, handbook_before=handbook_before, stats=review_stats)
    except Exception as e:                            # noqa: BLE001
        from alpha_agents.data.clock import LookAheadError
        if isinstance(e, LookAheadError):
            raise
        review_stats["trade_review_failed"] = review_stats.get("trade_review_failed", 0) + 1
        logger.warning("%s: trade reviews failed for %s: %s", day, trader_id, e)
    counts.update(review_stats)

    try:
        got = await trader_review.review_decisions(
            conn,
            trader_id=trader_id,
            day=day,
            model=model,
            facts=facts_text,
            record=record_text,
        )
        counts["decision_reviews"] += got["decision_reviews"]
        counts["lesson_candidates"] += got["lesson_candidates"]
        counts["lesson_candidates"] += trader_review.lessons_from_trade_reviews(
            conn, trader_id=trader_id, day=day)
    except Exception as e:                            # noqa: BLE001
        from alpha_agents.data.clock import LookAheadError
        if isinstance(e, LookAheadError):
            raise
        logger.warning("%s: Trader decision review failed for %s: %s",
                       day, trader_id, e)

    if review_market and facts_text:
        context = "\n\n".join(x for x in (
            exposure_text,
            handbook.load(trader_id, before=handbook_before)) if x)
        if await market_review.write(conn, trader_id=trader_id, date=day,
                                     facts=facts_text, model=model,
                                     record=record_text, context=context):
            counts["market_review"] = 1
            counts["lesson_candidates"] += (
                trader_review.lessons_from_market_review(
                    conn, trader_id=trader_id, day=day))
        elif model is not None:
            # write() returns None without a model or facts; both are ruled
            # out here, so None is an error or an unreadable reply.
            counts["market_review_failed"] = 1

    try:
        learned = trader_learning.advance(
            trader_id=trader_id, as_of=day, conn=conn)
        counts["lessons_created"] += learned["lessons_created"]
        counts["rules_created"] += learned["rules_created"]
    except Exception as e:                            # noqa: BLE001
        logger.warning("%s: Trader learning advance failed for %s: %s",
                       day, trader_id, e)

    # The legacy handbook is intentionally read-only here. One review is
    # evidence, not authority. T7 aggregates candidates into Lessons/Rules
    # only after repeated support/counterevidence checks.
    return counts
