"""Give the agent its own experience back.

What was broken. The learning chain wrote and nothing read. Four functions
existed, fully implemented, and **none of them had a caller**:

- ``memory_store.reinforce_trading_principle`` — so ``win_rate`` stayed NULL
- ``knowledge_snapshots.approve`` — so the snapshot table held 0 rows
- ``policy_registry`` never got a version naming a snapshot
- ``learning_candidates.advance_candidate`` — 21 candidates stuck at
  ``observation``

The consequence reached the agent's own prompt, every session, as a line
that reads like patience rather than a broken wire::

    【交易经验手册】（未生效：没有任何已批准的知识快照在生效）

Two principles with real content and real evidence sat in the table since
2026-09-09 and the trader never saw either of them. "It starts new every
day" was literally true.

**What this module does not do.** It does not put anything in force. It
freezes a snapshot and a candidate version, both write-only, and stops.
Moving the pointer is ``policy_registry.promote``, which demands a cited
verdict with n paired samples and a forward window, and this module has no
opinion about that gate.

**Why there is no per-principle win rate here.** The evidence a principle
carries is written by the model that proposed it — ``lessons.py`` asks for
``{"code", "date", "outcome"}`` in free text. Counting those as wins would
be the model grading its own claim, which ``docs/GOLDEN_PRINCIPLES.md``
forbids for exactly this reason. A principle's utility has to come from what
the book did with it, measured forward, and that measurement is an A/B
replay rather than a field in the row.

**Why holdout_gate is not that measurement either.** It scores a shadow
run bound to the version, and shadow's producers are probability mappings
(``constant_0.5``, ``remap_confidence``). That gate answers "is this
calibration better", not "did this knowledge make better trades". The gap is
real and is recorded in the plan rather than papered over.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Who is recorded as having approved the snapshot. Not a person's name and
#: not a model's: a reader of ``knowledge_snapshots`` must be able to tell at
#: a glance that a pipeline froze this, and that freezing is not approval to
#: act — the pointer is a separate move with its own preconditions.
ACTOR = "knowledge_freeze"


def eligible(min_evidence: int = 1) -> list[dict]:
    """Principles and playbooks worth freezing, newest first.

    ``min_evidence`` is deliberately low and deliberately not a quality
    gate. Nothing here decides whether a principle is *good*; that is the
    A/B replay's job. What this filters is rows that cannot even be pointed
    at — a principle with no evidence at all is a sentence, not a claim.
    """
    from alpha_agents.data import memory_store

    conn = memory_store._get_conn()
    items: list[dict] = []
    for row in conn.execute(
            "SELECT id, principle, evidence_count FROM trading_principles "
            "WHERE status = 'active' AND evidence_count >= ? "
            "ORDER BY id", (min_evidence,)).fetchall():
        items.append({"entity_type": "principle", "entity_id": int(row["id"]),
                      "what": row["principle"],
                      "evidence_count": int(row["evidence_count"] or 0)})
    for row in conn.execute(
            "SELECT id, name, total_trades FROM playbooks "
            "WHERE status = 'active' ORDER BY id").fetchall():
        items.append({"entity_type": "playbook", "entity_id": int(row["id"]),
                      "what": row["name"],
                      "evidence_count": int(row["total_trades"] or 0)})
    return items


def freeze_candidate(*, reason: str, min_evidence: int = 1) -> dict:
    """Freeze today's knowledge into a snapshot and a candidate version.

    Returns what it wrote, or ``{"frozen": False, "why": ...}`` when there is
    nothing to freeze. Nothing is put in force and nothing a trader does
    changes: a candidate version is a record of a configuration, and the
    pointer is not touched.
    """
    from alpha_agents.data import knowledge_snapshots, policy_registry
    from alpha_agents.evolution import policy_sources

    items = eligible(min_evidence)
    if not items:
        logger.info("knowledge freeze: nothing eligible (min_evidence=%d)",
                    min_evidence)
        return {"frozen": False, "why": "no eligible knowledge"}

    snapshot_id = knowledge_snapshots.approve(
        approved_by=ACTOR, reason=reason,
        items=[{"entity_type": item["entity_type"],
                "entity_id": item["entity_id"]} for item in items],
        notes=f"{len(items)} item(s): "
              + "; ".join(f"{i['entity_type']}#{i['entity_id']}" for i in items))

    sources = policy_sources.collect(knowledge_snapshot_id=snapshot_id)
    version_id = policy_registry.freeze(
        sources=sources, created_by=ACTOR,
        reason=f"{reason}（知识快照 #{snapshot_id}，{len(items)} 条）")

    logger.info("knowledge freeze: snapshot #%d with %d item(s) → candidate "
                "version #%d. Nothing is in force yet.",
                snapshot_id, len(items), version_id)
    return {"frozen": True, "snapshot_id": snapshot_id,
            "version_id": version_id, "items": items}


def in_force() -> dict:
    """What the agent can actually read right now.

    Exists because the answer was invisible without it: the prompt said
    "未生效" and nothing said whether that meant "nothing learned yet" or
    "the wire is cut".
    """
    from alpha_agents.data import knowledge_snapshots
    from alpha_agents.evolution import feedback

    snapshot_id = feedback.in_force_snapshot_id()
    if snapshot_id is None:
        return {"snapshot_id": None, "items": [],
                "why": "the policy in force names no knowledge snapshot"}
    return {"snapshot_id": snapshot_id,
            "items": knowledge_snapshots.items_for(snapshot_id)}
