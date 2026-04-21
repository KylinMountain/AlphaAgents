"""Replay mode — context-var based 'as of date' for historical walk-forward.

When a replay context is active:
  - ``get_realtime_quotes`` returns the historical daily close instead of live price
  - ``get_industry_fund_flow`` / ``get_concept_fund_flow`` return the latest
    daily_snapshots row on or before ``as_of``
  - ``compute_vpa_with_llm`` and ``_load_ohlcv`` should be called with
    ``as_of=get_replay_as_of()`` explicitly (the context var is a soft hint,
    not a global monkey-patch; callers must opt in)
  - ``datetime.now()`` is NOT mocked globally — only the specific functions
    above are replay-aware

Production code paths unaffected when ``set_replay_as_of(None)``.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

_as_of: ContextVar[str | None] = ContextVar("replay_as_of", default=None)


def set_replay_as_of(date: str | None) -> None:
    """Set the 'as of' date for replay. Pass None to disable."""
    _as_of.set(date)


def get_replay_as_of() -> str | None:
    """Current replay date, or None for live mode."""
    return _as_of.get()


def is_replay_active() -> bool:
    return _as_of.get() is not None


@contextmanager
def replay_as_of(date: str):
    """Context manager: run a code block in replay mode for ``date``.

    Usage:
        with replay_as_of("2026-03-15"):
            r = compute_vpa_with_llm("300274", as_of="2026-03-15")
            # get_realtime_quotes inside returns 03-15 close price
    """
    token = _as_of.set(date)
    try:
        yield date
    finally:
        _as_of.reset(token)
