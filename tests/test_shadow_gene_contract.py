"""The gene contract: a shadow run may only measure what its producer executes.

The live book ran the defect this guards before it was guarded: run #3 was
bound to a version whose only change was decision.theme_gate.w_rel, while
the candidate producer's whole path reads decision.confidence_priors. The
run opened, emitted three forecasts identical to the champion's to the last
digit, and would have filled its paired sample and reached the gate as
evidence -- lineage-valid, causally inert. These tests pin the refusal at
every door the record can be entered or consumed through.
"""

from __future__ import annotations

import pytest

from alpha_agents.evolution import holdout_gate as HG
from alpha_agents.data import policy_registry as PR
from alpha_agents.evolution import shadow as SH
from alpha_agents.evolution import policy_sources as PS
from alpha_agents.data import scoring

FROZEN_AT = "2026-05-01"
LATER_DAY = "2026-06-02"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A private database, as the other shadow suites use.

    MEMORY_DB_PATH is imported into memory_store at module scope and is not read from the environment -- patching the module is the only way, and a subprocess with the env var set writes to the real data db."""
    from alpha_agents.data import memory_store

    monkeypatch.setattr(memory_store, "MEMORY_DB_PATH",
                        tmp_path / "memory.db", raising=False)
    monkeypatch.setattr(memory_store._local, "conn", None, raising=False)
    conn = memory_store._get_conn()
    yield conn
    c = getattr(memory_store._local, "conn", None)
    if c is not None:
        c.close()
    memory_store._local.conn = None


def _sources(tag: str, decision_override: dict | None = None) -> dict:
    """A distinct configuration per tag; the decision block overridable."""
    decision = dict(scoring.DEFAULT_DECISION_PARAMS)
    if decision_override:
        decision.update(decision_override)
    return {
        "prompts": {"morning_scan.md": tag},
        "model": {"agent_model": "qwen-plus"},
        "retrieval": {"feedback._PLAYBOOKS_BUDGET": 400},
        "rules": {"holdout_gate.MIN_VALIDATION_SAMPLES": 20},
        "knowledge": {"snapshot_id": None},
        "decision": decision,
    }


def _incumbent() -> int:
    return PR.freeze(sources=_sources("incumbent"), created_by="kylin",
                     reason="the incumbent", frozen_at=FROZEN_AT)


def _theme_candidate() -> int:
    """A version whose only changed gene is one the producer never reads."""
    return PR.freeze(
        sources=_sources("theme", {"theme_gate": {
            **scoring.DEFAULT_DECISION_PARAMS["theme_gate"], "w_rel": 0.40}}),
        created_by="kylin", reason="a theme-weight candidate",
        frozen_at=FROZEN_AT)


def _priors_candidate() -> int:
    """A version whose change lives on the producer's executed surface."""
    return PR.freeze(
        sources=_sources("priors", {"confidence_priors": {
            "high": 0.62, "medium": 0.53, "low": 0.50}}),
        created_by="kylin", reason="a mapping candidate", frozen_at=FROZEN_AT)


class TestTheContractAtTheDoor:
    """open_run is the only door a run enters through, so it refuses first."""

    def test_a_theme_change_is_refused(self, store):
        _incumbent()
        version = _theme_candidate()
        with pytest.raises(SH.ShadowError) as exc:
            SH.open_run(policy_version_id=version, reason="measure it",
                        report_type="morning",
                        producer=SH.CANDIDATE_NAME, opened_at=FROZEN_AT)
        # The refusal names the gene, so the operator reads the defect rather
        # than a permission error.
        assert "decision.theme_gate.w_rel" in str(exc.value)
        assert "decision.confidence_priors" in str(exc.value)
        assert SH.runs() == []

    def test_a_change_on_the_executed_surface_opens(self, store):
        _incumbent()
        version = _priors_candidate()
        run_id = SH.open_run(policy_version_id=version, reason="measure it",
                             report_type="morning",
                             producer=SH.CANDIDATE_NAME, opened_at=FROZEN_AT)
        assert SH.runs()[0]["id"] == run_id

    def test_a_shadow_of_the_incumbent_itself_opens(self, store):
        """A shadow of the in-force version changes no gene, so the contract
        is silent: that trap is the self-comparison warning's job, not this
        one's."""
        incumbent = _incumbent()
        SH.open_run(policy_version_id=incumbent, reason="the clock",
                    report_type="morning", producer=SH.CANDIDATE_NAME,
                    opened_at=FROZEN_AT)
        assert len(SH.runs()) == 1

    def test_the_baseline_is_exempt(self, store):
        """The baseline reads nothing and supports no promotion; its empty
        surface is irrelevant by kind, so the incumbent opens for it too."""
        incumbent = _incumbent()
        SH.open_run(policy_version_id=incumbent, reason="start the clock",
                    report_type="morning", producer=SH.BASELINE_NAME,
                    opened_at=FROZEN_AT)
        assert SH.runs()[0]["producer"] == SH.BASELINE_NAME


class TestTheReferenceIsRecovered:
    """A manually frozen candidate carries no parent_id; the reference is
    read out of the record instead of guessed."""

    def test_an_explicit_parent_is_used_as_is(self, store):
        incumbent = _incumbent()
        child = PR.freeze(sources=_sources("child"), created_by="kylin",
                          reason="explicit parent", frozen_at="2026-05-20",
                          parent_id=incumbent)
        assert SH.reference_version_for(child) == incumbent

    def test_the_transition_trail_supplies_a_missing_parent(self, store):
        """An installed incumbent leaves an append-only trail row; a candidate
frozen afterwards, with no parent written, reads its reference from
that trail."""
        incumbent = _incumbent()
        PR.install(version_id=incumbent, actor="kylin", reason="first policy",
                   at="2026-05-02")
        candidate = PR.freeze(sources=_sources("candidate"),
                              created_by="kylin", reason="no parent written",
                              frozen_at="2026-06-01")
        assert SH.reference_version_for(candidate) == incumbent

    def test_without_a_trail_the_latest_earlier_version_is_used(self, store):
        """No install has ever happened, so there is no trail; the fallback
is the latest version frozen earlier -- and with three candidates
in the record that is unambiguously the middle one, not the first."""
        _incumbent()
        second = PR.freeze(sources=_sources("second"), created_by="kylin",
                           reason="second", frozen_at="2026-05-10")
        third = PR.freeze(sources=_sources("third"), created_by="kylin",
                          reason="third", frozen_at="2026-05-20")
        assert SH.reference_version_for(third) == second

    def test_a_first_version_has_no_reference(self, store):
        version = _incumbent()
        assert SH.reference_version_for(version) is None
        # And a first version with nothing behind it opens anyway: there is
        # no change to cover.
        SH.open_run(policy_version_id=version, reason="start the clock",
                    report_type="morning", producer=SH.BASELINE_NAME,
                    opened_at=FROZEN_AT)


class TestLegacyRunsAreRefusedEverywhere:
    """A run that predates the rule keeps existing; every door that could
    turn it into a verdict refuses it."""

    def _plant(self, store):
        """An incompatible run, written through the back door."""
        incumbent = _incumbent()
        PR.install(version_id=incumbent, actor="kylin", reason="first policy",
                   at="2026-05-02")
        version = _theme_candidate()
        conn = store
        SH.init_schema(conn)
        conn.execute(
            "INSERT INTO shadow_runs (policy_version_id, trader_id, "
            "producer, report_type, reason, opened_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, 'open')",
            (version, f"shadow-{version}",
             SH.CANDIDATE_NAME, "morning",
             "planted", FROZEN_AT))
        store.commit()
        return version, conn.execute(
            "SELECT id FROM shadow_runs").fetchone()["id"]

    def test_emit_refuses_the_legacy_run(self, store):
        version, run_id = self._plant(store)
        with pytest.raises(SH.ShadowError) as exc:
            SH.emit_for_date(run_id, LATER_DAY, panel=["600000"])
        assert "decision.theme_gate.w_rel" in str(exc.value)
        assert SH.predictions_for(run_id) == []

    def test_the_gate_refuses_the_legacy_run(self, store):
        version, run_id = self._plant(store)
        with pytest.raises(HG.GateError) as exc:
            HG.run_gate(version, report_type="morning")
        assert "decision.theme_gate.w_rel" in str(exc.value)
        # Refused loudly, and refused as a refusal: no verdict-shaped row.
        assert HG.get_gate_decisions(policy_version_id=version) == []

    def test_integrity_reports_the_legacy_run(self, store):
        version, run_id = self._plant(store)
        problems = SH.integrity()
        assert any(f"shadow run #{run_id}" in p and
                   "decision.theme_gate.w_rel" in p for p in problems)
    def test_a_closed_run_leaves_the_audit(self, store):
        """Closing the run is the resolution; reporting a run that can no
        longer emit, gate or open anything would only train the operator to
        ignore the audit. The run keeps its incompatibility -- it just has
        no door left to walk through."""
        version, run_id = self._plant(store)
        SH.close_run(run_id, reason="retired by the contract",
                     closed_at=LATER_DAY)
        assert all(f"shadow run #{run_id}" not in p for p in SH.integrity())


class TestTheReportIsReadable:
    """compatibility_report carries the facts; the assertion only raises."""

    def test_the_report_names_both_sides(self, store):
        _incumbent()
        version = _theme_candidate()
        got = SH.compatibility_report(version, SH.CANDIDATE_NAME)
        # Every changed leaf is reported, including the prompt fingerprint;
        # the verdict binds only the decision block.
        assert got["changed_genes"] == ["decision.theme_gate.w_rel",
                                        "prompts.morning_scan.md"]
        assert got["decision_changed_genes"] == ["decision.theme_gate.w_rel"]
        assert got["observed_genes"] == ["decision.confidence_priors"]
        assert got["uncovered_genes"] == ["decision.theme_gate.w_rel"]
        assert got["compatible"] is False
        assert got["reference_version_id"] == 1
        assert got["reference_rule"] == "transition_or_earlier"

    def test_an_explicit_parent_reports_its_rule(self, store):
        incumbent = _incumbent()
        child = PR.freeze(sources=_sources("child"), created_by="kylin",
                          reason="explicit parent", frozen_at="2026-05-20",
                          parent_id=incumbent)
        got = SH.compatibility_report(child, SH.CANDIDATE_NAME)
        assert got["reference_rule"] == "parent_id"
        assert got["compatible"] is True

    def test_the_baseline_reports_compatible_with_no_surface(self, store):
        _incumbent()
        version = _theme_candidate()
        got = SH.compatibility_report(version, SH.BASELINE_NAME)
        assert got["compatible"] is True
        assert got["uncovered_genes"] == []


class TestTheContractIsFlatLeafPaths:
    """A gene is a leaf valued by its path, and the comparison is symmetric."""

    def test_nested_blocks_flatten_to_leaves(self):
        flat = SH.flatten_sources(
            {"decision": {"theme_gate": {"w_rel": 0.4}}, "model": {"n": "x"}})
        assert flat == {"decision.theme_gate.w_rel": 0.4, "model.n": "x"}

    def test_a_dropped_leaf_counts_as_changed(self):
        base = {"decision": {"a": 1, "b": 2}}
        target = {"decision": {"a": 1}}
        assert SH.changed_genes(target, base) == {"decision.b"}

    def test_an_equal_configuration_changes_nothing(self):
        a = {"decision": {"theme_gate": {"w_rel": 0.35}}}
        b = {"decision": {"theme_gate": {"w_rel": 0.35}}}
        assert SH.changed_genes(a, b) == set()


class TestTheLiveConfigurationStillPasses:
    """The shipped sources read the live kernel, so a whole-block override is
    the only way to stage a change here; the contract must survive that
    shape."""

    def test_a_collected_candidate_whose_only_change_is_executed(self, store):
        _incumbent()
        live = PS.collect(decision_params={
            "confidence_priors": {"high": 0.66, "medium": 0.55, "low": 0.50}})
        version = PR.freeze(sources=live, created_by="kylin",
                            reason="collected, not hand-built",
                            frozen_at=FROZEN_AT)
        SH.open_run(policy_version_id=version, reason="measure it",
                    report_type="morning", opened_at=FROZEN_AT)
        assert len(SH.runs()) == 1
