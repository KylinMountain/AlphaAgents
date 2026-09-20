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

    **The name overclaimed, and this module now says so twice.** Running under
    a candidate's version is *lineage*: it proves the version was in force. It
    does not prove the decision would have differed without it — a decision
    that runs on variant 17 and orders the same code at the same size as the
    incumbent changed nothing. The metric kept the name ``decision_change_rate``
    and the paragraph above kept claiming it answered "did the lesson change
    what the system did", which is a counterfactual and cannot be read off a
    single ledger.

    The counterfactual is now its own function (:func:`counterfactual_changes`),
    over decisions that actually form a pair: same trader, same information
    cutoff, same code, **different** ``policy_ref``. It compares the frozen
    intents field by field and reports the rate over *pairs*, with the pair
    count as its denominator. ``lineage_rate`` is what the old number is, named
    honestly.
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
    """The rates, over the most recent ``limit`` decisions.

    Every rate carries its denominator, because a rate over zero decisions is
    not 0% — it is undefined, and printing 0% would read as "the loop is
    failing" when the truth is "nothing has happened yet".

    ``decision_change_rate`` is **lineage**, and is kept under its old key for
    callers that already read it, with ``lineage_rate`` as the honest alias and
    ``lineage_note`` stating what it does not mean. The counterfactual answer —
    "would the decision have differed without the candidate's policy" — is
    :func:`counterfactual_changes` and its rate is ``counterfactual_change_rate``;
    it is ``None`` (undefined) until a real ON/OFF pair exists.
    """
    chains = trace_decisions(limit=limit, conn=conn)
    total = len(chains)
    traced = sum(1 for c in chains if c.complete)
    changed = sum(1 for c in chains if c.candidate_id is not None)
    cf = counterfactual_changes(limit=limit, conn=conn)
    return {
        "decisions": total,
        "causal_trace_rate": (traced / total) if total else None,
        "traced": traced,
        "decision_change_rate": (changed / total) if total else None,
        "lineage_rate": (changed / total) if total else None,
        "changed": changed,
        "counterfactual_change_rate": (
            (cf["changed"] / cf["pairs"]) if cf["pairs"] else None),
        "counterfactual_changed": cf["changed"],
        "counterfactual_pairs": cf["pairs"],
        "broken": [c.as_dict() for c in chains if not c.complete][:10],
        "note": (
            "A decision records which policy version it ran under. Since "
            "2026-09-21 it also records the sha256 of the rendered knowledge "
            "block it was shown (features_json._ctx.knowledge_hash), so "
            "'was this note in front of the agent' is now checkable rather "
            "than inferred: re-render the block as of decided_at and compare. "
            "'traced' still means only that the chain "
            "episode → candidate → variant → decision is complete; it does "
            "not mean the decision quoted the candidate, and a decision "
            "predating the hash has none to check."),
        "lineage_note": (
            "decision_change_rate / lineage_rate counts decisions that ran "
            "under a version a candidate proposed. That is lineage, not "
            "behaviour change: a decision on variant 17 that orders the same "
            "code at the same size as the incumbent is counted here and "
            "changed nothing. The counterfactual rate needs a real ON/OFF "
            "pair over one world state (same trader, same information_cutoff, "
            "same code, different policy_ref) and compares the frozen intents "
            "field by field."),
    }


#: The intent fields a counterfactual comparison looks at. Named rather than
#: compared as whole payloads: a payload also carries the reason prose and the
#: source, and two decisions that order the same thing for differently-worded
#: reasons have not changed behaviour. ``None`` on both sides counts as equal —
#: "both declined to state a stop" is the same decision, not a difference.
_INTENT_FIELDS = ("action", "code", "shares", "size_pct",
                  "entry_low", "entry_high", "stop_loss", "target_price")


def _intent_of(payload_json: str | None) -> dict:
    """The comparable part of a frozen decision payload.

    Absent fields normalise to ``None`` so a payload that omits a key and one
    that writes ``null`` compare equal — otherwise the comparison would report
    a difference that exists only in serialisation.
    """
    try:
        payload = json.loads(payload_json) if payload_json else {}
    except (TypeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {k: payload.get(k) for k in _INTENT_FIELDS}


def _intent_differs(a: dict, b: dict) -> bool:
    """Do these two intents describe different behaviour?

    Numeric fields are compared at their stored precision. ``0`` and ``None``
    are **not** the same: "no position" and "a zero-size position" are
    different instructions, and the whole point of a counterfactual is to
    catch that kind of difference rather than smooth it away.
    """
    for key in _INTENT_FIELDS:
        av, bv = a.get(key), b.get(key)
        if av is None and bv is None:
            continue
        if isinstance(av, (int, float)) and isinstance(bv, (int, float)):
            if float(av) != float(bv):
                return True
            continue
        if av != bv:
            return True
    return False


def counterfactual_changes(*, limit: int = 200,
                           conn: sqlite3.Connection | None = None,
                           other_conn: sqlite3.Connection | None = None
                           ) -> dict:
    """Pair decisions that differ **only** in which policy decided them.

    A pair is two ``decision_snapshots`` rows with the same ``trader_id``,
    same ``information_cutoff``, same ``code`` and **different** ``policy_ref``.
    That is the closest a ledger can get to the ON/OFF experiment the design
    asks for (§8.6): the same trader, the same world state, two policies, and
    the intents they produced.

    ``other_conn`` is the ON/OFF case, and it is why this takes two
    connections. The design is explicit that the two arms have **independent
    books** ("账本各自独立"), so the pair lives in two files and a single-`conn`
    reading can never see one. Rows from both are read into one index and
    paired by the same key; ``arm`` on each side records which book a
    decision came from, so a difference can be attributed without assuming
    which connection was "the experiment".

    It is deliberately strict about what it will pair, and it reports what it
    refused:

    * two rows under the **same** policy are not a pair — that is a retry or a
      second order, and comparing it would report noise as change;
    * rows whose cutoff differs are not paired — they decided on different
      information, so any difference could be the day rather than the policy.

    A **one-sided** opportunity — same trader, cutoff and code, but only one
    arm has a row — is the case the design says must stay in the denominator
    ("不得只比两边都买过的票的交集"). It is reported as
    ``one_sided`` rather than silently dropped, because "the other arm chose
    not to act" is exactly the change being measured.
    """
    if conn is None:
        from alpha_agents.data.memory_store import _get_conn
        conn = _get_conn()

    def _rows(source: sqlite3.Connection, arm: str) -> list[dict]:
        got = source.execute(
            "SELECT id, trader_id, code, information_cutoff, policy_ref, "
            "       payload_json "
            "FROM decision_snapshots "
            "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{**dict(r), "arm": arm} for r in got]

    rows = _rows(conn, "a")
    if other_conn is not None:
        rows += _rows(other_conn, "b")

    groups: dict[tuple, list] = {}
    for row in rows:
        key = (row["trader_id"], row["information_cutoff"], row["code"])
        groups.setdefault(key, []).append(row)

    pairs: list[dict] = []
    one_sided: list[dict] = []
    unpaired = 0
    for key, group in groups.items():
        by_policy: dict[str, list] = {}
        for row in group:
            by_policy.setdefault(row["policy_ref"] or "", []).append(row)
        if len(by_policy) < 2:
            # One policy on this world state. With two books that is not
            # "no counterfactual" — it is the one-sided case, and it is
            # counted separately so a reader can tell it from a same-book
            # retry.
            if other_conn is not None and len({r["arm"] for r in group}) == 1:
                one_sided.append({
                    "trader_id": key[0], "information_cutoff": key[1],
                    "code": key[2], "arm": group[0]["arm"],
                    "policy_ref": group[0]["policy_ref"],
                    "intent": _intent_of(group[0]["payload_json"]),
                })
            else:
                unpaired += len(group)
            continue
        refs = sorted(by_policy)
        # Every **distinct policy pair**, not "incumbent vs challenger": the
        # snapshot records which policy decided, not which one is the parent,
        # and picking one by string order would be inventing the ancestry this
        # module exists to verify. Each pair is labelled by its own refs.
        for i, ref_a in enumerate(refs):
            for ref_b in refs[i + 1:]:
                a_row = by_policy[ref_a][0]
                b_row = by_policy[ref_b][0]
                a = _intent_of(a_row["payload_json"])
                b = _intent_of(b_row["payload_json"])
                pairs.append({
                    "trader_id": key[0],
                    "information_cutoff": key[1],
                    "code": key[2],
                    "policy_a": a_row["policy_ref"],
                    "policy_b": b_row["policy_ref"],
                    "arm_a": a_row["arm"],
                    "arm_b": b_row["arm"],
                    "decision_a": a_row["id"],
                    "decision_b": b_row["id"],
                    "changed": _intent_differs(a, b),
                    "intent_a": a,
                    "intent_b": b,
                })
        # Extra decisions under a policy that already contributed one row to
        # every comparison: counted, not silently dropped. Two orders under
        # one policy are not a second counterfactual.
        unpaired += len(group) - len(refs)

    changed = sum(1 for p in pairs if p["changed"])
    return {
        "pairs": len(pairs),
        "changed": changed,
        "unpaired_decisions": unpaired,
        "one_sided": one_sided[:20],
        "one_sided_count": len(one_sided),
        "differences": [p for p in pairs if p["changed"]][:10],
        "note": (
            "Pairs share trader, information_cutoff and code and differ only "
            "in policy_ref. Zero pairs means no decision has yet been taken "
            "twice under two policies, so behaviour change is undefined — "
            "not zero. Neither ref is claimed to be the incumbent: the "
            "snapshot records which policy decided, not which is the parent. "
            "one_sided lists opportunities only one arm acted on — the design "
            "requires those in the denominator, and they are the cases a "
            "lineage metric cannot see."),
    }


def summary_lines(*, limit: int = 200,
                  conn: sqlite3.Connection | None = None) -> list[str]:
    """The rates as report lines, for the daily review.

    Lineage and behaviour change are printed as two different things, because
    they are. The first line answers "did a decision run under a version a
    candidate proposed"; the second answers "did the decision actually differ"
    — and the second says "undefined" rather than "0%" while no ON/OFF pair
    exists, so a reader cannot mistake "nothing was tested" for "nothing
    changed".
    """
    r = rates(limit=limit, conn=conn)
    if r["decisions"] == 0:
        return ["• 因果链：还没有决策快照，比率无从计算（不是 0%）"]

    def _pct(v):
        return "—" if v is None else f"{v * 100:.1f}%"

    out = [
        f"• 因果链：{r['traced']}/{r['decisions']} 条决策可完整追溯到 episode"
        f"（causal_trace_rate {_pct(r['causal_trace_rate'])}）",
        f"• 血缘：{r['changed']}/{r['decisions']} 条决策跑在候选提出的版本上"
        f"（lineage_rate {_pct(r['lineage_rate'])}）——**这不等于行为改变**",
    ]
    if r["counterfactual_pairs"]:
        out.append(
            f"• 行为改变：{r['counterfactual_changed']}/{r['counterfactual_pairs']} "
            f"对同世界状态的 ON/OFF 决策产生了不同 intent"
            f"（counterfactual_change_rate {_pct(r['counterfactual_change_rate'])}）")
    else:
        out.append(
            "• 行为改变：**尚无 ON/OFF 配对**（同一个决策在不同 policy 下各跑一次），"
            "所以行为改变率是未定义而不是 0% —— 没有任何决策被反事实验证过")
    return out


def single_step_counterfactual(*, theme: str, day: str, board_row: dict,
                               params_a: dict, params_b: dict) -> dict:
    """Would a different policy have admitted this theme, on this board?

    The whole-window ON/OFF comparison cannot attribute a difference to the
    policy, and the reason is structural rather than a bug: the first model
    sample differs, so the two arms buy different names, so from the second
    decision onward their books differ and their prompts differ (measured:
    39 journal entries, **only the first** with an identical `request_hash`).
    A recording therefore cannot be shared, and the divergence is the model's
    noise, not the policy's effect.

    This is the step that *can* be attributed. Hold the world fixed — one
    board, one day, one score — and ask each policy the same question. The
    only thing that differs is the numbers in ``params_a`` / ``params_b``, so
    a difference is caused by them and nothing else.

    What it measures, stated plainly: **admission**, not trading. It answers
    "would this name have been allowed through the gate", which is the whole
    of what the variant's current single gene (`theme_gate.w_rel`) touches.
    A variant that changed position size or entry logic would need a
    different instrument, and this function says so by taking the parameters
    rather than a candidate id.

    ``board_row`` is the theme's board as of ``day``: it needs
    ``net_flow_yi``, ``change_pct`` and the frame it was ranked in (see
    ``rebuild_theme_scores.score_day``). The frame matters — a percentile of
    a pre-filtered list is not a percentile.
    """
    from alpha_agents.data.scoring import percentile as _percentile

    if not board_row or "frame_flows" not in board_row:
        return {"comparable": False,
                "reason": "board_row needs the frame it was ranked in "
                          "(frame_flows / frame_changes); a percentile over "
                          "a pre-filtered list is not a percentile"}

    def _verdict(params: dict) -> dict:
        gate = {**params}
        flow_pct = _percentile(board_row["frame_flows"],
                               board_row["net_flow_yi"])
        rel_pct = _percentile(board_row["frame_changes"],
                              board_row["change_pct"])
        confirm = float(board_row.get("confirm") or 0.0)
        score = (gate["w_flow"] * flow_pct + gate["w_rel"] * rel_pct
                 + gate["w_confirm"] * confirm)
        bar = gate["admit_score"]
        return {"score": round(score, 6), "admit_score": bar,
                "admitted": score >= bar, "flow_pct": round(flow_pct, 4),
                "rel_pct": round(rel_pct, 4), "confirm": confirm}

    a, b = _verdict(params_a), _verdict(params_b)
    return {
        "comparable": True,
        "theme": theme,
        "as_of": day,
        "a": a, "b": b,
        "changed": a["admitted"] != b["admitted"],
        # The size of the effect window, which is the honest measure of how
        # much a weight change *could* matter: only a score within this
        # distance of the bar can flip.
        "flip_margin": round(abs(b["score"] - a["score"]), 6),
        "note": (
            "Same board, same day, two parameter sets: a difference here is "
            "caused by the parameters. It measures admission, not trading."),
    }
