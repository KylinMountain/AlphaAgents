"""The three §9 outcome labels, produced — and kept apart.

``data/outcomes.py`` stores labels and their lifecycle. This module
computes them, and it lives in ``evolution`` rather than ``data`` for a
layering reason that is also the right conceptual one: grading a
decision's *process* is ``evolution.process_quality``'s job, and ``data``
may not import upward. All three evaluators are learning-side, so they
sit together here.

The three, and what each may not be confused with (§9):

* **forecast** — ``label_forecast``. Did the call beat the market over
  the horizon *it declared*? The horizon comes from
  ``predictions.horizon_days`` / ``deadline``; a row that pre-dates the
  declaration falls back to the global default and says so in the label's
  evidence (``legacy_horizon``) instead of recording a claim its author
  never made.
* **trade** — ``sweep_trade_labels``. What the position realised, derived
  from ``position_exits``. The label carries **refs to the ledger legs,
  never the money**: the return has one home and it is not this table.
* **process** — ``sweep_process_labels``. Was the decision well formed on
  the day it was made. Graded by ``process_quality.grade_thesis``, which
  reads only the thesis. ``outcomes.assert_no_pnl_in_process`` refuses a
  process label carrying a realised result, so "but it made money" has
  nowhere to enter.

Two design choices worth naming, because the alternative is the bug:

**Nothing is overwritten.** A matured label is not re-derived. A forecast
already graded keeps its grade even if a later run computes something
different; a correction is appended as a revision naming the row it
supersedes. The live copy in ``predictions.brier`` may move — that is a
mutable working number — while the frozen label stays put, and the two
being allowed to differ is the whole point of having a label at all.

**The order that never traded gets no trade label.** A cancelled or
refused order has no realised result, and inventing a zero would report
"traded and broke even", which is a different fact and the one that
flatters. Its absence is the honest encoding, and the episode's cancel
event is where that decision is recorded.
"""

from __future__ import annotations

import logging
import sqlite3

from alpha_agents.data import episodes, outcomes
from alpha_agents.data import thesis as T
from alpha_agents.evolution import process_quality

logger = logging.getLogger(__name__)

#: Which version of each evaluator produced a label. Bumping one is how a
#: changed rule becomes visible in the record: without it, a label that
#: moved because the rule changed is indistinguishable from one that moved
#: because the market did.
FORECAST_EVALUATOR = "forecast-label@1"
TRADE_EVALUATOR = "trade-label@1"
PROCESS_EVALUATOR = "process-label@1"

PREDICTION = "prediction"
POSITION = "position"
THESIS = "thesis"


# ── Forecast ───────────────────────────────────────────────────────────


def label_forecast(conn: sqlite3.Connection, *, prediction: dict,
                   scored: dict | None, as_of: str) -> dict:
    """Grade one forecast against the horizon it declared.

    ``scored`` is ``scoring.score_prediction``'s result, or None when the
    horizon arrived and the evidence did not — no price history, a
    suspended stock, a delisted code. None is a *censored* label, not an
    excuse to wait: §9 wants "we cannot say" recorded, because a system
    that silently drops the un-gradeable ones reports a hit rate over a
    sample it chose.

    Returns ``{"outcome_id", "state", "changed"}``. ``changed`` is False
    for a forecast already graded — a matured label is never re-derived.
    """
    prediction_id = int(prediction["id"])
    legacy = bool(prediction.get("legacy_horizon"))
    declared = {
        "prediction_id": prediction_id,
        "code": prediction.get("code"),
        "date": prediction.get("date"),
        "horizon_days": prediction.get("horizon_days"),
        "deadline": prediction.get("deadline"),
        # Recorded, not hidden: this forecast never declared a horizon, so
        # the fallback is the evaluator's assumption rather than the
        # author's claim.
        "legacy_horizon": legacy,
    }
    label_id = outcomes.ensure_label(
        conn, kind=outcomes.FORECAST, subject_type=PREDICTION,
        subject_id=prediction_id,
        episode_id=episodes.episode_for(conn, column="prediction_id",
                                        value=prediction_id),
        evaluator_version=FORECAST_EVALUATOR,
        evidence=declared, available_at=prediction.get("deadline") or as_of
    )["id"]
    state = outcomes.get(conn, label_id)["state"]

    if state == outcomes.MATURED:
        return {"outcome_id": label_id, "state": state, "changed": False}

    if scored is None:
        if state == outcomes.CENSORED:
            return {"outcome_id": label_id, "state": state, "changed": False}
        new_id = outcomes.resolve(
            conn, label_id, state=outcomes.CENSORED,
            evaluator_version=FORECAST_EVALUATOR,
            evidence={**declared, "reason": "no score could be computed"},
            available_at=as_of)
        return {"outcome_id": new_id, "state": outcomes.CENSORED,
                "changed": True}

    # A censored label whose evidence finally arrived is corrected by
    # appending a revision — the live case for ``censored → revised``.
    target = (outcomes.REVISED if state == outcomes.CENSORED
              else outcomes.MATURED)
    evidence = {
        **declared,
        "outcome": bool(scored.get("outcome")),
        "prob": scored.get("prob"),
        "brier": scored.get("brier"),
        "log_score": scored.get("log_score"),
        "excess_return": scored.get("excess_return"),
        "residual_alpha": scored.get("residual_alpha"),
        "scored_with_horizon": scored.get("horizon_days"),
    }
    new_id = outcomes.resolve(
        conn, label_id, state=target, evaluator_version=FORECAST_EVALUATOR,
        evidence=evidence, available_at=as_of)
    return {"outcome_id": new_id, "state": target, "changed": True}


def declare_outstanding_forecasts(conn: sqlite3.Connection, *, as_of: str,
                                  limit: int = 500) -> int:
    """Declare a ``pending`` label for every forecast not yet matured.

    Makes "how many forecasts are still awaiting their horizon" a number
    rather than an inference. Without it the only labels that exist are
    the resolved ones, and a system that has been quiet for a month looks
    the same as one that stopped forecasting.
    """
    rows = conn.execute(
        "SELECT id, date, code, prob, horizon_days, deadline FROM predictions "
        "WHERE prob IS NOT NULL AND scored_at IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM outcomes o WHERE o.kind = ? "
        "                AND o.subject_type = ? AND o.subject_id = "
        "                    predictions.id) "
        "ORDER BY date DESC LIMIT ?",
        (outcomes.FORECAST, PREDICTION, limit)).fetchall()
    declared = 0
    for row in rows:
        row = dict(row)
        outcomes.declare(
            conn, kind=outcomes.FORECAST, subject_type=PREDICTION,
            subject_id=int(row["id"]),
            episode_id=episodes.episode_for(conn, column="prediction_id",
                                            value=int(row["id"])),
            evaluator_version=FORECAST_EVALUATOR,
            evidence={"prediction_id": row["id"], "code": row["code"],
                      "date": row["date"], "horizon_days": row["horizon_days"],
                      "deadline": row["deadline"],
                      "legacy_horizon": row["deadline"] is None},
            available_at=row["deadline"] or as_of)
        declared += 1
    return declared


# ── Trade ──────────────────────────────────────────────────────────────


def sweep_trade_labels(conn: sqlite3.Connection, *, as_of: str,
                       limit: int = 500) -> dict:
    """Label what each position has realised, from the ledger.

    A position still open keeps a ``pending`` label and nothing else: no
    terminal value is fabricated, because a number derived from a price
    this layer does not have would be a number with no provenance. A
    closed position is ``matured`` with **refs** to its exit legs; the
    realised return is read from ``position_exits`` by whoever needs it
    (``position_exits`` is the one home that number has).

    Orders that never traded get no label at all — see the module
    docstring.
    """
    rows = conn.execute(
        "SELECT id, status FROM virtual_portfolio WHERE status IN "
        "('open', 'stopped', 'target_hit', 'expired') ORDER BY id LIMIT ?",
        (limit,)).fetchall()
    counts = {"pending": 0, "matured": 0, "unchanged": 0}
    for row in rows:
        position_id = int(row["id"])
        legs = conn.execute(
            "SELECT id, exit_date FROM position_exits WHERE position_id = ? "
            "ORDER BY id", (position_id,)).fetchall()
        open_now = row["status"] == "open"
        if not open_now and not legs:
            # Closed with no ledger legs: a pending order that was
            # cancelled, which is not a trade. No label.
            continue
        label_id = outcomes.ensure_label(
            conn, kind=outcomes.TRADE, subject_type=POSITION,
            subject_id=position_id,
            episode_id=episodes.episode_for_ref(conn, position_id),
            evaluator_version=TRADE_EVALUATOR,
            evidence={"position_id": position_id, "status": "open", "legs": 0},
            available_at=None)["id"]
        if open_now:
            counts["pending"] += 1
            continue

        leg_ids = [int(l["id"]) for l in legs]
        if outcomes.get(conn, label_id)["state"] != outcomes.PENDING:
            counts["unchanged"] += 1
            continue
        outcomes.resolve(
            conn, label_id, state=outcomes.MATURED,
            evaluator_version=TRADE_EVALUATOR,
            evidence={
                "position_id": position_id,
                # Refs, not values. The realised amount lives in
                # position_exits and is read from there.
                "exit_ids": leg_ids,
                "legs": len(leg_ids),
                "closed_on": legs[-1]["exit_date"],
                "status": row["status"],
            },
            available_at=legs[-1]["exit_date"] or as_of)
        counts["matured"] += 1
    return counts


# ── Process ────────────────────────────────────────────────────────────


def sweep_process_labels(conn: sqlite3.Connection, *, as_of: str,
                         limit: int = 200) -> dict:
    """Grade each thesis on the decision itself, and on how it ended.

    Two moments, one chain. The grade is knowable the day the thesis is
    written, so it matures immediately — waiting for the result before
    grading the plan is how process learning becomes outcome learning with
    extra steps. How it *ended* arrives later (a ``blind_spot`` is a thesis
    whose invalidation conditions never fired, which is a process failure
    and cannot be known in advance), so that is appended as a **revision**
    of the matured label rather than folded into it.

    That is also the production path for ``matured → revised``: a
    correction is a new row naming the one it corrects, never an edit.

    The three counts are disjoint: every thesis graded lands in exactly
    one bucket, so ``matured + revised + unchanged`` is the number of
    theses looked at and "how many needed nothing" is answerable.
    """
    theses = (T.get_active() + T.get_closed(days=400))[:limit]
    counts = {"matured": 0, "revised": 0, "unchanged": 0}
    for th in theses:
        grade = process_quality.grade_thesis(th)
        # Guarded here as well as in the store: this dict is built from a
        # grader that reads only the thesis, and the assertion is what
        # keeps it that way if the grader ever grows.
        outcomes.assert_no_pnl_in_process(grade)
        label_id = outcomes.ensure_label(
            conn, kind=outcomes.PROCESS, subject_type=THESIS,
            subject_id=int(th.id),
            episode_id=episodes.episode_for(conn, column="thesis_id",
                                            value=int(th.id)),
            evaluator_version=PROCESS_EVALUATOR,
            evidence=grade, available_at=as_of)["id"]
        state = outcomes.get(conn, label_id)["state"]

        just_matured = state == outcomes.PENDING
        if just_matured:
            label_id = outcomes.resolve(
                conn, label_id, state=outcomes.MATURED,
                evaluator_version=PROCESS_EVALUATOR,
                evidence=grade, available_at=as_of)
            state = outcomes.MATURED

        # ``status`` rather than ``close_kind``: a blind spot closed for no
        # condition at all, so its close_kind is empty and testing that
        # would silently skip exactly the ending this label exists to
        # record.
        ended = getattr(th, "status", "") in T.CLOSED_STATUSES
        # One bucket per thesis. A thesis that matures and is then revised
        # in the same sweep is counted once, as revised — the three numbers
        # must add up to the number of theses graded, or "how many did
        # nothing" is unanswerable.
        if ended and state == outcomes.MATURED:
            outcomes.resolve(
                conn, label_id, state=outcomes.REVISED,
                evaluator_version=PROCESS_EVALUATOR,
                evidence={**grade,
                          "close_kind": getattr(th, "close_kind", ""),
                          # The one process failure that is only knowable
                          # at the end: no invalidation condition ever
                          # fired, so the plan never covered the way it
                          # actually went.
                          "blind_spot": getattr(th, "status", "")
                          == T.BLIND_SPOT},
                available_at=as_of)
            counts["revised"] += 1
        elif just_matured:
            counts["matured"] += 1
        else:
            counts["unchanged"] += 1
    return counts
