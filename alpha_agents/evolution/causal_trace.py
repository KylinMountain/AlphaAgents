"""Causal trace: did a learned observation actually change a decision?

The diagram's last arrow. Every other box asks "did the mechanism run"; this
one asks the only question that makes the loop a loop — **can a decision be
traced back to the episode that caused it.**

What can be traced today, stated before the code rather than discovered after:

    episode → candidate → variant(version) → decision → outcome

Each link exists in the book:

* ``learning_candidates.evidence_episode_ids`` names the episodes a candidate
  was distilled from (written by ``evolution.evidence``);
* ``policy_variants`` (added by ``evolution.variant``) records which candidate
  proposed which frozen version;
* ``decision_snapshots.policy_ref`` names the version in force when a decision
  was made, and ``policy_registry.parse_ref`` splits it;
* ``episodes`` joins the decision to its outcome.

What is **not** traceable, and this module says so instead of implying
otherwise: a decision does not record *which rule text* it read. The prompt
assembly (``evolution.context_builder``) renders principles into a string and
the string is not kept per decision. So a trace can say "this decision ran
under the version a candidate proposed" and cannot say "this decision quoted
sentence three of that candidate". Closing that would mean persisting the
rendered knowledge block per decision — a schema and prompt-path change that
this module deliberately does not make on its own, because a trace that
overclaims is worse than one that names its own boundary.

The two rates the plan asks for:

``causal_trace_rate``
    Of the decisions made after a candidate was promoted, what fraction ran
    under a version that traces to a candidate at all. A rate of zero is the
    honest answer while nothing has been promoted, and it is a *finding*: the
    loop has never turned in production.

``decision_change_rate``
    Of the decisions made after a variant was frozen, what fraction ran under
    a version that variant produced. This is the number that separates "the
    system recorded a lesson" from "the lesson changed what the system did".
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class Chain:
    """One traced decision, with each link it could establish."""

    decision_id: int
    code: str | None
    decided_at: str | None
    policy_ref: str | None
    version_id: int | None = None
    candidate_id: int | None = None
    episode_ids: list[int] = field(default_factory=list)
    broken_at: str | None = None

    @property
    def complete(self) -> bool:
        """Every link from the decision back to a cited episode is present."""
        return (self.version_id is not None and self.candidate_id is not None
                and bool(self.episode_ids))

    def as_dict(self) -> dict:
        return {
            "decision_id": self.decision_id,
            "code": self.code,
            "decided_at": self.decided_at,
            "policy_ref": self.policy_ref,
            "version_id": self.version_id,
            "candidate_id": self.candidate_id,
            "episode_ids": self.episode_ids,
            "broken_at": self.broken_at,
            "complete": self.complete,
        }


def _variant_links(conn: sqlite3.Connection) -> dict[int, int]:
    """``version_id → candidate_id`` for every variant ever built."""
    try:
        rows = conn.execute(
            "SELECT version_id, candidate_id FROM policy_variants").fetchall()
    except sqlite3.DatabaseError:
        # No variant has ever been built on this database. That is the normal
        # state before the EVOLVE half has run once, not an error.
        return {}
    return {int(r["version_id"]): int(r["candidate_id"]) for r in rows}


def _cited_episodes(conn: sqlite3.Connection, candidate_id: int) -> list[int]:
    row = conn.execute(
        "SELECT evidence_episode_ids FROM learning_candidates WHERE id = ?",
        (candidate_id,)).fetchone()
    if row is None or not row["evidence_episode_ids"]:
        return []
    try:
        cited = json.loads(row["evidence_episode_ids"])
    except (TypeError, ValueError):
        return []
    ids: set[int] = set()
    for key in ("supporting", "opposing"):
        for value in (cited.get(key) or []):
            if isinstance(value, int):
                ids.add(value)
    return sorted(ids)


def trace_decisions(*, limit: int = 200,
                    conn: sqlite3.Connection | None = None) -> list[Chain]:
    """Every recent decision, with as much of its chain as the book holds.

    Newest first. A decision whose chain stops partway is returned with
    ``broken_at`` naming the link that was missing — dropping it would make
    "no trace" and "no decision" the same output, which is the distinction
    this whole module exists to keep.
    """
    from alpha_agents.data import policy_registry as PR

    if conn is None:
        from alpha_agents.data.memory_store import _get_conn
        conn = _get_conn()

    links = _variant_links(conn)
    rows = conn.execute(
        "SELECT id, code, decided_at, policy_ref FROM decision_snapshots "
        "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    out: list[Chain] = []
    for row in rows:
        chain = Chain(decision_id=int(row["id"]), code=row["code"],
                      decided_at=row["decided_at"],
                      policy_ref=row["policy_ref"])
        parsed = PR.parse_ref(row["policy_ref"])
        if parsed is None:
            chain.broken_at = "policy_ref"
            out.append(chain)
            continue
        _, version_id, _ = parsed
        chain.version_id = version_id
        candidate_id = links.get(version_id)
        if candidate_id is None:
            chain.broken_at = "version→candidate"
            out.append(chain)
            continue
        chain.candidate_id = candidate_id
        chain.episode_ids = _cited_episodes(conn, candidate_id)
        if not chain.episode_ids:
            chain.broken_at = "candidate→episode"
        out.append(chain)
    return out


def rates(*, limit: int = 200,
          conn: sqlite3.Connection | None = None) -> dict:
    """The two rates, over the most recent ``limit`` decisions.

    Both are reported with their denominator, because a rate over zero
    decisions is not 0% — it is undefined, and printing 0% would read as
    "the loop is failing" when the truth is "nothing has happened yet".
    """
    chains = trace_decisions(limit=limit, conn=conn)
    total = len(chains)
    traced = sum(1 for c in chains if c.complete)
    changed = sum(1 for c in chains if c.candidate_id is not None)
    return {
        "decisions": total,
        "causal_trace_rate": (traced / total) if total else None,
        "traced": traced,
        "decision_change_rate": (changed / total) if total else None,
        "changed": changed,
        "broken": [c.as_dict() for c in chains if not c.complete][:10],
        "note": (
            "A decision records which policy version it ran under, not which "
            "rule text it read: the rendered knowledge block is not persisted "
            "per decision. So 'traced' means the chain "
            "episode → candidate → variant → decision is complete, and it "
            "does not mean the decision quoted the candidate."),
    }


def summary_lines(*, limit: int = 200,
                  conn: sqlite3.Connection | None = None) -> list[str]:
    """The rates as report lines, for the daily review."""
    r = rates(limit=limit, conn=conn)
    if r["decisions"] == 0:
        return ["• 因果链：还没有决策快照，两个比率都无从计算（不是 0%）"]
    def _pct(v):
        return "—" if v is None else f"{v * 100:.1f}%"
    return [
        f"• 因果链：{r['traced']}/{r['decisions']} 条决策可完整追溯到 episode"
        f"（causal_trace_rate {_pct(r['causal_trace_rate'])}）",
        f"• 行为改变：{r['changed']}/{r['decisions']} 条决策跑在候选提出的版本上"
        f"（decision_change_rate {_pct(r['decision_change_rate'])}）",
    ]
