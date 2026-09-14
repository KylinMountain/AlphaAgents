"""Nothing enters the playbook without surviving a held-out check.

Context evolution without a gate is high variance and unsafe, not merely
suboptimal: Dynamic Cheatsheet scored 70.7% on ALFWorld and 0.14 on
WebShop, where plain ReAct scored 0.43. The fix that held up was a
keep-better gate against a held-out slice — a candidate is admitted only
when it does not degrade performance there.

Two properties make the gate real rather than decorative:

  * Validation is *forward*: the days after the candidate came into
    existence. A proportional split of history is the tempting version
    and it leaks — a candidate is distilled from reflection on recent
    days, so the recent slice is exactly what it was fitted to.
    Reflection cannot reach forward in time, which is what makes this
    boundary sound rather than merely tidy.
  * Comparison is a paired test on the same predictions, not a
    difference of means. Day-to-day variance in A-share returns dwarfs
    any plausible playbook effect, so unpaired means would accept noise
    roughly half the time.

The cost is that a new candidate has no validation data on the day it is
created, so the gate abstains and the champion stands. That is the
intended behaviour: a system with no track record should not promote.

**Phase 4 / U3 makes the window underived-from-a-parameter.** The gate was
called once as ``run_gate("daily_playbook", today, today)`` — a zero-length
validation window, written by hand, which abstained every time and looked
like governance for four days. The fix is not "remember to pass the right
date": :func:`run_gate` now takes a *policy version* and derives the window
from that version's own ``frozen_at``, and it **raises** when the window
cannot contain forward evidence. A caller can no longer express the old
bug, and an impossible window is an error rather than an abstention —
"insufficient" for a window that could never have held anything is a
verdict-shaped way of saying nothing happened.

See docs/self_improvement_roadmap.md G2.
"""

import json
import logging
import sqlite3
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Below this the gate abstains and keeps the champion. Underpowered
# comparisons are worse than none — they launder noise as evidence.
#
# The unit is a **paired sample** — one ``(date, code)`` both sides scored — and
# not a day. The two were spelled the same way for as long as this constant has
# existed (``paired_keys`` counts samples; ``scripts/policy.py`` printed the
# result as "paired day(s)"). It matters because this number is the denominator
# a verdict rests on, which is the number an operator reads to decide whether
# asking for one is worth the trip. ``validation_days`` counts the distinct
# dates behind those samples, is recorded beside every verdict, and is *not*
# what this floor compares against.
MIN_VALIDATION_SAMPLES = 20

# The floor the repository's own rule declares — golden principles §7, "No
# conclusion with n < 50 reaches production code". This is **not** a gate
# threshold and this module does not enforce it: a verdict at n in
# [MIN_VALIDATION_SAMPLES, GOVERNANCE_MIN_SAMPLES) satisfies the gate *and*
# satisfies the version in force, while contradicting §7. Nothing refuses it.
# :func:`promotion_floor_gap` exists so that state is reportable rather than
# assumed away.
#
# It is a named constant instead of a number quoted in prose because a report
# has to compare a *version's* declared floor against it, and a report that
# parses markdown to find its own threshold has a second source of truth.
#
# Deliberately absent from ``policy_sources._RULE_SOURCES``. That tuple declares
# which constants are *behaviour*; hashing this one into every version would
# report a drift the moment it changed while nothing a trader does had moved.
GOVERNANCE_MIN_SAMPLES = 50

# Brier degradation tolerated before rejecting. Not zero: a candidate
# should not be blocked by rounding.
BRIER_TOLERANCE = 0.005

# The floor on how far back the champion's graded forecasts are read. The
# comparison itself is over the forward window; this bound exists so the scan
# does not grow with the database forever, and it is raised when a version is
# old enough that the window would otherwise be truncated. See
# :func:`_lookback_days`.
GATE_LOOKBACK_DAYS = 180


class GateError(ValueError):
    """A gate call that cannot produce evidence, or names nothing to compare.

    Distinct from an abstention on purpose. An abstention is a *verdict*:
    there was a real window and too little in it. This is raised when the
    question itself is malformed — no such policy version, no shadow run, or
    a window that ends before it starts.
    """


def forward_window(rows: list[dict], frozen_at: str) -> list[dict]:
    """The rows strictly after a freeze — the only days that can be evidence.

    The single definition of the boundary. §11's "validation is forward" is
    enforced by routing every caller through this function rather than by
    each of them comparing dates: a second comparison is a second chance to
    get the direction or the strictness wrong, and the failure is silent —
    the gate simply abstains.
    """
    cutoff = str(frozen_at)[:10]
    return [r for r in rows if r.get("date") and str(r["date"])[:10] > cutoff]


def _lookback_days(frozen_at: str, when: str) -> int:
    """How far back to read the champion so the forward window is not truncated.

    ``get_scored_predictions`` can only express its floor as "N days ago", and
    ``forward_window`` then cuts to dates after the freeze. A fixed N would
    silently drop the *oldest* days of a long-running version's forward window
    — evidence that exists in the database and that the gate would report as
    never having been collected. The bound is kept (an unbounded scan grows
    with the database forever) but it is derived, not chosen.
    """
    try:
        age = (datetime.strptime(when, "%Y-%m-%d")
               - datetime.strptime(frozen_at, "%Y-%m-%d")).days
    except ValueError:
        # Unparseable dates are not this function's error to report; the
        # window check in run_gate owns that. Read the bounded default.
        return GATE_LOOKBACK_DAYS
    return max(GATE_LOOKBACK_DAYS, age + 2)


def paired_brier_test(champion: list[float], challenger: list[float]) -> dict:
    """Paired comparison of two Brier series over the same predictions.

    Returns the mean difference (challenger - champion; negative is an
    improvement), a paired t statistic, and how many pairs it is based
    on. A t of -2 or beyond is the conventional bar, but the caller
    decides — this function only measures.
    """
    pairs = [(c, x) for c, x in zip(champion, challenger)
             if c is not None and x is not None]
    n = len(pairs)
    if n < 2:
        return {"n": n, "mean_diff": None, "t_stat": None}

    diffs = [x - c for c, x in pairs]
    mean_diff = sum(diffs) / n
    var = sum((d - mean_diff) ** 2 for d in diffs) / (n - 1)
    if var <= 0:
        # Identical series: no difference, and no variance to divide by.
        return {"n": n, "mean_diff": round(mean_diff, 6),
                "t_stat": 0.0 if mean_diff == 0 else None}

    se = (var / n) ** 0.5
    return {"n": n, "mean_diff": round(mean_diff, 6),
            "t_stat": round(mean_diff / se, 4) if se else None}


def paired_keys(champion_scores: list[dict],
                challenger_scores: list[dict]) -> set:
    """The (date, code) pairs both sides scored — the comparison's denominator.

    Declared once and used by both the verdict and the audit record, because
    "how many pairs was this based on" answered two ways is worse than not
    answered: the verdict would say ``insufficient`` while the recorded
    denominator said something else.
    """
    champ = {(r.get("date"), r.get("code")) for r in champion_scores}
    chall = {(r.get("date"), r.get("code")) for r in challenger_scores}
    return champ & chall


def evaluate_candidate(champion_scores: list[dict],
                       challenger_scores: list[dict]) -> dict:
    """Decide whether a challenger may be promoted.

    Both inputs are graded predictions keyed by (date, code), so the
    comparison is paired over the predictions both scored.

    ``outcome`` names the verdict in one word — ``insufficient``, ``reject``
    or ``promote`` — because the two booleans it summarises (``promote``,
    ``abstained``) are easy to read backwards, and the audit row is the thing
    a person actually reads months later.
    """
    champ_by_key = {(r.get("date"), r.get("code")): r for r in champion_scores}
    chall_by_key = {(r.get("date"), r.get("code")): r for r in challenger_scores}
    shared = sorted(paired_keys(champion_scores, challenger_scores))

    if len(shared) < MIN_VALIDATION_SAMPLES:
        return {
            "promote": False,
            "outcome": "insufficient",
            "reason": (f"验证样本 {len(shared)} < {MIN_VALIDATION_SAMPLES}，"
                       "维持 champion"),
            "n": len(shared),
            "abstained": True,
        }

    champ = [champ_by_key[k].get("brier") for k in shared]
    chall = [chall_by_key[k].get("brier") for k in shared]
    test = paired_brier_test(champ, chall)

    if test["mean_diff"] is None:
        return {"promote": False, "outcome": "insufficient",
                "reason": "无可比样本", "n": test["n"], "abstained": True}

    degraded = test["mean_diff"] > BRIER_TOLERANCE
    promote = not degraded
    if degraded:
        reason = (f"Brier 退化 {test['mean_diff']:+.4f} "
                  f"(t={test['t_stat']}, n={test['n']}) — 拒绝")
    else:
        reason = (f"Brier {test['mean_diff']:+.4f} 不退化 "
                  f"(t={test['t_stat']}, n={test['n']}) — 通过")

    return {"promote": promote,
            "outcome": "promote" if promote else "reject",
            "reason": reason, "abstained": False, **test}


def promotion_floor_gap(declared: int | None,
                        when: str = "the version") -> list[str]:
    """Complaints about a version's declared promotion floor, or an empty list.

    Reported rather than repaired, and this is the only place the report can be
    made: the floor a promotion actually re-checks is read from the **frozen
    version** — ``policy_registry`` re-reads
    ``holdout_gate.MIN_VALIDATION_SAMPLES`` out of the version's own ``rules``
    block — so "which number would let a promotion through" is a property of a
    version, not of the code in front of you. That is why the number is passed
    in and not read here.

    The gap is real and narrow: a verdict at n in [20, 50) passes the gate,
    passes the version's own declared floor, and contradicts §7 of the golden
    principles. Raising ``MIN_VALIDATION_SAMPLES`` to 50 would enforce §7 in
    code and turn every version already frozen into a drifted one, because a
    behaviour-changing edit after freezing is a new candidate — so the honest
    state is "declared and unenforced", and this function exists so that state
    is visible instead of assumed away. Deciding between the two is an
    operator's call, not this module's.

    ``when`` names the thing being judged, so the complaint reads as a sentence
    about a version or about an open experiment.
    """
    if declared is None:
        return [f"{when} declares no promotion floor, so the repository's "
                f"n < {GOVERNANCE_MIN_SAMPLES} rule (GOLDEN_PRINCIPLES §7) is "
                "the only boundary there is — and no code enforces it"]
    if declared < GOVERNANCE_MIN_SAMPLES:
        return [f"{when} declares a promotion floor of {declared} paired "
                f"sample(s), below the repository's "
                f"n >= {GOVERNANCE_MIN_SAMPLES} (GOLDEN_PRINCIPLES §7). A "
                f"verdict at n in [{declared}, {GOVERNANCE_MIN_SAMPLES}) would "
                "satisfy the gate and satisfy this version, and contradict "
                "§7 — nothing refuses it"]
    return []


_GATE_TABLE = """
CREATE TABLE IF NOT EXISTS gate_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    candidate TEXT NOT NULL,
    policy_version_id INTEGER,
    promoted INTEGER NOT NULL,
    abstained INTEGER NOT NULL DEFAULT 0,
    outcome TEXT,
    n INTEGER,
    validation_days INTEGER,
    evidence_scope TEXT,
    mean_diff REAL,
    t_stat REAL,
    reason TEXT,
    detail_json TEXT
)
"""

# For databases created before these columns existed. ``validation_days`` in
# particular: it used to live only inside ``detail_json``, which made
# tech-debt D7's own "recognise it is fixed" criterion — "a gate_decisions row
# whose validation_days is not 0" — uncheckable by SQL.
#
# ``policy_version_id`` is here so eligibility is keyed on the version rather
# than on the ``candidate`` label. The label is prose; a promotion that joins
# itself to its evidence by parsing prose gets an empty list the moment the
# label changes, and an empty eligibility list reads as "not eligible" — the
# safe-looking answer, and the same shape as the defect this phase fixes.
#
# ``evidence_scope`` is here for that same reason. It records who emitted the
# challenger's forecasts, and ``policy_registry`` refuses to promote on
# anything that is not a candidate's. It used to live only inside
# ``detail_json``, so reading it back meant parsing a payload — and there an
# absent key reads as "not baseline-only", which is to say as promotable. A
# guard whose missing case is the permissive one is not a guard.
_GATE_MIGRATIONS = (
    "ALTER TABLE gate_decisions ADD COLUMN outcome TEXT",
    "ALTER TABLE gate_decisions ADD COLUMN validation_days INTEGER",
    "ALTER TABLE gate_decisions ADD COLUMN policy_version_id INTEGER",
    "ALTER TABLE gate_decisions ADD COLUMN evidence_scope TEXT",
)


def _ensure_gate_table(conn: sqlite3.Connection) -> None:
    conn.execute(_GATE_TABLE)
    for statement in _GATE_MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError as exc:
            # Only the re-run case is expected. A bare pass here would also
            # swallow a locked database and leave the gate reading a column
            # that does not exist.
            if "duplicate column name" not in str(exc).lower():
                raise


def record_gate_decision(candidate_name: str, decision: dict,
                         today: str | None = None) -> None:
    """Persist the verdict, including rejections.

    A rejected candidate that leaves no trace will be proposed again next
    week and rejected again; the record is what makes that visible.

    ``validation_days`` is the number of distinct days the paired comparison
    actually rested on — not the length of the window. A window of thirty
    days on which the two sides never graded the same stock on the same day
    has zero days of evidence, and reporting the window length would make
    that look like a month of it.

    The version id is read from ``decision`` rather than passed alongside the
    name, so the row cannot record a name that says one version and a column
    that says another. ``evidence_scope`` is written as its own column for the
    same reason ``validation_days`` is: it is the fact a promotion is refused
    on, so it has to be readable without parsing the stored verdict.
    """
    from alpha_agents.data.memory_store import _get_conn, _write_lock

    today = today or datetime.now().strftime("%Y-%m-%d")
    try:
        with _write_lock:
            conn = _get_conn()
            _ensure_gate_table(conn)
            conn.execute(
                "INSERT INTO gate_decisions (date, candidate, "
                "policy_version_id, promoted, abstained, outcome, n, "
                "validation_days, evidence_scope, mean_diff, t_stat, reason, "
                "detail_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (today, candidate_name, decision.get("policy_version_id"),
                 1 if decision.get("promote") else 0,
                 1 if decision.get("abstained") else 0,
                 decision.get("outcome"), decision.get("n"),
                 decision.get("validation_days"),
                 decision.get("evidence_scope"), decision.get("mean_diff"),
                 decision.get("t_stat"), decision.get("reason", ""),
                 json.dumps(decision, ensure_ascii=False)),
            )
            conn.commit()
    except Exception as e:
        # The review must not fail because the audit did. Logged at warning
        # rather than debug: a verdict that leaves no record is the gap this
        # table exists to close, and a silent one is indistinguishable from
        # the gate never having run.
        logger.warning("Gate decision not recorded: %s", e)


def get_gate_history(days: int = 30) -> list[dict]:
    """Recent gate verdicts, newest first."""
    from alpha_agents.data.memory_store import _get_conn

    try:
        conn = _get_conn()
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT * FROM gate_decisions WHERE date >= ? "
            "ORDER BY id DESC", (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def run_gate(policy_version_id: int, *, report_type: str = "morning",
             today: str | None = None) -> dict:
    """Score a frozen version's challenger against the champion, and record it.

    **The window comes from the version, not from the caller.** There is no
    ``created_date`` parameter to pass and no default that can produce an
    empty window: the validation slice is the days strictly after the
    version's own ``frozen_at``. The bug this replaces was
    ``run_gate("daily_playbook", today, today)`` — a window the caller wrote
    by hand, which was zero days long, so the gate abstained on every run
    while appearing to govern. Removing the parameter removes the bug, and
    removing it is not the same as documenting it.

    A window that cannot contain evidence is refused rather than abstained
    on. Asking on the freeze day itself means "no day after the freeze has
    happened yet", which is a malformed question, not a verdict of
    insufficient evidence — and answering it ``insufficient`` would put a
    verdict-shaped row in the audit for a window that could never have held
    anything.

    The champion is the graded forecasts of the same ``report_type``; the
    challenger is the shadow run bound to this version. Both are cut to the
    forward window, and the comparison is paired on (date, code).

    The verdict also carries an ``evidence_scope``: which kind of producer
    emitted the challenger's forecasts. A verdict against the no-skill
    baseline says whether the champion has skill at all, and says nothing about
    whether a *policy* is better, so ``policy_registry`` refuses to promote on
    it. The scope is derived from ``shadow.PRODUCERS``, so it follows the code
    that produced the forecasts rather than anything the caller named.

    A version shadowed by more than one producer is refused rather than
    resolved: the gate grades one challenger, and silently taking the oldest
    run would make the verdict describe a comparison the reader has no reason
    to think was chosen. Refusing is the same posture as the window check — an
    ambiguous question is not answered quietly.
    """
    from alpha_agents.data import clock, policy_registry
    from alpha_agents.data.memory_store import get_scored_predictions
    from alpha_agents.evolution import shadow

    version = policy_registry.get_version(policy_version_id)
    if version is None:
        raise GateError(
            f"No policy version #{policy_version_id} to evaluate. A verdict "
            "has to name the policy it is about, or it cannot be acted on.")
    when = str(today or clock.today())[:10]
    frozen_at = str(version["frozen_at"])[:10]
    if frozen_at >= when:
        raise GateError(
            f"Policy version #{policy_version_id} was frozen on {frozen_at} "
            f"and the gate is being asked on {when}. Validation is forward: "
            "nothing dated at or before the freeze can be evidence for it, so "
            "this window cannot contain any. This is refused rather than "
            "recorded as insufficient — a window that could never hold "
            "evidence must not leave a verdict-shaped row behind.")

    open_runs = shadow.open_runs_for(policy_version_id, report_type)
    if len(open_runs) > 1:
        raise GateError(
            f"{len(open_runs)} shadow runs are open on policy version "
            f"#{policy_version_id} for {report_type!r}, by producers "
            f"{sorted(r['producer'] for r in open_runs)}. The gate grades one "
            "challenger, and picking one silently would let the verdict "
            "describe whichever run happened to be opened first — the shape of "
            "a gate that answers a question nobody asked. Close all but the "
            "run being evaluated.")
    run = (open_runs[0] if open_runs
           else shadow.latest_run_for(policy_version_id, report_type))
    if run is None:
        raise GateError(
            f"No shadow run measures policy version #{policy_version_id} on "
            f"{report_type!r}. There is nothing on the challenger side of the "
            "comparison, and a comparison against nothing is not a verdict.")

    champion = forward_window(
        get_scored_predictions(days=_lookback_days(frozen_at, when),
                               report_type=report_type),
        frozen_at)
    challenger = forward_window(shadow.scored_for(run["id"]), frozen_at)

    decision = evaluate_candidate(champion, challenger)
    decision["validation_days"] = len(
        {day for day, _code in paired_keys(champion, challenger)})
    decision["policy_version_id"] = policy_version_id
    decision["run_id"] = run["id"]
    # Read from the producer that emitted the forecasts, never from a label on
    # the run. A caller-supplied name decided this before: a run named anything
    # other than "constant_0.5" carried candidate-grade evidence while its
    # forecasts were the no-skill constant, so the promotion guard was
    # satisfied by the label rather than by the evidence.
    decision["evidence_scope"] = shadow.scope_for(run["producer"])

    name = f"{version['policy_key']}#{policy_version_id}"
    record_gate_decision(name, decision, when)
    logger.info("Gate '%s': %s", name, decision["reason"])
    return decision


def get_gate_decisions(*, policy_version_id: int | None = None,
                       limit: int = 200) -> list[dict]:
    """Recent verdicts, newest first, optionally for one policy version.

    Read side for the audit. ``get_gate_history`` answers "what has the gate
    said lately"; this answers "what has it said about *this* version", which
    is the question a promotion eligibility check asks.

    The filter is an equality on the stored id rather than a ``LIKE`` on the
    composed ``candidate`` label. Not because ``LIKE '%#12'`` would match
    ``#120`` — it would not, ``LIKE`` being anchored at the end — but because
    the label is prose: a promotion joined to its evidence by parsing prose
    returns an empty list the moment the label changes, and an empty
    eligibility list reads as "not eligible".
    """
    from alpha_agents.data.memory_store import _get_conn

    conn = _get_conn()
    _ensure_gate_table(conn)
    if policy_version_id is None:
        rows = conn.execute(
            "SELECT * FROM gate_decisions ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM gate_decisions WHERE policy_version_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (policy_version_id, limit)).fetchall()
    return [dict(row) for row in rows]


def eligible_decisions(policy_version_id: int) -> list[dict]:
    """Verdicts for this version that could support a promotion.

    A promotion needs a verdict that said *promote* — not merely one that
    compared something. The tempting predicate is "not abstained, and resting
    on at least one day of paired evidence", but a **rejection** satisfies
    both: it is a real verdict over a real window that says the challenger
    degrades the champion. Promoting on one of those would invert the
    decision the gate exists to make.

    This is also the machine form of D7's own acceptance criterion — "a
    ``gate_decisions`` row whose ``validation_days`` is not 0" — and it exists
    so the promotion service does not have to re-derive eligibility from
    ``detail_json``.
    """
    return [row for row in get_gate_decisions(
                policy_version_id=policy_version_id)
            if row["outcome"] == "promote"
            and not row["abstained"]
            and (row["validation_days"] or 0) > 0]
