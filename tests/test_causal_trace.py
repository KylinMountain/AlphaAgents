"""The diagram's last arrow: can a decision be traced back to its episode?

Every other test in this area asks whether a mechanism ran. This one asks the
only question that makes the loop a loop, and it is careful about what the
answer means.

The chain that exists in the book:

    episode → candidate → variant(version) → decision → outcome

The chain that does **not**: a decision records which policy version it ran
under, not which rule text it read. The rendered knowledge block is not
persisted per decision, so a trace can say "this decision ran under the
version a candidate proposed" and cannot say "this decision quoted sentence
three of that candidate". These tests pin the boundary as well as the links,
because a trace that overclaims is worse than one that names its limit.
"""

from __future__ import annotations

import json

import pytest

from alpha_agents.data import learning_candidates as LC
from alpha_agents.data import memory_store
from alpha_agents.data import policy_registry as PR
from alpha_agents.data import scoring
from alpha_agents.evolution import causal_trace as CT
from alpha_agents.evolution import variant as V


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_store, "MEMORY_DB_PATH",
                        tmp_path / "memory.db", raising=False)
    monkeypatch.setattr(memory_store._local, "conn", None, raising=False)
    conn = memory_store._get_conn()
    yield conn
    c = getattr(memory_store._local, "conn", None)
    if c is not None:
        c.close()
    memory_store._local.conn = None


def _sources(tag="a"):
    return {
        "prompts": {"m": tag}, "model": {"agent_model": "q"},
        "retrieval": {"feedback._LESSONS_BUDGET": 600},
        "rules": {"holdout_gate.MIN_VALIDATION_SAMPLES": 20},
        "knowledge": {"snapshot_id": None},
        "decision": dict(scoring.DEFAULT_DECISION_PARAMS),
    }


def _candidate(episodes=(1, 2)):
    return LC.save_candidate(
        entity_type="principle", operation="create", source="unit-test",
        source_date="2026-01-05",
        payload={"n": 4, "evidence_bundle": {
            "search_scope": "everything", "matching_rule": "r",
            "eligible": 2, "excluded": 0, "excluded_reasons": {},
            "cutoff": "2026-01-05"}},
        claim="c", applicable_context="ctx",
        proposed_behavior_delta={"field": "t1_change_rank", "direction": "down"},
        evidence_episode_ids={"supporting": list(episodes), "opposing": []})


def _decision(store, *, policy_ref, code="600000"):
    store.execute(
        "INSERT INTO decision_snapshots (trader_id, code, information_cutoff, "
        " decided_at, payload_json, policy_ref, content_hash) "
        "VALUES ('default', ?, '2026-01-05', '2026-01-05', '{}', ?, 'h')",
        (code, policy_ref))
    store.commit()
    return store.execute("SELECT MAX(id) m FROM decision_snapshots").fetchone()["m"]


def _promote_eligible(store, version_id: int, *, scope=PR.SCOPE_CANDIDATE):
    """A gate verdict a promotion may cite, and a person's approval.

    §11 needs both and the approval cites the verdict. Building them here
    rather than bypassing them is the point: a test that promoted without
    evidence would be exercising a promotion path that does not exist.
    """
    from alpha_agents.evolution import holdout_gate as HG

    HG.record_gate_decision(f"trader#{version_id}", {
        "promote": True, "abstained": False, "n": 25,
        "policy_version_id": version_id, "validation_days": 5,
        "outcome": "promote", "evidence_scope": scope,
        "reason": "forward window passed",
    })
    decision = [d for d in HG.get_gate_decisions()
                if d["policy_version_id"] == version_id][0]
    PR.approve(version_id=version_id, approved_by="kylin",
               reason="beat the champion", gate_decision=decision,
               sources=PR.sources_of(version_id))


class TestTheChainIsWalkedBackwards:
    def test_a_promoted_variant_traces_to_its_episodes(self, store):
        parent = PR.freeze(sources=_sources("p"), created_by="k", reason="seed")
        cid = _candidate(episodes=(7, 8))
        built = V.build_variant(cid, parent_version_id=parent, built_by="k",
                                write_file=False)
        PR.install(version_id=parent, actor="k", reason="first")
        _promote_eligible(store, built.version_id)
        PR.promote(version_id=built.version_id, actor="k", reason="go",
                   sources=PR.sources_of(built.version_id))
        ref = PR.active_ref()
        _decision(store, policy_ref=ref)

        chains = CT.trace_decisions()
        assert len(chains) == 1
        chain = chains[0]
        assert chain.version_id == built.version_id
        assert chain.candidate_id == cid
        assert chain.episode_ids == [7, 8]
        assert chain.complete is True
        assert chain.broken_at is None

    def test_a_decision_under_a_hand_frozen_version_is_not_traced(self, store):
        """The honest zero. A version nobody proposed has no candidate, and
        the chain stops there rather than inventing one by timestamp."""
        parent = PR.freeze(sources=_sources("p"), created_by="k", reason="seed")
        PR.install(version_id=parent, actor="k", reason="first")
        _decision(store, policy_ref=PR.active_ref())
        chain = CT.trace_decisions()[0]
        assert chain.candidate_id is None
        assert chain.complete is False
        assert chain.broken_at == "version→candidate"

    def test_a_decision_with_no_policy_ref_is_broken_at_the_first_link(
            self, store):
        _decision(store, policy_ref=None)
        chain = CT.trace_decisions()[0]
        assert chain.broken_at == "policy_ref"
        assert chain.version_id is None

    def test_a_candidate_with_no_episodes_breaks_the_last_link(self, store):
        """Schema-legal, and the shape all five production proposers wrote
        before ``evolution.evidence`` existed.

        The row is planted directly because ``build_variant`` now refuses
        such a candidate (D25) — which is the point of that guard, and it
        means this branch is only reachable for candidates already in the
        book. The trace still has to report it honestly rather than treating
        an empty citation list as a complete chain.
        """
        parent = PR.freeze(sources=_sources("p"), created_by="k", reason="seed")
        cid = _candidate(episodes=(1, 2))
        built = V.build_variant(cid, parent_version_id=parent, built_by="k",
                                write_file=False)
        # Blank the citations after the fact, as a pre-D25 row would be.
        store.execute(
            "UPDATE learning_candidates SET evidence_episode_ids = ? "
            "WHERE id = ?",
            (json.dumps({"supporting": [], "opposing": []}), cid))
        store.commit()
        PR.install(version_id=parent, actor="k", reason="first")
        _promote_eligible(store, built.version_id)
        PR.promote(version_id=built.version_id, actor="k", reason="go",
                   sources=PR.sources_of(built.version_id))
        _decision(store, policy_ref=PR.active_ref())
        chain = CT.trace_decisions()[0]
        assert chain.broken_at == "candidate→episode"
        assert chain.episode_ids == []


class TestTheRatesCarryTheirDenominator:
    def test_no_decisions_is_undefined_not_zero_percent(self, store):
        """A rate over zero decisions is not 0%. Printing 0% would read as
        'the loop is failing' when the truth is 'nothing has happened yet'."""
        r = CT.rates()
        assert r["decisions"] == 0
        assert r["causal_trace_rate"] is None
        assert r["decision_change_rate"] is None
        assert "无从计算" in CT.summary_lines()[0]

    def test_the_rates_are_fractions_of_the_decisions(self, store):
        parent = PR.freeze(sources=_sources("p"), created_by="k", reason="seed")
        cid = _candidate()
        built = V.build_variant(cid, parent_version_id=parent, built_by="k",
                                write_file=False)
        PR.install(version_id=parent, actor="k", reason="first")
        _decision(store, policy_ref=PR.active_ref())        # untraced
        _promote_eligible(store, built.version_id)
        PR.promote(version_id=built.version_id, actor="k", reason="go",
                   sources=PR.sources_of(built.version_id))
        _decision(store, policy_ref=PR.active_ref())        # traced
        r = CT.rates()
        assert r["decisions"] == 2
        assert r["traced"] == 1
        assert r["causal_trace_rate"] == pytest.approx(0.5)
        assert r["decision_change_rate"] == pytest.approx(0.5)

    def test_it_names_what_traced_does_not_mean(self, store):
        """The boundary, in the payload. A reader must not read 'traced' as
        'the decision quoted the candidate'."""
        assert "not which rule text" in CT.rates()["note"]


class TestTheLinkIsRecordedOnce:
    def test_re_proposing_the_same_change_does_not_duplicate(self, store):
        parent = PR.freeze(sources=_sources("p"), created_by="k", reason="seed")
        cid = _candidate()
        first = V.build_variant(cid, parent_version_id=parent, built_by="k",
                                write_file=False)
        second = V.build_variant(cid, parent_version_id=parent, built_by="k",
                                 write_file=False)
        assert first.version_id == second.version_id
        rows = store.execute(
            "SELECT COUNT(*) n FROM policy_variants WHERE version_id = ?",
            (first.version_id,)).fetchone()
        assert rows["n"] == 1

    def test_the_link_records_what_changed(self, store):
        parent = PR.freeze(sources=_sources("p"), created_by="k", reason="seed")
        cid = _candidate()
        built = V.build_variant(cid, parent_version_id=parent, built_by="k",
                                write_file=False)
        row = store.execute(
            "SELECT * FROM policy_variants WHERE version_id = ?",
            (built.version_id,)).fetchone()
        assert row["candidate_id"] == cid
        assert row["parent_version_id"] == parent
        change = json.loads(row["change_json"])
        assert change["param"] == "w_rel"
