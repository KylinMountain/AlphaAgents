"""U4 — promotion and rollback, through one audited entry point.

§14 ends Phase 4 with "evidence controls future policy changes through one
audited entry point". The dangerous failure here is not a crash; it is a
promotion service that looks like governance and has no rule in it. So the
tests are mostly about **refusals**, and about the fact that a refusal leaves
the database exactly as it was.

The tests are in nine groups:

1. One writer. A machine-checkable claim: exactly one statement in the package
   writes ``active_policy``, and it is in one function.
2. Approving is not promoting. §11 does not let an evaluation promote, so the
   approval is a separate record made by a separate act — and it moves nothing.
3. Promotion needs all four conditions at once: an incumbent, a recorded
   human approval, a live configuration that still hashes to the version, and
   a sound record.
4. The move is a compare-and-swap. Two promotions prepared against the same
   sequence: one writes, the other changes nothing.
5. The swap primitive itself, exercised directly — the ``WHERE`` is what makes
   it atomic, and the service-level tests cannot reach it because they are
   refused one layer earlier.
6. Refusals change nothing. All four refusal classes — rejected, insufficient,
   evaluation missing, unapproved — leave the pointer and every frozen content
   hash byte-identical.
7. Rollback restores and deletes nothing. Only the pointer moves; fills,
   ledger and outcomes are untouched.
8. Integrity notices a pointer nobody authorised.
9. The operator CLI, because an ``approve()`` nobody can call is a promise
   rather than a tool.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

from alpha_agents.data import memory_store, policy_registry as PR

FROZEN_1 = "2026-01-01"
FROZEN_2 = "2026-01-02"
FROZEN_3 = "2026-01-03"


def _sources(tag: str) -> dict:
    """A syntactically valid source set, distinguishable by ``tag``."""
    return {
        "prompts": {"morning_scan.md": tag},
        "model": {"agent_model": "qwen-plus"},
        "retrieval": {"feedback._PLAYBOOKS_BUDGET": 400},
        "rules": {"holdout_gate.MIN_VALIDATION_SAMPLES": 20},
        "knowledge": {"snapshot_id": None},
    }


def _verdict(version_id: int, *, outcome="promote", abstained=0, days=7,
             decision_id=1) -> dict:
    return {"id": decision_id, "policy_version_id": version_id,
            "outcome": outcome, "abstained": abstained, "validation_days": days,
            "n": 25, "reason": f"{outcome} over {days} day(s)"}


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A private database.

    ``MEMORY_DB_PATH`` is imported into ``memory_store`` at module scope and is
    not read from the environment — patching the module is the only way, and a
    subprocess with the env var set writes to the real ``data/memory.db``.
    """
    monkeypatch.setattr(memory_store, "MEMORY_DB_PATH",
                        tmp_path / "memory.db", raising=False)
    monkeypatch.setattr(memory_store._local, "conn", None, raising=False)
    conn = memory_store._get_conn()
    yield conn
    c = getattr(memory_store._local, "conn", None)
    if c is not None:
        c.close()
    memory_store._local.conn = None


def _snapshot(conn) -> dict:
    """Every user table's rows — the assertion that nothing moved."""
    out = {}
    for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"):
        if name.startswith("sqlite_"):
            continue
        rows = conn.execute(f'SELECT * FROM "{name}"').fetchall()
        out[name] = sorted(tuple(row) for row in rows)
    return out


def _nonempty(conn) -> dict:
    """Tables holding at least one row.

    ``CREATE TABLE IF NOT EXISTS`` runs at every entry point, so a refusal
    legitimately materialises empty containers. The claim is that no *record*
    was made.
    """
    return {name: rows for name, rows in _snapshot(conn).items() if rows}


def _hashes(conn) -> dict:
    """The frozen content hashes — what a promotion must not disturb."""
    return {row["id"]: row["content_hash"] for row in
            conn.execute("SELECT id, content_hash FROM policy_versions")}


@pytest.fixture()
def frozen(store) -> dict:
    """Three versions, the first in force, each with a distinguishable source."""
    ids = {}
    for tag, when in (("v1", FROZEN_1), ("v2", FROZEN_2), ("v3", FROZEN_3)):
        ids[tag] = PR.freeze(sources=_sources(tag), created_by="kylin",
                             reason=f"frozen {tag}", frozen_at=when)
    PR.install(version_id=ids["v1"], actor="kylin", reason="first policy")
    ids["_sources"] = {tag: _sources(tag) for tag in ("v1", "v2", "v3")}
    return ids


def _approve(ids, tag, **over):
    kwargs = dict(version_id=ids[tag], approved_by="kylin",
                  reason="beat the baseline", gate_decision=_verdict(ids[tag]),
                  sources=ids["_sources"][tag], at=FROZEN_3)
    kwargs.update(over)
    return PR.approve(**kwargs)


def _promote(ids, tag, **over):
    kwargs = dict(version_id=ids[tag], actor="kylin", reason="go",
                  sources=ids["_sources"][tag], at=FROZEN_3)
    kwargs.update(over)
    return PR.promote(**kwargs)


# ── 1. One writer ──────────────────────────────────────────────────────


class TestThePointerHasExactlyOneWriter:
    """Acceptance 8: a grep-checkable claim, not a convention."""

    PACKAGE = Path(__file__).resolve().parent.parent / "alpha_agents"
    WRITE = re.compile(r"\b(?:INSERT\s+INTO|UPDATE)\s+active_policy\b", re.I)

    def _write_sites(self) -> list[tuple[Path, str]]:
        sites = []
        for path in sorted(self.PACKAGE.rglob("*.py")):
            text = path.read_text()
            for match in self.WRITE.finditer(text):
                enclosing = None
                for definition in re.finditer(r"^def (\w+)", text[:match.start()],
                                              re.M):
                    enclosing = definition.group(1)
                sites.append((path, enclosing))
        return sites

    def test_only_one_statement_writes_the_pointer(self):
        sites = self._write_sites()
        assert len(sites) == 1, [
            f"{path.name}:{fn}" for path, fn in sites]

    def test_the_writer_is_the_named_function(self):
        path, enclosing = self._write_sites()[0]
        assert path.name == "policy_registry.py"
        assert enclosing == "_write_pointer"

    def test_no_module_outside_the_registry_touches_the_table(self):
        names = {path.name for path, _ in self._write_sites()}
        assert names == {"policy_registry.py"}


# ── 2. Approving is not promoting ──────────────────────────────────────


class TestApprovingIsNotPromoting:
    def test_approving_writes_one_table_and_moves_no_pointer(self, store, frozen):
        before = _nonempty(store)
        pointer_before = PR.active()
        _approve(frozen, "v2")
        after = _nonempty(store)
        assert set(after) - set(before) == {"policy_approvals"}
        assert PR.active() == pointer_before

    def test_an_approval_records_who_and_why(self, store, frozen):
        approval_id = _approve(frozen, "v2")
        row = PR.approvals_for()[0]
        assert row["id"] == approval_id
        assert row["approved_by"] == "kylin"
        assert row["version_id"] == frozen["v2"]
        assert row["gate_decision_id"] == 1
        assert row["content_hash"] == PR.get_version(frozen["v2"])["content_hash"]

    def test_approvals_are_append_only(self, store, frozen):
        _approve(frozen, "v2")
        with pytest.raises(sqlite3.IntegrityError):
            store.execute("UPDATE policy_approvals SET approved_by = 'nobody'")
        with pytest.raises(sqlite3.IntegrityError):
            store.execute("DELETE FROM policy_approvals")

    def test_two_people_approving_are_two_records(self, store, frozen):
        """An approval is an act, not a state — deduping would lose who."""
        _approve(frozen, "v2", approved_by="kylin")
        _approve(frozen, "v2", approved_by="someone-else")
        assert [r["approved_by"] for r in PR.approvals_for()] == [
            "kylin", "someone-else"]

    def test_the_newest_approval_is_the_one_found(self, store, frozen):
        _approve(frozen, "v2", approved_by="first")
        _approve(frozen, "v2", approved_by="second")
        assert PR.approval_for(frozen["v2"])["approved_by"] == "second"

    def test_an_approval_for_a_different_version_is_not_returned(
            self, store, frozen):
        _approve(frozen, "v2")
        assert PR.approval_for(frozen["v3"]) is None


class TestAnApprovalRequiresAVerdictThatSaysPromote:
    """Every refusal is structural, checked in the registry, not the CLI."""

    def _refused(self, store, frozen, verdict, **over):
        before = _nonempty(store)
        with pytest.raises(PR.PolicyError) as exc:
            _approve(frozen, "v2", gate_decision=verdict, **over)
        assert _nonempty(store) == before
        return str(exc.value)

    def test_a_rejection_is_refused(self, store, frozen):
        message = self._refused(store, frozen,
                                _verdict(frozen["v2"], outcome="reject"))
        assert "not 'promote'" in message

    def test_an_abstention_is_refused(self, store, frozen):
        message = self._refused(
            store, frozen,
            _verdict(frozen["v2"], outcome="insufficient", abstained=1, days=0))
        assert "abstained" in message or "not 'promote'" in message

    def test_zero_days_of_evidence_is_refused(self, store, frozen):
        message = self._refused(store, frozen,
                                _verdict(frozen["v2"], days=0))
        assert "zero days" in message

    def test_a_verdict_about_another_version_is_refused(self, store, frozen):
        message = self._refused(store, frozen, _verdict(frozen["v3"]))
        assert "not #" in message or "Evidence for one policy" in message

    def test_no_verdict_at_all_is_refused(self, store, frozen):
        message = self._refused(store, frozen, None)
        assert "must cite a gate verdict" in message

    def test_a_drifted_configuration_is_refused(self, store, frozen):
        message = self._refused(store, frozen, _verdict(frozen["v2"]),
                                sources=_sources("someone-edited-a-prompt"))
        assert "no longer describes the live configuration" in message

    def test_missing_sources_are_refused_rather_than_skipped(self, store, frozen):
        """No drift check would make the hash claim decorative."""
        message = self._refused(store, frozen, _verdict(frozen["v2"]),
                                sources=None)
        assert "needs the live configuration" in message


# ── 3. Promotion needs everything at once ──────────────────────────────


class TestPromotionRequiresEveryCondition:
    def test_an_approved_clean_version_is_promoted(self, store, frozen):
        _approve(frozen, "v2")
        seq = _promote(frozen, "v2")
        assert seq == 2
        assert PR.active()["version_id"] == frozen["v2"]
        assert PR.active()["version_seq"] == 2

    def test_the_transition_cites_the_approval_and_the_verdict(
            self, store, frozen):
        approval_id = _approve(frozen, "v2")
        _promote(frozen, "v2")
        step = PR.transitions_for()[-1]
        assert step["kind"] == "promote"
        assert step["from_version_id"] == frozen["v1"]
        assert step["to_version_id"] == frozen["v2"]
        assert step["version_seq"] == 2
        evidence = json.loads(step["evidence_json"])
        assert evidence["approval_id"] == approval_id
        assert evidence["gate_decision_id"] == 1
        assert evidence["content_hash"] == PR.get_version(frozen["v2"])["content_hash"]

    def test_unapproved_is_refused(self, store, frozen):
        before = _nonempty(store)
        with pytest.raises(PR.PolicyError) as exc:
            _promote(frozen, "v2")
        assert "has not been approved by a person" in str(exc.value)
        assert _nonempty(store) == before

    def test_drifted_after_approval_is_refused(self, store, frozen):
        """An approval given for a configuration that then changed is stale."""
        _approve(frozen, "v2")
        before = _nonempty(store)
        with pytest.raises(PR.PolicyError) as exc:
            _promote(frozen, "v2",
                     sources=_sources("drifted-after-the-approval"))
        assert "no longer describes the live configuration" in str(exc.value)
        assert _nonempty(store) == before

    def test_promoting_the_version_already_in_force_is_refused(
            self, store, frozen):
        _approve(frozen, "v1")
        with pytest.raises(PR.PolicyError) as exc:
            _promote(frozen, "v1")
        assert "already in force" in str(exc.value)

    def test_promoting_with_nothing_in_force_is_refused(self, store):
        """There is no incumbent, so there is nothing to improve on."""
        version = PR.freeze(sources=_sources("only"), created_by="kylin",
                            reason="only", frozen_at=FROZEN_1)
        PR.approve(version_id=version, approved_by="kylin", reason="r",
                   gate_decision=_verdict(version), sources=_sources("only"))
        with pytest.raises(PR.PolicyError) as exc:
            PR.promote(version_id=version, actor="kylin", reason="r",
                       sources=_sources("only"))
        assert "nothing to promote past" in str(exc.value)

    def test_a_version_that_does_not_exist_is_refused(self, store, frozen):
        with pytest.raises(PR.PolicyError) as exc:
            _promote(frozen, "v2", version_id=4242)
        assert "No policy version #4242" in str(exc.value)


# ── 4. The move is a compare-and-swap ──────────────────────────────────


class TestTheSwapIsAtomic:
    def test_the_loser_of_a_race_changes_nothing(self, store, frozen):
        """Two promotions prepared against the same sequence.

        Both read ``version_seq`` before either writes — the shape two
        operators, or an operator and a scheduled job, actually take.
        """
        _approve(frozen, "v2")
        _approve(frozen, "v3")
        expected = PR.active()["version_seq"]

        _promote(frozen, "v2", expected_seq=expected)
        after_winner = _nonempty(store)

        with pytest.raises(PR.PolicyError) as exc:
            _promote(frozen, "v3", expected_seq=expected)
        assert "moved since this promotion was prepared" in str(exc.value)
        assert _nonempty(store) == after_winner
        assert PR.active()["version_id"] == frozen["v2"]

    def test_a_stale_expectation_is_refused_without_writing(self, store, frozen):
        _approve(frozen, "v2")
        before = _nonempty(store)
        with pytest.raises(PR.PolicyError) as exc:
            _promote(frozen, "v2", expected_seq=99)
        assert "not 99" in str(exc.value)
        assert _nonempty(store) == before

    def test_the_sequence_only_ever_rises(self, store, frozen):
        _approve(frozen, "v2")
        _approve(frozen, "v3")
        _promote(frozen, "v2")
        _promote(frozen, "v3")
        seqs = [step["version_seq"] for step in PR.transitions_for()]
        assert seqs == [1, 2, 3]


# ── 5. The swap primitive ──────────────────────────────────────────────


class TestTheSwapPrimitive:
    """The ``WHERE`` clause *is* the atomicity, not a check before it.

    A mutation probe that replaced the ``WHERE`` with a tautology stayed
    green, and that is worth recording rather than hiding: ``promote`` also
    compares the sequence in Python before it calls, so a stale expectation is
    caught twice and the outer check fires first. The statement's own refusal
    is therefore unpinned by the service-level tests, and a guard that lives
    only in a caller can be reordered away. These tests exercise the
    primitive directly for that reason.
    """

    def _pointer(self, store):
        return store.execute("SELECT * FROM active_policy").fetchone()

    def test_a_stale_expected_seq_changes_no_rows(self, store, frozen):
        changed = PR._write_pointer(
            store, policy_key="trader", version_id=frozen["v3"],
            version_seq=2, actor="x", reason="x", at=FROZEN_3, expected_seq=99)
        assert changed == 0
        assert self._pointer(store)["version_id"] == frozen["v1"]

    def test_the_matching_expected_seq_lands(self, store, frozen):
        changed = PR._write_pointer(
            store, policy_key="trader", version_id=frozen["v2"],
            version_seq=2, actor="x", reason="x", at=FROZEN_3, expected_seq=1)
        assert changed == 1
        assert self._pointer(store)["version_id"] == frozen["v2"]

    def test_expected_seq_zero_never_matches_a_real_pointer(self, store, frozen):
        """Which is what makes "install refuses when one exists" the same code
        path as the swap, rather than a separate check that could drift."""
        changed = PR._write_pointer(
            store, policy_key="trader", version_id=frozen["v2"],
            version_seq=1, actor="x", reason="x", at=FROZEN_3, expected_seq=0)
        assert changed == 0
        assert self._pointer(store)["version_id"] == frozen["v1"]


# ── 6. Refusals change nothing ─────────────────────────────────────────


class TestRefusalsChangeNothing:
    """Acceptance 5: the pointer and every frozen hash, byte-identical.

    Four refusal classes, because "the promotion did not happen" is a weaker
    claim than "the database is exactly as it was". A refusal that quietly
    bumped a sequence or rewrote a hash would be worse than a crash: the
    record would still look valid.
    """

    def _assert_untouched(self, store, frozen, **over):
        before = _nonempty(store)
        hashes = _hashes(store)
        with pytest.raises(PR.PolicyError):
            _promote(frozen, "v2", **over)
        assert _nonempty(store) == before
        assert _hashes(store) == hashes
        assert PR.active()["version_id"] == frozen["v1"]
        assert PR.active()["version_seq"] == 1

    def test_rejected_verdict(self, store, frozen):
        with pytest.raises(PR.PolicyError):
            _approve(frozen, "v2",
                     gate_decision=_verdict(frozen["v2"], outcome="reject"))
        self._assert_untouched(store, frozen)

    def test_insufficient_verdict(self, store, frozen):
        with pytest.raises(PR.PolicyError):
            _approve(frozen, "v2", gate_decision=_verdict(
                frozen["v2"], outcome="insufficient", abstained=1, days=0))
        self._assert_untouched(store, frozen)

    def test_evaluation_never_ran(self, store, frozen):
        """No verdict on record at all — the D7 state, as a refusal."""
        self._assert_untouched(store, frozen)

    def test_approved_but_not_promoted(self, store, frozen):
        """Approval is not promotion: the pointer must still be untouched."""
        _approve(frozen, "v2")
        assert PR.active()["version_id"] == frozen["v1"]
        assert PR.active()["version_seq"] == 1

    def test_a_drifted_promotion_leaves_the_hashes_alone(self, store, frozen):
        _approve(frozen, "v2")
        hashes = _hashes(store)
        with pytest.raises(PR.PolicyError):
            _promote(frozen, "v2", sources=_sources("drift"))
        assert _hashes(store) == hashes


# ── 7. Rollback restores and deletes nothing ───────────────────────────


class TestRollbackRestoresWithoutDeleting:
    def _promote_to_v2(self, store, frozen):
        _approve(frozen, "v2")
        _promote(frozen, "v2")
        return frozen["_sources"]["v1"]

    def test_rollback_moves_the_pointer_back(self, store, frozen):
        sources_v1 = self._promote_to_v2(store, frozen)
        seq = PR.rollback(to_version_id=frozen["v1"], actor="kylin",
                          reason="drawdown breach", sources=sources_v1,
                          at=FROZEN_3)
        assert seq == 3
        assert PR.active()["version_id"] == frozen["v1"]

    def test_rollback_deletes_nothing(self, store, frozen):
        """Only the pointer and the trail move; everything else is identical."""
        sources_v1 = self._promote_to_v2(store, frozen)
        before = _snapshot(store)
        PR.rollback(to_version_id=frozen["v1"], actor="kylin",
                    reason="drawdown breach", sources=sources_v1, at=FROZEN_3)
        after = _snapshot(store)

        changed = {name for name in after
                   if before.get(name) != after[name]}
        assert changed == {"active_policy", "policy_transitions"}
        for name in after:
            if name not in changed:
                assert len(after[name]) == len(before[name]), name

    def test_the_rollback_leaves_a_trail_entry(self, store, frozen):
        sources_v1 = self._promote_to_v2(store, frozen)
        PR.rollback(to_version_id=frozen["v1"], actor="kylin",
                    reason="drawdown breach", sources=sources_v1, at=FROZEN_3)
        step = PR.transitions_for()[-1]
        assert step["kind"] == "rollback"
        assert step["from_version_id"] == frozen["v2"]
        assert step["to_version_id"] == frozen["v1"]
        assert json.loads(step["evidence_json"])["restored_from_seq"] == 2

    def test_rolling_back_to_a_version_that_never_ran_is_refused(
            self, store, frozen):
        """That is a promotion, and a promotion needs forward evidence."""
        with pytest.raises(PR.PolicyError) as exc:
            PR.rollback(to_version_id=frozen["v3"], actor="kylin", reason="r",
                        sources=frozen["_sources"]["v3"])
        assert "was never in force" in str(exc.value)

    def test_rolling_back_to_the_current_version_is_refused(self, store, frozen):
        with pytest.raises(PR.PolicyError) as exc:
            PR.rollback(to_version_id=frozen["v1"], actor="kylin", reason="r",
                        sources=frozen["_sources"]["v1"])
        assert "already in force" in str(exc.value)

    def test_a_drifted_rollback_target_is_refused(self, store, frozen):
        """Rolling the pointer back does not restore the files it names.

        A pointer claiming version 1 is in force while version 1's prompts are
        no longer on disk would be a lie, so the drift check applies here too.
        """
        self._promote_to_v2(store, frozen)
        before = _nonempty(store)
        with pytest.raises(PR.PolicyError) as exc:
            PR.rollback(to_version_id=frozen["v1"], actor="kylin", reason="r",
                        sources=_sources("drifted"))
        assert "no longer describes the live configuration" in str(exc.value)
        assert _nonempty(store) == before

    def test_rollback_then_promote_again_works(self, store, frozen):
        """The door is not one-way: the earlier approval is still good."""
        sources_v1 = self._promote_to_v2(store, frozen)
        PR.rollback(to_version_id=frozen["v1"], actor="kylin", reason="r",
                    sources=sources_v1, at=FROZEN_3)
        seq = _promote(frozen, "v2")
        assert seq == 4
        assert PR.active()["version_id"] == frozen["v2"]


# ── 8. Integrity notices an unauthorised pointer ───────────────────────


class TestIntegrityNoticesAnUnauthorisedPointer:
    def _corrupt_trail(self, store, version_id: int) -> None:
        """Insert a transition at a sequence that leaves a hole.

        Insert, not update: ``policy_transitions`` is append-only, so the only
        way to make the trail unsound through the schema is to append a step
        that skips ahead. That the UPDATE route is blocked is itself asserted
        in ``TestApprovingIsNotPromoting`` — this helper works with the
        constraint rather than around it.
        """
        store.execute(
            "INSERT INTO policy_transitions "
            "(policy_key, from_version_id, to_version_id, version_seq, kind, "
            " actor, reason, evidence_json, at) "
            "VALUES ('trader', NULL, ?, 7, 'install', 'x', 'x', '{}', ?)",
            (version_id, FROZEN_3))
        store.commit()

    def test_a_clean_record_is_silent(self, store, frozen):
        assert PR.integrity() == []

    def test_a_pointer_no_install_put_in_force_is_reported(self, store, frozen):
        """The failure mode the whole slice exists to make visible."""
        store.execute(
            "UPDATE active_policy SET version_id = ?, version_seq = 2",
            (frozen["v3"],))
        store.commit()
        problems = PR.integrity()
        assert any("nobody authorised" in p for p in problems), problems

    def test_a_pointer_at_a_missing_version_is_reported(self, store, frozen):
        store.execute("UPDATE active_policy SET version_id = 9999")
        store.commit()
        problems = PR.integrity()
        assert any("does not exist" in p for p in problems), problems

    def test_a_gap_in_the_trail_is_reported(self, store, frozen):
        _approve(frozen, "v2")
        _promote(frozen, "v2")
        self._corrupt_trail(store, frozen["v3"])
        problems = PR.integrity()
        assert any("gap in its transition trail" in p for p in problems), problems

    def test_promotion_refuses_while_the_record_is_unsound(self, store, frozen):
        """A move must not land on top of a hole in the audit trail."""
        _approve(frozen, "v2")
        self._corrupt_trail(store, frozen["v3"])
        before = _nonempty(store)
        with pytest.raises(PR.PolicyError) as exc:
            _promote(frozen, "v2")
        assert "not sound" in str(exc.value)
        assert PR.active()["version_id"] == frozen["v1"]
        assert _nonempty(store) == before


# ── 8. The operator CLI is the entry point ─────────────────────────────


def _load_cli():
    """Import ``scripts/policy.py`` by path — ``scripts`` is not a package."""
    import importlib.util
    path = (Path(__file__).resolve().parent.parent / "scripts" / "policy.py")
    spec = importlib.util.spec_from_file_location("policy_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestTheOperatorCLI:
    """T4's lesson: an ``approve()`` nobody can call is a promise, not a tool."""

    @pytest.fixture()
    def cli(self, store, frozen, monkeypatch):
        module = _load_cli()
        # The collector is the seam the registry deliberately does not cross:
        # it may not import the layer that reads live configuration, so the
        # CLI passes it in. Patched here to the source sets the versions were
        # frozen from, so the drift check is exercised rather than bypassed.
        # Tests that move the live configuration set ``cli.LIVE`` first.
        # ``_load_cli`` builds a fresh module per test, so this does not leak.
        module.LIVE = {"sources": frozen["_sources"]["v2"]}
        monkeypatch.setattr(module.policy_sources, "collect",
                            lambda **kw: module.LIVE["sources"])
        return module

    def test_status_changes_nothing(self, cli, store, frozen, capsys):
        before = _nonempty(store)
        assert cli.main(["status"]) == 0
        assert _nonempty(store) == before
        out = capsys.readouterr().out
        assert "nothing" not in out.lower() or "in force" in out
        assert f"#{frozen['v1']}" in out

    def test_status_prints_the_database_it_would_write_to(self, cli, capsys):
        cli.main(["status"])
        assert "policy target:" in capsys.readouterr().out

    def test_approve_without_a_verdict_is_refused_and_writes_nothing(
            self, cli, store, frozen, capsys):
        before = _nonempty(store)
        assert cli.main(["approve", "--version", str(frozen["v2"]),
                         "--by", "kylin", "--reason", "r"]) == 1
        assert _nonempty(store) == before
        assert "refused" in capsys.readouterr().err

    def test_approve_then_promote_moves_the_pointer(self, cli, store, frozen):
        from alpha_agents.evolution import holdout_gate
        # A verdict has to exist for the CLI to cite one.
        holdout_gate.record_gate_decision(f"trader#{frozen['v2']}", {
            "promote": True, "abstained": False, "outcome": "promote",
            "policy_version_id": frozen["v2"], "validation_days": 9, "n": 30,
            "reason": "beat the baseline",
        }, today=FROZEN_3)

        assert cli.main(["approve", "--version", str(frozen["v2"]),
                         "--by", "kylin", "--reason", "looks better"]) == 0
        assert PR.active()["version_id"] == frozen["v1"], "approval moved it"

        assert cli.main(["promote", "--version", str(frozen["v2"]),
                         "--by", "kylin", "--reason", "go"]) == 0
        assert PR.active()["version_id"] == frozen["v2"]
        assert PR.active()["version_seq"] == 2

    def test_promote_dry_run_writes_nothing(self, cli, store, frozen):
        before = _nonempty(store)
        assert cli.main(["promote", "--version", str(frozen["v2"]),
                         "--by", "kylin", "--reason", "r", "--dry-run"]) == 0
        assert _nonempty(store) == before

    def test_rollback_restores_and_reports_nothing_deleted(self, cli, store,
                                                           frozen, capsys):
        _approve(frozen, "v2")
        _promote(frozen, "v2")
        # The live configuration must match the version being restored, so
        # rolling back to v1 means the live config is v1's.
        cli.LIVE["sources"] = frozen["_sources"]["v1"]
        assert cli.main(["rollback", "--to-version", str(frozen["v1"]),
                         "--by", "kylin", "--reason", "breach"]) == 0
        assert PR.active()["version_id"] == frozen["v1"]
        assert "Nothing was deleted" in capsys.readouterr().out

    def test_a_rollback_to_a_drifted_version_is_refused(self, cli, store,
                                                        frozen, capsys):
        """The pointer cannot restore files, so it must not pretend to."""
        _approve(frozen, "v2")
        _promote(frozen, "v2")
        before = _nonempty(store)
        assert cli.main(["rollback", "--to-version", str(frozen["v1"]),
                         "--by", "kylin", "--reason", "breach"]) == 1
        assert "no longer describes" in capsys.readouterr().err
        assert _nonempty(store) == before
        assert PR.active()["version_id"] == frozen["v2"]

    def test_a_refusal_exits_nonzero_with_the_reason(self, cli, store, frozen,
                                                     capsys):
        assert cli.main(["promote", "--version", str(frozen["v2"]),
                         "--by", "kylin", "--reason", "r"]) == 1
        assert "has not been approved" in capsys.readouterr().err

    def test_status_reports_integrity_problems(self, cli, store, frozen,
                                               capsys):
        store.execute("UPDATE active_policy SET version_id = 9999")
        store.commit()
        assert cli.main(["status"]) == 0
        assert "does not exist" in capsys.readouterr().out
