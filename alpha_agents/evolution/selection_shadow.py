"""Forward-only shadow evidence for selection_rank variants.

The probability shadow cannot grade a panel-construction gene. This module
therefore watches future Opportunity Journal sets produced under the parent
policy and replays parent/challenger selection_rank on the exact same
candidate pool.

**Where those sets come from, stated because it decides whether this module
can run at all:** the Opportunity Journal is written only by the replay runner
(``scripts/walk_forward.py``). No production module writes it, so in the
production deployment there is no sample source and a run opened there stays at
zero forever. :func:`sample_source_present` answers this, ``process`` refuses
with a diagnosis rather than a sqlite error, and ``summary`` reports it.

It does not promote. Once the preregistered number of fully matured sets has
been reached, a seal freezes the sample and its summary. A later promotion gate
may cite that seal, but this module itself has no pointer-moving API.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from alpha_agents.data import clock, memory_store, policy_registry
from alpha_agents.evolution import dream_agent, dream_selection, gene_registry
from alpha_agents.evolution.dream_world import (
    OpportunityDreamWorld, OpportunitySetObservation, OpportunityObservation,
)

EVIDENCE_SCOPE = "forward_selection_shadow"


class SelectionShadowError(ValueError):
    pass


_RUNS = """
CREATE TABLE IF NOT EXISTS selection_shadow_runs (
    id INTEGER PRIMARY KEY,
    parent_version_id INTEGER NOT NULL,
    variant_version_id INTEGER NOT NULL,
    opened_on TEXT NOT NULL,
    horizon INTEGER NOT NULL,
    minimum_sets INTEGER NOT NULL,
    manifest_json TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

_ROWS = """
CREATE TABLE IF NOT EXISTS selection_shadow_rows (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    opportunity_set_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    result_json TEXT NOT NULL,
    result_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, opportunity_set_id),
    FOREIGN KEY(run_id) REFERENCES selection_shadow_runs(id)
)
"""

_SEALS = """
CREATE TABLE IF NOT EXISTS selection_shadow_seals (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL UNIQUE,
    sealed_at TEXT NOT NULL,
    sample_count INTEGER NOT NULL,
    summary_json TEXT NOT NULL,
    summary_hash TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES selection_shadow_runs(id)
)
"""

_GUARDS = (
    ("selection_shadow_runs", "run"),
    ("selection_shadow_rows", "row"),
    ("selection_shadow_seals", "seal"),
)


def _dump(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False)


def _hash(value) -> str:
    return hashlib.sha256(_dump(value).encode("utf-8")).hexdigest()


def init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_RUNS)
    conn.execute(_ROWS)
    conn.execute(_SEALS)
    for table, label in _GUARDS:
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS {table}_no_update "
            f"BEFORE UPDATE ON {table} BEGIN "
            f"SELECT RAISE(ABORT, 'selection shadow {label} is append-only'); END")
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS {table}_no_delete "
            f"BEFORE DELETE ON {table} BEGIN "
            f"SELECT RAISE(ABORT, 'selection shadow {label} is append-only'); END")


def _version_ref(version_id: int) -> str:
    version = policy_registry.get_version(version_id)
    if version is None:
        raise SelectionShadowError(f"No policy version #{version_id}")
    return (
        f"{version['policy_key']}#{version['id']}@"
        f"{version['content_hash'][:12]}")


def open_run(*, parent_version_id: int, variant_version_id: int,
             opened_on: str | None = None, horizon: int = 5,
             minimum_sets: int = 20, minimum_behavior_changes: int = 5,
             minimum_mean_delta: float = 0.0,
             conn: sqlite3.Connection | None = None) -> int:
    changed = dream_agent.changed_genes(
        parent_version_id, variant_version_id)
    try:
        gene_registry.assert_exact_coverage(
            changed, gene_registry.SELECTION_RANK_GENES,
            actor="selection shadow producer/evaluator")
    except gene_registry.GeneRegistryError as exc:
        raise SelectionShadowError(
            f"selection shadow cannot observe changed gene(s): {exc}") from exc
    if horizon <= 0 or minimum_sets <= 0 or minimum_behavior_changes <= 0:
        raise SelectionShadowError(
            "horizon, minimum_sets and minimum_behavior_changes must be positive")
    if opened_on is not None:
        raise SelectionShadowError(
            "opened_on is writer-controlled and cannot be supplied by callers")
    opened_on = str(clock.today())[:10]
    manifest = {
        "schema_version": 1,
        "parent_version_id": parent_version_id,
        "variant_version_id": variant_version_id,
        "changed_genes": changed,
        # Strictly forward: opportunity sets on the open day are ambiguous
        # because the run row records a date, not an intra-day timestamp.
        # The first eligible decision is therefore the next trading day.
        "opened_on": opened_on,
        "forward_rule": "opportunity_set.day > opened_on",
        "horizon": int(horizon),
        "minimum_sets": int(minimum_sets),
        "minimum_behavior_changes": int(minimum_behavior_changes),
        "minimum_mean_delta": float(minimum_mean_delta),
        "evidence_scope": EVIDENCE_SCOPE,
        "promotion_eligible": False,
    }
    target = conn if conn is not None else memory_store._get_conn()
    init_schema(target)
    with target:
        cur = target.execute(
            "INSERT INTO selection_shadow_runs "
            "(parent_version_id,variant_version_id,opened_on,horizon,"
            "minimum_sets,manifest_json,manifest_hash,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (parent_version_id, variant_version_id, opened_on, horizon,
             minimum_sets, _dump(manifest), _hash(manifest), clock.today()))
    return int(cur.lastrowid)


def get_run(run_id: int, conn: sqlite3.Connection | None = None) -> dict | None:
    conn = conn if conn is not None else memory_store._get_conn()
    init_schema(conn)
    row = conn.execute(
        "SELECT * FROM selection_shadow_runs WHERE id=?", (run_id,)).fetchone()
    return dict(row) if row else None


def seal(run_id: int, conn: sqlite3.Connection | None = None) -> dict | None:
    conn = conn if conn is not None else memory_store._get_conn()
    init_schema(conn)
    row = conn.execute(
        "SELECT * FROM selection_shadow_seals WHERE run_id=?",
        (run_id,)).fetchone()
    return dict(row) if row else None


def _set_world(set_row, items, context) -> OpportunityDreamWorld:
    obs = tuple(
        OpportunityObservation(
            item_id=int(row["id"]),
            code=str(row["code"]),
            status=str(row["status"]),
            researched=bool(row["researched"]),
            selected=bool(row["selected"]),
            panel_row=json.loads(row["panel_row_json"]),
            outcome={},
        )
        for row in items
    )
    group = OpportunitySetObservation(
        set_id=int(set_row["id"]),
        day=str(set_row["day"]),
        phase=str(set_row["phase"]),
        information_cutoff=str(set_row["information_cutoff"]),
        trader_id=str(set_row["trader_id"]),
        policy_ref=set_row["policy_ref"],
        parse_error=set_row["parse_error"],
        context=context,
        items=obs,
    )
    payload = {
        "set_id": group.set_id,
        "day": group.day,
        "phase": group.phase,
        "policy_ref": group.policy_ref,
        "context": group.context,
    }
    return OpportunityDreamWorld(
        start=group.day, end=group.day, sets=(group,),
        world_hash=_hash(payload))


def sample_source_present(conn: sqlite3.Connection | None = None) -> bool:
    """Whether this deployment has an Opportunity Journal at all.

    The journal is written by the replay runner (``scripts/walk_forward.py``)
    and by nothing in ``alpha_agents/pipeline/``. So in the production
    deployment the table does not exist, and every selection shadow run there
    is structurally unable to accumulate a sample — not slowly, never.

    Reported rather than assumed so a run can say so itself. A progress meter
    that reads 0/20 forever, with no statement of why, is how an experiment
    that cannot start gets mistaken for one that is merely young.
    """
    target = conn if conn is not None else memory_store._get_conn()
    row = target.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='opportunity_sets'").fetchone()
    return row is not None


def _require_sample_source(conn: sqlite3.Connection) -> None:
    """Refuse to process with a diagnosis, not with a sqlite error.

    Without this, ``process`` on a deployment with no journal raises
    ``OperationalError: no such table: opportunity_sets`` — which reads as a
    broken query rather than as the experiment having no sample source. The
    two need different responses, so they must not share an exception type.
    """
    if not sample_source_present(conn):
        raise SelectionShadowError(
            "No opportunity_sets table in this deployment, so this selection "
            "shadow run can never accumulate a sample. The Opportunity "
            "Journal is written only by the replay runner "
            "(scripts/walk_forward.py); no production module produces it, so "
            "a forward selection shadow requires the dual-rank panel to be "
            "wired into the live selection path first.")


def process(*, run_id: int, history_conn,
            conn: sqlite3.Connection | None = None) -> dict:
    """Score newly matured future sets, then seal at the sample floor."""
    target = conn if conn is not None else memory_store._get_conn()
    init_schema(target)
    run = get_run(run_id, target)
    if run is None:
        raise SelectionShadowError(f"No selection shadow run #{run_id}")
    existing_seal = seal(run_id, target)
    if existing_seal is not None:
        return {
            "run_id": run_id, "sealed": True,
            "sample_count": int(existing_seal["sample_count"]),
            "new_rows": 0,
        }

    existing_count = int(target.execute(
        "SELECT COUNT(*) n FROM selection_shadow_rows WHERE run_id=?",
        (run_id,)).fetchone()["n"])
    minimum_sets = int(run["minimum_sets"])
    if existing_count > minimum_sets:
        raise SelectionShadowError(
            "unsealed selection shadow already exceeds its preregistered "
            "sample floor; refusing to truncate historical evidence")
    if existing_count == minimum_sets:
        _seal(run, target)
        return {
            "run_id": run_id, "new_rows": 0,
            "sample_count": minimum_sets, "sealed": True,
        }
    remaining = minimum_sets - existing_count

    parent_ref = _version_ref(int(run["parent_version_id"]))
    # Checked here, at the point of use, and not earlier: the invariants
    # above are about this run's own ledger and must be reported whether or
    # not the environment has a journal. This one is about the environment.
    _require_sample_source(target)
    sets = target.execute(
        "SELECT s.* FROM opportunity_sets s "
        "LEFT JOIN selection_shadow_rows r "
        "ON r.run_id=? AND r.opportunity_set_id=s.id "
        "WHERE r.id IS NULL AND s.day>? AND s.policy_ref=? "
        "ORDER BY s.day, s.id",
        (run_id, run["opened_on"], parent_ref)).fetchall()

    written = 0
    with target:
        for set_row in sets:
            if written >= remaining:
                break
            context_row = target.execute(
                "SELECT context_json FROM opportunity_contexts "
                "WHERE opportunity_set_id=?", (set_row["id"],)).fetchone()
            if context_row is None:
                continue
            context = json.loads(context_row["context_json"])
            if not context.get("candidate_pool"):
                continue
            items = target.execute(
                "SELECT * FROM opportunity_items "
                "WHERE opportunity_set_id=? ORDER BY id",
                (set_row["id"],)).fetchall()
            world = _set_world(set_row, items, context)
            result = dream_selection.panel_policy_counterfactual(
                world,
                parent_version_id=int(run["parent_version_id"]),
                variant_version_id=int(run["variant_version_id"]),
                horizon=int(run["horizon"]),
                history_conn=history_conn)
            if result["comparable_sets"] != 1:
                continue
            target.execute(
                "INSERT INTO selection_shadow_rows "
                "(run_id,opportunity_set_id,day,result_json,result_hash,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (run_id, set_row["id"], set_row["day"], _dump(result),
                 _hash(result), clock.today()))
            written += 1

    count = target.execute(
        "SELECT COUNT(*) n FROM selection_shadow_rows WHERE run_id=?",
        (run_id,)).fetchone()["n"]
    if int(count) == int(run["minimum_sets"]):
        _seal(run, target)
    elif int(count) > int(run["minimum_sets"]):
        raise SelectionShadowError(
            "selection shadow exceeded its preregistered sample floor")

    return {
        "run_id": run_id,
        "new_rows": written,
        "sample_count": int(count),
        "sealed": seal(run_id, target) is not None,
    }


def _seal(run: dict, conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        "SELECT result_json FROM selection_shadow_rows "
        "WHERE run_id=? ORDER BY id",
        (run["id"],)).fetchall()
    minimum_sets = int(run["minimum_sets"])
    if len(rows) < minimum_sets:
        raise SelectionShadowError("cannot seal before the preregistered floor")
    if len(rows) > minimum_sets:
        raise SelectionShadowError(
            "cannot seal a run that already exceeds its preregistered floor")

    results = [json.loads(row["result_json"]) for row in rows]
    deltas = [
        float(result["variant_minus_parent_mean"])
        for result in results
        if result.get("variant_minus_parent_mean") is not None
    ]
    changed = sum(
        int(result.get("behavior_changed_sets") or 0) for result in results)
    summary = {
        "run_id": int(run["id"]),
        "parent_version_id": int(run["parent_version_id"]),
        "variant_version_id": int(run["variant_version_id"]),
        "sample_count": len(results),
        "behavior_changed_sets": changed,
        "mean_panel_return_delta": (
            round(sum(deltas) / len(deltas), 4) if deltas else None),
        "evidence_scope": EVIDENCE_SCOPE,
        "promotion_eligible": False,
        "note": (
            "Forward sample sealed at the preregistered floor. This is "
            "selection-shadow evidence, not yet an eligible promotion gate."),
    }
    with conn:
        cur = conn.execute(
            "INSERT INTO selection_shadow_seals "
            "(run_id,sealed_at,sample_count,summary_json,summary_hash) "
            "VALUES (?,?,?,?,?)",
            (run["id"], clock.today(), len(results),
             _dump(summary), _hash(summary)))
    return int(cur.lastrowid)


def summary(run_id: int, conn: sqlite3.Connection | None = None) -> dict:
    conn = conn if conn is not None else memory_store._get_conn()
    init_schema(conn)
    run = get_run(run_id, conn)
    if run is None:
        raise SelectionShadowError(f"No selection shadow run #{run_id}")
    sealed = seal(run_id, conn)
    rows = conn.execute(
        "SELECT result_json FROM selection_shadow_rows "
        "WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
    present = sample_source_present(conn)
    return {
        "run": run,
        "sample_count": len(rows),
        "remaining": max(0, int(run["minimum_sets"]) - len(rows)),
        # A run whose source is absent is not "young", it is unable to start.
        # Stated here so the number is never read without its precondition.
        "sample_source_present": present,
        "sample_source": ("opportunity_sets" if present else None),
        "seal": ({
            **sealed,
            "summary": json.loads(sealed["summary_json"]),
        } if sealed else None),
        "promotion_eligible": False,
    }
