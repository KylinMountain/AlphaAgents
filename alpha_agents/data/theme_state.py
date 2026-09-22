"""The theme's state, not the theme's flow on one day.

Why this exists. ``theme_flow_negative`` and ``theme_rank_worse_than`` ask
state questions — "is my theme still attracting money", "is it still a
leading theme" — and both were handed a single session's number. Measured
over 2026-01-05..03-31 (56 sessions, ~385 concepts a day, ranked the way the
replay ranks):

- The day-over-day Spearman of the whole concept flow ranking is **−0.004**.
  Not a weak signal; none. A concept that led today is no more likely than
  chance to lead tomorrow.
- So ``theme_flow_negative`` on a single day's net fired on the next session
  **65.8%** of the time and at least once within three sessions **99.2%** of
  the time — while riding on 34 of 34 theses a replay wrote. Every position
  carried a near-certain exit that said nothing about the position.

Accumulating the same quantity over five sessions restores the state it was
supposed to measure: Spearman +0.899, and the flow condition drops to 32.4%
on the next session. Five because it matches the traders' 3–5 day horizons
and because three (+0.677) is not yet stable, while ten and above start
lagging the rotation they are meant to track.

**This feeds the thesis evaluator only.** Selection keeps reading whatever it
reads: the direction table already works in 5-day relative returns and 5-day
breadth change, and changing both at once would make neither measurable.

**What five sessions does not fix.** Rank is a position among competitors, so
it moves when others move. A theme in the top 5 leaves the top 50 within five
sessions 96.9% of the time even on the smoothed rank — leadership rotates,
and no threshold or window escapes that. ``theme_rank_worse_than`` measures
rotation, not deterioration; the vocabulary now says so in the numbers.
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)

#: Sessions accumulated before ranking. See the module docstring for the
#: measurement this comes from; it is not a tuning knob to sweep.
WINDOW_SESSIONS = 5

_YUAN_TO_YI = 1e8


def _sessions_ending(conn: sqlite3.Connection, as_of: str, window: int) -> list[str]:
    """The last ``window`` trade dates at or before ``as_of`` (YYYYMMDD).

    Read from the data rather than from a calendar so holidays and suspended
    sessions need no special case, and never past ``as_of`` — a replay asking
    for a window must not be handed tomorrow.
    """
    rows = conn.execute(
        "SELECT DISTINCT trade_date FROM stock_fund_flow_daily "
        "WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT ?",
        (as_of, window)).fetchall()
    return [r[0] for r in rows]


def concept_state(as_of: str, *, window: int = WINDOW_SESSIONS
                  ) -> tuple[dict[str, int], dict[str, float]]:
    """``(rank by cumulative flow, cumulative flow in 亿)`` as of a session.

    ``as_of`` is ISO ``YYYY-MM-DD``. Rank is 1-based over every concept the
    window could price. Returns two empty dicts when the window cannot be
    read: a condition whose input is missing must not fire, and an empty
    mapping is how ``thesis.MarketView`` is told "not measured this cycle".
    """
    from alpha_agents.config import DATA_DIR, DB_PATH
    from alpha_agents.data.sector_flow_synth import synth_concept_flow

    compact = as_of.replace("-", "")
    totals: dict[str, float] = {}
    stocks = snaps = hist = None
    try:
        stocks = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        snaps = sqlite3.connect(f"file:{DATA_DIR / 'market_snapshots.db'}?mode=ro",
                                uri=True)
        hist = sqlite3.connect(f"file:{DATA_DIR / 'market_history.db'}?mode=ro",
                               uri=True)
        for conn in (stocks, snaps, hist):
            conn.row_factory = sqlite3.Row
        sessions = _sessions_ending(snaps, compact, window)
        if not sessions:
            logger.warning("theme state: no sessions at or before %s", as_of)
            return {}, {}
        for session in sessions:
            for row in synth_concept_flow(stocks, snaps, hist, session):
                totals[row["name"]] = totals.get(row["name"], 0.0) + row["main_net"]
    except Exception as exc:                          # noqa: BLE001
        # Returned empty, not raised: the callers treat "could not measure" as
        # "do not fire", and a failed read must not close a position.
        logger.warning("theme state unavailable at %s: %s", as_of, exc)
        return {}, {}
    finally:
        for conn in (stocks, snaps, hist):
            if conn is not None:
                conn.close()

    order = sorted(totals, key=lambda name: -totals[name])
    ranks = {name: i + 1 for i, name in enumerate(order)}
    flows = {name: round(totals[name] / _YUAN_TO_YI, 4) for name in totals}
    logger.info("theme state %s: %d concepts over %d session(s), leader %s",
                as_of, len(ranks), len(sessions), order[0] if order else "-")
    return ranks, flows
