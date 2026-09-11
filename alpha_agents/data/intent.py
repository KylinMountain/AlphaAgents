"""One entry point for every action that changes the book.

Before this module the book had four independent doors —

    create_pending_order  →  a pending row
    open_position         →  an already-filled row
    add_to_position       →  a bigger position
    close_position        →  a smaller or closed position
    _cancel_order         →  a cancelled order

— and each answered a different question when asked "did this happen?".
``create_pending_order`` returned ``None`` for a duplicate *and* for a
theme-weakness refusal; ``close_position`` returned ``False`` for a
terminal row *and* for a NaN price. A caller that wanted to log a refusal
had to re-derive the reason, and none of them could answer "what did the
system decide, and was it accepted" from the book alone.

``submit_intent`` is that answer. It takes a ``TradeIntent`` that names
the action, the evidence, the information cutoff, the price and size
bounds, the expiry and the owning policy; it writes a row stating the
attempt; it dispatches to exactly one implementation; and it records
whether the action was accepted. The public names above keep their
signatures and their return values, but they are wrappers now — they
build an intent and submit it, so there is exactly one path into the
book and exactly one place a refusal is decided.

What this module deliberately does **not** do is re-implement the
business rules. Sizing, theme strength, drawdown gating and T+1 are
still where they were; moving them would be a rewrite, and the point of
this slice is to make the existing rules observable, not to restate
them. The dispatch table is the whole of what is new.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from alpha_agents.data.memory_store import _get_conn, _write_lock
from alpha_agents.data.trader import DEFAULT_TRADER

logger = logging.getLogger(__name__)

# ── Actions ────────────────────────────────────────────────────────────
# The six things a business path may ask the book to do.
OPEN = "open"            # place a pending order (limit, waits for the zone)
OPEN_NOW = "open_now"    # insert an already-filled position
ADD = "add"              # buy more of an open position
TRIM = "trim"            # sell part of an open position
CLOSE = "close"          # sell all of an open position
CANCEL = "cancel"        # call off a pending order

ALL_ACTIONS = frozenset({OPEN, OPEN_NOW, ADD, TRIM, CLOSE, CANCEL})

#: Actions that need an existing position / order to act on.
NEEDS_POSITION = frozenset({ADD, TRIM, CLOSE, CANCEL})

# ── Intent lifecycle ───────────────────────────────────────────────────
# Mirrors order_state's posture: declare the states and the legal moves
# once, assert on write, never let a caller invent a combination.
SUBMITTED = "submitted"
ACCEPTED = "accepted"
REJECTED = "rejected"
INTENT_TERMINAL = frozenset({ACCEPTED, REJECTED})
_LEGAL_INTENT = {
    SUBMITTED: frozenset({ACCEPTED, REJECTED}),
    ACCEPTED: frozenset(),
    REJECTED: frozenset(),
}


class IllegalIntentTransition(ValueError):
    """Raised when an intent row would be left in an unreachable state."""


def assert_intent_transition(current: str, target: str) -> str:
    allowed = _LEGAL_INTENT.get(current)
    if allowed is None:
        raise IllegalIntentTransition(
            f"Unknown intent status {current!r}; cannot move to {target!r}")
    if target not in allowed:
        raise IllegalIntentTransition(
            f"Illegal intent transition {current!r} → {target!r}. Legal "
            f"from {current!r}: {', '.join(sorted(allowed)) or '(none)'}.")
    return target


def is_terminal(status: str) -> bool:
    return status in INTENT_TERMINAL


# ── The intent itself ──────────────────────────────────────────────────


@dataclass
class TradeIntent:
    """What a business path wants the book to do, and on what basis.

    Deliberately a plain data object with no behaviour: it is the
    contract, and a contract that runs code is a contract with the rules
    in two places. Validation lives in ``_reject_reason`` so there is
    one answer to "why was this refused".
    """

    action: str
    code: str = ""
    trader_id: str = DEFAULT_TRADER
    name: str = ""
    theme: str = ""
    # ── where and when ──
    order_date: str | None = None
    # ── price constraints ──
    entry_low: float | None = None
    entry_high: float | None = None
    stop_loss: float | None = None
    target_price: float | None = None
    price: float | None = None          # fill / close price for the instant actions
    # ── size constraints ──
    shares: int | None = None
    size_pct: float | None = None
    #: Sizing policy flag: recompute the stop to hold its original distance
    #: from the new average. Only the automated pullback top-up sets it.
    recalc_stop: bool = False
    # ── life ──
    expire_days: int | None = None
    # ── evidence: what this decision rests on ──
    reason: str = ""
    source: str = ""
    thesis_id: int | None = None
    prediction_id: int | None = None
    information_cutoff: str | None = None
    # ── who owns the rule that produced this ──
    policy_ref: str | None = None
    # ── target of a position-scoped action ──
    position_id: int | None = None
    #: Retry key for the ledger. Passed through untouched: it is the
    #: caller's identity for the command, not the intent's.
    command_id: str | None = None

    def evidence(self) -> dict:
        """The decision basis, as it will be frozen onto the intent row."""
        return {
            "reason": self.reason,
            "source": self.source,
            "thesis_id": self.thesis_id,
            "prediction_id": self.prediction_id,
            "price": self.price,
            "entry_low": self.entry_low,
            "entry_high": self.entry_high,
            "stop_loss": self.stop_loss,
            "target_price": self.target_price,
            "shares": self.shares,
            "size_pct": self.size_pct,
            "recalc_stop": self.recalc_stop,
            "expire_days": self.expire_days,
            "order_date": self.order_date,
        }


@dataclass
class IntentResult:
    """What happened. ``accepted`` is the one bit callers should read."""
    intent_id: int
    action: str
    accepted: bool
    reject_reason: str | None = None
    result: object = None
    detail: dict = field(default_factory=dict)


# ── Validation ─────────────────────────────────────────────────────────


def _finite(value) -> bool:
    return (type(value) in (int, float) and value == value
            and value not in (float("inf"), float("-inf")))


def _reject_reason(intent: TradeIntent) -> str | None:
    """Why this intent cannot be submitted, or None if it can.

    Shape only — business refusals (a weak theme, no capital, a duplicate
    order) come back from the implementation and are recorded as the
    intent's reject reason at that point. Splitting them this way keeps
    one place for "malformed" and lets "well-formed but refused" carry
    the implementation's own words.
    """
    if intent.action not in ALL_ACTIONS:
        return (f"unknown action {intent.action!r}; known: "
                f"{', '.join(sorted(ALL_ACTIONS))}")
    if not intent.trader_id or not isinstance(intent.trader_id, str):
        return "trader_id is required"
    if intent.action in NEEDS_POSITION:
        if intent.position_id is None:
            return f"{intent.action} needs a position_id"
        if type(intent.position_id) is not int or intent.position_id <= 0:
            return f"position_id must be a positive int, got {intent.position_id!r}"
    if intent.action in (OPEN, OPEN_NOW):
        if not intent.code or not isinstance(intent.code, str):
            return "code is required for an open intent"
    if intent.action in (OPEN_NOW, TRIM, CLOSE):
        if not _finite(intent.price) or intent.price <= 0:
            return (f"{intent.action} needs a finite positive price, "
                    f"got {intent.price!r}")
    if intent.action in (OPEN, OPEN_NOW):
        for label, bound in (("entry_low", intent.entry_low),
                             ("entry_high", intent.entry_high),
                             ("stop_loss", intent.stop_loss),
                             ("target_price", intent.target_price)):
            if bound is not None and not _finite(bound):
                return f"{label} must be a finite number or None, got {bound!r}"
    if intent.shares is not None and (type(intent.shares) is not int
                                      or intent.shares <= 0):
        return f"shares must be a positive int or None, got {intent.shares!r}"
    return None


# ── Dispatch ───────────────────────────────────────────────────────────
# Each entry is the one implementation of an action. Late imports: the
# implementations live in portfolio / portfolio_exit, which import this
# module, so a module-level import here would be a cycle. The same
# pattern is used throughout the data layer for the same reason.


def _dispatch(intent: TradeIntent):
    from alpha_agents.data import portfolio as P
    from alpha_agents.data import portfolio_exit as PE

    if intent.action == OPEN:
        order_id = P._create_pending_order_impl(
            code=intent.code, name=intent.name, theme=intent.theme,
            order_date=intent.order_date or _today(),
            entry_low=intent.entry_low, entry_high=intent.entry_high,
            stop_loss=intent.stop_loss, target_price=intent.target_price,
            source=intent.source or "intent", reason=intent.reason,
            trader_id=intent.trader_id,
            prediction_id=intent.prediction_id, thesis_id=intent.thesis_id)
        return order_id, {"order_id": order_id}

    if intent.action == OPEN_NOW:
        position_id = P._open_position_impl(
            code=intent.code, name=intent.name, theme=intent.theme,
            open_date=intent.order_date or _today(),
            open_price=intent.price, stop_loss=intent.stop_loss,
            target_price=intent.target_price,
            source=intent.source or "intent", reason=intent.reason,
            shares=intent.shares, trader_id=intent.trader_id,
            prediction_id=intent.prediction_id, thesis_id=intent.thesis_id)
        return position_id, {"position_id": position_id}

    if intent.action == ADD:
        added = P._add_to_position_impl(
            intent.position_id, price=intent.price,
            reason=intent.reason, size_pct=intent.size_pct,
            recalc_stop=intent.recalc_stop)
        return added, {"added": added}

    if intent.action in (TRIM, CLOSE):
        shares = intent.shares
        if intent.action == CLOSE:
            shares = None   # sell the whole position
        ok = PE._close_position_impl(
            intent.position_id, close_price=intent.price,
            close_reason=intent.reason, shares=shares,
            command_id=intent.command_id)
        return ok, {"closed": ok}

    if intent.action == CANCEL:
        P._cancel_order_impl(intent.position_id, intent.reason)
        return True, {"cancelled": intent.position_id}

    raise ValueError(f"no dispatch for action {intent.action!r}")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _accepted(action: str, result) -> bool:
    """Did the implementation actually do the thing?

    Each action answers differently because the pre-S5 signatures did —
    ``None`` from ``create_pending_order`` means refused, ``False`` from
    ``close_position`` means refused. Reading the same values the callers
    already read keeps the wrapper's return value identical to before.
    """
    if action in (OPEN, OPEN_NOW):
        return result is not None
    if action in (TRIM, CLOSE, ADD):
        return bool(result)
    if action == CANCEL:
        return True    # a cancel of an unknown/finished order is a no-op,
                       # not a refusal — see _cancel_order_unlocked.
    return False


def _reject_from_result(action: str, result) -> str:
    if action in (OPEN, OPEN_NOW):
        return ("refused by the order path (duplicate, theme too weak, "
                "no capital, or a rejected link)")
    if action in (TRIM, CLOSE):
        return ("refused (position not open, invalid price/quantity, or "
                "T+1 settlement gate)")
    if action == ADD:
        return "refused (no room, invalid price, or position not open)"
    return "refused"


# ── The entry point ────────────────────────────────────────────────────


def submit_intent(intent: TradeIntent,
                  conn: sqlite3.Connection | None = None) -> IntentResult:
    """Validate, record, dispatch, record the outcome. The one door.

    Writes a row to ``intents`` with status='submitted' before any
    business rule runs, then flips it to 'accepted' or 'rejected'. A row
    still at 'submitted' afterwards means the process died mid-action —
    exactly the failure that has no other trace.

    Never raises for a refused action; a refusal is a recorded outcome,
    not an exception. A *malformed* intent is refused the same way, so
    there is one shape of answer for callers to handle.
    """
    if conn is None:
        conn = _get_conn()

    reason = _reject_reason(intent)
    intent_id = _record_submitted(conn, intent)

    if reason is not None:
        _finalise(conn, intent_id, REJECTED, reason, None)
        logger.info("Intent #%d %s refused: %s", intent_id, intent.action, reason)
        return IntentResult(intent_id=intent_id, action=intent.action,
                            accepted=False, reject_reason=reason)

    try:
        result, detail = _dispatch(intent)
    except Exception as e:
        # Record the failure where the decision was made, then let it
        # propagate. A raised exception is a system fault (a rolled-back
        # write, a broken invariant), not a decision — swallowing it here
        # would turn "the database refused this sale" into "the sale was
        # declined", which is the silent-exception failure this codebase
        # forbids. The intent row survives so the attempt is visible.
        msg = f"{type(e).__name__}: {e}"
        try:
            _finalise(conn, intent_id, REJECTED, msg, None)
        except Exception as inner:
            logger.error("Could not mark intent #%d rejected: %s",
                         intent_id, inner)
        logger.warning("Intent #%d %s raised: %s", intent_id, intent.action, msg)
        raise

    if _accepted(intent.action, result):
        _finalise(conn, intent_id, ACCEPTED, None, detail)
        return IntentResult(intent_id=intent_id, action=intent.action,
                            accepted=True, result=result, detail=detail)

    rej = _reject_from_result(intent.action, result)
    _finalise(conn, intent_id, REJECTED, rej, detail)
    logger.info("Intent #%d %s refused: %s", intent_id, intent.action, rej)
    return IntentResult(intent_id=intent_id, action=intent.action,
                        accepted=False, reject_reason=rej, result=result)


def _record_submitted(conn: sqlite3.Connection,
                      intent: TradeIntent) -> int:
    with _write_lock:
        cur = conn.execute(
            "INSERT INTO intents "
            "(action, status, trader_id, code, position_id, "
            " information_cutoff, policy_ref, evidence_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (intent.action, SUBMITTED, intent.trader_id, intent.code or None,
             intent.position_id, intent.information_cutoff, intent.policy_ref,
             json.dumps(intent.evidence(), ensure_ascii=False)))
        conn.commit()
    return int(cur.lastrowid)


def _finalise(conn: sqlite3.Connection, intent_id: int,
              target: str, reject_reason: str | None,
              detail: dict | None) -> None:
    """Move the row to a terminal status, asserting the transition."""
    with _write_lock:
        row = conn.execute("SELECT status FROM intents WHERE id = ?",
                           (intent_id,)).fetchone()
        current = row["status"] if row else SUBMITTED
        assert_intent_transition(current, target)
        conn.execute(
            "UPDATE intents SET status = ?, reject_reason = ?, "
            "result_json = ?, decided_at = ? WHERE id = ?",
            (target, reject_reason,
             json.dumps(detail, ensure_ascii=False) if detail else None,
             datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), intent_id))
        conn.commit()


# ── Read side ──────────────────────────────────────────────────────────


def never_decided(conn: sqlite3.Connection | None = None) -> list[dict]:
    """Intents still sitting at 'submitted' — a process that died mid-action.

    Exposed rather than merely logged: a submitted-but-undecided intent
    is the only evidence that a write path crashed between deciding and
    acting, and nothing else in the book records it.
    """
    if conn is None:
        conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM intents WHERE status = ? ORDER BY id", (SUBMITTED,),
    ).fetchall()
    return [dict(r) for r in rows]


def history(conn: sqlite3.Connection | None = None, *, action: str | None = None,
            trader_id: str | None = None, limit: int = 100) -> list[dict]:
    """Recent intents, newest first, optionally filtered."""
    if conn is None:
        conn = _get_conn()
    sql = "SELECT * FROM intents WHERE 1=1"
    params: list = []
    if action:
        sql += " AND action = ?"
        params.append(action)
    if trader_id:
        sql += " AND trader_id = ?"
        params.append(trader_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]