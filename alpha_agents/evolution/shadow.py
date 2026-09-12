"""The challenger: a frozen baseline that forecasts and is graded, and nothing else.

§11 requires the experiment to "begin with one champion, one challenger, and an
appropriate frozen simple non-LLM baseline". ``holdout_gate`` has the decision
rule — a paired Brier test over the same predictions — and no producer, so the
challenger side of every comparison was empty. This module is the producer.

**Why a separate table, and not a ``_shadow`` suffix on ``predictions``.**

The obvious implementation is to write challenger forecasts into ``predictions``
with a distinguishable ``report_type`` and let the gate filter on it. That is
wrong here, and measurably so: four champion paths read ``predictions`` without
a report-type filter —

* ``get_today_intraday_predictions`` filters ``report_type LIKE 'intraday%'``,
  which matches ``intraday_signal_shadow`` — a shadow row would be handed to
  the intraday monitor as one of its own prior recommendations;
* ``decision_context`` reads every prediction for a date, so a shadow forecast
  would enter the recorded decision boundary;
* ``get_prediction_stats`` feeds the agents' prompts, so a shadow row would
  change what the champion is told about its own hit rate;
* ``principle_scoring`` grades principles off ``predictions``, so shadow
  results would contaminate champion learning.

Each of those could be fixed with another ``NOT LIKE '%_shadow'`` clause. That
would make isolation a property of remembering to write the clause in every
future reader, which is precisely the class of defect this repository keeps
finding. A separate table makes it structural: the champion's readers select
from ``predictions`` and cannot see a row that is not there. §11's "shadow
fills cannot reach the main account, and shadow-derived lessons cannot enter
the champion's active knowledge" then holds by construction rather than by
review.

**The baseline.** A constant 0.5 — "permanently uncertain". ``brier_score``'s
own docstring names 0.25 as "the line to beat", so this challenger asks the one
question a first experiment should ask: does the champion beat no skill at all.
It is non-LLM, deterministic, and has nothing to freeze beyond its own
definition, which is what makes it a usable bound rather than a second opinion.

**The panel is shared; the forecast is not.** The challenger forecasts exactly
the codes the champion forecast that day, because §12 requires prediction
comparison to use "a common opportunity panel" and a paired test over the same
predictions. Reading the champion's codes to define the panel is not
contamination — it fixes *what* is compared, and the probability is a constant
that does not look at the champion's. What is deliberately absent is any
treatment of abstentions or missing responses: the panel is the champion's
codes, the denominator is reported as ``paired`` in :func:`coverage`, and a
challenger that is missing rows is visibly missing them rather than scored as
if it had answered.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from alpha_agents.data import memory_store, policy_registry, scoring
from alpha_agents.evolution import holdout_gate

#: The baseline's forecast. 0.5 is the permanently-uncertain forecaster, whose
#: Brier is 0.25 — the line the champion has to beat.
BASELINE_PROB = 0.5
BASELINE_NAME = "constant_0.5"

#: Namespace prefix for a shadow book. §11 keeps the branches' cash, positions,
#: orders, working memory and knowledge separate; a distinct ``trader_id`` is
#: how that separation is visible wherever a trader is named.
SHADOW_TRADER_PREFIX = "shadow-"

DEFAULT_HORIZON_DAYS = scoring.DEFAULT_HORIZON_DAYS


class ShadowError(ValueError):
    """A shadow-run operation that would make the experiment unreadable."""


# ── Schema, owned here ─────────────────────────────────────────────────

_RUNS = """
CREATE TABLE IF NOT EXISTS shadow_runs (
    id INTEGER PRIMARY KEY,
    policy_version_id INTEGER NOT NULL,
    trader_id TEXT NOT NULL,
    report_type TEXT NOT NULL,
    baseline TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open', 'closed')),
    reason TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
)
"""

_PREDICTIONS = """
CREATE TABLE IF NOT EXISTS shadow_predictions (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    policy_version_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    prob REAL NOT NULL,
    horizon_days INTEGER NOT NULL,
    deadline TEXT NOT NULL,
    brier REAL,
    log_score REAL,
    excess_return REAL,
    residual_alpha REAL,
    outcome INTEGER,
    scored_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(run_id, date, code)
)
"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_shadow_runs_version "
    "ON shadow_runs(policy_version_id)",
    "CREATE INDEX IF NOT EXISTS idx_shadow_pred_run ON shadow_predictions(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_shadow_pred_due ON shadow_predictions(deadline)",
)


def init_schema(conn: sqlite3.Connection) -> None:
    """Create this module's tables on the supplied connection."""
    conn.execute(_RUNS)
    conn.execute(_PREDICTIONS)
    for statement in _INDEXES:
        conn.execute(statement)


# ── Validation ─────────────────────────────────────────────────────────


def _text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ShadowError(
            f"A shadow run needs a nonempty {field}: a run without one cannot "
            "be told apart from another afterwards.")
    return value.strip()


def _positive_id(value, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ShadowError(f"{field} must be a positive integer, got {value!r}")
    return value


# ── Runs ───────────────────────────────────────────────────────────────


def open_run(*, policy_version_id: int, reason: str,
             trader_id: str | None = None, report_type: str = "morning",
             baseline: str = BASELINE_NAME,
             opened_at: str | None = None) -> int:
    """Open a shadow run of one frozen policy version. Returns its id.

    Bound to a version rather than to "the current policy": the point of the
    experiment is that a *specific* frozen version was measured, and
    "promote version 7" has to mean the evaluation of version 7. A run whose
    version is not on record is refused — it could never be promoted, and
    recording it would be a promise with nothing behind it.

    Refuses a second *open* run for the same (version, report type, baseline):
    two shadows of one version would be counted twice by any later comparison,
    and the duplicate is silent.
    """
    _positive_id(policy_version_id, "policy_version_id")
    reason = _text(reason, "reason")
    report_type = _text(report_type, "report_type")
    baseline = _text(baseline, "baseline")
    when = _text(opened_at, "opened_at") if opened_at else _today()
    if policy_registry.get_version(policy_version_id) is None:
        raise ShadowError(
            f"No policy version #{policy_version_id} to shadow: a run that "
            "cannot name the policy it is measuring could never be promoted.")
    trader = _text(trader_id, "trader_id") if trader_id else \
        f"{SHADOW_TRADER_PREFIX}{policy_version_id}"

    with memory_store._write_lock:
        conn = memory_store._get_conn()
        init_schema(conn)
        with conn:
            existing = conn.execute(
                "SELECT id FROM shadow_runs WHERE policy_version_id = ? "
                "AND report_type = ? AND baseline = ? AND status = 'open'",
                (policy_version_id, report_type, baseline)).fetchone()
            if existing:
                raise ShadowError(
                    f"Policy version #{policy_version_id} already has an open "
                    f"{report_type} shadow of {baseline} (run #{existing['id']}). "
                    "Close it before opening another, or the two will be "
                    "counted as one experiment.")
            cursor = conn.execute(
                "INSERT INTO shadow_runs (policy_version_id, trader_id, "
                "report_type, baseline, status, reason, opened_at) "
                "VALUES (?, ?, ?, ?, 'open', ?, ?)",
                (policy_version_id, trader, report_type, baseline, reason, when))
    return int(cursor.lastrowid)


def close_run(run_id: int, *, reason: str,
              closed_at: str | None = None) -> None:
    """End a shadow run. Its predictions and scores stay readable."""
    reason = _text(reason, "reason")
    when = _text(closed_at, "closed_at") if closed_at else _today()
    with memory_store._write_lock:
        conn = memory_store._get_conn()
        init_schema(conn)
        with conn:
            row = conn.execute("SELECT status FROM shadow_runs WHERE id = ?",
                               (run_id,)).fetchone()
            if row is None:
                raise ShadowError(f"No shadow run #{run_id} to close.")
            if row["status"] == "closed":
                raise ShadowError(f"Shadow run #{run_id} is already closed.")
            conn.execute(
                "UPDATE shadow_runs SET status = 'closed', closed_at = ?, "
                "reason = reason || ' | closed: ' || ? WHERE id = ?",
                (when, reason, run_id))


def get_run(run_id: int) -> dict | None:
    conn = memory_store._get_conn()
    init_schema(conn)
    row = conn.execute("SELECT * FROM shadow_runs WHERE id = ?",
                       (run_id,)).fetchone()
    return dict(row) if row else None


def runs(*, status: str | None = None, limit: int = 200) -> list[dict]:
    conn = memory_store._get_conn()
    init_schema(conn)
    sql = ("SELECT * FROM shadow_runs "
           + ("WHERE status = ? " if status else "")
           + "ORDER BY id LIMIT ?")
    args = (status, limit) if status else (limit,)
    return [dict(row) for row in conn.execute(sql, args)]


def open_run_for(policy_version_id: int,
                 report_type: str = "morning") -> dict | None:
    """The open run shadowing a version, if there is one."""
    conn = memory_store._get_conn()
    init_schema(conn)
    row = conn.execute(
        "SELECT * FROM shadow_runs WHERE policy_version_id = ? "
        "AND report_type = ? AND status = 'open' ORDER BY id LIMIT 1",
        (policy_version_id, report_type)).fetchone()
    return dict(row) if row else None


# ── The producer ───────────────────────────────────────────────────────


def panel_for(date: str, report_type: str) -> list[str]:
    """The champion's codes for a date — the shared opportunity panel.

    §12: prediction comparison uses a common opportunity panel. Taking the
    champion's own selections is the panel that makes the comparison paired;
    grading the challenger on a different set of stocks would compare two
    different questions. The challenger's *probability* does not read anything
    here — only the set of codes is shared.
    """
    conn = memory_store._get_conn()
    rows = conn.execute(
        "SELECT DISTINCT code FROM predictions WHERE date = ? "
        "AND report_type = ? AND code IS NOT NULL ORDER BY code",
        (date, report_type)).fetchall()
    return [row["code"] for row in rows]


def emit_for_date(run_id: int, date: str, *,
                  panel: list[str] | None = None,
                  horizon_days: int = DEFAULT_HORIZON_DAYS) -> list[int]:
    """Write the challenger's forecasts for one date. Returns their ids.

    Idempotent per (run, date, code): re-emitting a day updates the forecast
    rather than adding a second row for the same stock, because two rows would
    be two forecasts and the paired test would count the stock twice.

    Emitting writes forecasts and nothing else. No order, no position, no
    intent — the challenger is graded, never traded.
    """
    run = get_run(run_id)
    if run is None:
        raise ShadowError(f"No shadow run #{run_id} to emit for.")
    if run["status"] != "open":
        raise ShadowError(
            f"Shadow run #{run_id} is closed; a closed experiment must not "
            "keep producing forecasts, or its denominator would grow after "
            "the fact.")
    if panel is None:
        panel = panel_for(date, run["report_type"])
    deadline = _deadline_for(date, horizon_days)
    if deadline is None:
        raise ShadowError(
            f"A {horizon_days}-day horizon does not produce a deadline; the "
            "forecast could never mature.")

    ids: list[int] = []
    with memory_store._write_lock:
        conn = memory_store._get_conn()
        init_schema(conn)
        with conn:
            for code in panel:
                code = _text(code, "code")
                conn.execute(
                    "INSERT INTO shadow_predictions (run_id, policy_version_id, "
                    "date, code, prob, horizon_days, deadline) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(run_id, date, code) DO UPDATE SET "
                    "prob = excluded.prob, horizon_days = excluded.horizon_days, "
                    "deadline = excluded.deadline",
                    (run_id, run["policy_version_id"], date, code,
                     BASELINE_PROB, int(horizon_days), deadline))
                row = conn.execute(
                    "SELECT id FROM shadow_predictions WHERE run_id = ? "
                    "AND date = ? AND code = ?", (run_id, date, code)).fetchone()
                ids.append(int(row["id"]))
    return ids


def _deadline_for(date: str, horizon_days: int) -> str | None:
    """The day a forecast matures. Calendar days, matching ``predictions``."""
    try:
        days = int(horizon_days)
    except (TypeError, ValueError):
        return None
    if days <= 0:
        return None
    return (datetime.strptime(str(date)[:10], "%Y-%m-%d")
            + timedelta(days=days)).strftime("%Y-%m-%d")


# ── Grading ────────────────────────────────────────────────────────────


def score_due(*, as_of: str | None = None, run_id: int | None = None) -> dict:
    """Grade shadow forecasts whose horizon has elapsed.

    The outcome comes from market data through ``data.scoring``, never from a
    model — §12: "the evaluator calculates measures from independent facts and
    a frozen protocol; it must not treat a model's confidence in itself as
    ground truth". A forecast whose market data is not there yet stays
    unscored rather than being graded as a miss, so an absent price cannot
    manufacture evidence.
    """
    when = _text(as_of, "as_of") if as_of else _today()
    conn = memory_store._get_conn()
    init_schema(conn)
    sql = ("SELECT * FROM shadow_predictions WHERE scored_at IS NULL "
           "AND deadline <= ?")
    args: list = [when]
    if run_id is not None:
        sql += " AND run_id = ?"
        args.append(run_id)
    sql += " ORDER BY date, code"

    graded = skipped = 0
    with memory_store._write_lock:
        for row in conn.execute(sql, tuple(args)).fetchall():
            result = scoring.score_prediction(
                row["code"], row["date"], row["prob"], row["horizon_days"])
            if result is None:
                skipped += 1
                continue
            conn.execute(
                "UPDATE shadow_predictions SET brier = ?, log_score = ?, "
                "excess_return = ?, residual_alpha = ?, outcome = ?, "
                "scored_at = ? WHERE id = ?",
                (result.get("brier"), result.get("log_score"),
                 result.get("excess_return"), result.get("residual_alpha"),
                 1 if result.get("outcome") else 0, result.get("scored_at"),
                 row["id"]))
            graded += 1
        conn.commit()
    return {"graded": graded, "unscorable": skipped, "as_of": when}


# ── Reading ────────────────────────────────────────────────────────────


def predictions_for(run_id: int) -> list[dict]:
    conn = memory_store._get_conn()
    init_schema(conn)
    rows = conn.execute(
        "SELECT * FROM shadow_predictions WHERE run_id = ? ORDER BY date, code",
        (run_id,)).fetchall()
    return [dict(row) for row in rows]


def scored_for(run_id: int) -> list[dict]:
    """The challenger's graded forecasts — what the gate compares on."""
    conn = memory_store._get_conn()
    init_schema(conn)
    rows = conn.execute(
        "SELECT * FROM shadow_predictions WHERE run_id = ? "
        "AND scored_at IS NOT NULL ORDER BY date, code", (run_id,)).fetchall()
    return [dict(row) for row in rows]


def paired_count(run_id: int) -> int:
    """Graded (date, code) pairs both sides scored.

    The honest progress number. ``evaluate_candidate`` takes the intersection
    of the two sides' keys, so a challenger with a hundred forecasts that
    share no date and code with the champion has an n of zero — and reporting
    the raw forecast count instead would make an experiment look far closer to
    a verdict than it is.
    """
    conn = memory_store._get_conn()
    init_schema(conn)
    row = conn.execute(
        "SELECT COUNT(*) n FROM shadow_predictions s "
        "WHERE s.run_id = ? AND s.scored_at IS NOT NULL AND s.brier IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM predictions p WHERE p.date = s.date "
        "  AND p.code = s.code AND p.scored_at IS NOT NULL "
        "  AND p.brier IS NOT NULL)",
        (run_id,)).fetchone()
    return int(row["n"])


def coverage(run_id: int | None = None) -> dict:
    """How far each shadow run is from having evidence.

    The only honest progress meter in this slice: it answers "how many days
    are left" rather than "is it done", because the answer is a function of
    the market's calendar and not of anything this code can hurry.
    """
    conn = memory_store._get_conn()
    init_schema(conn)
    out = []
    for run in (runs() if run_id is None else
                ([get_run(run_id)] if get_run(run_id) else [])):
        row = conn.execute(
            "SELECT COUNT(*) total, "
            "  COUNT(scored_at) scored, "
            "  COUNT(DISTINCT CASE WHEN scored_at IS NOT NULL THEN date END) days "
            "FROM shadow_predictions WHERE run_id = ?", (run["id"],)).fetchone()
        paired = paired_count(run["id"])
        out.append({
            "run_id": run["id"],
            "policy_version_id": run["policy_version_id"],
            "status": run["status"],
            "report_type": run["report_type"],
            "opened_at": run["opened_at"],
            "forecasts": int(row["total"]),
            "scored": int(row["scored"]),
            "scored_days": int(row["days"]),
            "paired": paired,
            "needed": holdout_gate.MIN_VALIDATION_SAMPLES,
            "remaining": max(0, holdout_gate.MIN_VALIDATION_SAMPLES - paired),
        })
    return {"runs": out}


def integrity() -> list[str]:
    """Structural complaints about the shadow record, or an empty list.

    Reports rather than repairs. Three things the write boundary cannot
    enforce afterwards: a run naming a policy version that no longer exists, a
    forecast belonging to no run, and a forecast whose ``policy_version_id``
    disagrees with its run's — the last would let a promotion cite an
    evaluation of a policy it did not run.
    """
    conn = memory_store._get_conn()
    init_schema(conn)
    problems: list[str] = []
    for run in runs():
        if policy_registry.get_version(run["policy_version_id"]) is None:
            problems.append(
                f"shadow run #{run['id']} measures policy version "
                f"#{run['policy_version_id']}, which does not exist")
    for row in conn.execute(
            "SELECT s.id, s.run_id, s.policy_version_id FROM shadow_predictions s "
            "LEFT JOIN shadow_runs r ON r.id = s.run_id "
            "WHERE r.id IS NULL").fetchall():
        problems.append(
            f"shadow prediction #{row['id']} belongs to run #{row['run_id']}, "
            "which does not exist")
    for row in conn.execute(
            "SELECT s.id, s.policy_version_id, r.policy_version_id AS run_version "
            "FROM shadow_predictions s JOIN shadow_runs r ON r.id = s.run_id "
            "WHERE s.policy_version_id != r.policy_version_id").fetchall():
        problems.append(
            f"shadow prediction #{row['id']} names policy version "
            f"#{row['policy_version_id']} but its run names "
            f"#{row['run_version']}")
    return problems


def counts() -> dict:
    conn = memory_store._get_conn()
    init_schema(conn)
    return {
        "runs": int(conn.execute(
            "SELECT COUNT(*) n FROM shadow_runs").fetchone()["n"]),
        "open_runs": int(conn.execute(
            "SELECT COUNT(*) n FROM shadow_runs WHERE status = 'open'"
        ).fetchone()["n"]),
        "forecasts": int(conn.execute(
            "SELECT COUNT(*) n FROM shadow_predictions").fetchone()["n"]),
        "scored": int(conn.execute(
            "SELECT COUNT(*) n FROM shadow_predictions WHERE scored_at IS NOT NULL"
        ).fetchone()["n"]),
    }


def summary(run_id: int | None = None) -> dict:
    """The shadow side in one dict, for a report or a CLI."""
    return {
        "counts": counts(),
        "coverage": coverage(run_id),
        "integrity": integrity(),
    }


def _today() -> str:
    from alpha_agents.data import clock
    return clock.today()
