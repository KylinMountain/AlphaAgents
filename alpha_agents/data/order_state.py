"""Legal order states and the transitions allowed between them.

``virtual_portfolio.status`` was written by bare ``UPDATE`` statements in
three different modules, so "which state may follow which" lived only as a
convention in the reader's head. That holds until a path is added and
nobody notices it can produce a combination the rest of the code never
expected — a stop on a cancelled order, a fill on a terminal row. A
convention nobody can violate is worth more than one everybody remembers.

There is a second reason this exists, and it is why it lands before the
reservation work: releasing held cash requires distinguishing *"the order
is cancelled"* from *"a cancellation is requested but may still race a
fill"*. Only the first releases anything. Without that distinction the
kernel either leaks reservations or releases cash it still needs.

Two transitions look surprising and are deliberate:

``open → open``
    A partial exit leaves the position open with fewer shares. It is a real
    transition, recorded as one more leg in ``position_exits``, not a
    no-op — someone selling a third of a position has changed the book.

``cancel_pending → open``
    A fill that raced the cancellation wins. The design requires preserving
    a valid racing fill rather than discarding it to honour a request that
    had not completed.

Known misnomer, not fixed here: ``_status_from_reason``
(``alpha_agents/data/portfolio_exit.py``) returns ``expired`` for *any*
close reason that is not a stop or a target, so a position exited because
its theme decayed is labelled ``expired``. Renaming it means touching the
queries that read the value and the rows already carrying it, which is a
data-migration decision rather than a state-machine one. It is left
explicitly as-is so the naming is honest rather than quietly changed.
"""

from __future__ import annotations

PENDING = "pending"
CANCEL_PENDING = "cancel_pending"
OPEN = "open"
STOPPED = "stopped"
TARGET_HIT = "target_hit"
EXPIRED = "expired"
CANCELLED = "cancelled"
REJECTED = "rejected"

#: Every value the column is allowed to hold.
ALL = frozenset({PENDING, CANCEL_PENDING, OPEN, STOPPED, TARGET_HIT,
                 EXPIRED, CANCELLED, REJECTED})

#: Nothing follows these. A row here is history.
TERMINAL = frozenset({STOPPED, TARGET_HIT, EXPIRED, CANCELLED, REJECTED})

#: States a live order occupies: the book still expects something of it.
LIVE = frozenset({PENDING, CANCEL_PENDING, OPEN})

_LEGAL: dict[str, frozenset[str]] = {
    # A pending order is decided by the market or by a rule: it fills,
    # gets rejected, or is called off. Expiry goes through CANCELLED, which
    # is what the existing expiry paths already write.
    PENDING: frozenset({OPEN, REJECTED, CANCEL_PENDING, CANCELLED}),
    # Requested, not done. Either the remainder is confirmed cancelled, or
    # a fill arrives first and wins.
    CANCEL_PENDING: frozenset({CANCELLED, OPEN}),
    # Open can re-enter itself on a partial exit.
    OPEN: frozenset({OPEN, STOPPED, TARGET_HIT, EXPIRED}),
    STOPPED: frozenset(),
    TARGET_HIT: frozenset(),
    EXPIRED: frozenset(),
    CANCELLED: frozenset(),
    REJECTED: frozenset(),
}


class IllegalTransition(ValueError):
    """Raised when a status write would create an unreachable state."""


def validate(status: str) -> str:
    """Return ``status`` if it is a state this module knows, else raise.

    Guards the other direction from ``assert_transition``: a typo in a
    status literal writes a row no query will ever match, which looks like
    a stuck order rather than a bug.
    """
    if status not in ALL:
        raise ValueError(
            f"Unknown order status {status!r}. Known states: "
            f"{', '.join(sorted(ALL))}.")
    return status


def is_terminal(status: str) -> bool:
    """Is this row finished with, whatever happened?"""
    return status in TERMINAL


def is_live(status: str) -> bool:
    """Does the book still expect something from this row?"""
    return status in LIVE


def legal_from(status: str) -> frozenset[str]:
    """The states that may follow ``status``. Empty for a terminal one."""
    validate(status)
    return _LEGAL[status]


def can_transition(current: str, target: str) -> bool:
    """Would ``current → target`` be a legal move?"""
    try:
        return target in legal_from(current)
    except ValueError:
        return False


def assert_transition(current: str, target: str) -> str:
    """Assert ``current → target`` is legal, then return ``target``.

    Returning the target lets a caller write ``status = assert_transition(
    row["status"], OPEN)`` — the assertion sits on the path that uses the
    value rather than beside it, so it cannot drift out of scope.
    """
    if can_transition(current, target):
        return target
    allowed = _LEGAL.get(current)
    if allowed is None:
        raise IllegalTransition(
            f"Unknown current status {current!r}; cannot move to {target!r}. "
            f"Known states: {', '.join(sorted(ALL))}.")
    if not allowed:
        raise IllegalTransition(
            f"{current!r} is terminal — nothing follows it, so {target!r} "
            f"cannot be written. A closed order's history is its record; a "
            f"new position is a new row.")
    raise IllegalTransition(
        f"Illegal order transition {current!r} → {target!r}. Legal from "
        f"{current!r}: {', '.join(sorted(allowed))}.")
