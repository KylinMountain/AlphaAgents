"""Candidate → Policy Variant: the EVOLVE half of the loop.

Until this module existed there was no such thing in the repository. The
diagram the operator works from goes

    Candidate → Policy Variant → Frozen Future-Only Evaluation → …

and everything from *Policy Variant* rightward had an implementation —
``holdout_gate`` evaluates, ``policy_registry`` freezes and promotes,
``shadow`` runs the forward comparison — while the arrow **into** it did
not. A candidate was a row with a claim and a behaviour delta; a version was
a frozen configuration. Nothing turned the first into the second, so the
only way to get a variant was for a person to hand-write a ``sources`` dict
and call ``freeze``. That is what ``scripts/policy.py freeze`` does, and it
is why ``active_policy`` has never moved: the pipeline could propose and
could evaluate, but it could not *construct*.

Three things this module refuses to do, each because the alternative is a
lie the rest of the system would believe:

**It will not build a variant from empty evidence.** A candidate citing
``{"supporting": [], "opposing": []}`` can legally reach ``validated``
today, and all five production call sites write exactly that (tech-debt
D25). A variant built from one would be a behaviour change justified by
nothing, frozen into a version that later reads as evidence-backed. The
gate here is the reason D25 was deferred: the fix belongs on the
*transition*, and a transition needs something that can name the decision an
episode records. ``evolution.evidence`` now supplies that, so this module
enforces it.

**It will not invent a variant's parameters.** The delta a candidate carries
names a field and a direction; turning that into numbers is a design
decision. This module maps the deltas it actually understands and refuses
the rest by name, rather than guessing a magnitude and freezing the guess.

**It moves no pointer.** Building a variant writes a *frozen version* —
a name for behaviour — and nothing is in force until a human promotes it.
``policy_registry.freeze`` already has that property and this module does not
add to it: there is no code path here that reaches ``install``, ``approve``
or ``promote``.

The variant is a **file**, not only a row: ``freeze`` records the content
hash and the sources, and this module also writes the block to
``data/variants/<candidate_id>.json`` so a reviewer can read the proposed
configuration without a database client. §8.9 of the plan calls this the
deliverable shape — "已验证 + 归档 +（可选）存在一个变体文件".
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

#: Deltas this module knows how to turn into a parameter change.
#:
#: A mapping from the ``field`` a candidate's ``proposed_behavior_delta``
#: names to the parameter block it moves. Only what has been designed is
#: here; an unknown field is refused by name rather than guessed at, because
#: a guessed magnitude frozen into a version is indistinguishable from a
#: measured one afterwards.
#:
#: ``t1_change_rank`` / ``down`` is the one the Evidence Analyzer produces:
#: if ranking candidates by the previous session's change is backwards, the
#: selection should move toward the lower end. The parameter it moves is the
#: theme gate's ``w_rel`` — the weight on the cross-sectional relative
#: strength that the ranking reads. The step is deliberately small and
#: **declared here** rather than derived: a large first step would confound
#: "the direction is right" with "the magnitude is right", and the forward
#: window is the only thing that can separate them.
_SUPPORTED_DELTAS = {
    ("t1_change_rank", "down"): ("theme_gate", "w_rel", -0.05),
    ("t1_change_rank", "up"): ("theme_gate", "w_rel", +0.05),
}


class VariantError(ValueError):
    """A candidate cannot be turned into a policy variant, and why."""


#: The candidate → version link, owned here.
#:
#: It exists so the loop's last arrow can be walked backwards: a decision
#: names the policy version it ran under (``decision_snapshots.policy_ref``),
#: and this table says which candidate proposed that version. Without it the
#: chain from a decision back to the episode that caused it has a hole
#: exactly where the EVOLVE half sits, and ``causal_trace`` would have to
#: guess by timestamp.
#:
#: Written in the same transaction as ``policy_registry.freeze``, so a version
#: cannot exist with a variant row that disagrees about what it is.
_VARIANTS = """
CREATE TABLE IF NOT EXISTS policy_variants (
    id INTEGER PRIMARY KEY,
    candidate_id INTEGER NOT NULL,
    version_id INTEGER NOT NULL,
    parent_version_id INTEGER,
    change_json TEXT NOT NULL,
    built_by TEXT NOT NULL,
    built_at TEXT NOT NULL,
    UNIQUE(version_id)
)
"""


def _init_schema(conn: sqlite3.Connection) -> None:
    """Create this module's table on the supplied connection.

    Same posture as ``policy_registry`` and ``learning_candidates``: the table
    belongs to the module that reads it, so a reader cannot end up querying a
    table that only exists if some other module ran first.
    """
    conn.execute(_VARIANTS)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_variants_candidate "
        "ON policy_variants(candidate_id)")


@dataclass
class Variant:
    """A frozen configuration proposed by one candidate."""

    candidate_id: int
    version_id: int
    parent_version_id: int | None
    decision: dict
    changes: list[dict] = field(default_factory=list)
    path: Path | None = None

    def summary(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "version_id": self.version_id,
            "parent_version_id": self.parent_version_id,
            "changes": self.changes,
            "path": str(self.path) if self.path else None,
        }


def _assert_delta_matches_evidence(candidate: dict, delta: dict) -> None:
    """Refuse a delta that argues against the evidence it cites.

    The first production observation exposed this. ``evidence.PROPOSED_DELTA``
    was a constant — always ``direction: "down"``, on the assumption that the
    proposition held — and the real book said the opposite (contrast
    **+7.68%**). The candidate therefore proposed ranking *lower* while citing
    evidence that ranking higher did better, and a variant built from it would
    have moved the parameter the wrong way with a citation that made it look
    justified.

    ``proposed_delta`` derives the direction from the measurement now, so this
    cannot arise from that path. The guard is here because candidates already
    in the book were written by the old code, and because a hand-written
    candidate could reintroduce it: the check is on the pair, not on the
    writer.

    It compares against ``contrast_pct`` in the payload — high-T-1 median
    minus low-T-1 median, the same number ``proposed_delta`` reads. A
    candidate whose payload carries no contrast is not refused: there is
    nothing to contradict, and the bundle check already covers the search.
    """
    payload = candidate.get("payload_json") or "{}"
    try:
        parsed = json.loads(payload) if isinstance(payload, str) else dict(payload)
    except (TypeError, ValueError):
        return
    contrast = parsed.get("contrast_pct") if isinstance(parsed, dict) else None
    if contrast is None:
        return
    direction = delta.get("direction")
    expected = "down" if contrast < 0 else "up" if contrast > 0 else None
    if expected is None:
        raise VariantError(
            f"Candidate #{candidate.get('id')} has zero contrast in its "
            "evidence, so no direction is supported and none can be built.")
    if direction != expected:
        raise VariantError(
            f"Candidate #{candidate.get('id')} proposes direction "
            f"{direction!r} but its own evidence says {expected!r} "
            f"(contrast_pct={contrast:+.4f}: high-T-1 median minus low-T-1 "
            "median). A variant built from this would move the parameter "
            "opposite to the evidence it cites, and the citation would make "
            "it look justified. This is the defect the first production "
            "observation exposed.")


def _cited_episodes(candidate: dict) -> tuple[list[int], list[int]]:
    """The candidate's two citation buckets, both required to be present."""
    raw = candidate.get("evidence_episode_ids") or "{}"
    try:
        cited = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError) as e:
        raise VariantError(
            f"Candidate #{candidate.get('id')} has unreadable evidence: {e}") from e
    if not isinstance(cited, dict) or "supporting" not in cited \
            or "opposing" not in cited:
        raise VariantError(
            f"Candidate #{candidate.get('id')} does not state both citation "
            "buckets. 'we searched and found none' must be tellable from "
            "'nobody looked', and a candidate that omits a bucket states "
            "neither.")
    return list(cited["supporting"] or []), list(cited["opposing"] or [])


def assert_evidence_is_not_empty(candidate: dict) -> tuple[list[int], list[int]]:
    """Refuse a candidate whose evidence cites nothing, or declares no search.

    An empty ``supporting`` bucket means the proposal rests on no episode. It
    is schema-legal, it is what every production proposer wrote before
    ``evolution.evidence`` existed, and a variant built from it would be a
    behaviour change with nothing behind it that later reads as
    evidence-backed.

    An empty *opposing* bucket is **not** refused: "we searched the declared
    set and found no counter-example" is a finding, and it is the whole point
    of requiring the bucket to be stated.

    D25's stronger half is the bundle check. ``supporting = [ids picked by
    hand], opposing = []`` is schema-legal and proves nothing about whether a
    counter-example search ever ran, so a candidate whose payload carries no
    ``evidence_bundle`` is refused **even when it cites ids**. Without that,
    this guard would be satisfied by the very shape it exists to catch.
    """
    supporting, opposing = _cited_episodes(candidate)
    if not supporting and not opposing:
        raise VariantError(
            f"Candidate #{candidate.get('id')} cites no episodes at all, so a "
            "variant built from it would be a behaviour change justified by "
            "nothing. This is tech-debt D25: the guard belongs here, on the "
            "transition, rather than on save_candidate — an empty bucket is a "
            "legitimate observation, and only *building from it* is not.")
    if not supporting:
        raise VariantError(
            f"Candidate #{candidate.get('id')} cites only opposing evidence. "
            "A proposal whose evidence is only the cases against it is not a "
            "hypothesis either.")
    _assert_bundle_declared(candidate)
    return supporting, opposing


def _assert_bundle_declared(candidate: dict) -> dict:
    """Require a replayable declaration of the search (D25).

    The citation lists say *what was found*. The bundle says *what was
    searched, by what rule, and what was excluded* — which is the difference
    between evidence and a selection of agreeable examples.
    """
    payload = candidate.get("payload_json") or "{}"
    try:
        parsed = json.loads(payload) if isinstance(payload, str) else dict(payload)
    except (TypeError, ValueError) as e:
        raise VariantError(
            f"Candidate #{candidate.get('id')} has an unreadable payload: {e}") from e
    bundle = parsed.get("evidence_bundle") if isinstance(parsed, dict) else None
    if not isinstance(bundle, dict):
        raise VariantError(
            f"Candidate #{candidate.get('id')} cites episodes but declares no "
            "evidence_bundle, so nothing says what was searched or what was "
            "left out — 'supporting = [ids chosen by hand], opposing = []' is "
            "schema-legal and proves nothing. D25 requires a declared, "
            "replayable search (evolution.evidence.EvidenceBundle).")
    required = ("search_scope", "matching_rule", "eligible", "cutoff")
    missing = [k for k in required if not bundle.get(k) and bundle.get(k) != 0]
    if missing:
        raise VariantError(
            f"Candidate #{candidate.get('id')} has an evidence_bundle missing "
            f"{', '.join(missing)}; a bundle that does not state these cannot "
            "be replayed, so it is a label rather than a search.")
    return bundle


def _delta_to_change(delta: dict) -> dict:
    """One ``proposed_behavior_delta`` as a concrete parameter change."""
    if not isinstance(delta, dict):
        raise VariantError(
            f"proposed_behavior_delta must be an object, got {type(delta).__name__}")
    field_name = delta.get("field")
    direction = delta.get("direction")
    if not field_name or not direction:
        raise VariantError(
            "proposed_behavior_delta must name a 'field' and a 'direction'; "
            f"got {delta!r}. A delta that does not say what it moves cannot "
            "be built into a variant.")
    key = (field_name, direction)
    if key not in _SUPPORTED_DELTAS:
        raise VariantError(
            f"No variant mapping for field={field_name!r} "
            f"direction={direction!r}. Known: "
            f"{sorted(f'{f}/{d}' for f, d in _SUPPORTED_DELTAS)}. Inventing a "
            "magnitude here would freeze a guess that later reads as a "
            "measurement, so this refuses instead.")
    block, param, step = _SUPPORTED_DELTAS[key]
    return {"field": field_name, "direction": direction, "block": block,
            "param": param, "step": step,
            "note": delta.get("note", "")}


def _apply(decision: dict, change: dict) -> dict:
    """The decision block with one change applied. Returns a new dict."""
    out = json.loads(json.dumps(decision))          # deep copy, JSON-safe
    block = out.setdefault(change["block"], {})
    if change["param"] not in block:
        raise VariantError(
            f"Parameter {change['block']}.{change['param']} is not in the "
            "version being varied, so this delta has nothing to move. The "
            "mapping table and the decision parameters have drifted apart.")
    block[change["param"]] = round(block[change["param"]] + change["step"], 6)
    return out


def build_variant(candidate_id: int, *, parent_version_id: int,
                  built_by: str, reason: str | None = None,
                  write_file: bool = True) -> Variant:
    """Turn a candidate into a frozen policy variant. Moves no pointer.

    ``parent_version_id`` is the version the candidate was distilled against:
    the variant inherits everything from it and changes only what the delta
    names. Inheriting is the point — a variant that started from the code
    defaults would silently revert every other parameter that had been
    promoted since, which is a much larger behaviour change than the one
    being proposed.

    Returns the :class:`Variant`; raises :class:`VariantError` with the
    reason when the candidate cannot be built into one.
    """
    from alpha_agents.data import learning_candidates as LC
    from alpha_agents.data import policy_registry as PR
    from alpha_agents.data import scoring

    candidate = LC.get_candidate(candidate_id)
    if candidate is None:
        raise VariantError(f"No learning candidate #{candidate_id}")

    parent = PR.get_version(parent_version_id)
    if parent is None:
        raise VariantError(
            f"No policy version #{parent_version_id} to inherit from. A "
            "variant has to name the version it varies, or 'what changed' "
            "has no answer.")

    assert_evidence_is_not_empty(candidate)

    raw_delta = candidate.get("proposed_behavior_delta")
    try:
        delta = json.loads(raw_delta) if isinstance(raw_delta, str) else dict(raw_delta)
    except (TypeError, ValueError) as e:
        raise VariantError(
            f"Candidate #{candidate_id} has an unreadable behaviour delta: {e}") from e
    if not delta:
        raise VariantError(
            f"Candidate #{candidate_id} proposes no behaviour change, so "
            "there is nothing to vary. An observation with no delta is a "
            "note, not a candidate for a variant.")

    _assert_delta_matches_evidence(candidate, delta)
    change = _delta_to_change(delta)
    decision = _apply(scoring.decision_params_of(parent_version_id), change)

    sources = dict(PR.sources_of(parent_version_id) or {})
    if not sources:
        raise VariantError(
            f"Policy version #{parent_version_id} has no sources on record, "
            "so a variant cannot inherit from it.")
    sources[PR.SOURCE_DECISION] = decision

    # `freeze` is idempotent per content: proposing the same change twice
    # returns the same version rather than a second one. That is the correct
    # reading — a version names behaviour, and the same behaviour is the same
    # version however many times it is proposed.
    version_id = PR.freeze(
        sources=sources,
        created_by=built_by,
        reason=reason or (
            f"variant of version #{parent_version_id} proposed by candidate "
            f"#{candidate_id}: {change['block']}.{change['param']} "
            f"{change['step']:+.3f}"),
        parent_id=parent_version_id,
    )
    _record_link(candidate_id=candidate_id, version_id=version_id,
                 parent_version_id=parent_version_id, change=change,
                 built_by=built_by)

    path = None
    if write_file:
        path = _write_variant_file(candidate_id, version_id, parent_version_id,
                                   decision, change)
    return Variant(candidate_id=candidate_id, version_id=version_id,
                   parent_version_id=parent_version_id, decision=decision,
                   changes=[change], path=path)


def _record_link(*, candidate_id: int, version_id: int,
                 parent_version_id: int, change: dict, built_by: str) -> None:
    """Write the candidate → version link that makes the last arrow walkable.

    Idempotent per version: ``freeze`` returns an existing version for the
    same content, so re-proposing the same change must not write a second row
    — ``UNIQUE(version_id)`` enforces that and the insert ignores a repeat.

    It does not raise on a duplicate: the link already exists and says the
    same thing, and failing here would turn a legitimate re-proposal into an
    error after the version was already frozen.
    """
    from alpha_agents.data import memory_store
    from alpha_agents.data import clock

    with memory_store._write_lock:
        conn = memory_store._get_conn()
        with conn:
            _init_schema(conn)
            conn.execute(
                "INSERT INTO policy_variants "
                "(candidate_id, version_id, parent_version_id, change_json, "
                " built_by, built_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(version_id) DO NOTHING",
                (candidate_id, version_id, parent_version_id,
                 json.dumps(change, ensure_ascii=False, sort_keys=True),
                 built_by, clock.today()))


def _variant_dir() -> Path:
    from alpha_agents.config import DATA_DIR
    return DATA_DIR / "variants"


def _write_variant_file(candidate_id: int, version_id: int,
                        parent_version_id: int, decision: dict,
                        change: dict) -> Path:
    """Archive the proposed configuration where a reviewer can read it.

    §8.9's deliverable shape: "已验证 + 归档 +（可选）存在一个变体文件".
    Written beside the database rather than into it because its reader is a
    person with an editor, not a query.
    """
    directory = _variant_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"candidate-{candidate_id}.json"
    path.write_text(json.dumps({
        "candidate_id": candidate_id,
        "policy_version_id": version_id,
        "parent_version_id": parent_version_id,
        "change": change,
        "decision": decision,
        "note": ("This variant is frozen, not in force. Promotion is a "
                 "separate, audited human act (policy_registry.promote)."),
    }, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path
