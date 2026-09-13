"""The policy registry: what is in force, and whether it still is.

U1 of Phase 4. §14 makes the phase "evidence controls future policy changes
through one audited entry point"; this slice is the *record* that entry point
writes to — versions, the pointer, and the audit trail between them.

The property that makes it more than a changelog: a version's hash is computed
from the **live configuration** (prompt files, model identity, retrieval
budgets, decision-rule constants, the approved knowledge in force), so
verification answers "is what is in force still what is configured". Editing a
prompt without opening a version turns the version in force into a failed
check.

The tests are in nine groups:

1. A version is a record: who, when, why, and what it covered.
2. The record is append-only, enforced by the database.
3. The hash identifies content — the same configuration is one version, a
   changed one is another, and the projection is order-independent.
4. The collector and the record agree: a source set missing a name, or
   inventing one, is refused rather than silently changing what is hashed.
5. The pointer: installing works once, and refuses afterwards — that refusal
   is what stops ``install`` being a back door around promotion.
6. The reference a decision writes into ``policy_ref``, which was reserved in
   Phase 1 and never written until now.
7. Drift: a changed source fails verification, and names which source moved.
8. Integrity reports structural holes rather than repairing them.
9. Installing changes nothing outside this module's three tables — the
   machine form of "the registry is not an activation path".
"""

from __future__ import annotations

import sqlite3

import pytest

from alpha_agents.data import memory_store
from alpha_agents.data import policy_registry as PR
from alpha_agents.evolution import feedback
from alpha_agents.evolution import policy_sources as PS


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A private database.

    ``MEMORY_DB_PATH`` is imported into ``memory_store`` at module scope and
    is *not* read from the environment, so it has to be patched on the module
    — a subprocess with the env var set writes to the real ``data/memory.db``.
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


def _sources(**over) -> dict:
    """A complete, valid source set, with named sources overridden."""
    base = {
        "prompts": {"morning_scan.md": "aaa"},
        "model": {"agent_model": "qwen-plus"},
        "retrieval": {"feedback._PLAYBOOKS_BUDGET": 400},
        "rules": {"holdout_gate.MIN_VALIDATION_SAMPLES": 20},
        "knowledge": {"snapshot_id": None},
    }
    base.update(over)
    return base


def _freeze(**over) -> int:
    kwargs = dict(sources=_sources(), created_by="kylin",
                  reason="frozen for the forward window")
    kwargs.update(over)
    return PR.freeze(**kwargs)


def _install(version_id: int, **over) -> int:
    kwargs = dict(version_id=version_id, actor="kylin", reason="first policy")
    kwargs.update(over)
    return PR.install(**kwargs)


def _dump(conn) -> dict:
    """Every user table's contents, for the no-activation assertion."""
    out = {}
    for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"):
        if name.startswith("sqlite_"):
            continue
        rows = conn.execute(f'SELECT * FROM "{name}"').fetchall()
        out[name] = sorted(tuple(row) for row in rows)
    return out


# ── 1. A version is a record ───────────────────────────────────────────


class TestAVersionIsARecord:
    def test_a_frozen_version_names_its_author_and_reason(self, store):
        version = PR.get_version(_freeze(created_by="kylin", reason="because"))
        assert version["created_by"] == "kylin"
        assert version["reason"] == "because"

    def test_the_frozen_sources_are_stored_verbatim(self, store):
        sources = _sources()
        version = PR.get_version(_freeze(sources=sources))
        assert PR.sources_of(version["id"]) == sources

    def test_the_policy_key_defaults_to_the_trader(self, store):
        version = PR.get_version(_freeze())
        assert version["policy_key"] == PR.POLICY_KEY_DEFAULT == "trader"

    def test_a_parent_can_be_named(self, store):
        first = _freeze()
        second = _freeze(sources=_sources(rules={"changed": 1}), parent_id=first)
        assert PR.get_version(second)["parent_id"] == first

    def test_frozen_at_defaults_to_the_kernel_clock(self, store):
        from alpha_agents.data import clock
        version = PR.get_version(_freeze())
        assert version["frozen_at"] == clock.today()

    def test_frozen_at_can_be_declared_for_a_replay(self, store):
        version = PR.get_version(_freeze(frozen_at="2026-03-01"))
        assert version["frozen_at"] == "2026-03-01"

    def test_an_author_is_required(self, store):
        with pytest.raises(PR.PolicyError):
            _freeze(created_by="   ")

    def test_a_reason_is_required(self, store):
        with pytest.raises(PR.PolicyError):
            _freeze(reason="")

    def test_an_unknown_version_reads_as_none(self, store):
        assert PR.get_version(9999) is None

    def test_versions_are_listed_oldest_first(self, store):
        first = _freeze(sources=_sources(prompts={"a.md": "1"}))
        second = _freeze(sources=_sources(prompts={"a.md": "2"}))
        assert [v["id"] for v in PR.versions_for()] == [first, second]

    def test_latest_version_is_the_newest_not_the_active_one(self, store):
        first = _freeze(sources=_sources(prompts={"a.md": "1"}))
        second = _freeze(sources=_sources(prompts={"a.md": "2"}))
        _install(first)
        assert PR.latest_version()["id"] == second
        assert PR.active_version()["id"] == first


# ── 2. Append-only ─────────────────────────────────────────────────────


class TestTheRecordIsAppendOnly:
    def test_a_version_cannot_be_updated(self, store):
        version_id = _freeze()
        with pytest.raises(sqlite3.IntegrityError):
            store.execute("UPDATE policy_versions SET reason = 'edited' "
                          "WHERE id = ?", (version_id,))

    def test_a_version_cannot_be_deleted(self, store):
        version_id = _freeze()
        with pytest.raises(sqlite3.IntegrityError):
            store.execute("DELETE FROM policy_versions WHERE id = ?",
                          (version_id,))

    def test_a_transition_cannot_be_updated(self, store):
        _install(_freeze())
        with pytest.raises(sqlite3.IntegrityError):
            store.execute("UPDATE policy_transitions SET actor = 'someone'")

    def test_a_transition_cannot_be_deleted(self, store):
        _install(_freeze())
        with pytest.raises(sqlite3.IntegrityError):
            store.execute("DELETE FROM policy_transitions")

    def test_the_pointer_is_the_one_mutable_row(self, store):
        # Not an accident: the pointer has to move, which is exactly why
        # every move leaves an append-only transition behind it.
        _install(_freeze())
        store.execute("UPDATE active_policy SET reason = 'noted'")
        assert PR.active()["reason"] == "noted"


# ── 3. The hash identifies content ─────────────────────────────────────


class TestTheHashIdentifiesContent:
    def test_the_same_configuration_is_one_version(self, store):
        first = _freeze()
        second = _freeze(reason="frozen again on a later day")
        assert first == second
        assert PR.counts()["versions"] == 1

    def test_a_changed_source_is_a_different_version(self, store):
        first = _freeze(sources=_sources(prompts={"a.md": "1"}))
        second = _freeze(sources=_sources(prompts={"a.md": "2"}))
        assert first != second

    def test_the_hash_ignores_key_order(self, store):
        a = PR.content_hash_for(_sources(model={"m": "x", "b": "y"}))
        b = PR.content_hash_for(_sources(model={"b": "y", "m": "x"}))
        assert a == b

    def test_the_hash_moves_when_a_source_moves(self, store):
        base = PR.content_hash_for(_sources())
        assert PR.content_hash_for(_sources(retrieval={"budget": 1})) != base

    def test_verify_accepts_the_configuration_it_was_frozen_from(self, store):
        version_id = _freeze()
        assert PR.verify_version(version_id, _sources()) is True

    def test_verify_rejects_a_changed_configuration(self, store):
        version_id = _freeze()
        assert PR.verify_version(version_id, _sources(rules={"x": 1})) is False

    def test_verify_rejects_an_unknown_version(self, store):
        # Absent and drifted are both reasons not to promote.
        assert PR.verify_version(4242, _sources()) is False


# ── 4. Collector and record agree ──────────────────────────────────────


class TestTheCollectorAndTheRecordAgree:
    def test_a_missing_source_is_refused(self, store):
        incomplete = _sources()
        del incomplete["prompts"]
        with pytest.raises(PR.PolicyError) as exc:
            _freeze(sources=incomplete)
        assert "prompts" in str(exc.value)

    def test_an_unknown_source_is_refused(self, store):
        with pytest.raises(PR.PolicyError) as exc:
            _freeze(sources=_sources(vibes={"n": 1}))
        assert "vibes" in str(exc.value)

    def test_a_non_object_is_refused(self, store):
        with pytest.raises(PR.PolicyError):
            _freeze(sources=["prompts"])

    def test_a_non_serialisable_source_is_refused(self, store):
        with pytest.raises(PR.PolicyError):
            _freeze(sources=_sources(rules={"fn": object()}))

    def test_the_declared_names_are_exactly_what_the_collector_produces(
            self, store):
        # The contract between the two modules, asserted rather than assumed:
        # a source added on one side only would otherwise change what the hash
        # covers while every version still verified.
        assert tuple(sorted(PS.collect())) == tuple(sorted(PR.SOURCE_NAMES))

    def test_the_collector_reads_the_live_configuration(self, store):
        live = PS.collect()
        assert set(live["prompts"])  # the prompt files really were read
        assert live["model"]["agent_model"]
        assert live["rules"]["holdout_gate.MIN_VALIDATION_SAMPLES"] == 20

    def test_a_renamed_rule_constant_fails_loudly(self, store, monkeypatch):
        monkeypatch.delattr(feedback, "_PLAYBOOKS_BUDGET")
        with pytest.raises(PR.PolicyError) as exc:
            PS.collect()
        assert "_PLAYBOOKS_BUDGET" in str(exc.value)


# ── 5. The pointer ─────────────────────────────────────────────────────


class TestThePointer:
    def test_nothing_is_in_force_before_an_install(self, store):
        assert PR.active() is None
        assert PR.active_version() is None

    def test_installing_puts_a_version_in_force(self, store):
        version_id = _freeze()
        assert _install(version_id) == 1
        assert PR.active()["version_id"] == version_id
        assert PR.active_version()["id"] == version_id

    def test_the_first_change_is_seq_one(self, store):
        _install(_freeze())
        assert PR.active()["version_seq"] == 1

    def test_a_second_install_is_refused(self, store):
        first = _freeze(sources=_sources(prompts={"a.md": "1"}))
        second = _freeze(sources=_sources(prompts={"a.md": "2"}))
        _install(first)
        with pytest.raises(PR.PolicyError) as exc:
            _install(second, reason="sneaking it in")
        assert "promotion or a rollback" in str(exc.value)
        assert PR.active()["version_id"] == first

    def test_installing_an_unknown_version_is_refused(self, store):
        with pytest.raises(PR.PolicyError):
            _install(999)

    def test_installing_across_policy_keys_is_refused(self, store):
        version_id = _freeze(policy_key="trader")
        with pytest.raises(PR.PolicyError) as exc:
            _install(version_id, policy_key="other")
        assert "belongs to" in str(exc.value)

    def test_a_second_policy_can_be_installed_independently(self, store):
        _install(_freeze(sources=_sources(prompts={"a.md": "1"})))
        other = _freeze(policy_key="other", sources=_sources(prompts={"a.md": "2"}))
        assert _install(other, policy_key="other") == 1
        assert PR.active("other")["version_id"] == other
        assert PR.active()["version_id"] != other

    def test_the_install_leaves_a_transition(self, store):
        version_id = _freeze()
        _install(version_id)
        trail = PR.transitions_for()
        assert len(trail) == 1
        assert trail[0]["kind"] == "install"
        assert trail[0]["from_version_id"] is None
        assert trail[0]["to_version_id"] == version_id
        assert trail[0]["actor"] == "kylin"

    def test_an_actor_is_required(self, store):
        with pytest.raises(PR.PolicyError):
            _install(_freeze(), actor="")


# ── 6. The reference ───────────────────────────────────────────────────


class TestTheReference:
    def test_there_is_no_reference_before_a_policy_is_in_force(self, store):
        assert PR.active_ref() is None

    def test_the_reference_names_the_version_and_its_hash(self, store):
        version_id = _freeze()
        _install(version_id)
        ref = PR.active_ref()
        key, parsed_id, digest = PR.parse_ref(ref)
        assert key == "trader"
        assert parsed_id == version_id
        assert PR.get_version(version_id)["content_hash"].startswith(digest)

    def test_the_reference_round_trips(self, store):
        _install(_freeze())
        assert PR.parse_ref(PR.active_ref()) is not None

    def test_a_foreign_string_is_not_a_reference(self, store):
        # A decision written before the registry existed has no reference,
        # and that is a fact about history rather than an error.
        for value in (None, "", "daily_playbook", "trader#x@abc", "trader#1@"):
            assert PR.parse_ref(value) is None

    def test_the_reference_points_at_the_version_in_force(self, store):
        first = _freeze(sources=_sources(prompts={"a.md": "1"}))
        _install(first)
        assert PR.parse_ref(PR.active_ref())[1] == first


# ── 7. Drift ───────────────────────────────────────────────────────────


class TestDrift:
    def test_a_freshly_frozen_version_has_not_drifted(self, store):
        PS.freeze_live(created_by="kylin", reason="initial")
        assert PS.drifted() == []

    def test_editing_a_retrieval_budget_drifts_the_version(
            self, store, monkeypatch):
        version_id = PS.freeze_live(created_by="kylin", reason="initial")
        monkeypatch.setattr(feedback, "_PLAYBOOKS_BUDGET", 9999)
        assert PS.verify_live(version_id) is False
        assert [d["id"] for d in PS.drifted()] == [version_id]

    def test_drift_names_which_source_moved(self, store, monkeypatch):
        version_id = PS.freeze_live(created_by="kylin", reason="initial")
        monkeypatch.setattr(feedback, "_PLAYBOOKS_BUDGET", 9999)
        changed = PS.changed_sources(version_id)
        assert "retrieval" in changed
        assert changed["retrieval"]["frozen"] != changed["retrieval"]["live"]

    def test_an_unchanged_version_reports_no_changed_sources(self, store):
        version_id = PS.freeze_live(created_by="kylin", reason="initial")
        assert PS.changed_sources(version_id) == {}

    def test_changed_sources_of_an_unknown_version_is_empty(self, store):
        assert PS.changed_sources(999) == {}

    def test_an_unactivated_approval_does_not_drift_the_live_policy(self, store):
        # Approval does not activate knowledge. Only the pointer decides what
        # the live collector sees; unrelated new approvals cannot change it.
        from alpha_agents.data import knowledge_snapshots as KS
        version_id = PS.freeze_live(created_by="kylin", reason="initial")
        assert PS.verify_live(version_id) is True
        store.execute(
            "INSERT INTO trading_principles (principle, pattern_description, "
            "category, action_guidance, first_learned, last_reinforced) "
            "VALUES ('p','d','entry','g','2026-01-01','2026-01-01')")
        store.commit()
        KS.approve(approved_by="kylin", reason="reviewed",
                   items=[{"entity_type": "principle", "entity_id": 1}])
        assert PS.verify_live(version_id) is True
        assert "knowledge" not in PS.changed_sources(version_id)

    def test_an_older_drifted_version_is_reported_too(self, store, monkeypatch):
        # Every version is checked, not only the one in force: a version that
        # drifted cannot be rolled back to either.
        first = PS.freeze_live(created_by="kylin", reason="first")
        monkeypatch.setattr(feedback, "_PLAYBOOKS_BUDGET", 111)
        second = PS.freeze_live(created_by="kylin", reason="second")
        monkeypatch.setattr(feedback, "_PLAYBOOKS_BUDGET", 222)
        drifted = [d["id"] for d in PS.drifted()]
        assert drifted == [first, second]

    def test_summary_reports_the_registry_state(self, store):
        version_id = PS.freeze_live(created_by="kylin", reason="initial")
        PR.install(version_id=version_id, actor="kylin", reason="first")
        summary = PS.summary()
        assert summary["counts"]["versions"] == 1
        assert summary["active"]["version_id"] == version_id
        assert summary["integrity"] == []


# ── 8. Integrity ───────────────────────────────────────────────────────


class TestIntegrity:
    def test_an_empty_registry_is_integral(self, store):
        assert PR.integrity() == []

    def test_a_clean_install_is_integral(self, store):
        _install(_freeze())
        assert PR.integrity() == []

    def test_a_pointer_to_a_missing_version_is_reported(self, store):
        _install(_freeze())
        # The pointer is the mutable row, so it is the one that can be made
        # to lie; the check exists for a restored backup or a dropped trigger.
        store.execute("UPDATE active_policy SET version_id = 4242")
        problems = PR.integrity()
        assert any("does not exist" in p for p in problems)

    def test_a_pointer_across_policy_keys_is_reported(self, store):
        _install(_freeze())
        store.execute("UPDATE active_policy SET policy_key = 'other'")
        problems = PR.integrity()
        assert any("belongs to" in p for p in problems)

    def test_a_pointer_without_a_trail_is_reported(self, store):
        _install(_freeze())
        store.execute("UPDATE active_policy SET version_seq = 7")
        problems = PR.integrity()
        assert any("unrecorded" in p for p in problems)

    def test_a_transition_to_a_missing_version_is_reported(self, store):
        _install(_freeze())
        store.execute(
            "INSERT INTO policy_transitions (policy_key, from_version_id, "
            "to_version_id, version_seq, kind, actor, reason, at) "
            "VALUES ('trader', 1, 4242, 2, 'promote', 'kylin', 'r', '2026-01-01')")
        problems = PR.integrity()
        assert any("#4242" in p for p in problems)

    def test_counts_reports_versions_transitions_and_pointers(self, store):
        _install(_freeze())
        assert PR.counts() == {"versions": 1, "transitions": 1, "active": 1,
                               "approvals": 0}


# ── 9. Installing activates nothing ────────────────────────────────────


class TestInstallingActivatesNothing:
    def test_only_the_three_tables_an_install_touches_grow(self, store):
        # The T4 posture: rather than spot-check a table, dump every user
        # table and assert the changed set is exactly the permitted one.
        # ``policy_approvals`` is deliberately absent from the permitted set:
        # an install has no incumbent to be measured against, so there is
        # nothing to approve and nothing that may be written there.
        PR.init_schema(store)  # warm up: the tables exist before the dump
        before = _dump(store)
        _install(_freeze())
        after = _dump(store)
        assert set(before) == set(after)
        changed = {name for name in before if before[name] != after[name]}
        assert changed == {"policy_versions", "active_policy",
                           "policy_transitions"}, (
            f"the registry changed {sorted(changed)}; it is a record, and "
            "recording a policy must not move anything else")
        assert after["policy_approvals"] == []

    def test_the_reflective_writers_now_name_a_policy(self, store):
        # The reserved columns from Phase 1: this is the slice that fills
        # them. Nothing else in the slice has an observable effect, so this
        # is what "the registry is referenced" means in practice.
        from alpha_agents.data import intent
        _install(_freeze())
        result = intent.submit_intent(intent.TradeIntent(
            action=intent.OPEN, code="600000", name="X", theme="t",
            order_date="2026-01-05", entry_low=1.0, entry_high=2.0,
            information_cutoff="2026-01-05"))
        assert result.intent_id
        row = store.execute("SELECT policy_ref FROM intents WHERE id = ?",
                            (result.intent_id,)).fetchone()
        assert row["policy_ref"] == PR.active_ref()

    def test_without_a_policy_in_force_the_reference_stays_empty(self, store):
        from alpha_agents.data import intent
        result = intent.submit_intent(intent.TradeIntent(
            action=intent.OPEN, code="600000", name="X", theme="t",
            order_date="2026-01-05", entry_low=1.0, entry_high=2.0,
            information_cutoff="2026-01-05"))
        row = store.execute("SELECT policy_ref FROM intents WHERE id = ?",
                            (result.intent_id,)).fetchone()
        assert row["policy_ref"] is None

    def test_an_explicit_reference_wins(self, store):
        from alpha_agents.data import intent
        _install(_freeze())
        result = intent.submit_intent(intent.TradeIntent(
            action=intent.OPEN, code="600000", name="X", theme="t",
            order_date="2026-01-05", entry_low=1.0, entry_high=2.0,
            information_cutoff="2026-01-05", policy_ref="trader#99@deadbeef"))
        row = store.execute("SELECT policy_ref FROM intents WHERE id = ?",
                            (result.intent_id,)).fetchone()
        assert row["policy_ref"] == "trader#99@deadbeef"
