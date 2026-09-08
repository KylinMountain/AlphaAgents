"""Which markets are worth analysing right now.

The news monitor had no session gate at all: a fixed-interval loop that
called both the stock and the futures agent whenever the digest found
events. News flows around the clock — overseas sessions, policy, futures
— so it produced A-share sector calls at 18:29 from a closing snapshot,
for a market that would not open for fifteen hours. Twenty-eight reports
in three hours, most of them the same closed-market analysis rewritten,
each one two agent runs of tokens.

Outside a session the flashes are still worth *having*: the morning scan
digests the accumulated window. They are not worth *analysing*, and the
distinction is the whole point of this module.

Futures trade a night session (21:00–02:30) that A-shares do not. It is
off by default because most users of this system watch A-shares; set
FUTURES_NIGHT_SESSION=1 to analyse it.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, time

logger = logging.getLogger(__name__)

STOCK = "stock"
FUTURES = "futures"

# A-share continuous trading. The 11:30–13:00 lunch break is excluded:
# prices do not move, so an analysis there is the 11:30 one again.
_STOCK_SESSIONS = ((time(9, 30), time(11, 30)), (time(13, 0), time(15, 0)))

# Domestic futures day session runs alongside equities and a little past
# the close; the night session wraps past midnight.
_FUTURES_DAY = ((time(9, 0), time(11, 30)), (time(13, 30), time(15, 0)))
_FUTURES_NIGHT_START = time(21, 0)
_FUTURES_NIGHT_END = time(2, 30)


def _in_any(now: time, windows) -> bool:
    return any(start <= now <= end for start, end in windows)


def futures_night_enabled() -> bool:
    """Whether the 21:00–02:30 futures session is analysed.

    Read per call rather than captured at import: a long-running
    scheduler should pick up the setting on restart of the process, not
    of the module's first import somewhere unrelated.
    """
    return os.environ.get("FUTURES_NIGHT_SESSION", "").strip().lower() in (
        "1", "true", "yes", "on")


def analysis_targets(now: datetime | None = None,
                     is_trading_day: bool | None = None) -> set[str]:
    """Markets to run agents for at ``now``. Empty means ingest only.

    ``is_trading_day`` is passed in rather than looked up: the caller
    already knows (the scheduler holds a calendar), and a holiday lookup
    inside a hot loop would hit the network.
    """
    now = now or datetime.now()

    if is_trading_day is None:
        is_trading_day = now.weekday() < 5

    clock = now.time()
    targets: set[str] = set()

    if is_trading_day:
        if _in_any(clock, _STOCK_SESSIONS):
            targets.add(STOCK)
        if _in_any(clock, _FUTURES_DAY):
            targets.add(FUTURES)

    # The night session belongs to the evening of a trading day and the
    # small hours of the next calendar day, which may itself be a
    # weekend morning — Friday night runs into Saturday.
    if futures_night_enabled():
        if clock >= _FUTURES_NIGHT_START and is_trading_day:
            targets.add(FUTURES)
        elif clock <= _FUTURES_NIGHT_END and now.weekday() < 6:
            targets.add(FUTURES)

    return targets


def describe(targets: set[str]) -> str:
    """One phrase for a log line."""
    if not targets:
        return "仅摄取（非交易时段）"
    names = {STOCK: "股票", FUTURES: "期货"}
    return " + ".join(names[t] for t in (STOCK, FUTURES) if t in targets)
