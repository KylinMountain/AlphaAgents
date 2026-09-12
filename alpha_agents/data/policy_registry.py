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

Two writes, and they are not the same act:

* :func:`freeze` records a version. Idempotent per content — freezing the
  same configuration twice is one version, because a version identifies
  behaviour, not the act of recording it.
* :func:`install` sets the pointer for a policy that has none. It refuses
  once a pointer exists; from then on only promotion or rollback may move
  it, and those are the audited entry point.
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

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_policy_versions_key "
    "ON policy_versions(policy_key)",
    "CREATE INDEX IF NOT EXISTS idx_policy_transitions_key "
    "ON policy_transitions(policy_key)",
)

# A version is a fact about a configuration, and a transition is a fact about
# who moved the pointer. Neither may be rewritten afterwards — the pointer is
# the only mutable row, which is exactly why it needs an append-only trail.
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
            version = conn.execute(
                "SELECT policy_key FROM policy_versions WHERE id = ?",
                (version_id,)).fetchone()
            if version is None:
                raise PolicyError(
                    f"No policy version #{version_id} to install: the pointer "
                    "has to name a version that exists.")
            if version["policy_key"] != policy_key:
                raise PolicyError(
                    f"Policy version #{version_id} belongs to "
                    f"{version['policy_key']!r}, not {policy_key!r}.")
            if conn.execute("SELECT 1 FROM active_policy WHERE policy_key = ?",
                            (policy_key,)).fetchone():
                raise PolicyError(
                    f"Policy {policy_key!r} already has a version in force. "
                    "Moving it is a promotion or a rollback, both of which "
                    "require evidence — installing again would bypass them.")
            conn.execute(
                "INSERT INTO active_policy "
                "(policy_key, version_id, version_seq, changed_by, reason, "
                " changed_at) VALUES (?, ?, 1, ?, ?, ?)",
                (policy_key, version_id, actor, reason, when))
            conn.execute(
                "INSERT INTO policy_transitions "
                "(policy_key, from_version_id, to_version_id, version_seq, "
                " kind, actor, reason, evidence_json, at) "
                "VALUES (?, NULL, ?, 1, 'install', ?, ?, ?, ?)",
                (policy_key, version_id, actor, reason,
                 json.dumps(evidence or {}, ensure_ascii=False), when))
    return 1


# ── Reporting ──────────────────────────────────────────────────────────


def counts() -> dict:
    """Versions, transitions, and how many policies have one in force."""
    conn = memory_store._get_conn()
    init_schema(conn)
    return {
        "versions": int(conn.execute(
            "SELECT COUNT(*) n FROM policy_versions").fetchone()["n"]),
        "transitions": int(conn.execute(
            "SELECT COUNT(*) n FROM policy_transitions").fetchone()["n"]),
        "active": int(conn.execute(
            "SELECT COUNT(*) n FROM active_policy").fetchone()["n"]),
    }


def integrity() -> list[str]:
    """Structural complaints about the policy record, or an empty list.

    Reports rather than repairs, the posture ``reconciliation``,
    ``outcomes.integrity``, ``learning_candidates.integrity`` and
    ``knowledge_snapshots.integrity`` all take. Four things the write
    boundary cannot enforce afterwards:

    * a pointer naming a version that does not exist, or one belonging to
      another policy — the trader would be running something unnamed;
    * a transition naming a version that does not exist;
    * a ``version_seq`` sequence that does not start at 1 and rise by one,
      which would mean a transition was lost and the audit trail has a hole;
    * a pointer whose ``version_seq`` disagrees with the last transition,
      which would mean the pointer moved without leaving a record.
    """
    conn = memory_store._get_conn()
    init_schema(conn)
    problems: list[str] = []

    for pointer in conn.execute("SELECT * FROM active_policy").fetchall():
        version = conn.execute(
            "SELECT policy_key FROM policy_versions WHERE id = ?",
            (pointer["version_id"],)).fetchone()
        if version is None:
            problems.append(
                f"policy {pointer['policy_key']!r} is in force at version "
                f"#{pointer['version_id']}, which does not exist")
            continue
        if version["policy_key"] != pointer["policy_key"]:
            problems.append(
                f"policy {pointer['policy_key']!r} points at version "
                f"#{pointer['version_id']}, which belongs to "
                f"{version['policy_key']!r}")
        trail = transitions_for(pointer["policy_key"])
        if not trail:
            problems.append(
                f"policy {pointer['policy_key']!r} is in force with no "
                "transition recorded: the pointer moved without a trail")
            continue
        if trail[-1]["version_seq"] != pointer["version_seq"]:
            problems.append(
                f"policy {pointer['policy_key']!r} is at seq "
                f"{pointer['version_seq']} but its last transition is "
                f"{trail[-1]['version_seq']}: the pointer moved unrecorded")
        for position, step in enumerate(trail, start=1):
            if step["version_seq"] != position:
                problems.append(
                    f"policy {pointer['policy_key']!r} has a gap in its "
                    f"transition trail at position {position} "
                    f"(seq {step['version_seq']})")
                break

    for step in conn.execute("SELECT * FROM policy_transitions").fetchall():
        if conn.execute("SELECT 1 FROM policy_versions WHERE id = ?",
                        (step["to_version_id"],)).fetchone() is None:
            problems.append(
                f"policy transition #{step['id']} names version "
                f"#{step['to_version_id']}, which does not exist")
    return problems
