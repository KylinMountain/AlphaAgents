"""Outcome labels with a lifecycle, kept apart by kind.

Design §9 gives one episode three results that must not substitute for
each other:

* **forecast** — did the call beat the market over its declared horizon,
* **trade** — what the position actually realised, net, from the ledger,
* **process** — was the decision well-formed when it was made, before any
  result existed.

§9 also names the substitution that is forbidden for each, and each
prohibition is a rule this module can be asked about rather than a
sentence in a design doc:

* a **close must not rewrite the forecast label** — a trade label is
  written with ``kind='trade'`` and the forecast rows are untouched, so
  ``forecast_rows``/``forecast_for`` are the same before and after;
* a **correct forecast must not be counted as realised profit** — the
  trade label is derived from ``position_exits`` and nothing else, so a
  right call on a losing trade reports a correct forecast *and* a
  negative trade, both visibly;
* **a rule violation is not waived because it made money** — the process
  label is computed from the decision alone, and
  ``assert_no_pnl_in_process`` refuses a process label carrying a P&L
  field at all, so there is nowhere for "but it worked" to enter.

Why a lifecycle rather than a column. ``predictions.hit`` is a writable
cell on the source row, and the design asks for two things a mutable cell
cannot express: a label that cannot be overwritten by a later event, and
the ability to tell afterwards which version of the evaluator produced
it. So:

* rows are **append-only** (triggers, ``RAISE(ABORT)``) — a correction is
  a new row naming the one it supersedes;
* every row carries ``evaluator_version``, so a changed rule is
  distinguishable from a changed market;
* every row carries ``available_at`` — *when the label became knowable*,
  which is not when it was computed. Under replay the former is the only
  one that may be read;
* an unsettled subject keeps a ``pending`` row rather than a fabricated
  terminal value (``pending / matured / censored / revised``).

The state machine mirrors ``order_state`` and ``intent``: the legal moves
are declared once in ``_LEGAL``, asserted on every write, and a caller
cannot invent a transition. Because a row is never updated, a transition
*is* the appending of its successor — so ``pending → matured`` and
``matured → revised`` are the same operation with a different target, and
``matured → pending`` is refused by the table's own rules rather than by
anyone remembering not to.

This module stores labels and their state. It never stores money: amounts
live in ``predictions`` and ``position_exits``, and ``evidence_json``
carries refs to those rows (see ``alpha_agents/data/outcome_labels.py``
for the three producers).
"""

from __future__ import annotations

import json
import logging
import sqlite3

from alpha_agents.data import clock

logger = logging.getLogger(__name__)

# ── Kinds: the three of §9, which must not substitute for each other ───
FORECAST = "forecast"
TRADE = "trade"
PROCESS = "process"
KINDS = frozenset({FORECAST, TRADE, PROCESS})

# ── States ─────────────────────────────────────────────────────────────
PENDING = "pending"    # declared; the horizon has not arrived / still open
MATURED = "matured"    # resolved, with evidence
CENSORED = "censored"  # the horizon arrived and the evidence is missing
REVISED = "revised"    # a correction of an earlier label

STATES = frozenset({PENDING, MATURED, CENSORED, REVISED})

#: The one row state that cannot be moved on from. ``matured`` and
#: ``censored`` are resolved but still correctable — a label corrected
#: later appends a revision rather than rewriting itself, and a censored
#: label whose data finally arrives is the live case for that.
TERMINAL = frozenset({REVISED})

_LEGAL = {
    PENDING: frozenset({MATURED, CENSORED}),
    MATURED: frozenset({REVISED}),
    CENSORED: frozenset({REVISED}),
    REVISED: frozenset({REVISED}),
}

#: Fields a process label may not carry, because §9 forbids grading the
#: decision by what it earned. Checked on every process write.
_PNL_FIELDS = (
    "return_pct", "net_amount", "return_amount", "profit", "pnl", "hit",
    "gross_amount", "win",
)


class IllegalOutcomeTransition(ValueError):
    """Raised when a label would be left in an unreachable state."""


def assert_outcome_transition(current: str, target: str) -> str:
    allowed = _LEGAL.get(current)
    if allowed is None:
        raise IllegalOutcomeTransition(
            f"Unknown outcome state {current!r}; cannot move to {target!r}")
    if target not in allowed:
        raise IllegalOutcomeTransition(
            f"Illegal outcome transition {current!r} → {target!r}. Legal "
            f"from {current!r}: {', '.join(sorted(allowed)) or '(none)'}.")
    return target


def assert_no_pnl_in_process(evidence: dict) -> None:
    """Refuse a process label that carries a realised result.

    §9's third prohibition, as a rule the code enforces: "a rule violation
    is not excused because the trade made money". The way that enters a
    system is a process grader that reads the P&L, so the field simply may
    not be here. Raises rather than warns — a silently-excused violation
    is exactly the failure this guard exists for.
    """
    found = sorted(k for k in (evidence or {}) if k in _PNL_FIELDS)
    if found:
        raise ValueError(
            "A process outcome may not carry realised results "
            f"({', '.join(found)}). §9 keeps process and trade outcomes "
            "apart: grade the decision on what was knowable when it was "
            "made, or this becomes a P&L grade wearing a process label.")


# ── Writing ────────────────────────────────────────────────────────────


def declare(conn: sqlite3.Connection, *, kind: str, subject_type: str,
            subject_id: int, episode_id: int | None = None,
            evaluator_version: str | None = None,
            evidence: dict | None = None,
            available_at: str | None = None) -> int:
    """Open the label chain for a subject, in state ``pending``.

    Idempotent: a subject that already has a label keeps it and its id is
    returned. The partial unique index makes a second *initial* row
    impossible, so a re-run of a sweeper cannot fork a chain — and a
    fork is what makes "the current label" ambiguous, which is the one
    question this table exists to answer.
    """
    _check(kind, subject_type, subject_id)
    existing = initial(conn, kind, subject_type, subject_id)
    if existing is not None:
        logger.debug("Outcome for %s/%s#%d already declared (#%d)",
                     kind, subject_type, subject_id, existing["id"])
        return int(existing["id"])
    cur = conn.execute(
        "INSERT INTO outcomes (kind, state, subject_type, subject_id, "
        " episode_id, evaluator_version, evidence_json, available_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (kind, PENDING, subject_type, subject_id, episode_id,
         evaluator_version, _dump(evidence), available_at))
    return int(cur.lastrowid)


def ensure_label(conn: sqlite3.Connection, *, kind: str, subject_type: str,
                 subject_id: int, episode_id: int | None = None,
                 evaluator_version: str | None = None,
                 evidence: dict | None = None,
                 available_at: str | None = None) -> dict:
    """The **live** label for a subject, declaring a ``pending`` one if new.

    The accessor a labeller actually wants, and the reason it exists as
    its own function: ``declare`` answers "make sure a chain exists" and
    returns the chain's *first* row, which is the wrong handle for an
    evaluator that means to move the label on. Reading the state off the
    initial row and then resolving it works exactly once and then hits the
    one-successor-per-row index — a mistake this project made, and one
    that only shows up on the second run. Returning the live row makes it
    unrepresentable.

    The ``evidence`` / ``available_at`` arguments apply only when the chain
    is created: an existing label is not re-declared.
    """
    declare(conn, kind=kind, subject_type=subject_type, subject_id=subject_id,
            episode_id=episode_id, evaluator_version=evaluator_version,
            evidence=evidence, available_at=available_at)
    live = current(conn, kind, subject_type, subject_id)
    if live is None:                      # pragma: no cover - declare just made it
        raise RuntimeError(
            f"declared an outcome for {kind}/{subject_type}#{subject_id} and "
            "could not read it back")
    return live


def resolve(conn: sqlite3.Connection, outcome_id: int, *, state: str,
            evaluator_version: str | None = None,
            evidence: dict | None = None,
            available_at: str | None = None) -> int:
    """Append the successor of a label. Returns the new row's id.

    The transition is asserted against the row being superseded, so
    ``matured → pending`` is refused here *and* would be refused by the
    chain's own shape. Nothing is updated: a label that has been read is
    evidence, and evidence that can be edited is not evidence.
    """
    if state == PENDING:
        raise IllegalOutcomeTransition(
            "pending is where a chain starts; a label is resolved by "
            "appending matured / censored / revised, never by going back.")
    current = get(conn, outcome_id)
    if current is None:
        raise ValueError(f"Unknown outcome #{outcome_id}")
    assert_outcome_transition(current["state"], state)
    if current["kind"] == PROCESS:
        assert_no_pnl_in_process(evidence or {})
    cur = conn.execute(
        "INSERT INTO outcomes (kind, state, subject_type, subject_id, "
        " episode_id, evaluator_version, evidence_json, available_at, "
        " supersedes_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (current["kind"], state, current["subject_type"],
         current["subject_id"], current["episode_id"],
         evaluator_version or current["evaluator_version"],
         _dump(evidence), available_at, outcome_id))
    return int(cur.lastrowid)


def _check(kind: str, subject_type: str, subject_id: int) -> None:
    if kind not in KINDS:
        raise ValueError(f"Unknown outcome kind {kind!r}; known: "
                         f"{', '.join(sorted(KINDS))}")
    if not subject_type or not isinstance(subject_type, str):
        raise ValueError("An outcome needs a subject_type")
    if type(subject_id) is not int or subject_id <= 0:
        raise ValueError(f"subject_id must be a positive int, got "
                         f"{subject_id!r}")


def _dump(evidence: dict | None) -> str:
    return json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True)


# ── Reading ────────────────────────────────────────────────────────────

def _row(row) -> dict:
    out = dict(row)
    out["evidence"] = json.loads(out.pop("evidence_json") or "{}")
    return out


def get(conn: sqlite3.Connection, outcome_id: int) -> dict | None:
    """One label row, or None."""
    row = conn.execute(
        "SELECT * FROM outcomes WHERE id = ?", (outcome_id,)).fetchone()
    return _row(row) if row else None


def initial(conn: sqlite3.Connection, kind: str, subject_type: str,
            subject_id: int) -> dict | None:
    """The first label in a subject's chain, or None if none exists."""
    row = conn.execute(
        "SELECT * FROM outcomes WHERE kind = ? AND subject_type = ? "
        "AND subject_id = ? AND supersedes_id IS NULL",
        (kind, subject_type, subject_id)).fetchone()
    return _row(row) if row else None


def current(conn: sqlite3.Connection, kind: str, subject_type: str,
            subject_id: int) -> dict | None:
    """The live label for a subject — the newest row in its chain.

    One answer by construction: the unique index on ``supersedes_id``
    makes a chain linear, so "the newest row of this subject" cannot be a
    fork.
    """
    row = conn.execute(
        "SELECT * FROM outcomes WHERE kind = ? AND subject_type = ? "
        "AND subject_id = ? ORDER BY id DESC LIMIT 1",
        (kind, subject_type, subject_id)).fetchone()
    return _row(row) if row else None


def history(conn: sqlite3.Connection, kind: str, subject_type: str,
            subject_id: int) -> list[dict]:
    """Every label this subject ever had, oldest first."""
    rows = conn.execute(
        "SELECT * FROM outcomes WHERE kind = ? AND subject_type = ? "
        "AND subject_id = ? ORDER BY id",
        (kind, subject_type, subject_id)).fetchall()
    return [_row(r) for r in rows]


def for_episode(conn: sqlite3.Connection, episode_id: int) -> list[dict]:
    """Every label hung on one decision, oldest first."""
    rows = conn.execute(
        "SELECT * FROM outcomes WHERE episode_id = ? ORDER BY id",
        (episode_id,)).fetchall()
    return [_row(r) for r in rows]


def pending_labels(conn: sqlite3.Connection, *,
                   kind: str | None = None,
                   limit: int = 500) -> list[dict]:
    """Labels still awaiting a result, oldest first.

    A label with no successor. Read as a *set*, not per subject: "how many
    forecasts are outstanding" is a coverage question, and §9 asks for it.
    """
    sql = ("SELECT o.* FROM outcomes o "
           "WHERE o.state = ? AND NOT EXISTS "
           "(SELECT 1 FROM outcomes n WHERE n.supersedes_id = o.id)")
    params: list = [PENDING]
    if kind:
        sql += " AND o.kind = ?"
        params.append(kind)
    sql += " ORDER BY o.id LIMIT ?"
    params.append(limit)
    return [_row(r) for r in conn.execute(sql, params).fetchall()]


def counts(conn: sqlite3.Connection, *,
           kind: str | None = None) -> dict:
    """Live-label counts by state, and the three kinds side by side.

    The §9 separation as a number: a decision can be a correct forecast
    and a losing trade at once, and this reports both without merging
    them into one "performance" figure.
    """
    sql = ("SELECT o.kind, o.state, COUNT(*) n FROM outcomes o "
           "WHERE NOT EXISTS "
           "(SELECT 1 FROM outcomes n WHERE n.supersedes_id = o.id)")
    params: list = []
    if kind:
        sql += " WHERE o.kind = ?"
        params.append(kind)
    sql += " GROUP BY o.kind, o.state"
    out = {k: {s: 0 for s in sorted(STATES)} for k in sorted(KINDS)}
    for row in conn.execute(sql, params).fetchall():
        out[row["kind"]][row["state"]] = int(row["n"])
    return out


def integrity(conn: sqlite3.Connection) -> list[str]:
    """Structural complaints about the label store, or an empty list.

    Reports rather than repairs, the same posture ``reconciliation``
    takes: each complaint names the rows, and a fix is a deliberate write.
    Two things are checkable that the triggers cannot fully express — that
    every chain is internally consistent (a successor describes the same
    subject as the row it supersedes) and that nothing is left ``pending``
    on a day it should have resolved.
    """
    problems: list[str] = []
    for row in conn.execute(
            "SELECT n.id, n.kind, n.subject_type, n.subject_id, "
            "o.kind AS old_kind, o.subject_type AS old_type, "
            "o.subject_id AS old_id FROM outcomes n "
            "JOIN outcomes o ON o.id = n.supersedes_id "
            "WHERE n.kind <> o.kind OR n.subject_type <> o.subject_type "
            "OR n.subject_id <> o.subject_id").fetchall():
        problems.append(
            f"outcome #{row['id']} supersedes #{row['id']} but describes a "
            f"different subject ({row['kind']}/{row['subject_type']}"
            f"#{row['subject_id']} vs {row['old_kind']}/{row['old_type']}"
            f"#{row['old_id']})")
    for row in conn.execute(
            "SELECT id, kind, subject_type, subject_id, state, available_at "
            "FROM outcomes WHERE state = ? AND available_at IS NOT NULL "
            "AND available_at > ?", (PENDING, clock.today())).fetchall():
        problems.append(
            f"outcome #{row['id']} is pending but claims to be available on "
            f"{row['available_at']}, after the kernel clock")
    return problems
