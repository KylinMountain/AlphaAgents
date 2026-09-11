"""The learning unit: one decision, and everything that came of it.

Design §9: "the unit of learning is the decision episode — not the
retrospective report, and not only the positions that ran to profit."
Before this module the book could only express the second half of that
sentence. ``thesis → order → exits`` is a chain through rows that a
decision *produced*: an order that was refused never got an order row, an
order that was cancelled never got a position, a trim-and-then-abandon
never got a full exit. Every one of those is a decision the system made,
and none of them was visible as a unit. That is not only a gap in the
record — it is a **selection bias in the learning data**: the sample
consisted of the decisions that happened to trade, and "how often do we
decide, and how often does it come to anything" was not answerable at
all.

So an episode is written at the door, for *every* decision, before the
business rules have had their say. What becomes of it is the events:
``intent`` (with the verdict readable from the intent row it names),
``order``, ``fill``, ``add``, ``trim``, ``close``, ``cancel``. An episode
with no events is a decision the process died on — the same signal
``intent.never_decided`` gives, reached from the other side.

Two deliberate abstentions, both because the alternative would be a claim
with no code behind it:

* Nothing here copies an amount. Amounts live in ``position_exits`` and
  ``virtual_portfolio``; an event names the row that carries the number
  (``ref_id``) and nothing else. A second copy of a P&L is a second source
  of truth, and the first time the two disagree the audit is worthless.
* ``episode_events.kind`` covers only actions some code path performs.
  §9 also asks for holds and abstentions, and ``expire`` looks like it
  belongs next to ``cancel``; there is no writer for either in this
  repository (see the schema comment in ``memory_store``), so declaring
  them would be the "promised entry point that nothing can call" defect
  this project has already shipped twice.

Read-side: ``coverage`` is the number this table exists for, and
``get_episode`` / ``events_for`` are the accessors. This module is
deliberately **not wired into any prompt, retrieval or decision
context** — §10 is explicit that validation is not authorisation, and a
learning unit that quietly changed trading behaviour would be the
opposite of what it is for. What reads episodes today is the operator
(``scripts/episode_coverage.py``) and, from T3 on,
``learning_candidates.evidence_episode_ids``.

``note_fill`` / ``note_cancel`` are called from the book's write paths and
are therefore best-effort: a missing audit event must not stop a fill or
a cancel, but it must be visible, so each logs a warning naming the row.
That is the same posture ``attribution.freeze`` takes at its call site.
"""

from __future__ import annotations

import json
import logging
import sqlite3

from alpha_agents.data import clock

logger = logging.getLogger(__name__)

# ── The kinds, declared once ───────────────────────────────────────────
INTENT = "intent"    # the decision itself; the verdict is on the intent row
ORDER = "order"      # a pending order was written
FILL = "fill"        # the pending order became a position
ADD = "add"          # more shares bought into the position
TRIM = "trim"        # part of the position sold
CLOSE = "close"      # the whole position sold
CANCEL = "cancel"    # the pending order was called off

KINDS = frozenset({INTENT, ORDER, FILL, ADD, TRIM, CLOSE, CANCEL})

#: The statuses an intent row can rest at. Spelled out rather than imported
#: because ``intent`` imports this module, and a module-scope import in the
#: other direction would be a cycle. ``test_episodes`` asserts both of these
#: are still ``intent``'s own values, so the duplication cannot drift.
_REFUSED = "rejected"
_TERMINAL_INTENT = frozenset({"accepted", "rejected"})

OPEN = "open"
CLOSED = "closed"


# ── Writing ────────────────────────────────────────────────────────────


def open_episode(conn: sqlite3.Connection, *, trader_id: str,
                 code: str | None, at: str,
                 decision_snapshot_id: int | None = None,
                 intent_id: int | None = None,
                 thesis_id: int | None = None,
                 prediction_id: int | None = None,
                 order_id: int | None = None,
                 position_id: int | None = None,
                 information_cutoff: str | None = None) -> int:
    """Start the episode for one decision. Returns its id.

    ``decision_snapshot_id`` names the frozen boundary the decision was
    made on. It is left NULL when there is none to name — a decision taken
    outside the door, or one whose boundary could not be written. NULL is
    "unknown basis", which is a different statement from "no basis", and
    folding the two together would make an unrecorded decision look like
    an unconstrained one.

    ``code`` may be None for a decision that named no instrument: a
    malformed intent, or an action on a book row that does not exist. The
    episode is written anyway — it is still a decision, and it is exactly
    the kind of decision the coverage count must not lose.

    ``order_id`` and ``position_id`` are both the same
    ``virtual_portfolio.id`` once a pending order fills, because the fill
    updates that row in place rather than inserting a new one. They are
    kept as separate columns anyway because they are read as different
    claims: ``position_id IS NOT NULL`` is "this decision traded", which
    is false for an order that was cancelled still pending.
    """
    if not trader_id or not isinstance(trader_id, str):
        raise ValueError("An episode needs a trader_id")
    if code is not None and (not isinstance(code, str) or not code):
        raise ValueError("An episode's code is a nonempty string or None")
    if not at or not isinstance(at, str):
        raise ValueError("An episode needs an opening date")
    cur = conn.execute(
        "INSERT INTO episodes "
        "(trader_id, code, status, decision_snapshot_id, intent_id, "
        " thesis_id, prediction_id, order_id, position_id, "
        " information_cutoff, opened_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (trader_id, code, OPEN, decision_snapshot_id, intent_id, thesis_id,
         prediction_id, order_id, position_id, information_cutoff, at))
    return int(cur.lastrowid)


def attach_ref(conn: sqlite3.Connection, episode_id: int, *,
               order_id: int | None = None,
               position_id: int | None = None) -> None:
    """Link the rows this episode turned out to own.

    ``COALESCE`` rather than assignment: a ref already recorded is a fact
    about a row that existed earlier, and re-stamping it would let a later
    write move an episode onto a different order.
    """
    sets: list[str] = []
    params: list = []
    if order_id is not None:
        sets.append("order_id = COALESCE(order_id, ?)")
        params.append(order_id)
    if position_id is not None:
        sets.append("position_id = COALESCE(position_id, ?)")
        params.append(position_id)
    if not sets:
        return
    params.append(episode_id)
    conn.execute(f"UPDATE episodes SET {', '.join(sets)} WHERE id = ?", params)


def add_event(conn: sqlite3.Connection, episode_id: int, kind: str, *,
              at: str, ref_id: int | None = None,
              detail: dict | None = None) -> int:
    """Append one thing that happened because of a decision.

    ``ref_id`` names the row that carries the detail. The check on ``kind``
    is here rather than left to the table's ``CHECK`` constraint so that a
    caller inventing one gets a message saying which kinds exist, at the
    line it wrote, instead of an IntegrityError naming a table.
    """
    if kind not in KINDS:
        raise ValueError(f"Unknown episode event kind {kind!r}; known: "
                         f"{', '.join(sorted(KINDS))}")
    if not at or not isinstance(at, str):
        raise ValueError("An episode event needs a date")
    cur = conn.execute(
        "INSERT INTO episode_events (episode_id, kind, ref_id, at, detail_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (episode_id, kind, ref_id, at,
         json.dumps(detail or {}, ensure_ascii=False, sort_keys=True)))
    return int(cur.lastrowid)


def link_snapshot(conn: sqlite3.Connection, episode_id: int,
                  snapshot_id: int) -> None:
    """Record the frozen boundary this decision was made on.

    Written once, and only once, because the boundary does not exist at the
    moment the episode is opened: the same call that writes the order
    freezes the boundary, and the episode is opened before that call runs.
    So the link is filled in when it becomes knowable and never moved —
    a boundary that could be re-pointed after the result is known is
    precisely the hindsight this table is here to prevent.
    """
    conn.execute(
        "UPDATE episodes SET decision_snapshot_id = ? "
        "WHERE id = ? AND decision_snapshot_id IS NULL",
        (snapshot_id, episode_id))


def close_episode(conn: sqlite3.Connection, episode_id: int, *,
                  at: str) -> None:
    """End the episode. Idempotent: the first ending is the ending.

    Guarded by ``status = 'open'`` in the statement as well as by the
    ``episodes_identity_is_fixed`` trigger, so a second close cannot move
    the date a decision stopped being live.
    """
    conn.execute(
        "UPDATE episodes SET status = ?, closed_at = COALESCE(closed_at, ?) "
        "WHERE id = ? AND status = ?",
        (CLOSED, at, episode_id, OPEN))


def episode_for_ref(conn: sqlite3.Connection, ref_id: int | None) -> int | None:
    """The episode that owns a book row, or None if none does.

    ``ref_id`` is a ``virtual_portfolio.id``: the pending order and the
    position it becomes are one row, so one lookup answers both. Newest
    first, because if an id were ever reused the live episode is the one
    that most recently claimed it.
    """
    if ref_id is None:
        return None
    row = conn.execute(
        "SELECT id FROM episodes WHERE order_id = ? OR position_id = ? "
        "ORDER BY id DESC LIMIT 1", (ref_id, ref_id)).fetchone()
    return row["id"] if row else None


def owner_of(conn: sqlite3.Connection, ref_id: int) -> dict | None:
    """``code`` / ``trader_id`` of the book row a ref names, or None.

    Needed because a position-scoped intent (add / trim / close / cancel)
    carries no stock code — the caller names a position, and the position
    is where the code lives. Reading it here keeps that lookup out of the
    dispatcher, which is about routing rather than about the book.
    """
    row = conn.execute(
        "SELECT id, code, trader_id FROM virtual_portfolio WHERE id = ?",
        (ref_id,)).fetchone()
    return dict(row) if row else None


# ── The two book-path hooks ────────────────────────────────────────────
#
# These are the only two events the intent door cannot record for itself:
# a fill happens when the market reaches the order's zone, and one of the
# cancels in ``_fill_order`` (drawdown, no capital) fires inside the fill
# path rather than through a business intent. Both are best-effort, and
# both say so out loud when they cannot write.
#
# Neither may take ``memory_store._write_lock``: both are called from
# inside a critical section their caller already holds, and the lock is
# not reentrant. The door's own episode writes do take it, because the
# door holds nothing when it calls them.


def note_fill(conn: sqlite3.Connection, order_id: int, at: str) -> None:
    """Record that a pending order became a position.

    Sets ``position_id`` as well as appending the event, so
    "this decision traded" is a column test rather than a scan of the
    event log. A fill whose episode is missing is logged, not invented:
    an episode conjured here would carry no decision boundary and would
    claim the fill was a recorded decision when it was not.
    """
    try:
        episode_id = episode_for_ref(conn, order_id)
        if episode_id is None:
            logger.warning(
                "Fill of order #%d has no episode; the learning record "
                "cannot say this decision traded", order_id)
            return
        attach_ref(conn, episode_id, position_id=order_id)
        add_event(conn, episode_id, FILL, at=at, ref_id=order_id)
    except Exception as e:
        # A missing event is a gap in the audit; refusing the fill would be
        # a gap in the book. Visible, not fatal — the same call the freeze
        # path makes at its own call site.
        logger.warning("Could not record the fill event for order #%d: %s",
                       order_id, e)


def note_cancel(conn: sqlite3.Connection, order_id: int, reason: str) -> None:
    """Record that a pending order was called off, and end its episode.

    The single choke point for cancels: ``_cancel_order_unlocked`` is where
    a cancel actually happens, so this catches the ones a business path
    asked for *and* the ones the fill path performs for its own reasons
    (the drawdown gate, an unaffordable lot). Those last two are exactly
    the decisions a learning loop most needs to see, and routing the event
    through the intent door alone would have missed them.
    """
    try:
        episode_id = episode_for_ref(conn, order_id)
        if episode_id is None:
            logger.warning(
                "Cancel of order #%d has no episode; nothing to attach to",
                order_id)
            return
        at = clock.today()
        add_event(conn, episode_id, CANCEL, at=at, ref_id=order_id,
                  detail={"reason": (reason or "")[:200]})
        close_episode(conn, episode_id, at=at)
    except Exception as e:
        logger.warning("Could not record the cancel event for order #%d: %s",
                       order_id, e)


# ── Reading ────────────────────────────────────────────────────────────


def get_episode(conn: sqlite3.Connection, episode_id: int) -> dict | None:
    """One episode, or None if unknown."""
    row = conn.execute(
        "SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    return dict(row) if row else None


def events_for(conn: sqlite3.Connection, episode_id: int) -> list[dict]:
    """Everything this decision caused, in the order it happened."""
    rows = conn.execute(
        "SELECT * FROM episode_events WHERE episode_id = ? ORDER BY id",
        (episode_id,)).fetchall()
    return [dict(r) for r in rows]


def open_episodes(conn: sqlite3.Connection, *, trader_id: str | None = None,
                  limit: int = 100) -> list[dict]:
    """Episodes still live, newest first."""
    sql = "SELECT * FROM episodes WHERE status = ?"
    params: list = [OPEN]
    if trader_id:
        sql += " AND trader_id = ?"
        params.append(trader_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def coverage(conn: sqlite3.Connection, *,
             trader_id: str | None = None) -> dict:
    """How many decisions there were, and what became of them.

    This is the number §9's "selection and coverage" asks for, and the one
    the book could not produce before: "we decided 40 times and filled 6"
    was unanswerable, and a low fill rate looked exactly like a quiet week.

    Counted over episodes, not over positions, so the decisions that
    produced nothing are in both the numerator and the denominator. A
    decision with no *terminal* verdict is one the process died on: the
    intent event is written before the implementation runs, so its
    presence proves the decision was asked and its row's status proves
    whether it was answered.
    """
    scope = " AND e.trader_id = ?" if trader_id else ""
    tail: list = [trader_id] if trader_id else []

    def count(sql: str, params: list) -> int:
        return int(conn.execute(sql + scope, [*params, *tail]).fetchone()["n"])

    def episodes_with(kind: str) -> int:
        """Distinct episodes that hold at least one event of this kind."""
        return count(
            "SELECT COUNT(DISTINCT e.id) n FROM episode_events v "
            "JOIN episodes e ON e.id = v.episode_id WHERE v.kind = ?", [kind])

    def episodes_whose_intent_is(statuses: list[str]) -> int:
        """Decisions whose verdict came back and reads as one of these.

        The verdict lives on the intent row, so this reaches through
        ``ref_id`` — which is what a link column is for, and the reason
        the refusal is not copied into ``detail_json``.
        """
        marks = ", ".join("?" for _ in statuses)
        return count(
            "SELECT COUNT(DISTINCT e.id) n FROM episode_events v "
            "JOIN episodes e ON e.id = v.episode_id "
            "JOIN intents i ON i.id = v.ref_id "
            f"WHERE v.kind = ? AND i.status IN ({marks})",
            [INTENT, *statuses])

    decisions = count("SELECT COUNT(*) n FROM episodes e WHERE 1=1", [])
    verdicts = episodes_whose_intent_is(sorted(_TERMINAL_INTENT))
    refused = episodes_whose_intent_is([_REFUSED])
    traded = episodes_with(FILL)
    cancelled = episodes_with(CANCEL)
    return {
        "decisions": decisions,
        "verdicts": verdicts,
        "no_verdict": decisions - verdicts,
        "refused": refused,
        "traded": traded,
        "cancelled": cancelled,
        "fill_rate": (round(traded / decisions, 4) if decisions else None),
    }
