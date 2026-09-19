"""The challenger: a registered producer that forecasts and is graded, and nothing else.

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

**Two producers, and what each is for.** §11 names three roles, not two: a
champion, a challenger, and "an appropriate frozen simple non-LLM baseline".

``constant_0.5`` is the baseline — a constant 0.5, "permanently uncertain".
``brier_score``'s own docstring names 0.25 as "the line to beat", so it asks the
one question a first experiment should ask: does the champion beat no skill at
all. It is non-LLM, deterministic, and has nothing to freeze beyond its own
definition, which is what makes it a usable bound rather than a second opinion.
It is **not** a candidate policy, and that difference decides whether a verdict
may promote: beating a no-skill bound answers "does the champion have skill",
not "is this policy better".

``remap_confidence`` is the candidate, and the first this build has had. It
applies **its own version's** confidence → probability mapping to the champion's
recorded confidence label. That is §12's module-level experiment: the picks and
the upstream signal are held fixed and one mapping is varied, so a pair over the
same codes compares the mapping rather than two different opinions. Before it
existed every verdict this repo could produce was baseline-only and nothing was
promotable — the mechanism was delivered and could not run.

:data:`PRODUCERS` records each producer's *kind*, ``holdout_gate`` writes the
kind into the verdict as its evidence scope, and ``policy_registry`` refuses to
promote on anything that is not a candidate's. So the scope follows from the
code that emitted the forecasts, never from a label the caller typed — see the
note on :data:`PRODUCERS`.

**The panel is shared, and so is the signal.** The challenger forecasts exactly
the codes the champion forecast that day, because §12 requires prediction
comparison to use "a common opportunity panel" and a paired test over the same
predictions. Reading the champion's codes to define the panel is not
contamination — it fixes *what* is compared. The candidate reads one thing more:
the champion's confidence label, because a mapping with nothing to map is not a
mapping. What it must not read is the champion's *probability*: that is the
number the verdict grades, and a challenger derived from it would be grading
itself. What is deliberately absent is any treatment of abstentions or missing
responses: the panel is the champion's codes, the denominator is reported as
``paired`` in :func:`coverage`, and a challenger that is missing rows is visibly
missing them rather than scored as if it had answered.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from alpha_agents.data import memory_store, policy_registry, scoring
from alpha_agents.evolution import experiment_manifest, holdout_gate

#: The baseline's forecast. 0.5 is the permanently-uncertain forecaster, whose
#: Brier is 0.25 — the line the champion has to beat.
BASELINE_PROB = 0.5
BASELINE_NAME = "constant_0.5"
BASELINE_ONLY_SCOPE = "baseline_only"

#: The kinds a producer may declare. Only a candidate's verdict may promote.
KIND_BASELINE = "baseline"
KIND_CANDIDATE = "candidate"

#: Namespace prefix for a shadow book. §11 keeps the branches' cash, positions,
#: orders, working memory and knowledge separate; a distinct ``trader_id`` is
#: how that separation is visible wherever a trader is named.
SHADOW_TRADER_PREFIX = "shadow-"

DEFAULT_HORIZON_DAYS = scoring.DEFAULT_HORIZON_DAYS


class ShadowError(ValueError):
    """A shadow-run operation that would make the experiment unreadable."""


# ── Producers ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DecisionContext:
    """The world a shadow run is bound to.

    ``params`` is the decision-parameter block of the version the run measures,
    read out of that version rather than out of the running system — which is
    the only reason two versions can produce two different forecasts for the
    same day. ``signals`` is the champion's recorded confidence label per code,
    the upstream a mapping needs in order to be a mapping.

    Both are resolved **before** the write lock is taken and passed in, so the
    forecast never reaches back into a database while its own row is being
    written.
    """

    policy_version_id: int
    report_type: str
    params: dict
    signals: dict


@dataclass(frozen=True)
class Producer:
    """Who emits a run's forecasts, and what its verdict may support.

    ``forecast`` takes the date, the code and the :class:`DecisionContext` the
    run is bound to, and returns a probability. The baseline ignores all three,
    which is the point of it; a candidate reads the context, because the
    version's parameters are what it is a candidate *of* — a producer that
    could not see them could not differ from the champion it is measured
    against.
    """

    name: str
    kind: str
    forecast: Callable[[str, str, DecisionContext], float]
    # Policy genes this producer actually executes while producing the number
    # the gate grades. A parent path covers all of its leaf genes.
    observed_genes: frozenset[str] = frozenset()


#: The candidate producer's name, registered in :data:`PRODUCERS` below.
CANDIDATE_NAME = "remap_confidence"


def remap_confidence(date: str, code: str, ctx: DecisionContext) -> float:
    """The champion's signal, through this version's mapping.

    The challenger. §12's module-level experiment: the opportunity panel and the
    upstream confidence labels are the champion's and are held fixed, and the
    confidence → probability mapping is the one variable the version asserts.

    What this must never read is the champion's ``prob``. That number is what
    the verdict grades, and a challenger computed from it would be comparing a
    mapping against itself. A code the champion recorded no label for falls to
    a coin flip, which is what an unknown label means everywhere else; it is
    still counted in the panel, because dropping it would be a coverage
    decision made silently and :func:`coverage` is where that belongs.

    The limit worth stating: the champion's own probability came from the
    *whole* block applied on its own path — the evidence-count branch where the
    caller had the count, the label branch otherwise — while this can only
    apply the label branch, because a label is the only input the champion
    records. So the pair measures the label path, which is the path most of the
    book takes, and a promotion changes both.
    """
    return scoring.confidence_to_prob(ctx.signals.get(code), params=ctx.params)


#: The producers a shadow run may name, by name.
#:
#: Registration is what makes a producer real, and it is deliberately the only
#: route a name can take. A run used to record a free-text ``baseline`` label
#: that decided the verdict's evidence scope while the emitter wrote a constant
#: regardless of it: naming a run anything other than ``constant_0.5`` produced
#: "candidate_policy" evidence out of the no-skill baseline, so the guard
#: written to stop exactly that was satisfied by the label rather than by the
#: data. The name has to resolve here now, which is what ties the scope to the
#: emitter instead of to the caller.
#:
#: ``kind`` is fixed per name and must not be re-assigned: a verdict's scope is
#: read back through this table, so changing a name's kind would silently
#: reinterpret verdicts already in the audit.
#:
#: One baseline and one candidate. The baseline answers "does the champion have
#: skill"; the candidate is what a verdict can actually promote on. Neither is
#: an LLM, and that is a requirement rather than a preference: §11 asks for a
#: frozen simple non-LLM baseline, and §12 forbids treating a model's confidence
#: in itself as ground truth.
PRODUCERS: dict[str, Producer] = {
    BASELINE_NAME: Producer(
        name=BASELINE_NAME, kind=KIND_BASELINE,
        forecast=lambda date, code, ctx: BASELINE_PROB),
    CANDIDATE_NAME: Producer(
        name=CANDIDATE_NAME, kind=KIND_CANDIDATE,
        forecast=remap_confidence,
        # dims_passed is None in remap_confidence, so confidence_to_prob takes
        # only this branch. dim_base/dim_step/theme_gate cannot affect it.
        observed_genes=frozenset({"decision.confidence_priors"})),
}


def scope_for(producer_name: str) -> str:
    """The evidence scope a verdict from this producer is allowed to carry.

    Both branches are spelled out rather than "baseline, else candidate". An
    unregistered name, or a kind this module never defined, is a fact nobody
    can justify; letting it fall through to the promotable scope is how a shut
    gate reopens. Unknown fails closed, and loudly.
    """
    producer = PRODUCERS.get(producer_name)
    if producer is None:
        raise ShadowError(
            f"Shadow run names producer {producer_name!r}, which is not "
            "registered. Its forecasts exist but nothing says what produced "
            "them, so a verdict on them would carry a scope nobody can "
            "justify.")
    if producer.kind == KIND_BASELINE:
        return BASELINE_ONLY_SCOPE
    if producer.kind == KIND_CANDIDATE:
        return policy_registry.SCOPE_CANDIDATE
    raise ShadowError(
        f"Producer {producer_name!r} declares kind {producer.kind!r}, which "
        "this module does not define. A kind nobody defined cannot be read as "
        "either baseline or candidate.")


def _flatten_genes(value, prefix: str = "") -> dict[str, object]:
    """Flatten frozen policy sources into leaf gene paths."""
    if not isinstance(value, dict):
        return {prefix: value}
    out: dict[str, object] = {}
    for key in sorted(value):
        child = f"{prefix}.{key}" if prefix else str(key)
        out.update(_flatten_genes(value[key], child))
    return out


def _reference_version_id(version: dict) -> int | None:
    """Return the policy version a candidate is compared against."""
    parent = version.get("parent_id")
    if parent:
        return int(parent)
    pointer = policy_registry.active(version["policy_key"])
    if pointer and pointer["version_id"] != version["id"]:
        return int(pointer["version_id"])
    return None


def producer_compatibility(policy_version_id: int,
                           producer_name: str,
                           reference_version_id: int | None = None) -> dict:
    """Whether a producer executes every frozen source gene that changed.

    A version id proves lineage, not intervention. More samples cannot repair an
    experiment whose producer never executes the changed policy gene.
    """
    producer = PRODUCERS.get(producer_name)
    if producer is None:
        return {
            "compatible": False, "reference_version_id": None,
            "changed_genes": [], "uncovered_genes": [],
            "reason": f"producer {producer_name!r} is not registered",
        }
    if producer.kind == KIND_BASELINE:
        return {
            "compatible": True, "reference_version_id": None,
            "changed_genes": [], "uncovered_genes": [], "reason": "baseline",
        }

    version = policy_registry.get_version(policy_version_id)
    if version is None:
        return {
            "compatible": False, "reference_version_id": None,
            "changed_genes": [], "uncovered_genes": [],
            "reason": f"policy version #{policy_version_id} does not exist",
        }

    reference_id = (reference_version_id
                    if reference_version_id is not None
                    else _reference_version_id(version))
    if reference_id is None:
        return {
            "compatible": False, "reference_version_id": None,
            "changed_genes": [], "uncovered_genes": [],
            "reason": (
                f"policy version #{policy_version_id} has neither an explicit "
                "parent nor a distinct incumbent. The experiment cannot "
                "establish what changed, so candidate-grade evidence would be "
                "a version label without an intervention."),
        }

    before_sources = policy_registry.sources_of(reference_id)
    after_sources = policy_registry.sources_of(policy_version_id)
    if not before_sources or not after_sources:
        return {
            "compatible": False, "reference_version_id": reference_id,
            "changed_genes": [], "uncovered_genes": [],
            "reason": (
                f"policy version #{policy_version_id} or its reference "
                f"#{reference_id} has no frozen sources to diff."),
        }

    before = _flatten_genes(before_sources)
    after = _flatten_genes(after_sources)
    missing = object()
    changed = sorted(
        key for key in (set(before) | set(after))
        if before.get(key, missing) != after.get(key, missing)
    )
    if not changed:
        return {
            "compatible": False, "reference_version_id": reference_id,
            "changed_genes": [], "uncovered_genes": [],
            "reason": (
                f"policy version #{policy_version_id} has no changed gene "
                f"relative to reference version #{reference_id}; a candidate "
                "shadow would compare a policy with itself."),
        }

    def observed(gene: str) -> bool:
        return any(
            gene == scope or gene.startswith(scope + ".")
            for scope in producer.observed_genes
        )

    uncovered = [gene for gene in changed if not observed(gene)]
    if uncovered:
        return {
            "compatible": False, "reference_version_id": reference_id,
            "changed_genes": changed, "uncovered_genes": uncovered,
            "reason": (
                f"producer {producer_name!r} cannot evaluate policy version "
                f"#{policy_version_id}: changed gene(s) "
                f"{', '.join(uncovered)} are outside its observed genes "
                f"{sorted(producer.observed_genes)}. A verdict would grade a "
                "policy change the producer never executed."),
        }

    return {
        "compatible": True, "reference_version_id": reference_id,
        "changed_genes": changed, "uncovered_genes": [],
        "reason": "all changed genes are observed by the producer",
    }


def assert_producer_compatible(policy_version_id: int,
                               producer_name: str) -> dict:
    """Return the experiment contract or refuse before evidence is written."""
    result = producer_compatibility(policy_version_id, producer_name)
    if not result["compatible"]:
        raise ShadowError(result["reason"])
    return result



def prob_coverage(report_type: str, *, days: int = 30,
                  conn: sqlite3.Connection | None = None) -> dict:
    """Whether a report type's book can be paired at all, measured.

    A paired sample needs **both** sides scored, and ``paired_count`` requires
    ``brier IS NOT NULL`` on the champion row. ``brier`` is written only for a
    row that carried a ``prob``, and ``prob`` is written only for an
    ``actionable`` pick — a limit-up ``signal`` row is an observation, not a
    forecast, and carries none by design.

    So a report type whose rows are all signals is not a slow experiment: it
    is one that can never reach n=1, however long it runs. The operator would
    see ``0/20`` every day and read it as "early" rather than "impossible".

    This is measured from the book rather than declared, because the split
    between the two populations is a property of the code path that wrote the
    rows — ``intraday_signal`` and ``intraday`` come from the same function,
    one branch apart — and a constant list here would drift from it silently.
    """
    conn = conn if conn is not None else memory_store._get_conn()
    row = conn.execute(
        "SELECT COUNT(*) rows, "
        "  SUM(prob IS NOT NULL) with_prob, "
        "  SUM(brier IS NOT NULL) with_brier, "
        "  COUNT(DISTINCT date) days, "
        "  MAX(date) newest "
        "FROM predictions WHERE report_type = ? "
        "  AND date >= date('now', ?)",
        (report_type, f"-{int(days)} day")).fetchone()
    rows = int(row["rows"] or 0)
    with_prob = int(row["with_prob"] or 0)
    return {
        "report_type": report_type,
        "rows": rows,
        "with_prob": with_prob,
        "with_brier": int(row["with_brier"] or 0),
        "days": int(row["days"] or 0),
        "newest": row["newest"],
        "pairable": with_prob > 0,
        "window_days": int(days),
    }


def assert_pairable(report_type: str, *, days: int = 30) -> dict:
    """Refuse a report type that has never carried a probability.

    Called by ``open_run``. Opening an experiment on such a book is not a
    harmless no-op: it writes a run row, the daily task emits forecasts
    against an empty panel, ``paired_count`` stays at zero forever, and the
    progress line reads ``0/20`` — a shape identical to "the experiment just
    started". The operator has no way to tell the two apart from the report,
    which is the defect this refuses.

    A book with **no rows at all** is not refused: that is a fresh deployment
    or a report type nothing has run yet, and refusing it would make the
    experiment impossible to start before the data exists. The refusal is
    specifically "this type has a history and none of it can be graded".
    """
    cov = prob_coverage(report_type, days=days)
    if cov["rows"] > 0 and not cov["pairable"]:
        raise ShadowError(
            f"Report type {report_type!r} cannot be paired: {cov['rows']} "
            f"prediction(s) in the last {cov['window_days']} days and **none** "
            "carries a prob, so none can be scored for Brier. A paired sample "
            "needs both sides graded, so this experiment would report 0/needed "
            "forever — indistinguishable from one that just started. Pick a "
            "report type whose rows carry probabilities (the actionable book, "
            "e.g. 'intraday'), or open the experiment once such rows exist.")
    return cov


# ── Schema, owned here ─────────────────────────────────────────────────


_RUNS = """
CREATE TABLE IF NOT EXISTS shadow_runs (
    id INTEGER PRIMARY KEY,
    policy_version_id INTEGER NOT NULL,
    trader_id TEXT NOT NULL,
    report_type TEXT NOT NULL,
    producer TEXT NOT NULL,
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


def _rename_legacy_producer_column(conn: sqlite3.Connection) -> None:
    """``baseline`` was this column's name while the baseline was its only value.

    It holds the name of the registered producer, and that name is what decides
    a verdict's evidence scope — so once a second producer exists the old name
    describes one case of a general thing and reads as though the column were
    the comparison's baseline rather than its emitter. Renamed rather than
    shadowed by a second column: two columns for one fact is how a row ends up
    disagreeing with itself.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(shadow_runs)")}
    if "baseline" in columns and "producer" not in columns:
        conn.execute("ALTER TABLE shadow_runs RENAME COLUMN baseline TO producer")




def _migrate_run_contract_columns(conn: sqlite3.Connection) -> None:
    """Add experiment binding/seal columns to databases created before them."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(shadow_runs)")}
    additions = {
        "manifest_id": "INTEGER",
        "sealed_at": "TEXT",
        "gate_decision_id": "INTEGER",
    }
    for name, kind in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE shadow_runs ADD COLUMN {name} {kind}")


def manifest_for_run(run_id: int) -> dict | None:
    """The immutable experiment question bound to a shadow run."""
    conn = memory_store._get_conn()
    init_schema(conn)
    return experiment_manifest.for_run(conn, run_id)


def assert_run_evaluable(run: dict) -> dict | None:
    """Validate the frozen question a run is accumulating evidence for.

    Legacy baselines may have no manifest because their evidence is explicitly
    non-promotable. Candidate-grade evidence never gets that exception.
    """
    producer = PRODUCERS.get(run["producer"])
    if producer is None:
        raise ShadowError(
            f"Shadow run #{run['id']} names unregistered producer "
            f"{run['producer']!r}.")

    manifest = manifest_for_run(run["id"])
    if manifest is None:
        if producer.kind == KIND_BASELINE:
            assert_producer_compatible(
                run["policy_version_id"], run["producer"])
            return None
        raise ShadowError(
            f"Shadow run #{run['id']} predates the immutable experiment "
            "manifest. Candidate-grade evidence must state the intervention, "
            "evaluator and stopping rule before samples accumulate.")

    for field in ("policy_version_id", "producer", "report_type"):
        if manifest[field] != run[field]:
            raise ShadowError(
                f"Shadow run #{run['id']} disagrees with its manifest on "
                f"{field}: run={run[field]!r}, manifest={manifest[field]!r}.")

    compatibility = producer_compatibility(
        run["policy_version_id"], run["producer"],
        reference_version_id=manifest["reference_version_id"])
    if not compatibility["compatible"]:
        raise ShadowError(compatibility["reason"])
    if compatibility["changed_genes"] != manifest["changed_genes"]:
        raise ShadowError(
            f"Shadow run #{run['id']} changed-gene set no longer matches its "
            "manifest.")
    if sorted(producer.observed_genes) != manifest["observed_genes"]:
        raise ShadowError(
            f"Shadow run #{run['id']} producer observation surface changed "
            "after the experiment opened.")
    if (manifest["evaluator"] != experiment_manifest.EVALUATOR
            or manifest["metric"] != experiment_manifest.METRIC):
        raise ShadowError(
            f"Shadow run #{run['id']} names an evaluator this build does not "
            "implement.")
    if int(manifest["minimum_samples"]) <= 0:
        raise ShadowError(
            f"Shadow run #{run['id']} has a non-positive stopping floor.")
    return manifest


def seal_run(run_id: int, *, gate_decision_id: int, reason: str,
             sealed_at: str | None = None) -> None:
    """Seal a preregistered experiment after its final persisted verdict."""
    _positive_id(run_id, "run_id")
    _positive_id(gate_decision_id, "gate_decision_id")
    reason = _text(reason, "reason")
    when = _text(sealed_at, "sealed_at") if sealed_at else _today()
    with memory_store._write_lock:
        conn = memory_store._get_conn()
        init_schema(conn)
        with conn:
            experiment_manifest.seal(
                conn, run_id=run_id, gate_decision_id=gate_decision_id,
                reason=reason, sealed_at=when)


def init_schema(conn: sqlite3.Connection) -> None:
    """Create this module's tables on the supplied connection."""
    conn.execute(_RUNS)
    conn.execute(_PREDICTIONS)
    experiment_manifest.init_schema(conn)
    _rename_legacy_producer_column(conn)
    _migrate_run_contract_columns(conn)
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
             trader_id: str | None = None, report_type: str = "intraday",
             producer: str = BASELINE_NAME,
             opened_at: str | None = None) -> int:
    """Open a shadow run of one frozen policy version. Returns its id.

    Bound to a version rather than to "the current policy": the point of the
    experiment is that a *specific* frozen version was measured, and
    "promote version 7" has to mean the evaluation of version 7. A run whose
    version is not on record is refused — it could never be promoted, and
    recording it would be a promise with nothing behind it.

    The producer must be registered, and it is not a label. Its name is read
    back to decide the verdict's evidence scope, so a name with no emitter
    behind it would let a run claim a scope its forecasts never earned — the
    same defect as a version that is not on record, one level down.

    Refuses a second *open* run for the same (version, report type, producer):
    two shadows of one version would be counted twice by any later comparison,
    and the duplicate is silent.
    """
    _positive_id(policy_version_id, "policy_version_id")
    reason = _text(reason, "reason")
    report_type = _text(report_type, "report_type")
    producer = _text(producer, "producer")
    if producer not in PRODUCERS:
        raise ShadowError(
            f"No registered producer named {producer!r}, so nothing would emit "
            "this run's forecasts. A run may only name a producer that exists: "
            "the name decides the verdict's evidence scope, and a scope nobody "
            f"emitted is a promise with nothing behind it. Registered: "
            f"{sorted(PRODUCERS)}.")
    when = _text(opened_at, "opened_at") if opened_at else _today()
    if policy_registry.get_version(policy_version_id) is None:
        raise ShadowError(
            f"No policy version #{policy_version_id} to shadow: a run that "
            "cannot name the policy it is measuring could never be promoted.")
    compatibility = assert_producer_compatible(policy_version_id, producer)
    p = PRODUCERS[producer]
    manifest_payload = experiment_manifest.build(
        policy_version_id=policy_version_id,
        reference_version_id=compatibility["reference_version_id"],
        producer=producer, producer_kind=p.kind, report_type=report_type,
        changed_genes=compatibility["changed_genes"],
        observed_genes=sorted(p.observed_genes),
        minimum_samples=holdout_gate.MIN_VALIDATION_SAMPLES,
        brier_tolerance=holdout_gate.BRIER_TOLERANCE,
        opened_at=when)
    # Refuse a book that can never be paired, before writing the run row.
    # Checked here rather than in the CLI so both doors — the scheduled task
    # and ``scripts/policy.py`` — get the same answer.
    assert_pairable(report_type)
    trader = _text(trader_id, "trader_id") if trader_id else \
        f"{SHADOW_TRADER_PREFIX}{policy_version_id}"

    with memory_store._write_lock:
        conn = memory_store._get_conn()
        init_schema(conn)
        with conn:
            existing = conn.execute(
                "SELECT id FROM shadow_runs WHERE policy_version_id = ? "
                "AND report_type = ? AND producer = ? AND status = 'open'",
                (policy_version_id, report_type, producer)).fetchone()
            if existing:
                raise ShadowError(
                    f"Policy version #{policy_version_id} already has an open "
                    f"{report_type} shadow of {producer} (run #{existing['id']}). "
                    "Close it before opening another, or the two will be "
                    "counted as one experiment.")
            manifest_id = experiment_manifest.write(conn, manifest_payload)
            cursor = conn.execute(
                "INSERT INTO shadow_runs (policy_version_id, trader_id, "
                "report_type, producer, status, reason, opened_at, manifest_id) "
                "VALUES (?, ?, ?, ?, 'open', ?, ?, ?)",
                (policy_version_id, trader, report_type, producer, reason, when,
                 manifest_id))
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
    """The open run shadowing a version, if there is one.

    Ambiguous by construction once more than one producer shadows a version,
    so callers should prefer :func:`open_runs_for` and say what they do about
    the second one rather than take whichever is oldest.
    """
    conn = memory_store._get_conn()
    init_schema(conn)
    row = conn.execute(
        "SELECT * FROM shadow_runs WHERE policy_version_id = ? "
        "AND report_type = ? AND status = 'open' ORDER BY id LIMIT 1",
        (policy_version_id, report_type)).fetchone()
    return dict(row) if row else None


def open_runs_for(policy_version_id: int,
                  report_type: str = "morning") -> list[dict]:
    """Every open run shadowing a version, oldest first.

    A version can carry one open run per producer — the baseline as a
    calibration reference alongside the candidate under test. Anything that
    grades "the challenger" therefore has to say what it does when there is
    more than one, because the alternative is to pick one silently and let the
    verdict describe a run the reader has no reason to think was chosen.
    """
    conn = memory_store._get_conn()
    init_schema(conn)
    rows = conn.execute(
        "SELECT * FROM shadow_runs WHERE policy_version_id = ? "
        "AND report_type = ? AND status = 'open' ORDER BY id",
        (policy_version_id, report_type)).fetchall()
    return [dict(row) for row in rows]


def latest_run_for(policy_version_id: int,
                   report_type: str = "morning") -> dict | None:
    """The most recent run for a version, open or closed.

    A closed run is still the challenger's record. Closing means "stop
    forecasting", not "unmeasure what was forecast": a version promoted on the
    strength of a run must still be evaluable afterwards, or the evidence a
    promotion cited becomes unreadable the moment it is acted on.
    """
    conn = memory_store._get_conn()
    init_schema(conn)
    row = conn.execute(
        "SELECT * FROM shadow_runs WHERE policy_version_id = ? "
        "AND report_type = ? ORDER BY id DESC LIMIT 1",
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


def champion_horizons(date: str, report_type: str) -> dict[str, int]:
    """The horizon each champion forecast declared, per code.

    **The challenger must answer the same question.** ``review.py`` grades
    each champion row over the horizon *that row declared*, falling back to
    the global default only for rows written before the declaration existed.
    A challenger that emitted a fixed horizon would be compared against the
    champion on two different windows whenever they disagree — and the repo
    has already named this defect in ``review.py``: *"Grading a 3-day call
    over 5 days measured neither, and doing it silently made the two books
    comparable on a window only one of them chose."*

    Measured on the live book the day this was written: the champion's
    ``intraday`` rows for 2026-09-16 declare ``horizon_days = 3``, while the
    first challenger emitted at 5. Every pair from that day would have been
    two different questions.

    A code with two rows — two traders on the same day — takes the earliest
    by ``id``, matching ``_champion_signals``, which makes the same choice for
    the same reason and states it there.
    """
    conn = memory_store._get_conn()
    rows = conn.execute(
        "SELECT code, horizon_days FROM predictions WHERE date = ? "
        "AND report_type = ? AND code IS NOT NULL ORDER BY id",
        (date, report_type)).fetchall()
    out: dict[str, int] = {}
    for row in rows:
        if row["code"] in out:
            continue
        out[row["code"]] = int(row["horizon_days"] or DEFAULT_HORIZON_DAYS)
    return out


def emit_for_date(run_id: int, date: str, *,
                  panel: list[str] | None = None,
                  horizon_days: int | None = None) -> list[int]:
    """Write the challenger's forecasts for one date. Returns their ids.

    The forecast comes from the run's registered producer, looked up by the
    name the run recorded. That lookup is the only place a probability is
    decided, so "which producer emitted this" — the fact a verdict's evidence
    scope is derived from — is answered by the same act that writes the row.
    The producer is handed the *version it is bound to*: the parameters come
    from ``policy_version_id``, not from the configuration in force, which is
    what lets a challenger differ from the champion it is compared against.

    ``horizon_days`` defaults to **the horizon the champion declared for that
    code**, not to the global default. The pairing is the whole experiment: a
    challenger graded over a different window than the champion is a
    comparison of two questions, which is the failure ``review.py`` already
    documents. Pass an explicit value only to override deliberately, and note
    that doing so re-introduces the mismatch it exists to prevent.

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
    producer = PRODUCERS.get(run["producer"])
    if producer is None:
        raise ShadowError(
            f"Shadow run #{run_id} names producer {run['producer']!r}, which "
            "is not registered. Nothing can emit its forecasts: a row written "
            "by a producer the run does not name would be graded as the run's "
            "own, and its verdict would describe evidence it never produced.")
    # Re-check the immutable question before every write. Candidate runs that
    # predate manifests stop here rather than being grandfathered.
    assert_run_evaluable(run)
    if panel is None:
        panel = panel_for(date, run["report_type"])
    declared = champion_horizons(date, run["report_type"]) if horizon_days is None \
        else {}

    def _horizon(code: str) -> int:
        if horizon_days is not None:
            return int(horizon_days)
        return int(declared.get(code, DEFAULT_HORIZON_DAYS))
    # Resolved before the lock: the parameters of the version being measured,
    # and the champion's labels for the panel it will be paired against.
    ctx = DecisionContext(
        policy_version_id=run["policy_version_id"],
        report_type=run["report_type"],
        params=scoring.decision_params_of(run["policy_version_id"]),
        signals=_champion_signals(date, panel, run["report_type"]))

    ids: list[int] = []
    with memory_store._write_lock:
        conn = memory_store._get_conn()
        init_schema(conn)
        with conn:
            for code in panel:
                code = _text(code, "code")
                # Per code, because the champion declares per row. A single
                # deadline for the whole panel would answer a different
                # question for every code whose champion chose otherwise.
                code_horizon = _horizon(code)
                deadline = _deadline_for(date, code_horizon)
                if deadline is None:
                    raise ShadowError(
                        f"A {code_horizon}-day horizon does not produce a "
                        f"deadline for {code}; the forecast could never mature.")
                conn.execute(
                    "INSERT INTO shadow_predictions (run_id, policy_version_id, "
                    "date, code, prob, horizon_days, deadline) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(run_id, date, code) DO UPDATE SET "
                    "prob = excluded.prob, horizon_days = excluded.horizon_days, "
                    "deadline = excluded.deadline",
                    (run_id, run["policy_version_id"], date, code,
                     float(producer.forecast(date, code, ctx)),
                     code_horizon, deadline))
                row = conn.execute(
                    "SELECT id FROM shadow_predictions WHERE run_id = ? "
                    "AND date = ? AND code = ?", (run_id, date, code)).fetchone()
                ids.append(int(row["id"]))
    return ids


def _champion_signals(date: str, codes: list[str],
                      report_type: str) -> dict:
    """The champion's recorded confidence label for each code, one query.

    The upstream a candidate mapping needs. Scoped to the run's own report type
    because that is what ``panel_for`` does: a post-market prediction is not a
    forecast of the morning run, and pairing the morning panel with a
    post-market label would be a comparison of two different days' questions.

    A code with two rows — two traders on the same morning — takes the earliest
    by ``id``. The panel itself makes no trader distinction, so this is a
    choice; it is stated here rather than left to row order.
    """
    wanted = [_text(code, "code") for code in codes]
    if not wanted:
        return {}
    conn = memory_store._get_conn()
    placeholders = ",".join("?" for _ in wanted)
    rows = conn.execute(
        f"SELECT code, confidence FROM predictions WHERE date = ? "
        f"AND report_type = ? AND code IN ({placeholders}) ORDER BY id",
        (date, report_type, *wanted)).fetchall()
    signals: dict = {}
    for row in rows:
        signals.setdefault(row["code"], row["confidence"])
    return signals


def _deadline_for(date: str, horizon_days: int) -> str | None:
    """The day a forecast matures, in calendar days, matching ``predictions``.

    A *pre-filter*, not the maturity test. It is a declaration the forecast
    made about itself and it fires on days the market never opened, so
    ``score_due`` still asks ``scoring.evidence_window_closed`` before it
    grades anything: this date says "we should stop waiting around now", and
    only the market's own calendar says "the evidence could exist".
    """
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

    ``unscorable`` and ``deferred`` are counted apart because they are
    different facts, and reporting one number for both made a live experiment
    look like a data outage. ``deferred`` means the row's deadline passed on
    the calendar but the market has not traded the window shut
    (``scoring.evidence_window_closed``) — nothing is missing, we have not
    waited yet. ``unscorable`` means the window closed and still no score came
    back: a suspended stock, a delisted code, no benchmark for that day.
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

    graded = skipped = deferred = 0
    with memory_store._write_lock:
        for row in conn.execute(sql, tuple(args)).fetchall():
            if not scoring.evidence_window_closed(row["date"],
                                                  row["horizon_days"]):
                deferred += 1
                continue
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
    return {"graded": graded, "unscorable": skipped, "deferred": deferred,
            "as_of": when}


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

    Counted with the *same* filters the gate compares under — the run's own
    ``report_type`` and the forward window after the policy freeze. A progress
    meter that counts a wider set than the verdict will is worse than no meter:
    it reaches ``needed`` and then the verdict comes back ``insufficient``, and
    the operator is left guessing which of the two numbers lied.
    """
    run = get_run(run_id)
    if run is None:
        return 0
    frozen_at = ""
    version = policy_registry.get_version(run["policy_version_id"])
    if version is not None:
        frozen_at = str(version["frozen_at"])[:10]

    conn = memory_store._get_conn()
    init_schema(conn)
    row = conn.execute(
        "SELECT COUNT(*) n FROM shadow_predictions s "
        "WHERE s.run_id = ? AND s.scored_at IS NOT NULL AND s.brier IS NOT NULL "
        "AND s.date > ? "
        "AND EXISTS (SELECT 1 FROM predictions p WHERE p.date = s.date "
        "  AND p.code = s.code AND p.report_type = ? "
        "  AND p.scored_at IS NOT NULL "
        "  AND p.brier IS NOT NULL)",
        (run_id, frozen_at, run["report_type"])).fetchone()
    return int(row["n"])


def coverage(run_id: int | None = None) -> dict:
    """How far each shadow run is from having evidence.

    The only honest progress meter in this slice: it answers "how far along is
    it" rather than "is it done", because the answer is a function of the
    market's calendar and of nothing this code can hurry.

    ``paired`` and ``needed`` are counted in **(date, code) paired samples** —
    the unit the gate's floor compares against — and not in days. The two were
    printed as the same thing for as long as this function existed, which made
    the denominator of a verdict read as a calendar count. ``scored_days`` is
    the distinct dates behind those samples; it is carried separately so the
    two can never be mistaken for each other again, and today it decides one
    thing: ``policy_registry`` refuses a promotion citing a verdict that rests
    on zero days.
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
        manifest = manifest_for_run(run["id"])
        needed = (int(manifest["minimum_samples"]) if manifest
                  else holdout_gate.MIN_VALIDATION_SAMPLES)
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
            "needed": needed,
            "remaining": max(0, needed - paired),
            "manifest_id": run.get("manifest_id"),
            "sealed_at": run.get("sealed_at"),
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
            continue
        try:
            assert_run_evaluable(run)
        except ShadowError as exc:
            problems.append(f"shadow run #{run['id']} is not evaluable: {exc}")
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
