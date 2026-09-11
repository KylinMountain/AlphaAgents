"""The trading kernel's clock, and the two look-ahead guards built on it.

Most of the kernel already takes "today" as a parameter —
``check_pending_orders(today=…)``, ``check_positions(today=…)``,
``_fill_order(fill_date=…)``. What it also had was three places that
quietly reached for the wall clock instead, and under
``evolution.replay_mode`` those three dated a March decision in
September: the settlement lot an add creates, the exit a close books,
and the order date an intent defaults to. A replay that books its exits
months after the day it is simulating is not a replay — its T+1 window
is wrong, its proceeds settle on the wrong side of the run, and the
``settle_date`` it writes can never be reached by later replay days.

``today()`` is the single answer to "what day is it, for the kernel".

Deliberately **not** ``effective_eod_cut_date``: that function answers
"which end-of-day price data am I allowed to see", and rolls a
pre-close instant back to T-1. The kernel asks a different question. An
order placed at 06:30 on 3/20 is dated 3/20, not 3/19; the T+1 lot it
creates settles on 3/21. Feeding the data cut in here would date every
pre-open fill one day early and desynchronise every lot from the order
that produced it.

The two guards exist because "we recorded the cutoff" and "the cutoff
means something" are different claims, and until S6 the repository only
had the first. ``information_cutoff`` was written faithfully and then
never read by anything, so a decision that cited information from after
the instant it was made cost nothing. Both guards compare *dates*
(the first ten characters), which is the granularity the kernel works
at: an order's date, a fill's date, a lot's settle date.
"""

from __future__ import annotations

from datetime import datetime

__all__ = ["LookAheadError", "assert_decided_no_later_than",
           "assert_not_from_the_future", "guard_fill", "today"]


class LookAheadError(ValueError):
    """A write dated after the clock, or acting on information it cannot have.

    A look-ahead is a *system fault*, not a business decision — the
    distinction the intent layer draws for ``submit_intent``. A refusal
    is recorded and returned; this is raised.
    """


def today() -> str:
    """The kernel's calendar date, replay-aware.

    Live mode: the machine's local date, matching what the ledger has
    always written. Replay mode: the date part of the replay instant, so
    a walk-forward run dates its fills, its T+1 lots and its pending
    proceeds inside the replayed window.
    """
    # Late import, the way the other 60-odd replay-aware readers do it:
    # replay_mode is cross-cutting (registered in lint_harness's
    # CROSS_CUTTING_MODULES) so this is not a layering violation, but a
    # module-scope import here would pull the evolution package into
    # every `import alpha_agents.data.portfolio`.
    from alpha_agents.evolution.replay_mode import get_replay_as_of

    as_of = get_replay_as_of()
    if as_of:
        return str(as_of)[:10]
    return datetime.now().strftime("%Y-%m-%d")


def _date_of(stamp: str) -> str:
    """The date part of a date or timestamp string."""
    return str(stamp)[:10]


def assert_not_from_the_future(stamp: str, *, what: str) -> None:
    """Refuse a write dated after the kernel clock.

    The book may not contain a fill, an exit or a lot dated in the
    future of the world it is being simulated in. In live mode this
    catches a caller that computed the wrong date; under replay it
    catches the kernel reaching for the wall clock, which is the bug
    this module was written to make impossible.
    """
    clock = today()
    if _date_of(stamp) > clock:
        raise LookAheadError(
            f"{what} is dated {_date_of(stamp)}, after the kernel clock "
            f"{clock}"
            + (f" (replay as-of {clock})" if _is_replay() else "")
            + ". The kernel may not write a row the world has not reached.")


def assert_decided_no_later_than(information_cutoff: str, stamp: str, *,
                                 what: str) -> None:
    """Refuse an act that claims information from after it happened.

    ``information_cutoff`` is the latest instant the decision was allowed
    to see. Acting on ``stamp`` — filling an order, booking an exit —
    while citing a cutoff *later* than that act means the decision used
    the future: the declared basis is not available at the moment of the
    trade.

    The direction matters and is easy to get backwards. A cutoff earlier
    than the act is the normal case (decide on yesterday's close, act
    today); a cutoff *later* than the act is the impossible one.
    """
    if _date_of(information_cutoff) > _date_of(stamp):
        raise LookAheadError(
            f"{what} happened on {_date_of(stamp)} but cites information as "
            f"of {_date_of(information_cutoff)}. A decision cannot act on "
            "information that postdates the act.")


def _is_replay() -> bool:
    from alpha_agents.evolution.replay_mode import is_replay_active
    return is_replay_active()


def guard_fill(fill_date: str, *, order_id: int, conn) -> None:
    """The two time checks an order fill must pass, as one policy.

    Kept together because they answer one question — "may this fill
    happen, on this date, on this evidence?" — and because the fill path
    reads better for it: ``_fill_order`` is about capital and sizing, and
    the time boundary is a precondition rather than a step in it.

    Both raise. The caller must not catch ``LookAheadError`` in the same
    handler that tolerates a missing audit: a boundary that cannot be
    written is a gap in the record, but a boundary dated in the future is
    the record being wrong, and demoting it to a warning is how a leak
    reaches the learning data with every log line still healthy.
    """
    assert_not_from_the_future(fill_date, what="order fill date")
    # Late import: ``intent`` reads the boundary this module guards.
    from alpha_agents.data import intent
    # What the decision behind this order was allowed to know. None means
    # the order pre-dates the intents table or was written outside the
    # door — unknown is not unbounded, so there is no claim to check.
    cutoff = intent.cutoff_for_order(conn, order_id)
    if cutoff:
        assert_decided_no_later_than(
            cutoff, fill_date, what=f"fill of order #{order_id}")
