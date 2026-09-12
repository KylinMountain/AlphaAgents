"""Quarantined learning proposals and measurements, never active knowledge.

This module owns its schema independently of memory_store migrations. There is
no approval or promotion API: evidence collection is not permission to trade.

T3 of Phase 3 adds the two things design §10 asks of a candidate.

**A falsifiable proposal, not a remark.** A candidate states a ``claim``, the
``applicable_context`` it is supposed to hold in, the
``proposed_behavior_delta``, and the experiences it cites —
``evidence_episode_ids``, supporting *and* opposing, pointing at T1's
episodes. All four are required at the write boundary. Whether a claim is any
*good* is not something code can decide; that it is stated, structured, and
enumerable is.

**A lifecycle with a named actor.** ``observation → hypothesis → testing →
validated → retired``, with ``retired`` reachable from anywhere else. Movement
requires an ``actor`` and a ``reason`` and lands in ``candidate_transitions``,
which is append-only. **Reaching ``validated`` activates nothing** (§10:
validation does not itself activate knowledge) — activation is inclusion in an
approved snapshot, which is T4 and does not exist yet.

The evidence fingerprint is unchanged: it still identifies the *evidence*
(entity, operation, target, source, date, payload). The four proposal fields
are excluded from it on purpose — the same evidence can only ever yield the
same claim, and folding the claim in would turn one piece of evidence into as
many candidates as there are ways to phrase it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date

from alpha_agents.data import clock, memory_store

# ── The lifecycle (design §10) ─────────────────────────────────────────

OBSERVATION = "observation"
HYPOTHESIS = "hypothesis"
TESTING = "testing"
VALIDATED = "validated"
RETIRED = "retired"

#: Ordered as the lifecycle reads, not alphabetically.
STATUSES = (OBSERVATION, HYPOTHESIS, TESTING, VALIDATED, RETIRED)

#: Legal moves. Forward one step at a time, and ``retired`` from anywhere
#: else — a proposal can be dropped at any point, including after it was
#: validated, but it cannot walk *back*: new opposing evidence that
#: contradicts an earlier step is a new candidate (design §11's rule for
#: behaviour changes), not a silent edit of this one's history.
_LEGAL = {
    OBSERVATION: (HYPOTHESIS, RETIRED),
    HYPOTHESIS: (TESTING, RETIRED),
    TESTING: (VALIDATED, RETIRED),
    VALIDATED: (RETIRED,),
    RETIRED: (),
}

#: The two ways an episode can be cited. Both keys are required on every
#: write: "we found no opposing evidence" is a claim worth being explicit
#: about, and a missing key cannot be told from an omitted one.
SUPPORTING = "supporting"
OPPOSING = "opposing"
_EVIDENCE_KEYS = (SUPPORTING, OPPOSING)

#: The four columns T3 adds, in the order the migration adds them.
PROPOSAL_COLUMNS = ("claim", "applicable_context", "proposed_behavior_delta",
                    "evidence_episode_ids")


class IllegalCandidateTransition(ValueError):
    """A lifecycle move that is not allowed. Nothing is written."""


# ── Schema ─────────────────────────────────────────────────────────────

_CANDIDATES = (
    "CREATE TABLE IF NOT EXISTS learning_candidates ("
    "id INTEGER PRIMARY KEY, "
    "fingerprint TEXT NOT NULL UNIQUE, "
    "entity_type TEXT NOT NULL CHECK(entity_type IN ('principle', 'playbook')), "
    "operation TEXT NOT NULL CHECK(operation IN ('create', 'reinforce', 'update', 'retire')), "
    "target_id INTEGER, source TEXT NOT NULL, source_date TEXT NOT NULL, "
    "payload_json TEXT NOT NULL, "
    # The proposal itself. Nullable only so that rows written before T3 can
    # be migrated without inventing a claim their author never made; every
    # new write goes through save_candidate, which requires all four.
    "claim TEXT, applicable_context TEXT, proposed_behavior_delta TEXT, "
    "evidence_episode_ids TEXT, "
    "status TEXT NOT NULL DEFAULT 'observation' "
    "CHECK(status IN ('observation', 'hypothesis', 'testing', 'validated', 'retired')), "
    "created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))"
)

_OBSERVATIONS = (
    "CREATE TABLE IF NOT EXISTS learning_observations ("
    "id INTEGER PRIMARY KEY, "
    "entity_type TEXT NOT NULL CHECK(entity_type IN ('principle', 'playbook')), "
    "target_id INTEGER NOT NULL, source TEXT NOT NULL, source_date TEXT NOT NULL, "
    "payload_json TEXT NOT NULL, "
    "created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')), "
    "UNIQUE(entity_type, target_id, source, source_date, payload_json))"
)

_TRANSITIONS = (
    "CREATE TABLE IF NOT EXISTS candidate_transitions ("
    "id INTEGER PRIMARY KEY, "
    "candidate_id INTEGER NOT NULL, "
    "from_status TEXT NOT NULL, to_status TEXT NOT NULL, "
    "actor TEXT NOT NULL, reason TEXT NOT NULL, at TEXT NOT NULL, "
    "created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))"
)

_TRANSITIONS_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_candidate_transitions_candidate "
    "ON candidate_transitions(candidate_id)"
)

# Append-only, like every other history table here: "who moved this, and
# why" is exactly the record an interested party would want to edit.
_TRANSITIONS_GUARDS = (
    "CREATE TRIGGER IF NOT EXISTS candidate_transitions_no_update "
    "BEFORE UPDATE ON candidate_transitions BEGIN "
    "SELECT RAISE(ABORT, 'candidate_transitions is append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS candidate_transitions_no_delete "
    "BEFORE DELETE ON candidate_transitions BEGIN "
    "SELECT RAISE(ABORT, 'candidate_transitions is append-only'); END",
)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_candidates(conn: sqlite3.Connection) -> None:
    """Bring an existing table up to T3's shape, in place.

    Two changes, and they have to happen in this order: the four new
    columns are added with ``ALTER TABLE``, and only then is the table
    rebuilt to relax the ``CHECK(status = 'candidate')``, because a rebuild
    that runs first would create the new shape and then have nothing to
    migrate.
    """
    cols = _columns(conn, "learning_candidates")
    if not cols:  # fresh database, the CREATE above already made the T3 shape
        return
    for name in PROPOSAL_COLUMNS:
        if name not in cols:
            conn.execute(
                f"ALTER TABLE learning_candidates ADD COLUMN {name} TEXT")

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' "
        "AND name = 'learning_candidates'").fetchone()
    if row and "status = 'candidate'" in (row["sql"] or ""):
        _relax_status_check(conn)


def _relax_status_check(conn: sqlite3.Connection) -> None:
    """Rebuild ``learning_candidates`` so ``status`` may hold the lifecycle.

    SQLite cannot alter a CHECK, so the table is copied. The old single
    value ``candidate`` maps to ``observation`` — the first state of the
    lifecycle, which is what it always meant.
    """
    conn.execute("DROP TABLE IF EXISTS learning_candidates_legacy")
    conn.execute(
        "ALTER TABLE learning_candidates RENAME TO learning_candidates_legacy")
    conn.execute(_CANDIDATES)
    conn.execute(
        "INSERT INTO learning_candidates ("
        "id, fingerprint, entity_type, operation, target_id, source, "
        "source_date, payload_json, claim, applicable_context, "
        "proposed_behavior_delta, evidence_episode_ids, status, created_at) "
        "SELECT id, fingerprint, entity_type, operation, target_id, source, "
        "source_date, payload_json, claim, applicable_context, "
        "proposed_behavior_delta, evidence_episode_ids, "
        "CASE status WHEN 'candidate' THEN 'observation' ELSE status END, "
        "created_at FROM learning_candidates_legacy")
    conn.execute("DROP TABLE learning_candidates_legacy")


def init_schema(conn: sqlite3.Connection) -> None:
    """Initialize only the quarantine tables on the supplied connection."""
    conn.execute(_CANDIDATES)
    conn.execute(_OBSERVATIONS)
    conn.execute(_TRANSITIONS)
    _migrate_candidates(conn)
    # After the migration: an index created before a rebuild would be
    # dropped with the table it belonged to.
    conn.execute(_TRANSITIONS_INDEX)
    for guard in _TRANSITIONS_GUARDS:
        conn.execute(guard)


# ── Validation ─────────────────────────────────────────────────────────


def assert_transition(current: str, target: str) -> str:
    """Return ``target`` if the move is legal, raise otherwise."""
    allowed = _LEGAL.get(current)
    if allowed is None:
        raise IllegalCandidateTransition(
            f"Unknown candidate status {current!r}; cannot move to {target!r}")
    if target not in allowed:
        raise IllegalCandidateTransition(
            f"Illegal candidate transition {current!r} → {target!r}. Legal "
            f"from {current!r}: {', '.join(allowed) or '(none)'}.")
    return target


def _validate_envelope(entity_type: str, source: str, source_date: str, payload: dict) -> None:
    """Validate provenance, not the truth or trading merit of a proposal."""
    if entity_type not in ("principle", "playbook"):
        raise ValueError("Unsupported learning entity type")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("Learning provenance requires a nonempty source")
    if not isinstance(source_date, str) or date.fromisoformat(source_date).isoformat() != source_date:
        raise ValueError("Learning provenance requires a YYYY-MM-DD source_date")
    if not isinstance(payload, dict):
        raise ValueError("Learning payload must be an object")


def _statement(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"A candidate must state a {field}: an empty one leaves nothing "
            "to be wrong about later.")
    return value.strip()


def _delta(value) -> str:
    if not isinstance(value, dict) or not value:
        raise ValueError(
            "proposed_behavior_delta must name the change being proposed; a "
            "candidate that changes nothing cannot be evaluated.")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _episode_citations(value) -> str:
    """Canonicalise ``evidence_episode_ids``.

    A dict rather than a flat list because design §10 asks for supporting
    *and* opposing evidence: a proposal whose evidence is only the cases
    that agree with it is not a hypothesis, it is an advertisement.
    """
    if not isinstance(value, dict):
        raise ValueError(
            "evidence_episode_ids must be an object with keys "
            f"{' and '.join(repr(k) for k in _EVIDENCE_KEYS)}")
    unknown = sorted(set(value) - set(_EVIDENCE_KEYS))
    missing = [k for k in _EVIDENCE_KEYS if k not in value]
    if missing:
        raise ValueError(
            f"evidence_episode_ids is missing {', '.join(missing)}: an empty "
            "list has to be stated, not omitted, or 'we looked and found "
            "none' cannot be told from 'nobody looked'.")
    if unknown:
        raise ValueError(
            f"evidence_episode_ids has unknown keys {', '.join(unknown)}")
    canonical = {}
    for key in _EVIDENCE_KEYS:
        ids = value[key]
        if not isinstance(ids, list):
            raise ValueError(f"evidence_episode_ids[{key!r}] must be a list")
        for episode_id in ids:
            if type(episode_id) is not int or episode_id <= 0:
                raise ValueError(
                    f"evidence_episode_ids[{key!r}] must hold positive "
                    f"episode ids, got {episode_id!r}")
        canonical[key] = sorted(set(ids))
    return json.dumps(canonical, ensure_ascii=False, sort_keys=True)


# ── Writing ────────────────────────────────────────────────────────────


def save_candidate(*, entity_type: str, operation: str, source: str,
                   source_date: str, payload: dict, claim: str,
                   applicable_context: str, proposed_behavior_delta: dict,
                   evidence_episode_ids: dict,
                   target_id: int | None = None) -> int:
    """Persist a proposal; exact retries return the original candidate ID.

    The original payload and provenance are retained. Changed evidence produces
    a separate candidate, not an overwrite. Storage errors propagate to callers.

    The four proposal fields are excluded from the fingerprint, so an exact
    retry returns the same row — and if that row predates T3 and therefore has
    no claim yet, the claim the caller states is filled in **once**.
    ``COALESCE`` means a field that already holds a value is never rewritten:
    the claim is metadata about the evidence, not part of it, so stating it is
    not the "overwrite with changed evidence" this function refuses to do.
    """
    _validate_envelope(entity_type, source, source_date, payload)
    allowed = ("create", "reinforce") if entity_type == "principle" else ("create", "update", "retire")
    if operation not in allowed:
        raise ValueError("Unsupported candidate operation for this entity type")
    if operation == "create":
        if target_id is not None:
            raise ValueError("Create candidates cannot target existing knowledge")
    elif type(target_id) is not int or target_id <= 0:
        raise ValueError("Existing-knowledge candidates require a positive integer target_id")

    claim = _statement(claim, "claim")
    applicable_context = _statement(applicable_context, "applicable_context")
    delta_json = _delta(proposed_behavior_delta)
    citations_json = _episode_citations(evidence_episode_ids)

    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    identity = json.dumps([entity_type, operation, target_id, source, source_date, payload_json])
    fingerprint = hashlib.sha256(identity.encode('utf-8')).hexdigest()
    with memory_store._write_lock:
        conn = memory_store._get_conn()
        with conn:
            init_schema(conn)
            conn.execute(
                "INSERT INTO learning_candidates "
                "(fingerprint, entity_type, operation, target_id, source, source_date, "
                "payload_json, claim, applicable_context, proposed_behavior_delta, "
                "evidence_episode_ids) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(fingerprint) DO NOTHING",
                (fingerprint, entity_type, operation, target_id, source, source_date,
                 payload_json, claim, applicable_context, delta_json, citations_json),
            )
            conn.execute(
                "UPDATE learning_candidates SET "
                "claim = COALESCE(claim, ?), "
                "applicable_context = COALESCE(applicable_context, ?), "
                "proposed_behavior_delta = COALESCE(proposed_behavior_delta, ?), "
                "evidence_episode_ids = COALESCE(evidence_episode_ids, ?) "
                "WHERE fingerprint = ? AND ("
                "claim IS NULL OR applicable_context IS NULL "
                "OR proposed_behavior_delta IS NULL OR evidence_episode_ids IS NULL)",
                (claim, applicable_context, delta_json, citations_json, fingerprint),
            )
            row = conn.execute(
                "SELECT id FROM learning_candidates WHERE fingerprint = ?", (fingerprint,),
            ).fetchone()
            return row['id']


def record_observation(*, entity_type: str, target_id: int, source: str,
                       source_date: str, payload: dict) -> None:
    """Keep measurements separate from scores/annotations injected as rules."""
    _validate_envelope(entity_type, source, source_date, payload)
    if type(target_id) is not int or target_id <= 0:
        raise ValueError("Observations require a positive integer target_id")
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    with memory_store._write_lock:
        conn = memory_store._get_conn()
        with conn:
            init_schema(conn)
            conn.execute(
                "INSERT INTO learning_observations "
                "(entity_type, target_id, source, source_date, payload_json) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(entity_type, target_id, source, source_date, payload_json) DO NOTHING",
                (entity_type, target_id, source, source_date, payload_json),
            )


def advance_candidate(candidate_id: int, *, to_status: str, actor: str,
                      reason: str, at: str | None = None) -> dict:
    """Move one candidate along its lifecycle, recording who and why.

    This is the only writer of ``learning_candidates.status``. It exists so
    the later lifecycle states are reachable by a stated human action rather
    than declared and never produced; it is deliberately **not** called by
    any pipeline, because design §10 gives the learner permission to propose
    and request evaluation, not to grant itself authority.

    Reaching ``validated`` changes nothing about what the system does.
    """
    actor = _statement(actor, "actor")
    reason = _statement(reason, "reason")
    when = at or clock.today()
    with memory_store._write_lock:
        conn = memory_store._get_conn()
        with conn:
            init_schema(conn)
            row = conn.execute(
                "SELECT status FROM learning_candidates WHERE id = ?",
                (candidate_id,)).fetchone()
            if row is None:
                raise ValueError(f"No learning candidate #{candidate_id}")
            current = row["status"]
            assert_transition(current, to_status)
            conn.execute(
                "UPDATE learning_candidates SET status = ? WHERE id = ?",
                (to_status, candidate_id),
            )
            conn.execute(
                "INSERT INTO candidate_transitions "
                "(candidate_id, from_status, to_status, actor, reason, at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (candidate_id, current, to_status, actor, reason, when),
            )
    return {"candidate_id": candidate_id, "from": current, "to": to_status,
            "actor": actor, "reason": reason, "at": when}


# ── Reading ────────────────────────────────────────────────────────────


def get_candidate(candidate_id: int) -> dict | None:
    init_schema(memory_store._get_conn())
    row = memory_store._get_conn().execute(
        "SELECT * FROM learning_candidates WHERE id = ?",
        (candidate_id,)).fetchone()
    return dict(row) if row else None


def candidates_by_status(status: str | None = None,
                         *, limit: int = 200) -> list[dict]:
    """Candidates, oldest first, optionally filtered to one lifecycle state."""
    if status is not None and status not in STATUSES:
        raise ValueError(f"Unknown candidate status {status!r}; known: "
                         f"{', '.join(STATUSES)}")
    conn = memory_store._get_conn()
    init_schema(conn)
    if status is None:
        rows = conn.execute(
            "SELECT * FROM learning_candidates ORDER BY id LIMIT ?",
            (limit,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM learning_candidates WHERE status = ? ORDER BY id LIMIT ?",
            (status, limit)).fetchall()
    return [dict(r) for r in rows]


def transitions_for(candidate_id: int) -> list[dict]:
    conn = memory_store._get_conn()
    init_schema(conn)
    rows = conn.execute(
        "SELECT * FROM candidate_transitions WHERE candidate_id = ? ORDER BY id",
        (candidate_id,)).fetchall()
    return [dict(r) for r in rows]


def candidates_citing(episode_id: int, *, limit: int = 200) -> list[dict]:
    """Candidates that cite ``episode_id``, with the role they cited it in.

    §10's warning — "a cited experience appearing in a successful trade
    proves co-occurrence, not incremental value" — is only actionable if the
    citation can be enumerated in the first place.
    """
    conn = memory_store._get_conn()
    init_schema(conn)
    rows = conn.execute(
        "SELECT c.* FROM learning_candidates c WHERE EXISTS ("
        "  SELECT 1 FROM json_each(c.evidence_episode_ids) AS bucket "
        "  WHERE EXISTS (SELECT 1 FROM json_each(bucket.value) AS cited "
        "                WHERE cited.value = ?)) "
        "ORDER BY c.id LIMIT ?", (episode_id, limit)).fetchall()
    out = []
    for row in rows:
        entry = dict(row)
        buckets = json.loads(entry["evidence_episode_ids"] or "{}")
        entry["cited_as"] = [k for k in _EVIDENCE_KEYS
                             if episode_id in (buckets.get(k) or [])]
        out.append(entry)
    return out


def counts() -> dict:
    """How many candidates sit in each lifecycle state, including zeroes."""
    conn = memory_store._get_conn()
    init_schema(conn)
    out = {status: 0 for status in STATUSES}
    for row in conn.execute(
            "SELECT status, COUNT(*) n FROM learning_candidates "
            "GROUP BY status").fetchall():
        out[row["status"]] = int(row["n"])
    return out


def _cited_ids(citations_json: str | None) -> list[int]:
    if not citations_json:
        return []
    try:
        buckets = json.loads(citations_json)
    except (TypeError, ValueError):
        return []
    return [i for key in _EVIDENCE_KEYS for i in (buckets.get(key) or [])
            if type(i) is int]


def integrity() -> list[str]:
    """Structural complaints about the quarantine, or an empty list.

    Reports rather than repairs, the posture ``reconciliation`` and
    ``outcomes.integrity`` both take. Two things are checkable that the
    write boundary cannot enforce:

    * a candidate with no proposal fields — a row written before T3 that
      was never enriched. It is not a candidate in §10's sense and should
      not be counted as one.
    * a citation of an episode that does not exist. The evidence lives in
      a JSON array, so no foreign key can express this.
    """
    conn = memory_store._get_conn()
    init_schema(conn)
    problems: list[str] = []
    have_episodes = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'episodes'").fetchone() is not None
    for row in conn.execute(
            "SELECT id, claim, applicable_context, proposed_behavior_delta, "
            "evidence_episode_ids FROM learning_candidates ORDER BY id"
    ).fetchall():
        missing = [c for c in PROPOSAL_COLUMNS if row[c] is None]
        if missing:
            problems.append(
                f"learning candidate #{row['id']} states no "
                f"{', '.join(missing)}: it predates the proposal fields and "
                f"was never enriched, so it cannot be evaluated")
        if not have_episodes:
            continue
        for episode_id in _cited_ids(row["evidence_episode_ids"]):
            exists = conn.execute(
                "SELECT 1 FROM episodes WHERE id = ?",
                (episode_id,)).fetchone()
            if not exists:
                problems.append(
                    f"learning candidate #{row['id']} cites episode "
                    f"#{episode_id}, which does not exist")
    return problems
