"""Settling one day's orders against one day's bar, T+1 and open-only.

The plan's §4 table, as code. What a single daily bar can settle, and what it
must refuse to:

* **the open decides.** A buy fills at the open or it does not fill. This is
  narrower than :func:`alpha_agents.data.t1_execution.limit_on_open`, which
  argues a *single* resting limit is decidable from the session's low. M1
  declines that argument on purpose: the level it would fill at
  (``min(open, limit)``) is a price the bar cannot prove the order was resting
  *before*, and a zone order also carries a stop — one bar does not order two
  levels. Decline is cheap (it under-simulates); accepting is how a backtest
  invents a path. ``test_a_touch_the_open_missed_is_ambiguous_not_a_fill``
  pins the choice so a later reader cannot "fix" it without the conversation.
* **one-way limits are refused, and this is the expensive one.**
  ``market_on_open`` answers it; an open that *is* the up limit has no
  counterparty for a buy, so an untraded day is not a fill.
* **suspended is not a fill and not a cancel.** No bar means the instrument did
  not trade. The order keeps waiting.
* **T+1 is one date comparison** (:func:`may_sell`) and it lives here rather
  than in the caller so it cannot be forgotten by a second caller.

What it will not do
-------------------
It never returns a price that is not the open or a level the order itself
declared. There is no "we would have got a better fill" branch. And it will not
settle at all when it cannot determine the day's limits: a missing
``prev_close`` or an unmodelled board comes back ``UNDECIDABLE``, because a
limit check that silently does not happen is indistinguishable from a limit
check that passed — the failure this repository keeps writing modules against.

Capacity is deliberately absent from this module. It is **ADV20 from before the
fill** (see ``t1_execution``), which is known at decision time, not at settlement
time; the runner applies it where the decision is made.
"""

from __future__ import annotations

from dataclasses import dataclass

from alpha_agents.data.market_rules import MarketRule
from alpha_agents.data.t1_execution import (
    DayBar,
    in_entry_zone,
    market_on_open,
    two_levels,
)

#: The bar says the order would have filled at the open. The only fill status.
FILLED_AT_OPEN = "filled_at_open"

#: The session's range never reached the order. It waits, and it may expire.
NO_FILL = "no_fill"

#: The range overlapped the order but the open did not — a path question. The
#: plan's own word for it, and the count every report has to carry.
INTRADAY_AMBIGUOUS = "intraday_ambiguous"

#: The open was a one-way limit against this side, so there was no counterparty.
LIMIT_BLOCKED = "limit_blocked"

#: No bar for the day: the instrument did not trade.
SUSPENDED = "suspended"

#: The day's limits could not be determined, so no fill is claimed.
UNDECIDABLE = "undecidable"

#: Every status this module can return, so a report can iterate rather than
#: hand-maintain a list that drifts from the code.
ALL_STATUSES = (FILLED_AT_OPEN, NO_FILL, INTRADAY_AMBIGUOUS, LIMIT_BLOCKED,
                SUSPENDED, UNDECIDABLE)

#: Statuses that mean "the order is still alive and untraded".
STILL_WAITING = frozenset({NO_FILL, INTRADAY_AMBIGUOUS, LIMIT_BLOCKED, SUSPENDED,
                           UNDECIDABLE})


@dataclass(frozen=True)
class Verdict:
    """What one bar says about one order. ``price`` is set only when it filled."""

    status: str
    price: float | None
    reason: str


def bar_from_row(row: dict) -> DayBar | None:
    """A corpus row into a :class:`DayBar`, or ``None`` when it cannot be one.

    ``None`` rather than a zero-filled bar: a row with a missing or non-positive
    open is not a session anybody traded, and a bar of zeros would price a fill
    at 0.00 and look like data.
    """
    if not row:
        return None
    try:
        o = float(row["open"])
        h = float(row["high"])
        low = float(row["low"])
        c = float(row["close"])
    except (KeyError, TypeError, ValueError):
        return None
    if min(o, h, low, c) <= 0:
        return None
    return DayBar(date=str(row["date"]), open=o, high=h, low=low, close=c)


def may_sell(position_open_date: str | None, day: str) -> bool:
    """T+1: shares bought on a day may not be sold on that same day.

    A plain date comparison, and ``None`` — a position with no recorded open
    date — answers **False**. An unknown birth date is not a licence to sell
    today; the same missing-case discipline the promotion gate uses.
    """
    if not position_open_date:
        return False
    return str(position_open_date)[:10] < str(day)[:10]


def _limit_is_checkable(prev_close: float | None, rule: MarketRule | None) -> bool:
    """Whether the day's limits can be applied at all.

    ``rule.price_limit_pct is None`` **is** checkable — it means the instrument
    is genuinely uncapped that day (``market_rules`` returns that for a ChiNext
    listing's first five sessions), so an open always has a counterparty. What
    is not checkable is a missing rule or a missing reference price.
    """
    if rule is None or prev_close is None:
        return False
    return float(prev_close) > 0


def entry_verdict(
    *,
    entry_low: float | None,
    entry_high: float | None,
    bar: DayBar | None,
    prev_close: float | None,
    rule: MarketRule | None,
) -> Verdict:
    """May this resting **buy** fill today, at today's open?

    Entries are buys and only buys: ``intent.OPEN`` has no sell path, so there
    is no ``side`` argument to get wrong. If a sell-side entry is ever added,
    this function does not silently cover it.

    The zone is read the way ``check_pending_orders`` reads it — both bounds, an
    upper bound alone, a lower bound alone, or neither (a market order) — because
    the runner hands the same order to both and two readings of one zone is how
    the entry rule and the settlement rule drift apart.
    """
    if bar is None:
        return Verdict(SUSPENDED, None,
                       "no bar for the day: the instrument did not trade")
    if not _limit_is_checkable(prev_close, rule):
        return Verdict(UNDECIDABLE, None,
                       "the day's price limit could not be determined "
                       "(no rule or no previous close), so no fill is claimed")

    refused = market_on_open(bar, side="buy", prev_close=prev_close,
                             limit_pct=rule.price_limit_pct,
                             limit_rule=rule.reason)
    if refused.status != "filled":
        return Verdict(LIMIT_BLOCKED, None, refused.reason)

    if in_entry_zone(bar.open, entry_low, entry_high):
        return Verdict(FILLED_AT_OPEN, bar.open,
                       "the open is inside the entry zone")
    if _range_overlaps_zone(bar, entry_low, entry_high):
        return Verdict(
            INTRADAY_AMBIGUOUS, None,
            f"the open {bar.open} is outside the entry zone but the session's "
            f"range [{bar.low}, {bar.high}] overlaps it; a daily bar does not "
            "say whether the order was touched, so no fill is claimed")
    return Verdict(NO_FILL, None,
                   f"the session's range [{bar.low}, {bar.high}] never reached "
                   "the entry zone")


def exit_verdict(
    *,
    bar: DayBar | None,
    prev_close: float | None,
    rule: MarketRule | None,
    stop_loss: float | None,
    target_price: float | None,
) -> Verdict:
    """May this **sell** settle today, and at what price?

    The caller must have already asked :func:`may_sell`. This function does not
    know when the position was opened, and that is the caller's T+1 obligation —
    stated here rather than assumed, because it is the one rule that silently
    turns a compliant backtest into a same-day round trip.

    A gap is settled at the open rather than at the level:

    * ``open <= stop`` — a stop is a market order once triggered, so it fills at
      the market, which on a gap down is worse than the stop. Filling at the
      stop would be the optimism this module exists to refuse.
    * ``open >= target`` — a resting sell limit fills at ``max(open, limit)``
      (``limit_on_open``'s own rule for the single-limit case), so the open is
      correct here and the *better* price is the honest one.

    Only when neither gap applies do two levels in one session become
    ``intraday_ambiguous``.
    """
    if bar is None:
        return Verdict(SUSPENDED, None,
                       "no bar for the day: the instrument did not trade")
    if not _limit_is_checkable(prev_close, rule):
        return Verdict(UNDECIDABLE, None,
                       "the day's price limit could not be determined "
                       "(no rule or no previous close), so no exit is claimed")

    refused = market_on_open(bar, side="sell", prev_close=prev_close,
                             limit_pct=rule.price_limit_pct,
                             limit_rule=rule.reason)
    if refused.status != "filled":
        return Verdict(LIMIT_BLOCKED, None, refused.reason)

    has_stop = stop_loss is not None and stop_loss > 0
    has_target = target_price is not None and target_price > 0
    if not has_stop and not has_target:
        return Verdict(NO_FILL, None, "the position declares no stop and no target")

    if has_stop and bar.open <= stop_loss:
        return Verdict(FILLED_AT_OPEN, bar.open,
                       f"the open {bar.open} gapped through the stop "
                       f"{stop_loss}; a triggered stop fills at the market")
    if has_target and bar.open >= target_price:
        return Verdict(FILLED_AT_OPEN, bar.open,
                       f"the open {bar.open} is through the target "
                       f"{target_price}; a resting sell limit fills at the "
                       "better of the two")

    if has_stop and has_target:
        got = two_levels(bar, lower=stop_loss, upper=target_price)
        if got.status == "ambiguous":
            return Verdict(INTRADAY_AMBIGUOUS, None, got.reason)
        if got.status == "filled":
            return Verdict(FILLED_AT_OPEN, got.price, got.reason)
        return Verdict(NO_FILL, None, got.reason)

    if has_stop:
        if bar.low <= stop_loss:
            return Verdict(FILLED_AT_OPEN, stop_loss,
                           f"the session reached the stop {stop_loss}")
        return Verdict(NO_FILL, None,
                       f"the session's low {bar.low} never reached the stop "
                       f"{stop_loss}")
    if bar.high >= target_price:
        return Verdict(FILLED_AT_OPEN, target_price,
                       f"the session reached the target {target_price}")
    return Verdict(NO_FILL, None,
                   f"the session's high {bar.high} never reached the target "
                   f"{target_price}")


def _range_overlaps_zone(bar: DayBar, low: float | None,
                         high: float | None) -> bool:
    """Whether the session's range crossed the order without settling it.

    Answered from ``high``/``low`` only, and used **only** to decide whether to
    report an ambiguity — never to produce a price.
    """
    if low is not None and high is not None:
        return bar.low <= high and bar.high >= low
    if high is not None:
        return bar.low <= high
    if low is not None:
        return bar.high >= low
    return False
