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
