"""The audited entry point: freeze, install, and the refusals between them.

``scripts/policy.py`` is the only thing in the repo a person runs to change
which policy is in force (Phase 4 / §14). Until this slice it had four verbs —
status, approve, promote, rollback — and **none of them can create the first
row**: approve and promote both cite evidence about a version that must already
exist. So the registry had no way to get its first version, the production
database has no ``policy_versions`` table at all, and the whole controlled
mechanism was unreachable by an operator. That is the "step 0" the Phase 4
plan named and never did.

These tests drive the real ``main(argv)`` rather than the command functions, so
the argument wiring is under test as well as the effects — and the refusals are
tested as refusals, because a back door that skips the evidence rules is worse
than a missing verb.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from alpha_agents.data import memory_store, policy_registry as registry
from alpha_agents.data import scoring
from alpha_agents.evolution import holdout_gate, shadow


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A private registry database.

    ``MEMORY_DB_PATH`` is imported into ``memory_store`` at module scope, so
    patching the module is the only way to keep the run off the real
    ``data/memory.db`` — and the CLI prints the path it is about to touch,
    which is exactly the fact an operator needs before moving a policy.
    """
    monkeypatch.setattr(memory_store, "MEMORY_DB_PATH",
                        tmp_path / "memory.db", raising=False)
    monkeypatch.setattr(memory_store._local, "conn", None, raising=False)
    yield
    conn = getattr(memory_store._local, "conn", None)
    if conn is not None:
        conn.close()
    memory_store._local.conn = None


@pytest.fixture()
def cli():
    """The script, loaded by path — ``scripts/`` is not an importable package.

    The same six-line bootstrap ``test_policy_promotion``'s CLI tests use; that
    file's fixture also stubs ``_sources``, and these commands need the real
    one, so the two cannot share a fixture even though they share the loader.
    """
    path = Path(__file__).resolve().parents[1] / "scripts" / "policy.py"
    spec = importlib.util.spec_from_file_location("policy_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _freeze(cli, *extra) -> int:
    return cli.main(["freeze", "--by", "kylin", "--reason", "the seed",
                     *extra])


def _install(cli, *extra, version="1") -> int:
    return cli.main(["install", "--version", version, "--by", "kylin",
                     "--reason", "nothing was in force", *extra])


def _champion(store, date: str, code: str, *,
              report_type: str = "morning") -> int:
    """A champion pick, written through the real save path.

    There is deliberately no CLI verb for this: the champion's picks come from
    the trading pipeline, not from an operator. The challenger's panel is
    whatever those picks left in ``predictions``, so a test that wants a panel
    has to produce one the way the day does.
    """
    return memory_store.save_prediction(
        date, report_type, code, "X", "看多", "high", "t", 1.0, "reason",
        prob=0.6, trader_id="default", horizon_days=5)


class TestTheFirstVersionCanBeCreated:
    def test_freeze_writes_a_version(self, cli, store, capsys):
        assert _freeze(cli) == 0
        out = capsys.readouterr().out
        assert "version #1 written." in out
        versions = registry.versions_for()
        assert len(versions) == 1
        assert versions[0]["created_by"] == "kylin"
        assert versions[0]["reason"] == "the seed"

    def test_the_same_configuration_is_one_version(self, cli, store, capsys):
        """Freezing twice is naming the same behaviour twice, not a new
        version — which is what makes "promote version 7" a stable
        instruction."""
        _freeze(cli)
        capsys.readouterr()
        assert _freeze(cli) == 0
        assert "already on record" in capsys.readouterr().out
        assert len(registry.versions_for()) == 1

    def test_freeze_moves_no_pointer(self, cli, store, capsys):
        _freeze(cli)
        assert registry.active() is None
        assert "the pointer did not move" in capsys.readouterr().out

    def test_it_says_which_configuration_it_read(self, cli, store, capsys):
        """The hash is the whole content of the record, so it is printed."""
        _freeze(cli)
        version = registry.versions_for()[0]
        assert version["content_hash"][:16] in capsys.readouterr().out

    def test_the_date_can_be_fixed(self, cli, store):
        """``frozen_at`` is the start of the forward window, so a replay has
        to be able to set it rather than take the kernel clock."""
        _freeze(cli, "--at", "2026-09-13")
        assert registry.versions_for()[0]["frozen_at"] == "2026-09-13"

    def test_a_dry_run_writes_nothing(self, cli, store, capsys):
        assert _freeze(cli, "--dry-run") == 0
        assert registry.versions_for() == []
        assert "not on record" in capsys.readouterr().out

    def test_a_reason_is_required(self, cli, store):
        with pytest.raises(SystemExit):
            cli.main(["freeze", "--by", "kylin"])


class TestTheFirstVersionIsInstalled:
    def test_install_sets_the_pointer(self, cli, store, capsys):
        _freeze(cli)
        assert _install(cli) == 0
        pointer = registry.active()
        assert pointer["version_id"] == 1
        assert pointer["version_seq"] == 1
        assert "seq 1" in capsys.readouterr().out

    def test_install_is_recorded_as_a_transition(self, cli, store):
        _freeze(cli)
        _install(cli)
        steps = registry.transitions_for()
        assert [s["kind"] for s in steps] == ["install"]
        assert steps[0]["from_version_id"] is None
        assert steps[0]["to_version_id"] == 1

    def test_a_second_install_is_refused(self, cli, store, capsys):
        """The back door, as a refusal. From the second change onward the
        pointer may only move by a promotion or a rollback, both of which
        demand evidence — an install that could be repeated would be neither.
        """
        _freeze(cli)
        _install(cli)
        capsys.readouterr()
        assert _install(cli) == 1
        assert "promotion or a rollback" in capsys.readouterr().err
        assert registry.active()["version_seq"] == 1, "nothing moved"

    def test_installing_a_version_that_does_not_exist_is_refused(
            self, cli, store, capsys):
        assert _install(cli, version="7") == 1
        assert "No policy version #7" in capsys.readouterr().err
        assert registry.active() is None

    def test_a_dry_run_writes_nothing(self, cli, store, capsys):
        _freeze(cli)
        capsys.readouterr()
        assert _install(cli, "--dry-run") == 0
        out = capsys.readouterr().out
        assert "would put version #1" in out
        assert registry.active() is None

    def test_a_dry_run_reports_an_existing_pointer(self, cli, store, capsys):
        _freeze(cli)
        _install(cli)
        capsys.readouterr()
        assert _install(cli, "--dry-run") == 0
        assert "refused" in capsys.readouterr().out
        assert registry.active()["version_seq"] == 1


class TestStatusLooksBeforeYouLeap:
    def test_nothing_in_force_is_stated_not_implied(self, cli, store, capsys):
        assert cli.main(["status"]) == 0
        out = capsys.readouterr().out
        assert "nothing in force" in out
        assert "0 version(s) on record" in out

    def test_after_install_it_names_what_is_in_force(self, cli, store, capsys):
        _freeze(cli)
        _install(cli)
        capsys.readouterr()
        assert cli.main(["status"]) == 0
        out = capsys.readouterr().out
        assert "in force: version #1 at seq 1" in out
        assert "an install — no approval needed" in out

    def test_it_prints_the_database_it_would_move(self, cli, store, capsys):
        """A person about to move the active policy should see which database
        they are moving it in — this path does not read TMPDIR."""
        cli.main(["status"])
        assert "policy target: " in capsys.readouterr().out

    def test_it_prints_the_decision_parameters_in_force(self, cli, store,
                                                        capsys):
        """The values, not only the hash.

        Once a version is in force the decision parameters belong to the
        version, and the code defaults stop deciding anything — so an edit to
        those defaults has no effect, and it is invisible unless the mapping in
        force can be read back. Without this line the difference between
        "edited and inert" and "edited and working" is unobservable.
        """
        assert _freeze(cli, "--decision-json", '{"dim_step": 0.09}') == 0
        _install(cli)
        capsys.readouterr()

        assert cli.main(["status"]) == 0
        out = capsys.readouterr().out
        assert '"dim_step": 0.09' in out
        assert '"dim_base": 0.44' in out, "the whole block is in force, not one key"


class TestACandidateDeclaresItsOwnParameters:
    """``--decision-json``: how a version that differs from the incumbent exists.

    The decision parameters are pointer-controlled, so they are not on disk to
    edit. A freeze with no staging therefore records exactly what is in force,
    and a candidate can only be written down by asserting what it changes.
    """

    def test_a_candidate_records_the_block_it_asserts(self, cli, store, capsys):
        _freeze(cli)
        capsys.readouterr()
        assert _freeze(cli, "--decision-json", '{"dim_step": 0.09}') == 0
        versions = registry.versions_for()
        assert len(versions) == 2, "a different block is a different version"
        block = registry.sources_of(versions[1]["id"])["decision"]
        assert block["dim_step"] == 0.09
        assert block["dim_base"] == scoring.DEFAULT_DECISION_PARAMS["dim_base"]
        assert "this version asserts" in capsys.readouterr().out

    def test_the_asserted_block_changes_the_mapping_once_in_force(
            self, cli, store):
        """The whole point of the flag: a version that behaves differently."""
        bolder = ('{"confidence_priors": {"high": 0.72, "medium": 0.60, '
                  '"low": 0.45}}')
        _freeze(cli)
        _freeze(cli, "--decision-json", bolder)
        assert scoring.confidence_to_prob("high") == pytest.approx(0.58)

        _install(cli, version="2")
        assert scoring.confidence_to_prob("high") == pytest.approx(0.72)

    def test_malformed_json_is_refused_and_writes_nothing(self, cli, store,
                                                         capsys):
        assert _freeze(cli, "--decision-json", "{oops") == 1
        assert "not valid JSON" in capsys.readouterr().err
        assert registry.versions_for() == []

    def test_a_non_object_is_refused(self, cli, store, capsys):
        assert _freeze(cli, "--decision-json", "[1, 2]") == 1
        assert "must be a JSON object" in capsys.readouterr().err
        assert registry.versions_for() == []


class TestTheDriftCheckStagesTheVersionUnderTest:
    """``_sources`` reads a version's own pointer-controlled values.

    Reading the ones in force instead would make the target hash to something
    other than itself, so a promotion could never move the pointer to a version
    that differs — and a rollback could never restore one.
    """

    def test_a_version_that_is_not_in_force_still_hashes_to_itself(
            self, cli, store):
        incumbent = cli.policy_sources.freeze_live(created_by="kylin",
                                                   reason="the incumbent")
        cli.registry.install(version_id=incumbent, actor="kylin",
                             reason="first policy")
        candidate = cli.policy_sources.freeze_live(
            created_by="kylin", reason="a candidate",
            decision_params={"dim_step": 0.09})

        staged = cli._sources(candidate)
        assert (cli.registry.content_hash_for(staged)
                == cli.registry.get_version(candidate)["content_hash"])
        assert cli.policy_sources.verify_live(candidate) is True
        assert cli.policy_sources.drifted() == []

    def test_an_unknown_version_is_refused(self, cli, store):
        with pytest.raises(ValueError, match="No policy version"):
            cli._sources(4242)


class TestTheExperimentIsDrivable:
    """The operator surface for the loop: open it, feed it, grade it, ask.

    These verbs are what "the mechanism is runnable" means from outside the
    code. Before them ``emit_for_date`` and ``run_gate`` had no caller but the
    tests, so the only way to run an experiment was to write Python — which is
    also why nobody ever ran one.
    """

    FROZEN_AT = "2026-05-01"
    DAY = "2026-06-02"

    def _seed(self, cli, store, capsys):
        """An incumbent in force, a candidate frozen well in the past."""
        assert _freeze(cli, "--at", self.FROZEN_AT) == 0
        assert _install(cli) == 0
        assert _freeze(cli, "--at", self.FROZEN_AT,
                       "--decision-json", '{"dim_step": 0.09}') == 0
        capsys.readouterr()
        # Read the id back rather than assume it: two freezes of *different*
        # configurations are two versions, and a test that hardcodes "2" would
        # keep passing if they silently became one.
        return registry.versions_for()[-1]["id"]

    def _open(self, cli, version: int, *extra) -> int:
        # The report type is stated rather than defaulted. The default moved
        # from `morning` to `intraday` on 2026-09-17 because `morning` has
        # produced nothing since 2026-09-10 and `intraday_signal` carries no
        # prob, so neither can ever yield a paired sample. These tests
        # exercise the CLI's mechanics on the `morning` book, so they say so
        # rather than inherit a default that is a judgement about which book
        # is worth measuring.
        return cli.main(["shadow-open", "--version", str(version),
                         "--producer", "remap_confidence",
                         "--report-type", "morning",
                         "--reason", "measure the candidate", *extra])

    def test_opening_a_run_names_what_it_measures(self, cli, store, capsys):
        version = self._seed(cli, store, capsys)
        assert self._open(cli, version) == 0
        out = capsys.readouterr().out
        assert "shadow run #1 opened" in out
        assert f"policy version #{version}" in out
        assert "remap_confidence" in out
        assert shadow.runs()[0]["producer"] == "remap_confidence"

    def test_opening_a_run_on_the_incumbent_warns(self, cli, store, capsys):
        """The trap, as a warning: a shadow of the version in force applies the
        same parameters as the champion, so it compares a policy with itself."""
        self._seed(cli, store, capsys)
        assert self._open(cli, 1, "--dry-run") == 0
        assert "compares a policy with itself" in capsys.readouterr().out

    def test_a_baseline_on_the_incumbent_says_why_it_is_not_that_trap(
            self, cli, store, capsys):
        """The warning is about a producer that *reads* the version's
        parameters. A baseline emits one constant and reads none of them, so
        the same setup measures the champion against no skill — which is the
        only way to start the clock before deciding what to test. A warning
        that fires here is how the one that matters gets scrolled past."""
        self._seed(cli, store, capsys)
        capsys.readouterr()
        assert cli.main(["shadow-open", "--version", "1",
                         "--producer", "constant_0.5", "--reason", "r",
                         "--dry-run"]) == 0
        out = capsys.readouterr().out
        assert "WARNING" not in out
        assert "baseline producer" in out
        assert "cannot move the pointer" in out

    def test_a_dry_run_writes_nothing(self, cli, store, capsys):
        version = self._seed(cli, store, capsys)
        assert self._open(cli, version, "--dry-run") == 0
        assert shadow.runs() == []

    def test_an_unregistered_producer_is_refused(self, cli, store, capsys):
        version = self._seed(cli, store, capsys)
        assert cli.main(["shadow-open", "--version", str(version),
                         "--producer", "nobody_emits_this",
                         "--reason", "r"]) == 1
        assert "No registered producer" in capsys.readouterr().err
        assert shadow.runs() == []

    def test_emitting_uses_the_champions_panel(self, cli, store, capsys):
        version = self._seed(cli, store, capsys)
        self._open(cli, version)
        _champion(store, self.DAY, "600000")
        capsys.readouterr()

        assert cli.main(["shadow-emit", "--run", "1", "--date", self.DAY]) == 0
        assert "wrote 1 forecast(s)" in capsys.readouterr().out
        rows = shadow.predictions_for(1)
        assert [r["code"] for r in rows] == ["600000"]
        # The version's own mapping, not the one in force.
        assert rows[0]["prob"] == pytest.approx(
            scoring.DEFAULT_DECISION_PARAMS["confidence_priors"]["high"])

    def test_emitting_before_the_champion_says_so(self, cli, store, capsys):
        """An empty panel is reported, not passed off as a quiet day."""
        version = self._seed(cli, store, capsys)
        self._open(cli, version)
        capsys.readouterr()

        assert cli.main(["shadow-emit", "--run", "1", "--date", self.DAY]) == 0
        out = capsys.readouterr().out
        assert "wrote 0 forecast(s)" in out
        assert "panel was empty" in out

    def test_the_panel_can_be_previewed(self, cli, store, capsys):
        version = self._seed(cli, store, capsys)
        self._open(cli, version)
        _champion(store, self.DAY, "600000")
        capsys.readouterr()

        assert cli.main(["shadow-emit", "--run", "1", "--date", self.DAY,
                         "--dry-run"]) == 0
        assert "600000" in capsys.readouterr().out
        assert shadow.predictions_for(1) == []

    def test_scoring_keeps_the_three_outcomes_apart(self, cli, store, capsys):
        """Same split as the champion's labels: nothing is ``unscorable``
        merely because the window is open."""
        version = self._seed(cli, store, capsys)
        self._open(cli, version)
        _champion(store, self.DAY, "600000")
        cli.main(["shadow-emit", "--run", "1", "--date", self.DAY])
        capsys.readouterr()

        assert cli.main(["shadow-score", "--at", "2026-06-30"]) == 0
        out = capsys.readouterr().out
        assert "0 graded, 1 window still open, 0 unscorable" in out

    def test_a_verdict_is_recorded_and_moves_nothing(self, cli, store, capsys):
        version = self._seed(cli, store, capsys)
        self._open(cli, version)
        capsys.readouterr()

        assert cli.main(["gate", "--version", str(version)]) == 0
        out = capsys.readouterr().out
        assert "verdict recorded: insufficient" in out
        assert "the pointer did not move" in out
        row = holdout_gate.get_gate_decisions(policy_version_id=version)[0]
        assert row["outcome"] == "insufficient"
        assert registry.active()["version_id"] == 1, "a verdict is not a move"

    def test_asking_on_the_freeze_day_is_refused_not_recorded(
            self, cli, store, capsys):
        """A window that could never hold evidence must not leave a
        verdict-shaped row behind."""
        _freeze(cli)                      # frozen on the kernel clock: today
        _install(cli)
        assert _freeze(cli, "--decision-json", '{"dim_step": 0.09}') == 0
        version = registry.versions_for()[-1]["id"]
        self._open(cli, version)
        capsys.readouterr()
        assert cli.main(["gate", "--version", str(version)]) == 1
        assert "Validation is forward" in capsys.readouterr().err
        assert holdout_gate.get_gate_decisions() == []

    def test_gate_has_no_dry_run_argument(self, cli, store):
        """Refused at parse time: there is no way to preview the answer without
        re-deciding eligibility outside the gate."""
        with pytest.raises(SystemExit):
            cli.main(["gate", "--version", "1", "--dry-run"])

    def test_status_carries_the_progress_meter(self, cli, store, capsys):
        version = self._seed(cli, store, capsys)
        self._open(cli, version)
        capsys.readouterr()
        assert cli.main(["status"]) == 0
        out = capsys.readouterr().out
        assert "shadow run #1" in out
        assert f"0/{holdout_gate.MIN_VALIDATION_SAMPLES} paired sample(s)" in out

    def test_the_progress_meter_counts_samples_not_days(self, cli, store,
                                                       capsys):
        """The gate's floor counts paired ``(date, code)`` samples. This command
        printed that number as "paired day(s)" and said "N to go", so one
        morning of twenty picks read as twenty days of evidence — and the
        number an operator reads to decide whether asking for a verdict is
        worth the trip was the wrong one to be wrong about.

        Two champions on one day, so the two counts are the same only by
        accident and are labelled separately anyway.
        """
        version = self._seed(cli, store, capsys)
        self._open(cli, version)
        _champion(store, self.DAY, "600000")
        _champion(store, self.DAY, "600001")
        assert cli.main(["shadow-emit", "--run", "1", "--date", self.DAY]) == 0
        capsys.readouterr()

        assert cli.main(["status"]) == 0
        out = capsys.readouterr().out
        assert "paired day(s)" not in out
        assert (f"0/{holdout_gate.MIN_VALIDATION_SAMPLES} paired sample(s) over "
                "0 scored day(s), 0 of 2 forecast(s) scored") in out

    def test_status_names_the_promotion_floor_and_reports_no_conflict(
            self, cli, store, capsys):
        """The floor a promotion is re-checked against belongs to the *version*,
        and the repository's rule is stated in samples beside it.

        They agree today — D15 was closed on 2026-09-15 by lowering the rule to
        20 — so the assertion that matters is the *absence* of the complaint.
        Before that they disagreed and this command printed "nothing refuses it";
        a report that printed the rule only when it was violated would make
        "they agree" and "the rule was never read" the same output, so the rule
        line is asserted positive and the complaint negative.
        """
        self._seed(cli, store, capsys)
        capsys.readouterr()
        assert cli.main(["status"]) == 0
        out = capsys.readouterr().out
        assert (f"version #1 declares {holdout_gate.MIN_VALIDATION_SAMPLES} "
                "paired sample(s)") in out
        assert f"gate abstains below {holdout_gate.MIN_VALIDATION_SAMPLES}" in out
        assert f"repository rule: n >= {holdout_gate.GOVERNANCE_MIN_SAMPLES}" in out
        assert "nothing refuses it" not in out

    def test_a_version_at_the_governance_floor_reports_no_gap(
            self, cli, store, capsys, monkeypatch):
        """The other branch of the same comparison, so the complaint above is
        not a sentence this command always prints.
        """
        monkeypatch.setattr(holdout_gate, "MIN_VALIDATION_SAMPLES",
                            holdout_gate.GOVERNANCE_MIN_SAMPLES)
        _freeze(cli)
        _install(cli)
        capsys.readouterr()
        assert cli.main(["status"]) == 0
        out = capsys.readouterr().out
        assert (f"declares {holdout_gate.GOVERNANCE_MIN_SAMPLES} paired "
                "sample(s)") in out
        assert "nothing refuses it" not in out

    def test_status_says_when_nothing_is_under_experiment(self, cli, store,
                                                         capsys):
        assert cli.main(["status"]) == 0
        assert "nothing is under experiment" in capsys.readouterr().out
