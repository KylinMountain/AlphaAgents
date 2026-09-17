"""Candidate → Policy Variant, the arrow the EVOLVE half was missing.

Everything from *Policy Variant* rightward already existed — ``holdout_gate``
evaluates, ``policy_registry`` freezes and promotes, ``shadow`` runs the
forward comparison — while the arrow **into** it did not. A candidate was a
row with a claim; a version was a frozen configuration; nothing turned the
first into the second.

Three properties are pinned here, and the third is the one with a debt entry
behind it:

1. a variant **inherits** its parent version and changes only what the delta
   names — starting from the code defaults would silently revert every other
   promoted parameter, which is a far larger behaviour change than the one
   proposed;
2. it **moves no pointer** — building is not promoting;
3. it **refuses empty evidence** (D25). All five production proposers write
   ``{"supporting": [], "opposing": []}``, so without this guard the first
   variant ever built would be a behaviour change justified by nothing, and
   it would be frozen into a version that later reads as evidence-backed.
"""

from __future__ import annotations

import json

import pytest

from alpha_agents.data import learning_candidates as LC
from alpha_agents.data import memory_store
from alpha_agents.data import policy_registry as PR
from alpha_agents.data import scoring
from alpha_agents.evolution import variant as V


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_store, "MEMORY_DB_PATH",
                        tmp_path / "memory.db", raising=False)
    monkeypatch.setattr(memory_store._local, "conn", None, raising=False)
    monkeypatch.setattr("alpha_agents.config.DATA_DIR", tmp_path, raising=False)
    conn = memory_store._get_conn()
    yield conn
    c = getattr(memory_store._local, "conn", None)
    if c is not None:
        c.close()
    memory_store._local.conn = None


@pytest.fixture()
def parent(store) -> int:
    """A frozen version for a variant to inherit from."""
    return PR.freeze(
        sources={
            "prompts": {"morning_scan.md": "aaa"},
            "model": {"agent_model": "qwen-plus"},
            "retrieval": {"feedback._PLAYBOOKS_BUDGET": 400},
            "rules": {"holdout_gate.MIN_VALIDATION_SAMPLES": 20},
            "knowledge": {"snapshot_id": None},
            "decision": dict(scoring.DEFAULT_DECISION_PARAMS),
        },
        created_by="kylin", reason="the seed")


def _candidate(**over) -> int:
    kwargs = dict(
        entity_type="principle", operation="create", source="unit-test",
        source_date="2026-01-05",
        # The payload carries the declared search, not just the ids. D25: the
        # citation lists say what was found, the bundle says what was
        # searched, by what rule, and what was excluded. Without it a
        # candidate is refused even when it cites ids.
        payload={
            "n": 4,
            "trades": [],
            "evidence_bundle": {
                "search_scope": "every position closed on or before 2026-01-05",
                "matching_rule": "t1_change_above_cut XOR return_above_window_median",
                "eligible": 3,
                "excluded": 0,
                "excluded_reasons": {},
                "cutoff": "2026-01-05",
            },
        },
        claim="T-1 涨得更多的候选，实际收益更差",
        applicable_context="pullback 交易员，窗口自 2025-07-01 起",
        proposed_behavior_delta={
            "field": "t1_change_rank", "direction": "down",
            "note": "选股向 T-1 涨幅更低的一端移动"},
        evidence_episode_ids={"supporting": [1, 2], "opposing": [3]},
    )
    kwargs.update(over)
    return LC.save_candidate(**kwargs)


class TestItInheritsFromItsParent:
    def test_only_the_named_parameter_moves(self, store, parent):
        cid = _candidate()
        built = V.build_variant(cid, parent_version_id=parent,
                                built_by="analyst")
        before = scoring.decision_params_of(parent)
        after = scoring.decision_params_of(built.version_id)

        assert after["theme_gate"]["w_rel"] == pytest.approx(
            before["theme_gate"]["w_rel"] - 0.05)
        # Everything else is inherited verbatim. This is the load-bearing
        # assertion: a variant built from the code defaults would revert any
        # other promoted parameter and still look like a one-field change.
        for key in before:
            if key == "theme_gate":
                continue
            assert after[key] == before[key], f"{key} was not inherited"

    def test_the_parent_is_recorded(self, store, parent):
        cid = _candidate()
        built = V.build_variant(cid, parent_version_id=parent,
                                built_by="analyst")
        version = PR.get_version(built.version_id)
        assert version["parent_id"] == parent

    def test_a_variant_without_a_parent_is_refused(self, store, parent):
        cid = _candidate()
        with pytest.raises(V.VariantError, match="inherit"):
            V.build_variant(cid, parent_version_id=4242, built_by="analyst")

    def test_proposing_the_same_change_twice_is_one_version(self, store, parent):
        """A version names behaviour, so the same behaviour is the same
        version however many times it is proposed."""
        cid = _candidate()
        first = V.build_variant(cid, parent_version_id=parent, built_by="a")
        second = V.build_variant(cid, parent_version_id=parent, built_by="a")
        assert first.version_id == second.version_id


class TestItRefusesEmptyEvidence:
    def test_both_buckets_empty_is_refused(self, store, parent):
        """D25, as a test. This is what every production proposer writes."""
        cid = _candidate(evidence_episode_ids={"supporting": [], "opposing": []})
        with pytest.raises(V.VariantError, match="D25"):
            V.build_variant(cid, parent_version_id=parent, built_by="analyst")

    def test_only_opposing_evidence_is_refused(self, store, parent):
        cid = _candidate(evidence_episode_ids={"supporting": [], "opposing": [1]})
        with pytest.raises(V.VariantError, match="only opposing"):
            V.build_variant(cid, parent_version_id=parent, built_by="analyst")

    def test_an_empty_opposing_bucket_is_allowed(self, store, parent):
        """The other direction: "searched and found no counter-example" is a
        finding, and refusing it would make the guard the permissive one."""
        cid = _candidate(evidence_episode_ids={"supporting": [1, 2], "opposing": []})
        assert V.build_variant(cid, parent_version_id=parent,
                               built_by="analyst").version_id > 0

    def test_a_missing_bucket_is_refused(self, store, parent):
        """Omitting a bucket states neither "found none" nor "looked".

        ``save_candidate`` refuses this at the write boundary, so the row is
        planted directly — this module's guard has to hold for rows that are
        already in the book, including any written before that check existed.
        """
        cid = _candidate()
        store.execute(
            "UPDATE learning_candidates SET evidence_episode_ids = ? WHERE id = ?",
            (json.dumps({"supporting": [1]}), cid))
        store.commit()
        with pytest.raises(V.VariantError, match="both citation buckets"):
            V.build_variant(cid, parent_version_id=parent, built_by="analyst")


class TestItRefusesToInventParameters:
    def test_an_unknown_field_is_refused_by_name(self, store, parent):
        cid = _candidate(proposed_behavior_delta={
            "field": "some_unmapped_field", "direction": "down"})
        with pytest.raises(V.VariantError, match="No variant mapping"):
            V.build_variant(cid, parent_version_id=parent, built_by="analyst")

    def test_a_delta_without_a_direction_is_refused(self, store, parent):
        cid = _candidate(proposed_behavior_delta={"field": "t1_change_rank"})
        with pytest.raises(V.VariantError, match="direction"):
            V.build_variant(cid, parent_version_id=parent, built_by="analyst")

    def test_a_candidate_with_no_delta_is_refused(self, store, parent):
        """``save_candidate`` refuses an empty delta, so the row is planted
        directly: this guard covers rows already in the book."""
        cid = _candidate()
        store.execute(
            "UPDATE learning_candidates SET proposed_behavior_delta = '{}' "
            "WHERE id = ?", (cid,))
        store.commit()
        with pytest.raises(V.VariantError, match="nothing to vary"):
            V.build_variant(cid, parent_version_id=parent, built_by="analyst")

    def test_a_drifted_mapping_table_is_refused(self, store, parent, monkeypatch):
        """If the parameter a mapping names is not in the version, the mapping
        and the decision parameters have drifted apart — say so rather than
        silently adding a key."""
        monkeypatch.setitem(V._SUPPORTED_DELTAS, ("t1_change_rank", "down"),
                            ("theme_gate", "no_such_param", -0.05))
        cid = _candidate()
        with pytest.raises(V.VariantError, match="drifted apart"):
            V.build_variant(cid, parent_version_id=parent, built_by="analyst")


class TestBuildingIsNotPromoting:
    def test_the_pointer_does_not_move(self, store, parent):
        """§11: building a variant writes a name for behaviour. Nothing is in
        force until a person promotes it."""
        PR.install(version_id=parent, actor="kylin",
                   reason="nothing was in force")
        before = PR.active(PR.POLICY_KEY_DEFAULT)
        cid = _candidate()
        built = V.build_variant(cid, parent_version_id=parent,
                                built_by="analyst")
        after = PR.active(PR.POLICY_KEY_DEFAULT)
        assert after["version_id"] == before["version_id"], (
            "building a variant moved the policy pointer")
        assert built.version_id != after["version_id"]

    def test_it_writes_no_approval(self, store, parent):
        cid = _candidate()
        V.build_variant(cid, parent_version_id=parent, built_by="analyst")
        assert PR.approvals_for(PR.POLICY_KEY_DEFAULT) == []


class TestTheVariantIsArchived:
    def test_a_file_a_reviewer_can_read(self, store, parent, tmp_path):
        cid = _candidate()
        built = V.build_variant(cid, parent_version_id=parent,
                                built_by="analyst")
        assert built.path is not None and built.path.exists()
        doc = json.loads(built.path.read_text(encoding="utf-8"))
        assert doc["candidate_id"] == cid
        assert doc["policy_version_id"] == built.version_id
        assert doc["parent_version_id"] == parent
        assert doc["change"]["param"] == "w_rel"
        assert "not in force" in doc["note"]

    def test_the_file_can_be_skipped(self, store, parent):
        cid = _candidate()
        built = V.build_variant(cid, parent_version_id=parent,
                                built_by="analyst", write_file=False)
        assert built.path is None

    def test_the_summary_carries_what_changed(self, store, parent):
        cid = _candidate()
        summary = V.build_variant(cid, parent_version_id=parent,
                                  built_by="analyst").summary()
        assert summary["candidate_id"] == cid
        assert summary["changes"][0]["field"] == "t1_change_rank"
        assert summary["changes"][0]["step"] == -0.05


class TestTheSearchMustBeDeclared:
    """D25's stronger half.

    ``supporting = [ids picked by hand], opposing = []`` is schema-legal and
    proves nothing about whether a counter-example search ever ran. Requiring
    the ids to be non-empty would be satisfied by exactly that shape, so the
    guard is on the *bundle*: what was searched, by what rule, and what was
    excluded.
    """

    def test_ids_without_a_bundle_are_refused(self, store, parent):
        cid = _candidate(payload={"n": 4, "trades": []})
        with pytest.raises(V.VariantError, match="declares no evidence_bundle"):
            V.build_variant(cid, parent_version_id=parent, built_by="analyst")

    def test_a_bundle_missing_its_cutoff_is_refused(self, store, parent):
        cid = _candidate(payload={
            "evidence_bundle": {"search_scope": "everything",
                                "matching_rule": "r", "eligible": 3}})
        with pytest.raises(V.VariantError, match="missing cutoff"):
            V.build_variant(cid, parent_version_id=parent, built_by="analyst")

    def test_a_complete_bundle_is_accepted(self, store, parent):
        cid = _candidate()
        assert V.build_variant(cid, parent_version_id=parent,
                               built_by="analyst").version_id > 0


class TestTheDeclaredSearchCanBeReplayed:
    """The evaluator's check, per D25: replay the declared search and confirm
    the counts, rather than trusting the label."""

    def _trades(self):
        from alpha_agents.evolution import evidence as EV
        return [EV.Trade(i, f"60000{i}", r, "2026-01-05", t1, episode_id=i)
                for i, (t1, r) in enumerate(
                    [(5.0, -10.0), (4.0, -8.0), (1.0, 6.0), (0.5, 7.0)], start=1)]

    def test_a_faithful_bundle_replays(self):
        from alpha_agents.evolution import evidence as EV
        ev = EV.analyse(self._trades())
        verdict = EV.replay_bundle(ev.bundle.as_dict(), self._trades())
        assert verdict["ok"] is True, verdict["problems"]

    def test_an_inflated_eligible_count_is_caught(self):
        from alpha_agents.evolution import evidence as EV
        ev = EV.analyse(self._trades())
        bundle = ev.bundle.as_dict()
        bundle["eligible"] = 99
        verdict = EV.replay_bundle(bundle, self._trades())
        assert verdict["ok"] is False
        assert any("eligible=99" in p for p in verdict["problems"])

    def test_an_unknown_matching_rule_is_refused(self):
        from alpha_agents.evolution import evidence as EV
        ev = EV.analyse(self._trades())
        bundle = ev.bundle.as_dict()
        bundle["matching_rule"] = "trust me"
        verdict = EV.replay_bundle(bundle, self._trades())
        assert verdict["ok"] is False
        assert any("does not know" in p for p in verdict["problems"])


class TestADeltaCannotContradictItsOwnEvidence:
    """The defect the first production observation exposed.

    `evidence.PROPOSED_DELTA` was a constant `direction: "down"`, assuming the
    proposition held. The real book came back with contrast **+7.68%** — high
    T-1 names doing *better* — so candidate #3 proposed ranking lower while
    citing evidence that ranking higher worked. A variant built from it would
    have moved `w_rel` the wrong way, with a citation that made it look
    justified.

    `proposed_delta` derives the direction from the measurement now. This
    guard covers the rows already written by the old code and any hand-written
    candidate, because the check is on the pair rather than on the writer.
    """

    def _candidate_with_contrast(self, contrast, direction):
        return _candidate(
            payload={"n": 5, "contrast_pct": contrast,
                     "evidence_bundle": {
                         "search_scope": "s", "matching_rule": "r",
                         "eligible": 5, "excluded": 0, "excluded_reasons": {},
                         "cutoff": "2026-01-05"}},
            proposed_behavior_delta={"field": "t1_change_rank",
                                     "direction": direction})

    def test_the_old_constant_is_now_refused(self, store, parent):
        """Exactly candidate #3's shape: positive contrast, direction down."""
        cid = self._candidate_with_contrast(7.68, "down")
        with pytest.raises(V.VariantError, match="opposite to the evidence"):
            V.build_variant(cid, parent_version_id=parent, built_by="k")

    def test_the_matching_direction_is_accepted(self, store, parent):
        cid = self._candidate_with_contrast(7.68, "up")
        assert V.build_variant(cid, parent_version_id=parent,
                               built_by="k").version_id > 0

    def test_a_supported_proposition_still_ranks_lower(self, store, parent):
        cid = self._candidate_with_contrast(-3.0, "down")
        assert V.build_variant(cid, parent_version_id=parent,
                               built_by="k").version_id > 0

    def test_zero_contrast_supports_no_direction(self, store, parent):
        cid = self._candidate_with_contrast(0.0, "down")
        with pytest.raises(V.VariantError, match="zero contrast"):
            V.build_variant(cid, parent_version_id=parent, built_by="k")

    def test_a_candidate_without_a_contrast_is_not_refused(self, store, parent):
        """Nothing to contradict. The bundle check covers the search; this
        guard is about the direction and says nothing when there is none."""
        cid = _candidate()          # payload has no contrast_pct
        assert V.build_variant(cid, parent_version_id=parent,
                               built_by="k").version_id > 0
