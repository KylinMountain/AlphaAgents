"""What a daily bar can and cannot settle about a T+1 order.

The reproduction plan restricts execution to the open, and this module is the
reason: a daily bar gives ``O H L C`` and no order *between* them. Given
``open=10.30, high=10.50, low=9.40`` with an entry at 10.00 and a stop at 9.50,
both levels were touched and nothing in the data says whether the entry came
first or the stop did. Any answer would be an invented minute path.

So the model is deliberately narrow, and it fails towards ``ambiguous``:

* **market on open** — settles at the open, unless the open *is* the limit, in
  which case a buy has no counterparty (一字板, the case A-share backtests get
  wrong most expensively).
* **limit against the open** — settles from open and low/high alone, which *is*
  sufficient for a single resting order: a buy limit at 10.00 fills when the low
  reaches it, and at the better of the open and the limit.
* **two levels in one session** — ``ambiguous`` when both were touched, because
  that is exactly the question a bar cannot answer. The plan records these as
  ``intraday_ambiguous`` rather than resolving them, and the count is reported.

The future cannot be read because it is not representable
--------------------------------------------------------
Capacity is not an argument this module can take from the day it fills: 
:class:`DayBar` **has no volume field**. Sizing reads
:func:`capacity_shares`, which reads :func:`average_daily_volume` of the
sessions *before* the fill — the ADV20 the plan specifies. Using the fill day's
own volume to decide whether the fill happened would be execution-side
look-ahead, and the cheapest way to prevent it is for the type to make it
unspellable rather than for a docstring to forbid it. The day's volume belongs
in a **post-hoc liquidity report**, which is a different function with a
different input.
"""

from __future__ import annotations

from dataclasses import dataclass

#: A-share board lot. Purchases are whole lots, so a capacity cap floors to one.
LOT_SIZE = 100

#: How much of ADV20 a single order may take. Declared, not tuned: the walk must
#: state the assumption it is making rather than inherit an implicit one.
DEFAULT_PARTICIPATION = 0.10

FILLED = "filled"
NO_FILL = "no_fill"
AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class DayBar:
    """One session, without volume — see the module docstring.

    ``volume`` is absent on purpose: the open-time fill decision may not read the
    session it is filling in, and a field that cannot be passed cannot be read.
    """

    date: str
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class Fill:
    status: str
    price: float | None
    reason: str


def _reaches_up(price: float, bound: float | None) -> bool:
    """Whether ``price`` has climbed to ``bound``, to the cent."""
    return bound is not None and round(price, 2) >= round(bound, 2)


def _reaches_down(price: float, bound: float | None) -> bool:
    """Whether ``price`` has fallen to ``bound``, to the cent.

    The direction matters and is easy to get wrong: the *same* comparison
    direction for both sides refuses nearly every sell, because an ordinary open
    is at or above the down limit. A test caught exactly that here.
    """
    return bound is not None and round(price, 2) <= round(bound, 2)


def market_on_open(bar: DayBar, *, side: str, prev_close: float | None = None,
                   limit_pct: float | None = None,
                   limit_rule: str = "") -> Fill:
    """A market order at the open, refused when the open is a one-way limit.

    ``side`` is ``"buy"`` or ``"sell"``. ``limit_rule`` is carried into the
    refusal's reason so the operator can see which rule decided it — the caller
    gets it from :func:`alpha_agents.data.market_rules.market_rules`.
    """
    if side not in ("buy", "sell"):
        raise ValueError(f"side must be 'buy' or 'sell', not {side!r}")
    if prev_close is not None and limit_pct is not None:
        if side == "buy" and _reaches_up(bar.open, prev_close * (1 + limit_pct)):
            return Fill(NO_FILL, None,
                        f"open is the up limit ({limit_rule}) — no counterparty "
                        "for a buy, so an untraded day is not a fill")
        if side == "sell" and _reaches_down(bar.open, prev_close * (1 - limit_pct)):
            return Fill(NO_FILL, None,
                        f"open is the down limit ({limit_rule}) — no counterparty "
                        "for a sell")
    return Fill(FILLED, bar.open, "market order at the open")


def limit_on_open(bar: DayBar, limit_price: float, *, side: str) -> Fill:
    """A resting limit order, settled from open and low/high alone.

    This is decidable for a **single** order: a buy limit at ``p`` fills when the
    session's low reaches ``p``, and it fills at the better of the open and
    ``p``, because a resting bid above the opening price is satisfied by the
    auction. What is *not* decidable is the order of two levels — see
    :func:`two_levels`.
    """
    if side not in ("buy", "sell"):
        raise ValueError(f"side must be 'buy' or 'sell', not {side!r}")
    if limit_price <= 0:
        raise ValueError(f"limit price must be positive, not {limit_price}")
    if side == "buy":
        if bar.low > limit_price:
            return Fill(NO_FILL, None,
                        f"low {bar.low} never reached the buy limit {limit_price}")
        return Fill(FILLED, min(bar.open, limit_price),
                    "buy limit met: fills at the better of open and limit")
    if bar.high < limit_price:
        return Fill(NO_FILL, None,
                    f"high {bar.high} never reached the sell limit {limit_price}")
    return Fill(FILLED, max(bar.open, limit_price),
                "sell limit met: fills at the better of open and limit")


def two_levels(bar: DayBar, *, lower: float, upper: float) -> Fill:
    """A session that has to satisfy two levels, or refuse to guess.

    ``lower`` is the level a fall from above would meet (a protective stop for a
    long), ``upper`` the one a rise would meet (a target). When both are touched
    the bar cannot say which came first, and the plan's rule is to record it as
    ``intraday_ambiguous`` — the reviewer's example verbatim: ``open=10.30,
    high=10.50, low=9.40`` with an entry at 10.00 and a stop at 9.50 is *not* a
    finished round trip, it is an unanswered question.
    """
    if lower >= upper:
        raise ValueError(f"lower {lower} must be below upper {upper}")
    hit_lower, hit_upper = bar.low <= lower, bar.high >= upper
    if hit_lower and hit_upper:
        return Fill(AMBIGUOUS, None,
                    f"both {lower} and {upper} were touched; a daily bar does not "
                    "order them, so neither outcome is claimed")
    if hit_upper:
        return Fill(FILLED, upper, f"only the upper level {upper} was touched")
    if hit_lower:
        return Fill(FILLED, lower, f"only the lower level {lower} was touched")
    return Fill(NO_FILL, None,
                f"neither {lower} nor {upper} was touched")


def average_daily_volume(prior_volumes: list[float], window: int = 20) -> float | None:
    """ADV over the ``window`` sessions **before** the fill, or ``None``.

    ``None`` rather than a shorter average when there is not enough history:
    the caller must not be handed a proxy for a number it asked for precisely
    because the honest one was unavailable.
    """
    if window <= 0:
        raise ValueError(f"window must be positive, not {window}")
    if len(prior_volumes) < window:
        return None
    recent = prior_volumes[-window:]
    return sum(recent) / window


def capacity_shares(adv20: float | None, *,
                    participation: float = DEFAULT_PARTICIPATION,
                    lot_size: int = LOT_SIZE) -> int:
    """Whole lots a single order may take, from **ADV20 and nothing else**.

    The fill day's own volume is not a parameter and must not become one: a fill
    at the open cannot depend on a total that is only known at the close.
    """
    if participation <= 0 or participation > 1:
        raise ValueError(f"participation must be in (0, 1], not {participation}")
    if lot_size <= 0:
        raise ValueError(f"lot_size must be positive, not {lot_size}")
    if adv20 is None or adv20 <= 0:
        return 0
    return int(adv20 * participation) // lot_size * lot_size
