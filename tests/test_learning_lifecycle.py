"""Candidate knowledge: the proposal, the lifecycle, and the actor.

T3 of Phase 3. `learning_candidates` used to be a payload blob with a
single-value status: everything that got stored was a 'candidate' and
nothing could ever be anything else. Design §10 asks for two things a blob
cannot express — a **falsifiable proposal** (claim, applicable context,
proposed behaviour change, and the experiences cited for *and against* it)
and a **lifecycle** in which every move names who made it and why.

The tests are in six groups:

1. The proposal is required. All four fields are mandatory at the write
   boundary; whether a claim is any *good* is not something code decides.
2. The lifecycle. ``observation → hypothesis → testing → validated →
   retired``, ``retired`` from anywhere, never backwards.
3. The actor is recorded, and the record cannot be edited.
4. The migration off the single-value status, including the old rows whose
   ``candidate`` now means ``observation``.
5. The evidence fingerprint still identifies the *evidence*, so rewording a
   claim is not a new candidate and changed evidence still is.
6. The read side — counts, filters, and which candidates cite an episode.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from alpha_agents.data import learning_candidates as LC
from alpha_agents.data import memory_store


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_store, "MEMORY_DB_PATH",
                        tmp_path / "memory.db", raising=False)
    monkeypatch.setattr(memory_store._local, "conn", None, raising=False)
    yield
    c = getattr(memory_store._local, "conn", None)
    if c is not None:
        c.close()
    memory_store._local.conn = None


def _db():
    return memory_store._get_conn()


def _save(**over):
    """Save one candidate, overriding anything the test cares about."""
    kwargs = dict(
        entity_type="principle", operation="create", source="unit-test",
        source_date="2026-01-05",
        payload={"proposal": {"principle": "Synthetic"}},
        claim="Small-metal names with institutional inflow set up over 5 days",
        applicable_context="theme score positive and institutional present",
        proposed_behavior_delta={"create_principle": "Synthetic"},
        evidence_episode_ids={"supporting": [1], "opposing": [2]},
    )
    kwargs.update(over)
    return LC.save_candidate(**kwargs)


# ── 1. The proposal is required ────────────────────────────────────────


class TestACandidateMustStateItsProposal:
    def test_every_field_is_required(self, store):
        for field in LC.PROPOSAL_COLUMNS:
            kwargs = dict(
                entity_type="principle", operation="create",
                source="unit-test", source_date="2026-01-05",
                payload={"proposal": {}},
                claim="a claim", applicable_context="a context",
                proposed_behavior_delta={"create_principle": "x"},
                evidence_episode_ids={"supporting": [], "opposing": []},
            )
            kwargs.pop(field)
            with pytest.raises(TypeError, match=field):
                LC.save_candidate(**kwargs)

    @pytest.mark.parametrize("blank", ["", "   ", "\n", None, 7, ["a"]])
    def test_a_claim_that_is_not_a_statement_is_refused(self, store, blank):
        with pytest.raises(ValueError, match="claim"):
            _save(claim=blank)

    @pytest.mark.parametrize("blank", ["", "  ", None])
    def test_an_empty_context_is_refused(self, store, blank):
        with pytest.raises(ValueError, match="applicable_context"):
            _save(applicable_context=blank)

    @pytest.mark.parametrize("delta", [{}, None, "deprecate it", []])
    def test_a_proposal_that_changes_nothing_is_refused(self, store, delta):
        """A delta is what would be different afterwards. Without it there
        is nothing to evaluate, only something to agree with."""
        with pytest.raises(ValueError, match="proposed_behavior_delta"):
            _save(proposed_behavior_delta=delta)

    def test_both_evidence_buckets_must_be_stated(self, store):
        """'We looked and found no opposing evidence' and 'nobody looked'
        are different, and only one of them is a hypothesis."""
        with pytest.raises(ValueError, match="missing opposing"):
            _save(evidence_episode_ids={"supporting": [1]})
        with pytest.raises(ValueError, match="missing supporting"):
            _save(evidence_episode_ids={"opposing": [1]})

    def test_an_unknown_evidence_bucket_is_refused(self, store):
        with pytest.raises(ValueError, match="unknown keys"):
            _save(evidence_episode_ids={"supporting": [], "opposing": [],
                                       "rumoured": [1]})

    @pytest.mark.parametrize("bad", [[0], [-3], ["1"], [1.5], [True], None, "7"])
    def test_citations_must_be_episode_ids(self, store, bad):
        with pytest.raises(ValueError, match="evidence_episode_ids"):
            _save(evidence_episode_ids={"supporting": bad, "opposing": []})

    def test_a_stored_proposal_round_trips(self, store):
        cid = _save(evidence_episode_ids={"supporting": [5, 5, 3],
                                          "opposing": [9]})
        row = LC.get_candidate(cid)
        assert row["status"] == LC.OBSERVATION
        assert row["claim"].startswith("Small-metal")
        assert row["proposed_behavior_delta"] == '{"create_principle": "Synthetic"}'
        # Deduplicated and sorted, so the same citations are the same string.
        assert json.loads(row["evidence_episode_ids"]) == {"supporting": [3, 5],
                                                          "opposing": [9]}


# ── 2. The lifecycle ───────────────────────────────────────────────────


class TestTheCandidateLifecycle:
    def test_the_states_are_the_designs_ones_in_order(self):
        assert LC.STATUSES == (LC.OBSERVATION, LC.HYPOTHESIS, LC.TESTING,
                               LC.VALIDATED, LC.RETIRED)

    def test_a_new_candidate_starts_at_observation(self, store):
        assert LC.get_candidate(_save())["status"] == LC.OBSERVATION

    def test_the_lifecycle_is_walked_one_step_at_a_time(self, store):
        cid = _save()
        for step in (LC.HYPOTHESIS, LC.TESTING, LC.VALIDATED, LC.RETIRED):
            LC.advance_candidate(cid, to_status=step, actor="analyst",
                                 reason=f"moving to {step}", at="2026-01-06")
        assert LC.get_candidate(cid)["status"] == LC.RETIRED

    @pytest.mark.parametrize("start,target", [
        (LC.OBSERVATION, LC.TESTING),
        (LC.OBSERVATION, LC.VALIDATED),
        (LC.HYPOTHESIS, LC.VALIDATED),
        (LC.TESTING, LC.OBSERVATION),
        (LC.VALIDATED, LC.TESTING),
        (LC.RETIRED, LC.VALIDATED),
    ])
    def test_skipping_and_reversing_are_refused(self, store, start, target):
        """Forward one step, or out. Walking *back* would let a proposal
        edit its own history instead of becoming a new candidate."""
        cid = _save()
        for step in LC.STATUSES[1:LC.STATUSES.index(start) + 1]:
            LC.advance_candidate(cid, to_status=step, actor="a", reason="r")
        before = LC.transitions_for(cid)
        with pytest.raises(LC.IllegalCandidateTransition):
            LC.advance_candidate(cid, to_status=target, actor="a", reason="r")
        assert LC.get_candidate(cid)["status"] == start
        assert LC.transitions_for(cid) == before, "a refusal wrote something"

    def test_retired_is_reachable_from_any_earlier_state(self, store):
        for stop_at in (LC.OBSERVATION, LC.HYPOTHESIS, LC.TESTING,
                        LC.VALIDATED):
            cid = _save(payload={"proposal": {"principle": stop_at}})
            for step in LC.STATUSES[1:LC.STATUSES.index(stop_at) + 1]:
                LC.advance_candidate(cid, to_status=step, actor="a", reason="r")
            LC.advance_candidate(cid, to_status=LC.RETIRED, actor="a",
                                 reason="dropped")
            assert LC.get_candidate(cid)["status"] == LC.RETIRED

    def test_retired_has_no_way_out(self, store):
        cid = _save()
        LC.advance_candidate(cid, to_status=LC.RETIRED, actor="a", reason="r")
        for target in LC.STATUSES:
            with pytest.raises(LC.IllegalCandidateTransition):
                LC.advance_candidate(cid, to_status=target, actor="a", reason="r")

    def test_an_unknown_status_is_refused_by_name(self):
        with pytest.raises(LC.IllegalCandidateTransition, match="guessed"):
            LC.assert_transition("guessed", LC.HYPOTHESIS)

    def test_validated_is_not_activation(self, store):
        """§10: 'validation does not itself activate knowledge'. The only
        effect of reaching ``validated`` is the status column."""
        cid = _save()
        LC.advance_candidate(cid, to_status=LC.HYPOTHESIS, actor="a", reason="r")
        LC.advance_candidate(cid, to_status=LC.TESTING, actor="a", reason="r")
        LC.advance_candidate(cid, to_status=LC.VALIDATED, actor="a", reason="r")
        row = LC.get_candidate(cid)
        assert row["status"] == LC.VALIDATED
        # Nothing else moved: the payload is untouched, and no other table
        # grew a row that would make this candidate live.
        assert json.loads(row["payload_json"]) == {"proposal": {"principle": "Synthetic"}}
        assert LC.counts()[LC.VALIDATED] == 1

    def test_a_candidate_that_does_not_exist_is_reported(self, store):
        with pytest.raises(ValueError, match="No learning candidate #999"):
            LC.advance_candidate(999, to_status=LC.HYPOTHESIS, actor="a",
                                 reason="r")


# ── 3. The actor is recorded, and the record is append-only ────────────


class TestEveryMoveNamesItsActor:
    def test_a_move_without_an_actor_or_reason_is_refused(self, store):
        cid = _save()
        with pytest.raises(ValueError, match="actor"):
            LC.advance_candidate(cid, to_status=LC.HYPOTHESIS, actor="  ",
                                 reason="because")
        with pytest.raises(ValueError, match="reason"):
            LC.advance_candidate(cid, to_status=LC.HYPOTHESIS, actor="analyst",
                                 reason="")
        assert LC.get_candidate(cid)["status"] == LC.OBSERVATION
        assert LC.transitions_for(cid) == []

    def test_the_transition_records_who_what_and_why(self, store):
        cid = _save()
        LC.advance_candidate(cid, to_status=LC.HYPOTHESIS, actor="analyst",
                             reason="worth testing on a forward window",
                             at="2026-01-07")
        moves = LC.transitions_for(cid)
        assert len(moves) == 1
        assert moves[0]["from_status"] == LC.OBSERVATION
        assert moves[0]["to_status"] == LC.HYPOTHESIS
        assert moves[0]["actor"] == "analyst"
        assert moves[0]["reason"] == "worth testing on a forward window"
        assert moves[0]["at"] == "2026-01-07"

    def test_a_transition_cannot_be_edited_or_deleted(self, store):
        """The record of who authorised a move is exactly what an
        interested party would want to rewrite."""
        cid = _save()
        LC.advance_candidate(cid, to_status=LC.HYPOTHESIS, actor="analyst",
                             reason="r")
        conn = _db()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE candidate_transitions SET actor = 'someone else'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM candidate_transitions")
        conn.rollback()
        assert LC.transitions_for(cid)[0]["actor"] == "analyst"

    def test_the_whole_history_is_readable(self, store):
        cid = _save()
        LC.advance_candidate(cid, to_status=LC.HYPOTHESIS, actor="a1", reason="r1")
        LC.advance_candidate(cid, to_status=LC.TESTING, actor="a2", reason="r2")
        assert [(m["from_status"], m["to_status"]) for m in LC.transitions_for(cid)] == [
            (LC.OBSERVATION, LC.HYPOTHESIS), (LC.HYPOTHESIS, LC.TESTING)]


# ── 4. The migration off the single-value status ───────────────────────


_LEGACY_DDL = (
    "CREATE TABLE learning_candidates ("
    "id INTEGER PRIMARY KEY, "
    "fingerprint TEXT NOT NULL UNIQUE, "
    "entity_type TEXT NOT NULL CHECK(entity_type IN ('principle', 'playbook')), "
    "operation TEXT NOT NULL CHECK(operation IN ('create', 'reinforce', 'update', 'retire')), "
    "target_id INTEGER, source TEXT NOT NULL, source_date TEXT NOT NULL, "
    "payload_json TEXT NOT NULL, "
    "status TEXT NOT NULL DEFAULT 'candidate' CHECK(status = 'candidate'), "
    "created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))"
)


class TestTheOldTableMigrates:
    def _legacy(self, status="candidate"):
        conn = _db()
        conn.execute(_LEGACY_DDL)
        conn.execute(
            "INSERT INTO learning_candidates "
            "(fingerprint, entity_type, operation, source, source_date, "
            " payload_json, status) VALUES "
            "('fingerprint-kept', 'principle', 'create', 'old', '2026-01-01', "
            " '{\"proposal\": {}}', ?)", (status,))
        conn.commit()
        return conn

    def test_a_row_written_before_t3_becomes_an_observation(self, store):
        conn = self._legacy()
        LC.init_schema(conn)
        row = conn.execute(
            "SELECT status, fingerprint FROM learning_candidates").fetchone()
        assert row["status"] == LC.OBSERVATION
        assert row["fingerprint"] == "fingerprint-kept", "identity preserved"

    def test_the_relaxed_check_accepts_the_whole_lifecycle(self, store):
        conn = self._legacy()
        LC.init_schema(conn)
        # The migrated row is already an observation, so the other four
        # states are the ones that used to be impossible to store at all.
        for status in LC.STATUSES[1:]:
            conn.execute(
                "INSERT INTO learning_candidates "
                "(fingerprint, entity_type, operation, source, source_date, "
                " payload_json, status) VALUES "
                f"('fp-{status}', 'principle', 'create', 's', '2026-01-01', "
                "'{}', ?)", (status,))
        assert LC.counts() == {s: 1 for s in LC.STATUSES}

    def test_the_old_check_still_refuses_a_status_that_is_not_a_lifecycle_state(
            self, store):
        conn = self._legacy()
        LC.init_schema(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO learning_candidates "
                "(fingerprint, entity_type, operation, source, source_date, "
                " payload_json, status) VALUES "
                "('fp-bad', 'principle', 'create', 's', '2026-01-01', '{}', "
                "'active')")

    def test_the_proposal_columns_arrive_empty_not_invented(self, store):
        conn = self._legacy()
        LC.init_schema(conn)
        row = LC.get_candidate(1)
        for column in LC.PROPOSAL_COLUMNS:
            assert row[column] is None, (
                f"{column} must not be back-filled: the author never stated it")
        assert any("predates the proposal fields" in p
                   for p in LC.integrity())

    def test_migrating_twice_is_a_no_op(self, store):
        conn = self._legacy()
        LC.init_schema(conn)
        LC.init_schema(conn)
        assert LC.get_candidate(1)["status"] == LC.OBSERVATION
        assert conn.execute(
            "SELECT COUNT(*) n FROM learning_candidates").fetchone()["n"] == 1

    def test_a_fresh_database_is_born_at_the_t3_shape(self, store):
        conn = _db()
        LC.init_schema(conn)
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'learning_candidates'").fetchone()["sql"]
        assert "status = 'candidate'" not in sql
        assert "'observation'" in sql


# ── 5. The fingerprint still identifies the evidence ───────────────────


class TestTheFingerprintIsStillAboutEvidence:
    def test_rewording_the_claim_is_not_a_new_candidate(self, store):
        """The claim is a statement *about* the evidence. Folding it into
        the fingerprint would turn one finding into as many candidates as
        there are ways to phrase it."""
        first = _save(claim="Small metals with inflow set up")
        second = _save(claim="Institutional buying in small metals pays over five days")
        assert first == second
        assert LC.counts()[LC.OBSERVATION] == 1

    def test_changed_evidence_is_a_new_candidate(self, store):
        first = _save()
        second = _save(payload={"proposal": {"principle": "Different"}})
        assert second != first
        assert LC.counts()[LC.OBSERVATION] == 2

    def test_a_stated_claim_is_never_overwritten(self, store):
        first = _save(claim="The original claim")
        _save(claim="A later retry that says something else")
        assert LC.get_candidate(first)["claim"] == "The original claim"

    def test_a_legacy_row_is_enriched_once_and_then_left_alone(self, store):
        """Pre-T3 rows have no claim. Stating one is enrichment; it is not
        the 'changed evidence overwrites' this store refuses to do."""
        conn = _db()
        LC.init_schema(conn)
        payload = {"proposal": {"principle": "Synthetic"}}
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        import hashlib
        identity = json.dumps(["principle", "create", None, "unit-test",
                               "2026-01-05", payload_json])
        fingerprint = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        conn.execute(
            "INSERT INTO learning_candidates "
            "(fingerprint, entity_type, operation, source, source_date, "
            " payload_json) VALUES (?, 'principle', 'create', 'unit-test', "
            "'2026-01-05', ?)", (fingerprint, payload_json))
        conn.commit()
        assert LC.get_candidate(1)["claim"] is None

        # Empty citations, not the fixture default: this test is about the
        # claim being stated once, and the default cites episodes #1/#2,
        # which would make the integrity check below report them instead of
        # the thing under test.
        nothing_cited = {"supporting": [], "opposing": []}
        first = _save(claim="Stated once", evidence_episode_ids=nothing_cited)
        assert first == 1
        assert LC.get_candidate(1)["claim"] == "Stated once"
        _save(claim="Stated again, differently", evidence_episode_ids=nothing_cited)
        assert LC.get_candidate(1)["claim"] == "Stated once", "write-once"
        assert LC.integrity() == []


# ── 6. The read side ───────────────────────────────────────────────────


class TestTheReadSide:
    def test_counts_include_states_with_no_rows(self, store):
        _save()
        assert LC.counts() == {LC.OBSERVATION: 1, LC.HYPOTHESIS: 0,
                               LC.TESTING: 0, LC.VALIDATED: 0, LC.RETIRED: 0}

    def test_candidates_can_be_listed_by_state(self, store):
        keep = _save()
        other = _save(payload={"proposal": {"principle": "Other"}})
        LC.advance_candidate(other, to_status=LC.RETIRED, actor="a", reason="r")
        assert [c["id"] for c in LC.candidates_by_status(LC.OBSERVATION)] == [keep]
        assert [c["id"] for c in LC.candidates_by_status(LC.RETIRED)] == [other]
        assert len(LC.candidates_by_status()) == 2

    def test_an_unknown_status_filter_is_refused(self, store):
        with pytest.raises(ValueError, match="Unknown candidate status"):
            LC.candidates_by_status("probably-fine")

    def test_candidates_can_be_found_by_the_episode_they_cite(self, store):
        cites = _save(evidence_episode_ids={"supporting": [7], "opposing": []})
        against = _save(payload={"proposal": {"principle": "Against"}},
                        evidence_episode_ids={"supporting": [1], "opposing": [7]})
        _save(payload={"proposal": {"principle": "Unrelated"}},
              evidence_episode_ids={"supporting": [4], "opposing": []})

        supporting = LC.candidates_citing(7)
        assert [c["id"] for c in supporting] == [cites, against]
        assert supporting[0]["cited_as"] == [LC.SUPPORTING]
        assert supporting[1]["cited_as"] == [LC.OPPOSING]

    def test_an_episode_nobody_cites_returns_nothing(self, store):
        _save()
        assert LC.candidates_citing(999) == []

    def test_a_citation_of_an_episode_that_does_not_exist_is_reported(self, store):
        _save(evidence_episode_ids={"supporting": [7], "opposing": []})
        problems = LC.integrity()
        assert any("cites episode #7" in p for p in problems), problems

    def test_a_healthy_quarantine_is_quiet(self, store):
        _save(evidence_episode_ids={"supporting": [], "opposing": []})
        assert LC.integrity() == []
