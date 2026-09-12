"""The policy that is in force: one frozen version, and one audited pointer.

§11 gives a candidate a path — ``draft → frozen → forward_shadow →
insufficient / rejected / validated → approved → promoted`` — and §14 makes
Phase 4 "evidence controls future policy changes through one audited entry
point". This module is the *record* half of that: what a version of the
policy is, which one is in force, and how the pointer moved.

The design decision that makes it worth having: a version's ``content_hash``
is computed from the **live configuration**, not from a copy someone typed.
``evolution.policy_sources.collect()`` reads the prompt files, the model
identity, the retrieval budgets, the decision-rule constants and the approved
knowledge snapshot in force; :func:`verify_version` recomputes the same tuple
from the same sources. So "someone edited a prompt without opening a new
version" stops being a rumour discovered months later and becomes a failed
check on the version that is in force.

**Why the split across layers.** The pointer is *data*: it lives in a table,
so this module — which the storage layer may use — can answer "what is in
force" for the writers that need it. Both ``intents`` and ``decision_snapshots``
carry a ``policy_ref`` reserved in Phase 1 and never written, because there was
no registry to reference; that is this repository's recurring defect, and
filling it is the point. The *sources*, by contrast, live in ``evolution`` and
``tools``, which this layer must not import. So the collector passes them in:
every function here that needs live configuration takes it as an argument
rather than reaching for it.

**What this module is not.** An activation path. Recording a version changes
nothing about what the trader does. Even installing a pointer only *names*
which version is in force; making retrieval read that name is a separate
slice with its own argument to make. There is no ``apply_policy`` here.

Three writes, and they are not the same act:

* :func:`freeze` records a version. Idempotent per content — freezing the
  same configuration twice is one version, because a version identifies
  behaviour, not the act of recording it.
* :func:`approve` records that a person authorised a version for promotion,
  citing the gate verdict that justifies it. It moves no pointer.
* :func:`install` / :func:`promote` / :func:`rollback` move the pointer. They
  are the audited entry point, and all three go through one private
  statement (:func:`_write_pointer`) so "who may move what is in force" is a
  single place to read rather than a property of remembering to add a guard.

**Promotion is a claim about improvement; rollback is a claim about
restoration.** They are deliberately not the same operation:

* :func:`promote` needs an eligible gate verdict *and* a recorded human
  approval *and* a live configuration that still hashes to the version. §11:
  a successful automatic evaluation is not a licence to promote, and no LLM
  may approve its own candidate — so the approval is a separate record made
  by a separate act, and promotion refuses without one.
* :func:`rollback` needs the target to have been in force before, which is a
  fact in the transition trail rather than a new piece of state. It does not
  need a verdict, because restoring a version is not a claim that it is
  better than the incumbent — it is a claim that the incumbent is worse.
  It deletes nothing: fills, ledger and outcomes are untouched, and only the
  pointer moves.

Both are atomic compare-and-swap on ``active_policy.version_seq``: a change
that raced another loses without writing, rather than overwriting it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from alpha_agents.data import clock, memory_store

# ── The declared contract ──────────────────────────────────────────────

#: The policy every decision currently runs under. Named rather than
#: implicit so a second governed policy (a per-trader mandate, say) does not
#: silently inherit the first one's lineage.
POLICY_KEY_DEFAULT = "trader"

#: What a policy version covers, declared once.
#:
#: The writer and the verifier both project through this tuple, so a source
#: cannot end up hashed by one and forgotten by the other. Adding a source is
#: a deliberate edit here plus the collector's agreement — and the collector's
#: agreement is checked, not assumed: :func:`freeze` refuses a source dict
#: whose keys are not exactly these.
#:
#: ``knowledge`` is the approved knowledge snapshot in force. A change of
#: snapshot is a change of behaviour, so it belongs in the hash; the snapshot
#: id is a declaration, not a running counter, so the hash stays stable while
#: trades close.
SOURCE_NAMES = ("prompts", "model", "retrieval", "rules", "knowledge")

TRANSITION_KINDS = ("install", "promote", "rollback")


class PolicyError(ValueError):
    """A policy operation that would leave the record inconsistent."""


# ── Schema, owned here (not in memory_store migrations) ────────────────

_VERSIONS = """
CREATE TABLE IF NOT EXISTS policy_versions (
    id INTEGER PRIMARY KEY,
    policy_key TEXT NOT NULL,
    parent_id INTEGER,
    content_hash TEXT NOT NULL,
    sources_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    reason TEXT NOT NULL,
    frozen_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(policy_key, content_hash)
)
"""

_POINTER = """
CREATE TABLE IF NOT EXISTS active_policy (
    policy_key TEXT PRIMARY KEY,
    version_id INTEGER NOT NULL,
    version_seq INTEGER NOT NULL DEFAULT 1,
    changed_by TEXT NOT NULL,
    reason TEXT NOT NULL,
    changed_at TEXT NOT NULL
)
"""

_TRANSITIONS = """
CREATE TABLE IF NOT EXISTS policy_transitions (
    id INTEGER PRIMARY KEY,
    policy_key TEXT NOT NULL,
    from_version_id INTEGER,
    to_version_id INTEGER NOT NULL,
    version_seq INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('install', 'promote', 'rollback')),
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
)
"""

# A person's authorisation for a version, kept apart from the pointer move for
# §11's reason: a successful automatic evaluation is not a licence to promote.
# The row binds the authorisation to the *content hash*, not just the id, so an
# approval cannot outlive the configuration it was given for.
_APPROVALS = """
CREATE TABLE IF NOT EXISTS policy_approvals (
    id INTEGER PRIMARY KEY,
    policy_key TEXT NOT NULL,
    version_id INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    gate_decision_id INTEGER,
    approved_by TEXT NOT NULL,
    reason TEXT NOT NULL,
    at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
)
"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_policy_versions_key "
    "ON policy_versions(policy_key)",
    "CREATE INDEX IF NOT EXISTS idx_policy_transitions_key "
    "ON policy_transitions(policy_key)",
    "CREATE INDEX IF NOT EXISTS idx_policy_approvals_version "
    "ON policy_approvals(policy_key, version_id)",
)

# A version is a fact about a configuration, and a transition is a fact about
# who moved the pointer. Neither may be rewritten afterwards — the pointer is
# the only mutable row, which is exactly why it needs an append-only trail.
# An approval is a fact about what a person authorised, so it is append-only
# for the same reason: an approval that can be edited afterwards is not a
# record of an approval.
_GUARDS = (
    "CREATE TRIGGER IF NOT EXISTS policy_versions_no_update "
    "BEFORE UPDATE ON policy_versions BEGIN "
    "SELECT RAISE(ABORT, 'policy_versions is append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS policy_versions_no_delete "
    "BEFORE DELETE ON policy_versions BEGIN "
    "SELECT RAISE(ABORT, 'policy_versions is append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS policy_transitions_no_update "
    "BEFORE UPDATE ON policy_transitions BEGIN "
    "SELECT RAISE(ABORT, 'policy_transitions is append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS policy_transitions_no_delete "
    "BEFORE DELETE ON policy_transitions BEGIN "
    "SELECT RAISE(ABORT, 'policy_transitions is append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS policy_approvals_no_update "
    "BEFORE UPDATE ON policy_approvals BEGIN "
    "SELECT RAISE(ABORT, 'policy_approvals is append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS policy_approvals_no_delete "
    "BEFORE DELETE ON policy_approvals BEGIN "
    "SELECT RAISE(ABORT, 'policy_approvals is append-only'); END",
)


def init_schema(conn: sqlite3.Connection) -> None:
    """Create this module's tables on the supplied connection.

    Called at every entry point, the posture ``learning_candidates`` takes:
    the tables belong to the module that reads them, not to
    ``memory_store``'s migration list, so a reader cannot end up querying a
    table that only exists if some other module happened to run first.
    """
    conn.execute(_VERSIONS)
    conn.execute(_POINTER)
    conn.execute(_TRANSITIONS)
    conn.execute(_APPROVALS)
    for statement in _INDEXES:
        conn.execute(statement)
    for guard in _GUARDS:
        conn.execute(guard)


# ── Hashing ────────────────────────────────────────────────────────────


def _hash(payload) -> str:
    """Content hash, order-independent at every level."""
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _frozen(sources: dict) -> dict:
    """Project collected sources onto exactly the declared names."""
    return {name: sources[name] for name in SOURCE_NAMES}


def _text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PolicyError(
            f"A policy change needs a nonempty {field}: a version without one "
            "records that something happened, not what was decided.")
    return value.strip()


def _positive_id(value, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise PolicyError(f"{field} must be a positive integer, got {value!r}")
    return value


def _normalise_sources(sources) -> dict:
    if not isinstance(sources, dict):
        raise PolicyError(f"sources must be an object, got {sources!r}")
    unknown = sorted(set(sources) - set(SOURCE_NAMES))
    missing = sorted(set(SOURCE_NAMES) - set(sources))
    if unknown or missing:
        # Refusing is the point: a collector that stopped supplying a source,
        # or invented a new one, would otherwise silently change what the hash
        # covers while every version still verified.
        raise PolicyError(
            "A policy version must carry exactly the declared sources "
            f"({', '.join(SOURCE_NAMES)}); missing {missing or '(none)'}, "
            f"unknown {unknown or '(none)'}")
    try:
        json.dumps(_frozen(sources), ensure_ascii=False, sort_keys=True,
                   allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise PolicyError(f"Policy sources must be JSON-serialisable: {exc}") from exc
    return _frozen(sources)


def content_hash_for(sources: dict) -> str:
    """The hash of a source set, projected through the declared tuple."""
    return _hash(_normalise_sources(sources))


# ── Versions: reading ──────────────────────────────────────────────────


def get_version(version_id: int) -> dict | None:
    conn = memory_store._get_conn()
    init_schema(conn)
    row = conn.execute("SELECT * FROM policy_versions WHERE id = ?",
                       (version_id,)).fetchone()
    return dict(row) if row else None


def versions_for(policy_key: str = POLICY_KEY_DEFAULT,
                 *, limit: int = 200) -> list[dict]:
    """Every frozen version of one policy, oldest first."""
    conn = memory_store._get_conn()
    init_schema(conn)
    rows = conn.execute(
        "SELECT * FROM policy_versions WHERE policy_key = ? ORDER BY id LIMIT ?",
        (policy_key, limit)).fetchall()
    return [dict(row) for row in rows]


def latest_version(policy_key: str = POLICY_KEY_DEFAULT) -> dict | None:
    """The most recently frozen version, whether or not it is in force."""
    rows = versions_for(policy_key)
    return rows[-1] if rows else None


def sources_of(version_id: int) -> dict | None:
    """The sources a version was frozen from, verbatim."""
    version = get_version(version_id)
    return json.loads(version["sources_json"]) if version else None


# ── Versions: writing ──────────────────────────────────────────────────


def freeze(*, sources: dict, created_by: str, reason: str,
           policy_key: str = POLICY_KEY_DEFAULT,
           parent_id: int | None = None,
           frozen_at: str | None = None) -> int:
    """Record a version of a configuration. Returns its id.

    Idempotent per content: freezing a configuration that is already on
    record returns the existing version rather than writing a second one.
    A version identifies *behaviour*, and the same behaviour is the same
    version however many times it is recorded — which is also what makes
    "promote version 7" a stable instruction.

    ``frozen_at`` is the kernel clock's date by default. It is the *start* of
    the version's forward window: nothing dated at or before it can be
    evidence that this version is an improvement, because the version did not
    exist yet. That is §11's "validation is forward" made into a stored fact
    rather than a caller's convention.
    """
    created_by = _text(created_by, "created_by")
    reason = _text(reason, "reason")
    policy_key = _text(policy_key, "policy_key")
    if parent_id is not None:
        _positive_id(parent_id, "parent_id")
    when = _text(frozen_at, "frozen_at") if frozen_at else clock.today()
    frozen = _normalise_sources(sources)
    digest = _hash(frozen)

    with memory_store._write_lock:
        conn = memory_store._get_conn()
        init_schema(conn)
        with conn:
            existing = conn.execute(
                "SELECT id FROM policy_versions WHERE policy_key = ? "
                "AND content_hash = ?", (policy_key, digest)).fetchone()
            if existing:
                return int(existing["id"])
            cursor = conn.execute(
                "INSERT INTO policy_versions "
                "(policy_key, parent_id, content_hash, sources_json, "
                " created_by, reason, frozen_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (policy_key, parent_id, digest,
                 json.dumps(frozen, ensure_ascii=False, sort_keys=True),
                 created_by, reason, when))
    return int(cursor.lastrowid)


def verify_version(version_id: int, sources: dict) -> bool:
    """Does this version still describe the configuration it was frozen from?

    Recomputes the hash from freshly collected sources. False means the live
    configuration has moved away from the version — which is not corruption,
    it is a policy change that was made without opening a version, and the
    answer a promotion eligibility check needs. An unknown id returns False
    for the same reason ``knowledge_snapshots.verify_snapshot`` does: absent
    and drifted are both reasons not to promote, and the caller wants one
    answer.
    """
    version = get_version(version_id)
    if version is None:
        return False
    return content_hash_for(sources) == version["content_hash"]


# ── The pointer ────────────────────────────────────────────────────────


def active(policy_key: str = POLICY_KEY_DEFAULT) -> dict | None:
    """The pointer row for a policy, or None if nothing is in force."""
    conn = memory_store._get_conn()
    init_schema(conn)
    row = conn.execute("SELECT * FROM active_policy WHERE policy_key = ?",
                       (policy_key,)).fetchone()
    return dict(row) if row else None


def active_version(policy_key: str = POLICY_KEY_DEFAULT) -> dict | None:
    """The version currently in force, or None."""
    pointer = active(policy_key)
    return get_version(pointer["version_id"]) if pointer else None


def active_ref(policy_key: str = POLICY_KEY_DEFAULT) -> str | None:
    """The reference a decision writes into its ``policy_ref``, or None.

    Carries the version id *and* a prefix of the hash it was frozen with, so
    a reader can see at a glance which policy produced a decision and spot
    when the reference does not match any recorded version. Returns None
    rather than raising when nothing is in force: a decision written before
    a policy was ever installed is a real historical row, and inventing a
    reference for it would be worse than leaving it empty.
    """
    pointer = active(policy_key)
    if pointer is None:
        return None
    version = get_version(pointer["version_id"])
    if version is None:
        return None
    return (f"{policy_key}#{version['id']}@{version['content_hash'][:12]}")


def parse_ref(ref: str) -> tuple[str, int, str] | None:
    """Split a ``policy_ref`` back into (policy_key, version_id, hash prefix).

    Returns None for anything that is not one of ours — a decision written
    before the registry existed has no reference, and that is not an error.
    """
    if not isinstance(ref, str) or "#" not in ref or "@" not in ref:
        return None
    key, _, rest = ref.partition("#")
    version_id, _, digest = rest.partition("@")
    if not key or not version_id.isdigit() or not digest:
        return None
    return key, int(version_id), digest


def transitions_for(policy_key: str = POLICY_KEY_DEFAULT,
                    *, limit: int = 200) -> list[dict]:
    """Every recorded change to the pointer, oldest first."""
    conn = memory_store._get_conn()
    init_schema(conn)
    rows = conn.execute(
        "SELECT * FROM policy_transitions WHERE policy_key = ? "
        "ORDER BY id LIMIT ?", (policy_key, limit)).fetchall()
    return [dict(row) for row in rows]


def approvals_for(policy_key: str = POLICY_KEY_DEFAULT,
                  *, limit: int = 200) -> list[dict]:
    """Every recorded approval for a policy, oldest first."""
    conn = memory_store._get_conn()
    init_schema(conn)
    rows = conn.execute(
        "SELECT * FROM policy_approvals WHERE policy_key = ? "
        "ORDER BY id LIMIT ?", (policy_key, limit)).fetchall()
    return [dict(row) for row in rows]


def approval_for(version_id: int,
                 policy_key: str = POLICY_KEY_DEFAULT) -> dict | None:
    """The newest approval authorising a version, or None.

    Matched on the version's *current* content hash, so an approval given for
    a configuration that has since been re-frozen is not silently reused for
    the new one.
    """
    version = get_version(version_id)
    if version is None:
        return None
    conn = memory_store._get_conn()
    init_schema(conn)
    row = conn.execute(
        "SELECT * FROM policy_approvals WHERE policy_key = ? AND version_id = ? "
        "AND content_hash = ? ORDER BY id DESC LIMIT 1",
        (policy_key, version_id, version["content_hash"])).fetchone()
    return dict(row) if row else None


# ── The single writer ──────────────────────────────────────────────────


def _write_pointer(conn: sqlite3.Connection, *, policy_key: str,
                   version_id: int, version_seq: int, actor: str,
                   reason: str, at: str, expected_seq: int) -> int:
    """The only statement in the repository that writes ``active_policy``.

    One function, so that "who may move what is in force" is a single place to
    read. Insert and compare-and-swap are the same statement: on a policy with
    no pointer the row is created, and on one that already has a pointer the
    update applies *only* when ``version_seq`` still equals ``expected_seq``.
    A change that raced another therefore changes no rows at all instead of
    overwriting it.

    Returns rows changed: 1 if the write landed, 0 if the swap lost.
    ``install`` passes ``expected_seq=0``, which no real pointer can hold, so
    "refuse if one already exists" is the same code path rather than a
    separate check that could drift away from it.
    """
    cursor = conn.execute(
        "INSERT INTO active_policy "
        "(policy_key, version_id, version_seq, changed_by, reason, changed_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(policy_key) DO UPDATE SET "
        "  version_id = excluded.version_id, "
        "  version_seq = excluded.version_seq, "
        "  changed_by = excluded.changed_by, "
        "  reason = excluded.reason, "
        "  changed_at = excluded.changed_at "
        "WHERE active_policy.version_seq = ?",
        (policy_key, version_id, version_seq, actor, reason, at, expected_seq))
    return cursor.rowcount


def _record_transition(conn: sqlite3.Connection, *, policy_key: str,
                       from_version_id: int | None, to_version_id: int,
                       version_seq: int, kind: str, actor: str, reason: str,
                       evidence: dict | None, at: str) -> None:
    """Append the audit step that explains a pointer move."""
    conn.execute(
        "INSERT INTO policy_transitions "
        "(policy_key, from_version_id, to_version_id, version_seq, kind, "
        " actor, reason, evidence_json, at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (policy_key, from_version_id, to_version_id, version_seq, kind, actor,
         reason, json.dumps(evidence or {}, ensure_ascii=False), at))


def _problems_for(policy_key: str) -> list[str]:
    """Structural complaints about one policy's record, or an empty list.

    Split out of :func:`integrity` so the promotion path can ask the same
    question without filtering formatted strings. Moving a pointer on top of a
    trail with a hole in it deepens the hole, and "未决问题已解决" has to mean
    something checkable rather than something a reviewer nods at.
    """
    conn = memory_store._get_conn()
    init_schema(conn)
    problems: list[str] = []
    pointer = conn.execute("SELECT * FROM active_policy WHERE policy_key = ?",
                           (policy_key,)).fetchone()
    if pointer is not None:
        version = conn.execute(
            "SELECT policy_key FROM policy_versions WHERE id = ?",
            (pointer["version_id"],)).fetchone()
        if version is None:
            problems.append(
                f"policy {policy_key!r} is in force at version "
                f"#{pointer['version_id']}, which does not exist")
        elif version["policy_key"] != policy_key:
            problems.append(
                f"policy {policy_key!r} points at version "
                f"#{pointer['version_id']}, which belongs to "
                f"{version['policy_key']!r}")
        trail = transitions_for(policy_key)
        if not trail:
            problems.append(
                f"policy {policy_key!r} is in force with no transition "
                "recorded: the pointer moved without a trail")
        else:
            if trail[-1]["version_seq"] != pointer["version_seq"]:
                problems.append(
                    f"policy {policy_key!r} is at seq {pointer['version_seq']} "
                    f"but its last transition is "
                    f"{trail[-1]['version_seq']}: the pointer moved unrecorded")
            for position, step in enumerate(trail, start=1):
                if step["version_seq"] != position:
                    problems.append(
                        f"policy {policy_key!r} has a gap in its transition "
                        f"trail at position {position} (seq "
                        f"{step['version_seq']})")
                    break
    for step in conn.execute(
            "SELECT * FROM policy_transitions WHERE policy_key = ?",
            (policy_key,)).fetchall():
        if conn.execute("SELECT 1 FROM policy_versions WHERE id = ?",
                        (step["to_version_id"],)).fetchone() is None:
            problems.append(
                f"policy transition #{step['id']} names version "
                f"#{step['to_version_id']}, which does not exist")
    return problems


def _require_eligible_gate(gate_decision, version_id: int) -> dict:
    """The gate verdict a promotion is allowed to cite, or a refusal.

    Structural, and deliberately checked here rather than in the CLI: a
    promotion service that trusts its caller to have checked is a promotion
    service with no rule in it. The caller supplies the row (``evolution``
    owns producing it) and this layer decides whether it is sufficient.
    """
    if not isinstance(gate_decision, dict):
        raise PolicyError(
            "A promotion must cite a gate verdict. §11 makes the evidence the "
            "reason a pointer may move, so a promotion without one is a "
            "preference.")
    if gate_decision.get("policy_version_id") != version_id:
        raise PolicyError(
            f"The cited verdict is about policy version "
            f"#{gate_decision.get('policy_version_id')}, not #{version_id}. "
            "Evidence for one policy is not evidence for another.")
    if gate_decision.get("outcome") != "promote":
        raise PolicyError(
            f"The cited verdict is {gate_decision.get('outcome')!r}, not "
            "'promote'. A rejection and an abstention are both real verdicts, "
            "and neither is a licence to promote.")
    if gate_decision.get("abstained"):
        raise PolicyError(
            "The cited verdict abstained: there was too little forward "
            "evidence to compare, which is not the same as evidence of "
            "improvement.")
    if not (gate_decision.get("validation_days") or 0) > 0:
        raise PolicyError(
            "The cited verdict rests on zero days of paired evidence. D7 is "
            "exactly the record of a gate that recorded rows like that while "
            "appearing to govern.")
    return gate_decision


def _require_matches_live(version: dict, sources, what: str) -> None:
    """Refuse unless the live configuration still hashes to this version.

    The registry's whole claim is that a version names the configuration that
    is *running*, not a copy someone stored. An approval or a pointer move
    against a version the live config has drifted away from would put a name
    on behaviour nobody froze — and would do it silently, since the pointer
    would look perfectly valid afterwards.
    """
    if sources is None:
        raise PolicyError(
            f"{what} needs the live configuration to check for drift. "
            "Recomputing the hash is the point: a version that can be named "
            "without checking is a stored copy, not a record of what runs.")
    if content_hash_for(sources) != version["content_hash"]:
        raise PolicyError(
            f"Policy version #{version['id']} no longer describes the live "
            "configuration: a prompt, model, retrieval budget or rule has "
            "been edited without opening a new version. Restore the "
            "configuration, or freeze the current one as a new version — "
            f"{what} against a version that is not what runs would name "
            "behaviour nobody froze.")


def _resolve_version(conn: sqlite3.Connection, version_id: int,
                     policy_key: str) -> dict:
    version = conn.execute("SELECT * FROM policy_versions WHERE id = ?",
                           (version_id,)).fetchone()
    if version is None:
        raise PolicyError(
            f"No policy version #{version_id}: a pointer has to name a "
            "version that exists.")
    if version["policy_key"] != policy_key:
        raise PolicyError(
            f"Policy version #{version_id} belongs to "
            f"{version['policy_key']!r}, not {policy_key!r}.")
    return dict(version)


# ── Moving the pointer ─────────────────────────────────────────────────


def install(*, version_id: int, actor: str, reason: str,
            policy_key: str = POLICY_KEY_DEFAULT,
            evidence: dict | None = None, at: str | None = None) -> int:
    """Put a version in force where none was. Returns the new ``version_seq``.

    Refuses if the policy already has a pointer. The first policy is
    *installed* because there is no incumbent to be measured against —
    promotion is a claim about improvement, and there is nothing to improve
    on. From the second change onward the only way to move the pointer is
    :func:`promote` or :func:`rollback`, both of which demand evidence; this
    function cannot be used as a back door that skips them.
    """
    actor = _text(actor, "actor")
    reason = _text(reason, "reason")
    _positive_id(version_id, "version_id")
    when = _text(at, "at") if at else clock.today()

    with memory_store._write_lock:
        conn = memory_store._get_conn()
        init_schema(conn)
        with conn:
            _resolve_version(conn, version_id, policy_key)
            if _write_pointer(conn, policy_key=policy_key,
                              version_id=version_id, version_seq=1,
                              actor=actor, reason=reason, at=when,
                              expected_seq=0) != 1:
                # expected_seq=0 cannot match a real pointer, so reaching here
                # means one already existed.
                raise PolicyError(
                    f"Policy {policy_key!r} already has a version in force. "
                    "Moving it is a promotion or a rollback, both of which "
                    "require evidence — installing again would bypass them.")
            _record_transition(conn, policy_key=policy_key, from_version_id=None,
                               to_version_id=version_id, version_seq=1,
                               kind="install", actor=actor, reason=reason,
                               evidence=evidence, at=when)
    return 1


def approve(*, version_id: int, approved_by: str, reason: str,
            gate_decision, sources, policy_key: str = POLICY_KEY_DEFAULT,
            at: str | None = None) -> int:
    """Record a person's authorisation for a version. Returns the approval id.

    Writes one row and moves nothing. §11 is explicit that the approval
    boundary is a human one and that a successful automatic evaluation is not
    a licence to promote, so this is a separate act from :func:`promote` and
    leaves a separate record — an approval that could be inferred from the
    gate verdict would not be an approval.

    ``gate_decision`` is the ``gate_decisions`` row being cited;
    ``sources`` is the live configuration, collected by the caller
    (``evolution.policy_sources.collect()``), because this layer may not
    import the layer that reads it.
    """
    approved_by = _text(approved_by, "approved_by")
    reason = _text(reason, "reason")
    _positive_id(version_id, "version_id")
    when = _text(at, "at") if at else clock.today()

    with memory_store._write_lock:
        conn = memory_store._get_conn()
        init_schema(conn)
        with conn:
            version = _resolve_version(conn, version_id, policy_key)
            _require_eligible_gate(gate_decision, version_id)
            _require_matches_live(version, sources, "Approving a version")
            cursor = conn.execute(
                "INSERT INTO policy_approvals "
                "(policy_key, version_id, content_hash, gate_decision_id, "
                " approved_by, reason, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (policy_key, version_id, version["content_hash"],
                 gate_decision.get("id"), approved_by, reason, when))
    return int(cursor.lastrowid)


def promote(*, version_id: int, actor: str, reason: str, sources,
            expected_seq: int | None = None,
            policy_key: str = POLICY_KEY_DEFAULT,
            at: str | None = None) -> int:
    """Move the pointer to a version, if the evidence and a person allow it.

    Four things must hold at once, and each refusal names the one that failed:

    1. the policy has a pointer already — there is an incumbent to improve on;
    2. a person has approved this version at this content hash
       (:func:`approve`), because §11 does not let an evaluation promote;
    3. the live configuration still hashes to the version, so the pointer
       names what actually runs;
    4. the policy's record is structurally sound, so the move does not land
       on top of a trail with a hole in it.

    The move itself is a compare-and-swap on ``version_seq``. Pass
    ``expected_seq`` to make the swap conditional on a value read earlier —
    that is the form two concurrent promotions take, and exactly one of them
    writes. Omitting it means "the sequence as I just read it", which is still
    atomic because the read and the write share one transaction.
    """
    actor = _text(actor, "actor")
    reason = _text(reason, "reason")
    _positive_id(version_id, "version_id")
    when = _text(at, "at") if at else clock.today()

    with memory_store._write_lock:
        conn = memory_store._get_conn()
        init_schema(conn)
        with conn:
            version = _resolve_version(conn, version_id, policy_key)
            pointer = conn.execute(
                "SELECT * FROM active_policy WHERE policy_key = ?",
                (policy_key,)).fetchone()
            if pointer is None:
                raise PolicyError(
                    f"Policy {policy_key!r} has nothing in force, so there is "
                    "nothing to promote past. The first version is installed: "
                    "promotion is a claim about improvement, and there is no "
                    "incumbent to improve on.")
            if pointer["version_id"] == version_id:
                raise PolicyError(
                    f"Policy version #{version_id} is already in force; a "
                    "promotion has to name a different version.")
            approval = approval_for(version_id, policy_key)
            if approval is None:
                raise PolicyError(
                    f"Policy version #{version_id} has not been approved by a "
                    "person. §11: a successful evaluation is not a licence to "
                    "promote, and no model may approve its own candidate — "
                    "record the approval first.")
            _require_matches_live(version, sources, "Promoting a version")
            problems = _problems_for(policy_key)
            if problems:
                raise PolicyError(
                    "The policy record is not sound, so the pointer will not "
                    "be moved onto it: " + "; ".join(problems))
            expected = (pointer["version_seq"] if expected_seq is None
                        else expected_seq)
            if expected != pointer["version_seq"]:
                raise PolicyError(
                    f"The pointer is at seq {pointer['version_seq']}, not "
                    f"{expected_seq}: it moved since this promotion was "
                    "prepared. Re-read it and decide again.")
            next_seq = pointer["version_seq"] + 1
            if _write_pointer(conn, policy_key=policy_key,
                              version_id=version_id, version_seq=next_seq,
                              actor=actor, reason=reason, at=when,
                              expected_seq=expected) != 1:
                raise PolicyError(
                    "Another change moved the pointer first; this promotion "
                    "changed nothing.")
            _record_transition(
                conn, policy_key=policy_key,
                from_version_id=pointer["version_id"], to_version_id=version_id,
                version_seq=next_seq, kind="promote", actor=actor,
                reason=reason, at=when,
                evidence={"approval_id": approval["id"],
                          "gate_decision_id": approval["gate_decision_id"],
                          "content_hash": version["content_hash"]})
    return next_seq


def rollback(*, to_version_id: int, actor: str, reason: str, sources,
             expected_seq: int | None = None,
             policy_key: str = POLICY_KEY_DEFAULT,
             at: str | None = None) -> int:
    """Restore a version that was in force before. Returns the new seq.

    Deliberately not a promotion run backwards. A promotion claims a version
    is *better* and has to be paid for with forward evidence; a rollback
    claims the incumbent is worse, and the evidence for that is that the
    target was already in force once — a fact in the transition trail rather
    than a new piece of state to keep in sync.

    It requires no gate verdict, and that is not a loophole: the trail entry
    it rests on was itself only created by an install, a promotion or an
    earlier rollback, so a version cannot be reached this way unless the
    audited entry point put it in force once.

    **It deletes nothing.** Fills, ledger rows and outcomes are untouched;
    only the pointer moves. A rollback that tidied up after the version it
    replaced would destroy the evidence that the version was ever tried.

    The drift check applies here too. If the live configuration has moved away
    from the target, moving the pointer would name behaviour nobody froze —
    and it would not actually restore anything, because the prompts and rules
    the target was frozen from are the parts that drifted.
    """
    actor = _text(actor, "actor")
    reason = _text(reason, "reason")
    _positive_id(to_version_id, "to_version_id")
    when = _text(at, "at") if at else clock.today()

    with memory_store._write_lock:
        conn = memory_store._get_conn()
        init_schema(conn)
        with conn:
            version = _resolve_version(conn, to_version_id, policy_key)
            pointer = conn.execute(
                "SELECT * FROM active_policy WHERE policy_key = ?",
                (policy_key,)).fetchone()
            if pointer is None:
                raise PolicyError(
                    f"Policy {policy_key!r} has nothing in force, so there is "
                    "nothing to roll back from.")
            if pointer["version_id"] == to_version_id:
                raise PolicyError(
                    f"Policy version #{to_version_id} is already in force; a "
                    "rollback has to name a version that is not.")
            trail = transitions_for(policy_key)
            if not any(step["to_version_id"] == to_version_id for step in trail):
                raise PolicyError(
                    f"Policy version #{to_version_id} was never in force, so "
                    "there is nothing to restore. A rollback restores a "
                    "version the audited entry point already put in force; "
                    "choosing a version that has never run is a promotion, "
                    "and a promotion needs forward evidence.")
            _require_matches_live(version, sources, "Rolling back to a version")
            problems = _problems_for(policy_key)
            if problems:
                raise PolicyError(
                    "The policy record is not sound, so the pointer will not "
                    "be moved onto it: " + "; ".join(problems))
            expected = (pointer["version_seq"] if expected_seq is None
                        else expected_seq)
            if expected != pointer["version_seq"]:
                raise PolicyError(
                    f"The pointer is at seq {pointer['version_seq']}, not "
                    f"{expected_seq}: it moved since this rollback was "
                    "prepared. Re-read it and decide again.")
            next_seq = pointer["version_seq"] + 1
            if _write_pointer(conn, policy_key=policy_key,
                              version_id=to_version_id, version_seq=next_seq,
                              actor=actor, reason=reason, at=when,
                              expected_seq=expected) != 1:
                raise PolicyError(
                    "Another change moved the pointer first; this rollback "
                    "changed nothing.")
            _record_transition(
                conn, policy_key=policy_key,
                from_version_id=pointer["version_id"], to_version_id=to_version_id,
                version_seq=next_seq, kind="rollback", actor=actor,
                reason=reason, at=when,
                evidence={"restored_from_seq": pointer["version_seq"],
                          "content_hash": version["content_hash"]})
    return next_seq


# ── Reporting ──────────────────────────────────────────────────────────


def counts() -> dict:
    """Versions, transitions, approvals, and policies with one in force."""
    conn = memory_store._get_conn()
    init_schema(conn)
    return {
        "versions": int(conn.execute(
            "SELECT COUNT(*) n FROM policy_versions").fetchone()["n"]),
        "transitions": int(conn.execute(
            "SELECT COUNT(*) n FROM policy_transitions").fetchone()["n"]),
        "approvals": int(conn.execute(
            "SELECT COUNT(*) n FROM policy_approvals").fetchone()["n"]),
        "active": int(conn.execute(
            "SELECT COUNT(*) n FROM active_policy").fetchone()["n"]),
    }


def integrity() -> list[str]:
    """Structural complaints about the policy record, or an empty list.

    Reports rather than repairs, the posture ``reconciliation``,
    ``outcomes.integrity``, ``learning_candidates.integrity`` and
    ``knowledge_snapshots.integrity`` all take. The per-policy checks live in
    :func:`_problems_for` because the promotion path asks the same question;
    on top of those, this adds the two cross-policy ones:

    * a pointer at a version that no install put in force and no person
      approved — the trader would be running something nobody authorised;
    * a transition naming a version that does not exist.
    """
    conn = memory_store._get_conn()
    init_schema(conn)
    problems: list[str] = []
    for pointer in conn.execute("SELECT * FROM active_policy").fetchall():
        key = pointer["policy_key"]
        problems.extend(_problems_for(key))
        trail = transitions_for(key)
        installed = any(step["to_version_id"] == pointer["version_id"]
                        and step["kind"] == "install" for step in trail)
        if not installed and approval_for(pointer["version_id"], key) is None:
            problems.append(
                f"policy {key!r} is in force at version "
                f"#{pointer['version_id']}, which no install put in force and "
                "no person approved: the pointer names behaviour nobody "
                "authorised")
    return problems
