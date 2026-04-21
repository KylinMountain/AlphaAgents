"""Replay mode — context-var based 'as of' for historical walk-forward.

The as-of value can be either:
  - **Bare date** ``"2026-03-20"`` — EOD semantics. All replay-aware readers
    treat it as "after close on that day" and may include that date's EOD
    data. Equivalent to ``"2026-03-20 23:59"``. Back-compat default.
  - **Full timestamp** ``"2026-03-20 06:30"`` — point-in-time semantics.
    Useful for simulating the morning scan (06:30 pre-open) or an intraday
    cycle (e.g. 10:30). Time-indexed snapshots (news / sector flow / breadth
    / quote caches) respect the timestamp directly via ``captured_at <= as_of``.
    EOD-only data (daily K-lines, Tushare daily tables) must roll back to the
    previous trading day whenever the time is pre-close; use
    ``effective_eod_cut_date()`` to get the proper cut.

Production code paths unaffected when ``set_replay_as_of(None)``.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta

_as_of: ContextVar[str | None] = ContextVar("replay_as_of", default=None)

# Daily-K-line and EOD-aggregate data is published by the exchange at close.
# If the caller says "it's 14:30 on this day", that day's close hasn't
# happened yet — rolling back to T-1 prevents future-data leak.
_MARKET_CLOSE_HHMM = "15:00"


def set_replay_as_of(as_of: str | None) -> None:
    """Set the replay as-of. Pass None to disable.

    Accepts either ``"YYYY-MM-DD"`` (EOD semantics) or ``"YYYY-MM-DD HH:MM"``
    (point-in-time semantics). See module docstring for details.
    """
    _as_of.set(as_of)


def get_replay_as_of() -> str | None:
    """Current replay as-of string, or None for live mode."""
    return _as_of.get()


def is_replay_active() -> bool:
    return _as_of.get() is not None


def has_time_component(as_of: str | None = None) -> bool:
    """True if ``as_of`` carries a HH:MM suffix (point-in-time mode)."""
    if as_of is None:
        as_of = _as_of.get()
    return bool(as_of) and len(as_of) >= 16


def effective_eod_cut_date(as_of: str | None = None) -> str | None:
    """Return the YYYY-MM-DD cut date for EOD-only data sources.

    The rule:
      - Bare date ``"2026-03-20"`` → ``"2026-03-20"`` (EOD semantics; the
        caller is pretending to stand after-close, T's data is available)
      - ``"2026-03-20 HH:MM"`` where HH:MM ≥ 15:00 → ``"2026-03-20"``
      - ``"2026-03-20 HH:MM"`` where HH:MM < 15:00 → previous calendar day.
        Downstream ``WHERE trade_date <= ?`` queries naturally skip weekends
        because those dates have no rows. For morning replay of 3/23
        (Monday), this returns 3/22 (Sunday) and SQL resolves to Friday 3/20.

    Returns None if no replay is active.
    """
    if as_of is None:
        as_of = _as_of.get()
    if not as_of:
        return None
    date_part = as_of[:10]
    if not has_time_component(as_of):
        return date_part
    time_part = as_of[11:16]
    if time_part >= _MARKET_CLOSE_HHMM:
        return date_part
    d = datetime.strptime(date_part, "%Y-%m-%d") - timedelta(days=1)
    return d.strftime("%Y-%m-%d")


@contextmanager
def replay_as_of(as_of: str):
    """Context manager: run a code block in replay mode.

    Usage:
        # EOD replay (include today's close)
        with replay_as_of("2026-03-15"):
            r = compute_vpa_with_llm("300274", as_of="2026-03-15")

        # Morning replay (pre-open, only overnight/pre-market data)
        with replay_as_of("2026-03-15 06:30"):
            ...

        # Intraday replay (mid-session, yesterday's EOD + today's live snapshot)
        with replay_as_of("2026-03-15 10:30"):
            ...
    """
    token = _as_of.set(as_of)
    try:
        yield as_of
    finally:
        _as_of.reset(token)
