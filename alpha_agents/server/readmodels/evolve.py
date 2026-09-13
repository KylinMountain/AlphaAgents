"""Evolve laboratory: what is in force, what is being tested, what was decided.

Four sections, and a ``code`` block that is not a section because it is not
read from the database at all:

* ``pointer`` — the version in force and the pointer moves behind it.
* ``shadow`` — the forward runs, the forecasts inside them, and how many are
  paired with the champion yet.
* ``gates`` — the verdicts, including the abstentions, because a rejected
  candidate with no record gets proposed again next week.
* ``knowledge`` — the approved snapshots: the difference between "recorded"
  and "in force", which U5 made a gate.

**Why the ``code`` block exists.** Three of these four sections report
``absent`` on the current production database, and "no rows" is the wrong
reading of that — the tables were never created because nothing ever ran. The
underlying reason is in the code, not the data: ``shadow.PRODUCERS`` registers
one producer and it is the constant-0.5 baseline, while a promotion only
accepts ``candidate_policy`` evidence. So the promotion path is unreachable in
this build, and that fact has to travel with the payload or the page will
render "0 verdicts" as if it were a quiet week.

**Why every reader here is gated by a probe.** ``get_gate_history`` catches
its own exception and returns ``[]``, so "the table does not exist" and "no
verdict has ever been recorded" are the same answer. ``get_gate_decisions``
is worse for the opposite reason: it calls ``_ensure_gate_table``, so reading
it *migrates* — which is how a page view becomes a schema change.
"""

from __future__ import annotations

import logging

from alpha_agents.server.readmodels import Need, Section, section, workspace

logger = logging.getLogger(__name__)


def _read_pointer() -> tuple[dict, int]:
    from alpha_agents.data import policy_registry as pr
    versions = pr.versions_for(limit=50)
    active = pr.active_version()
    return {
        "active": active,
        "active_ref": pr.active_ref(),
        "versions": versions,
        "counts": pr.counts(),
        "approvals": pr.approvals_for(limit=50),
        "transitions": pr.transitions_for(limit=50),
        "integrity": pr.integrity(),
    }, len(versions)


def _read_shadow() -> tuple[dict, int]:
    from alpha_agents.evolution import shadow
    runs = shadow.runs(limit=100)
    return {
        "runs": runs,
        "counts": shadow.counts(),
        "paired": {r["id"]: shadow.paired_count(r["id"]) for r in runs},
        "integrity": shadow.integrity(),
    }, len(runs)


def _read_gates() -> tuple[dict, int]:
    from alpha_agents.evolution import holdout_gate as gate
    rows = gate.get_gate_decisions(limit=100)
    return {
        "decisions": rows,
        "n": len(rows),
        "promoted": sum(1 for r in rows if r.get("promoted")),
        "abstained": sum(1 for r in rows if r.get("abstained")),
        "non_abstain": sum(1 for r in rows
                           if not r.get("abstained")
                           and r.get("promoted") is not None),
    }, len(rows)


def _read_knowledge() -> tuple[dict, int]:
    from alpha_agents.data import knowledge_snapshots as ks
    snaps = ks.all_snapshots(limit=100)
    return {
        "snapshots": snaps,
        "counts": ks.counts(),
        "drifted": ks.drifted(),
        "integrity": ks.integrity(),
    }, len(snaps)


def _code_facts() -> dict:
    """What this build can do, as opposed to what the database holds."""
    from alpha_agents.data import policy_registry as pr
    from alpha_agents.evolution import shadow
    producers = sorted(shadow.PRODUCERS)
    candidates = sorted(name for name in producers
                        if shadow.PRODUCERS[name].kind == shadow.KIND_CANDIDATE)
    return {
        "producers": producers,
        "baseline": shadow.BASELINE_NAME,
        "candidate_producers": candidates,
        "promotion_accepts": pr.SCOPE_CANDIDATE,
        "reachable": bool(candidates),
    }


def snapshot() -> dict:
    """The evolve laboratory."""
    from alpha_agents.data import memory_store
    conn = memory_store._get_conn()
    sections = {
        "pointer": section(conn, Section(
            source="policy_versions + active_policy + policy_approvals",
            needs=(Need("policy_versions", (
                        "policy_key", "content_hash", "frozen_at")),
                   Need("active_policy", ("version_id", "changed_at")),
                   Need("policy_approvals", (
                        "version_id", "content_hash", "approved_by"))),
            read=_read_pointer,
            note=("active_policy 是**指针**，不是「最近一次批准的快照」。"
                  "后者会是第二个「什么在生效」的真相来源。"))),
        "shadow": section(conn, Section(
            source="shadow_runs + shadow_predictions",
            needs=(Need("shadow_runs", (
                        "policy_version_id", "report_type", "producer",
                        "status", "opened_at")),
                   Need("shadow_predictions", (
                        "run_id", "date", "prob", "brier"))),
            read=_read_shadow,
            note=("影子预测写在独立表里：冠军的读者从 predictions 选行，"
                  "看不见不存在的行 —— 隔离是结构性的，不靠每个读者记得加过滤。"))),
        "gates": section(conn, Section(
            source="gate_decisions",
            needs=(Need("gate_decisions", (
                "date", "candidate", "promoted", "abstained", "n",
                "validation_days", "evidence_scope")),),
            read=_read_gates,
            note=("既要看 promoted 也要看 abstained：只有被记录的拒绝才能阻止同一个"
                  "候选下周被再提一次。evidence_scope 是晋升拒绝所依据的那个事实。"))),
        "knowledge": section(conn, Section(
            source="knowledge_snapshots + knowledge_snapshot_items",
            needs=(Need("knowledge_snapshots", (
                        "approved_by", "approved_at")),
                   Need("knowledge_snapshot_items", (
                        "snapshot_id", "entity_type", "entity_id"))),
            read=_read_knowledge,
            note=("批准是「可查的事实」还是「已生效的行为」，取决于有没有第二列 —— "
                  "U5 之后检索引擎读的是 active_policy 指向的快照。"))),
    }
    return workspace("evolve", sections, code=_code_facts())
