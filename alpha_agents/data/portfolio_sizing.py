"""How big a position may be, and how much of the book may be exposed.

Split out of ``portfolio`` when the S5 intent wrappers pushed that file
past the 1200-line ceiling. The seam is real rather than cosmetic:
everything here answers a *sizing* question and nothing here writes a
row. ``portfolio`` decides whether an action happens; this module decides
how much of it happens.

None of these read the database directly — they read configuration
(``trader``), a late-imported thesis, or the sentiment cycle — so no
caller's connection patching is affected by living here.
"""

from __future__ import annotations

import logging

from alpha_agents.data.portfolio_exit import LOT_SIZE
from alpha_agents.data.trader import DEFAULT_TRADER

logger = logging.getLogger(__name__)


def _calc_shares(price: float, max_amount: float) -> int:
    """How many shares to buy, rounded down to whole lots.

    Returns 0 when even one lot is unaffordable, which callers read as
    "refused for lack of capital" rather than as a zero-size order.
    """
    if price <= 0:
        return 0
    max_shares = int(max_amount / price)
    lots = max_shares // LOT_SIZE
    return lots * LOT_SIZE


def _trader_pct(trader_id: str, field: str, fallback: float) -> float:
    """One sizing parameter off the trader's config, or the global default.

    Falls back rather than raising for the same reason ``get_trader``
    does: a position whose config file was deleted still has to be
    managed with *some* number.
    """
    if trader_id == DEFAULT_TRADER:
        return fallback
    try:
        from alpha_agents.data.trader import get_trader
        return float(getattr(get_trader(trader_id), field, fallback))
    except Exception as e:
        logger.debug("Trader %s %s fallback: %s", trader_id, field, e)
        return fallback


def _wanted_pct(code: str, trader_id: str = DEFAULT_TRADER) -> float:
    """How much of the book this idea asked for, as a fraction.

    The thesis states it. A pick that says nothing falls back to the
    trader's own default, which keeps behaviour unchanged for anything
    written before sizing was a decision.
    """
    try:
        from alpha_agents.data.thesis import get_active
        theses = [t for t in get_active(code=code, trader_id=trader_id)
                  if t.position_id is None]
        if theses and theses[-1].size_pct:
            return max(0.005, min(1.0, theses[-1].size_pct))
    except Exception as e:
        logger.debug("Size lookup fallback for %s: %s", code, e)
    from alpha_agents.data.portfolio import DEFAULT_POSITION_PCT
    return _trader_pct(trader_id, "default_size_pct", DEFAULT_POSITION_PCT)


def get_sentiment_exposure_limit(trader_id: str = DEFAULT_TRADER) -> float:
    """Max total exposure for one trader, given the sentiment phase.

    The phase is a fact about the market and is shared; the money it
    applies to is the trader's own.
    """
    from alpha_agents.data.portfolio import trader_capital
    capital = trader_capital(trader_id)
    try:
        from alpha_agents.data.sentiment_cycle import get_sentiment_cycle
        cycle = get_sentiment_cycle()
        phase = cycle.get("phase", "修复")
        pct = cycle["strategy"]["max_exposure_pct"]
        max_invest = capital * pct / 100
        logger.debug("Sentiment cycle: %s → max %.0f元 (%.0f%%)",
                     phase, max_invest, pct)
        return max_invest
    except Exception as e:
        logger.warning("Sentiment cycle failed, defaulting to 50%%: %s", e)
        return capital * 0.50