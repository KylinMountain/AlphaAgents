"""One security-eligibility vocabulary shared by every candidate entrypoint.

The caller supplies only facts available at its own decision clock. This module
does not query a database, infer missing history or decide position size.
"""

from __future__ import annotations

from dataclasses import dataclass

from alpha_agents.config import is_tradable


BOARD = "board"
NOT_LISTED = "not_listed"
UNKNOWN_INSTRUMENT = "unknown_instrument"
ST = "st"
SUSPENDED = "suspended"
NO_PRIOR_BAR = "no_prior_bar"

REASONS = (
    BOARD, NOT_LISTED, UNKNOWN_INSTRUMENT, ST, SUSPENDED, NO_PRIOR_BAR,
)


@dataclass(frozen=True)
class SecurityFacts:
    code: str
    listed: bool = True
    known: bool = True
    is_st: bool = False
    is_suspended: bool = False
    has_prior_bar: bool = True


def reason(facts: SecurityFacts) -> str | None:
    """Return the first deterministic exclusion reason, or None."""
    code = str(facts.code or "").strip()
    if not is_tradable(code):
        return BOARD
    if not facts.listed:
        return NOT_LISTED
    if not facts.known:
        return UNKNOWN_INSTRUMENT
    if facts.is_st:
        return ST
    if facts.is_suspended:
        return SUSPENDED
    if not facts.has_prior_bar:
        return NO_PRIOR_BAR
    return None


def eligible(facts: SecurityFacts) -> bool:
    return reason(facts) is None
