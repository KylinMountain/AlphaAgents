"""Approved knowledge: the record of what a person put in force, and nothing else.

§10 separates two words this codebase used to conflate. A candidate can be
*kept* — stored, argued about, validated — and still not be *in force*:
"validation does not itself activate knowledge", and a candidate "must stay
outside production decision retrieval until it is included in an approved
policy snapshot". Before this module there was no snapshot, so "kept" and
"in force" had no boundary between them and no way to tell one from the
other.

What this module is: the boundary, written down. One call to :func:`approve`
records one immutable snapshot — who approved it, when, why, and exactly
which version of which knowledge row went in.

What this module is **not**: an activation path. Nothing here changes a
prompt, a retrieval weight, or the return value of any ``_get_*``. A
snapshot is evidence of a human decision; the code's only job is to keep the
evidence. There is no "apply snapshot" function, and adding one would be a
different phase's work with a different argument to make.

Two hashes, and they answer different questions:

* ``version_hash`` (per item) — "which version of this knowledge row did we
  approve". Computed by :func:`version_hash_for` from a declared per-entity
  field tuple, never taken from the caller.
* ``content_hash`` (per snapshot) — "has this record been altered since it
  was written". Covers the declared fields *and* the item set, and
  :func:`verify_snapshot` recomputes it, the same posture
  ``attribution.verify_snapshot`` takes for decision boundaries.
"""

from __future__ import annotations

import hashlib
import json

from alpha_agents.data import clock, learning_candidates, memory_store

# ── What the hashes cover ──────────────────────────────────────────────

#: The snapshot's declared fields, hashed as a group. Declared once: the
#: writer and the verifier both project through this tuple, so a field added
#: to the boundary cannot end up covered by the write and missing from the
#: check.
_SNAPSHOT_FIELDS = ("approved_by", "approved_at", "reason", "notes")

#: The item fields, in the order the hash reads them.
_ITEM_FIELDS = ("entity_type", "entity_id", "candidate_id", "version_hash")

#: Which columns of a knowledge row define "this version".
#:
#: Only the *stated rule*, never the running counts. ``win_rate``,
#: ``evidence_count``, ``total_trades``, ``hit_rate`` and the date columns
#: move on their own as trades close, and a snapshot whose hash changes when
#: a counter ticks cannot answer "what did we approve". ``status`` and
#: ``weight`` are in because they change what is in force; the narrative
#: columns are in because they are the rule as a person reads it.
_KNOWLEDGE_FIELDS = {
    "principle": ("principle", "pattern_description", "category",
                  "action_guidance", "status"),
    "playbook": ("name", "pattern_json", "status", "weight", "annotation"),
}

_KNOWLEDGE_TABLES = {"principle": "trading_principles", "playbook": "playbooks"}

ENTITY_TYPES = tuple(_KNOWLEDGE_FIELDS)


# ── Hashing ────────────────────────────────────────────────────────────


def _hash(fields) -> str:
    """Content hash, order-independent at every level.

    ``sort_keys`` makes the projection order-insensitive and the item list is
    sorted before hashing, so "the same approval written twice" hashes the
    same however the caller happened to list its items.
    """
    blob = json.dumps(fields, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _frozen(declared: dict, items: list[dict]) -> dict:
    """Project an approval onto exactly the hashed fields."""
    return {
        "snapshot": {name: declared[name] for name in _SNAPSHOT_FIELDS},
        "items": sorted(
            ({name: item[name] for name in _ITEM_FIELDS} for item in items),
            key=lambda item: (item["entity_type"], item["entity_id"]),
        ),
    }


def _text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"An approval needs a nonempty {field}: a snapshot without one "
            "records that something happened, not what was decided.")
    return value.strip()


def _positive_id(value, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field} must be a positive integer, got {value!r}")
    return value


# ── Versions ───────────────────────────────────────────────────────────


def version_hash_for(conn, entity_type: str, entity_id: int) -> str | None:
    """The current version hash of one knowledge row, or None if absent.

    Reads the row through the declared field tuple, so the hash identifies
    *content* rather than a row id: an edited principle is a different
    version, and that is the difference an approval needs to be able to see.
    """
    if entity_type not in _KNOWLEDGE_FIELDS:
        raise ValueError(
            f"Unknown knowledge entity type {entity_type!r}; known: "
            f"{', '.join(ENTITY_TYPES)}")
    columns = _KNOWLEDGE_FIELDS[entity_type]
    row = conn.execute(
        f"SELECT {', '.join(columns)} FROM {_KNOWLEDGE_TABLES[entity_type]} "
        "WHERE id = ?", (entity_id,)).fetchone()
    if row is None:
        return None
    return _hash({name: row[name] for name in columns})


# ── Writing ────────────────────────────────────────────────────────────


def _normalise_items(items) -> list[dict]:
    if not isinstance(items, list) or not items:
        raise ValueError(
            "An approval must name at least one knowledge item; a snapshot "
            "of nothing approves nothing.")
    seen = set()
    out = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"Each approval item must be an object, got {item!r}")
        unknown = sorted(set(item) - {"entity_type", "entity_id", "candidate_id"})
        if unknown:
            raise ValueError(f"Approval item has unknown keys {', '.join(unknown)}")
        entity_type = item.get("entity_type")
        if entity_type not in _KNOWLEDGE_FIELDS:
            raise ValueError(
                f"Unknown knowledge entity type {entity_type!r}; known: "
                f"{', '.join(ENTITY_TYPES)}")
        entity_id = _positive_id(item.get("entity_id"), "entity_id")
        candidate_id = item.get("candidate_id")
        if candidate_id is not None:
            candidate_id = _positive_id(candidate_id, "candidate_id")
        key = (entity_type, entity_id)
        if key in seen:
            raise ValueError(
                f"{entity_type} #{entity_id} is listed twice in one approval; "
                "which version is approved has to be a single answer.")
        seen.add(key)
        out.append({"entity_type": entity_type, "entity_id": entity_id,
                    "candidate_id": candidate_id})
    return out


def _resolve(conn, entry: dict) -> dict:
    """Fill in the version hash, and refuse knowledge that is not there."""
    entity_type, entity_id = entry["entity_type"], entry["entity_id"]
    version_hash = version_hash_for(conn, entity_type, entity_id)
    if version_hash is None:
        raise ValueError(
            f"No {entity_type} #{entity_id} to approve: an approval has to "
            "point at knowledge that exists.")
    candidate_id = entry["candidate_id"]
    if candidate_id is not None and learning_candidates.get_candidate(candidate_id) is None:
        raise ValueError(
            f"No learning candidate #{candidate_id}: an approval that cites "
            "one cannot be checked against it afterwards.")
    return {**entry, "version_hash": version_hash}


def plan(items) -> list[dict]:
    """Validate an approval's items and resolve their version hashes.

    Writes nothing. It exists so a person can be shown exactly what would be
    approved, and so a dry run validates through the same code as the real
    thing instead of a second copy of the rules that could drift from it.
    """
    conn = memory_store._get_conn()
    return [_resolve(conn, entry) for entry in _normalise_items(items)]


def approve(*, approved_by: str, reason: str, items: list[dict],
            notes: str | None = None, approved_at: str | None = None) -> int:
    """Record one approval as an immutable snapshot. Returns its id.

    ``items`` is a list of ``{"entity_type", "entity_id", "candidate_id"?}``.
    ``version_hash`` is computed from the knowledge row itself and never
    accepted from the caller: an approval claiming a version the knowledge
    never had would be worthless as evidence.

    ``approved_at`` is the date the approval claims, defaulting to the kernel
    clock; ``frozen_at`` is always the kernel clock at write time. They agree
    in normal use and diverge under replay, which is why both are stored.

    This function writes a record. It does not change what the system does —
    see the module docstring.
    """
    approved_by = _text(approved_by, "approved_by")
    reason = _text(reason, "reason")
    if notes is not None and not isinstance(notes, str):
        raise ValueError("notes must be text or None")
    when = _text(approved_at, "approved_at") if approved_at else clock.today()
    frozen_at = clock.today()

    with memory_store._write_lock:
        conn = memory_store._get_conn()
        with conn:
            resolved = plan(items)
            declared = {"approved_by": approved_by, "approved_at": when,
                        "reason": reason, "notes": notes}
            cursor = conn.execute(
                "INSERT INTO knowledge_snapshots "
                "(approved_by, approved_at, reason, notes, content_hash, frozen_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (approved_by, when, reason, notes,
                 _hash(_frozen(declared, resolved)), frozen_at))
            snapshot_id = cursor.lastrowid
            conn.executemany(
                "INSERT INTO knowledge_snapshot_items "
                "(snapshot_id, entity_type, entity_id, candidate_id, version_hash) "
                "VALUES (?, ?, ?, ?, ?)",
                [(snapshot_id, item["entity_type"], item["entity_id"],
                  item["candidate_id"], item["version_hash"])
                 for item in resolved])
    return snapshot_id


# ── Reading ────────────────────────────────────────────────────────────


def get_snapshot(snapshot_id: int) -> dict | None:
    row = memory_store._get_conn().execute(
        "SELECT * FROM knowledge_snapshots WHERE id = ?",
        (snapshot_id,)).fetchone()
    return dict(row) if row else None


def items_for(snapshot_id: int) -> list[dict]:
    rows = memory_store._get_conn().execute(
        "SELECT * FROM knowledge_snapshot_items WHERE snapshot_id = ? ORDER BY id",
        (snapshot_id,)).fetchall()
    return [dict(row) for row in rows]


def snapshots_for_candidate(candidate_id: int) -> list[dict]:
    """Every snapshot that cites this candidate, oldest first."""
    rows = memory_store._get_conn().execute(
        "SELECT s.* FROM knowledge_snapshots s WHERE EXISTS ("
        "  SELECT 1 FROM knowledge_snapshot_items i "
        "  WHERE i.snapshot_id = s.id AND i.candidate_id = ?) "
        "ORDER BY s.id", (candidate_id,)).fetchall()
    return [dict(row) for row in rows]


def candidate_is_approved(candidate_id: int) -> bool:
    """Is this candidate inside any approved snapshot?

    The machine form of §10's "must stay outside production decision
    retrieval until approved". A candidate that is not approved is not a
    lesser kind of knowledge; it is simply not in force, and this is how a
    caller tells the difference without re-deriving it.
    """
    return memory_store._get_conn().execute(
        "SELECT 1 FROM knowledge_snapshot_items WHERE candidate_id = ? LIMIT 1",
        (candidate_id,)).fetchone() is not None


def entity_is_approved(entity_type: str, entity_id: int) -> bool:
    """Is this knowledge row inside any approved snapshot?

    Note what this does *not* claim: it does not say the approved version is
    the version in force now. Use :func:`drifted` for that.
    """
    if entity_type not in _KNOWLEDGE_FIELDS:
        raise ValueError(f"Unknown knowledge entity type {entity_type!r}")
    return memory_store._get_conn().execute(
        "SELECT 1 FROM knowledge_snapshot_items "
        "WHERE entity_type = ? AND entity_id = ? LIMIT 1",
        (entity_type, entity_id)).fetchone() is not None


def approved_entities(entity_type: str | None = None) -> list[dict]:
    """Every approved item, newest snapshot first, optionally one type."""
    if entity_type is not None and entity_type not in _KNOWLEDGE_FIELDS:
        raise ValueError(f"Unknown knowledge entity type {entity_type!r}")
    sql = ("SELECT * FROM knowledge_snapshot_items "
           + ("WHERE entity_type = ? " if entity_type else "")
           + "ORDER BY snapshot_id DESC, id")
    args = (entity_type,) if entity_type else ()
    return [dict(row) for row in memory_store._get_conn().execute(sql, args)]


def all_snapshots(*, limit: int = 200) -> list[dict]:
    """Every snapshot on record, oldest first."""
    rows = memory_store._get_conn().execute(
        "SELECT * FROM knowledge_snapshots ORDER BY id LIMIT ?",
        (limit,)).fetchall()
    return [dict(row) for row in rows]


def counts() -> dict:
    """Snapshots recorded, and items across all of them."""
    conn = memory_store._get_conn()
    return {
        "snapshots": int(conn.execute(
            "SELECT COUNT(*) n FROM knowledge_snapshots").fetchone()["n"]),
        "items": int(conn.execute(
            "SELECT COUNT(*) n FROM knowledge_snapshot_items").fetchone()["n"]),
    }


def verify_snapshot(snapshot_id: int) -> bool:
    """Recompute a snapshot's hash and report whether it still matches.

    The triggers make a rewrite fail at write time. This makes one
    *detectable* if a write ever got past them — a dropped trigger, a
    restored backup, a hand-edited file. An unknown id returns False rather
    than raising: "absent" and "tampered" are both reasons not to trust a
    snapshot, and a caller asking this question wants one answer.
    """
    snapshot = get_snapshot(snapshot_id)
    if snapshot is None:
        return False
    return _hash(_frozen(snapshot, items_for(snapshot_id))) == snapshot["content_hash"]


# ── Drift and integrity ────────────────────────────────────────────────


def drifted() -> list[dict]:
    """Approved items whose knowledge row has since changed.

    Not a fault — knowledge is expected to keep moving. It is the answer to
    "is what we approved still what is there", which is the one question the
    snapshot exists to make answerable, and the reason ``version_hash`` is
    content rather than a row id.
    """
    conn = memory_store._get_conn()
    out = []
    for item in approved_entities():
        current = version_hash_for(conn, item["entity_type"], item["entity_id"])
        if current != item["version_hash"]:
            out.append({**item, "current_version_hash": current})
    return out


def integrity() -> list[str]:
    """Structural complaints about the approval record, or an empty list.

    Reports rather than repairs, the posture ``reconciliation``,
    ``outcomes.integrity`` and ``learning_candidates.integrity`` all take.
    Four things the write boundary cannot enforce afterwards:

    * a snapshot whose ``content_hash`` no longer recomputes — the triggers
      should make that impossible, so its presence means one was bypassed;
    * an item whose knowledge row has since been deleted;
    * an item whose candidate has since been deleted;
    * a snapshot with no items, which approves nothing while looking like
      it approves something.
    """
    conn = memory_store._get_conn()
    problems: list[str] = []
    for snapshot in conn.execute(
            "SELECT id, content_hash FROM knowledge_snapshots ORDER BY id").fetchall():
        items = items_for(snapshot["id"])
        if not items:
            problems.append(
                f"knowledge snapshot #{snapshot['id']} has no items: it "
                "records an approval of nothing")
            continue
        if not verify_snapshot(snapshot["id"]):
            problems.append(
                f"knowledge snapshot #{snapshot['id']} does not match its "
                "content_hash: it was altered after it was written")
    for item in approved_entities():
        if version_hash_for(conn, item["entity_type"], item["entity_id"]) is None:
            problems.append(
                f"knowledge snapshot #{item['snapshot_id']} approves "
                f"{item['entity_type']} #{item['entity_id']}, which no longer "
                "exists")
        candidate_id = item["candidate_id"]
        if candidate_id is not None and \
                learning_candidates.get_candidate(candidate_id) is None:
            problems.append(
                f"knowledge snapshot #{item['snapshot_id']} cites learning "
                f"candidate #{candidate_id}, which no longer exists")
    return problems
