"""Phase 5 read models: what a workspace shows, and what it refuses to invent.

Nine groups. The first two are the contract; the rest are the three states,
the sources, the routes, and the honest boundary this build is stuck at.

1. the three workspaces are the ones the design names
2. a read model does not migrate
3. schema completeness is orthogonal to row count
4. `absent` is not `empty`, and neither is `partial`
5. every section names a source, and a complete schema backs every value
6. the routes serve what the read models return
7. `/api/portfolio` is the trade book, not a second copy of it
8. the evolve payload carries the gap the database cannot express
9. an empty database is a legal answer, not an error
"""

import asyncio
import json

import pytest

import alpha_agents.server.readmodels as rm
from alpha_agents.data import memory_store
from alpha_agents.server.readmodels import evolve, learn, trade

WORKSPACE_MODULES = {"trade": trade, "learn": learn, "evolve": evolve}


def _snapshot(name: str) -> dict:
    return WORKSPACE_MODULES[name].snapshot()


def _tables() -> set:
    return rm.table_names(memory_store._get_conn())


# The tables a read model would create if it called a data module's reader
# unguarded. Each one is created by that module's ``init_schema``, which its
# readers call on entry — so "did the page migrate the database" is answered
# by checking these five after a snapshot.
WOULD_MIGRATE = ("learning_candidates", "candidate_transitions",
                 "policy_versions", "shadow_runs", "gate_decisions")


class TestTheThreeWorkspacesAreTheOnesTheDesignNames:
    def test_the_design_names_exactly_these_three(self):
        assert rm.WORKSPACES == ("trade", "learn", "evolve")

    def test_each_module_answers_for_its_own_name(self):
        for name in rm.WORKSPACES:
            payload = rm.load(name).snapshot()
            assert payload["workspace"] == name

    def test_a_payload_has_states_and_sections(self):
        payload = _snapshot("trade")
        assert set(payload) == {"workspace", "generated_at", "states",
                                "sections", "code"}
        assert set(payload["states"]) == set(payload["sections"])

    def test_an_unknown_workspace_is_refused(self):
        with pytest.raises(KeyError):
            rm.load("playground")


class TestAReadModelDoesNotMigrate:
    """Opening a page must not change the schema.

    Every data module's reader calls its own ``init_schema`` on entry. If a
    read model calls one of those readers on a database that lacks the tables,
    it has already migrated it — and "who changed production" stops having one
    answer. The probe has to come first.
    """

    def test_the_snapshots_add_no_table(self):
        memory_store._get_conn()               # warm: _SCHEMA runs here
        before = _tables()

        for name in rm.WORKSPACES:
            _snapshot(name)

        assert _tables() == before

    def test_the_tables_a_reader_would_have_created_are_still_absent(self):
        memory_store._get_conn()
        for name in rm.WORKSPACES:
            _snapshot(name)
        present = _tables()
        for table in WOULD_MIGRATE:
            assert table not in present, f"{table} 被读模型建出来了"

    def test_the_refusal_is_reported_not_hidden(self):
        """The tables are absent, and the payload says so rather than 'no rows'."""
        memory_store._get_conn()
        assert _snapshot("evolve")["states"]["pointer"] == rm.STATE_UNAVAILABLE
        assert _snapshot("learn")["states"]["candidates"] == rm.STATE_UNAVAILABLE


class TestSchemaCompletenessIsOrthogonalToRowCount:
    """A section that could not be read says so, whatever its would-be count."""

    def test_absent_tables_report_unavailable_and_name_them(self):
        memory_store._get_conn()

        pointer = _snapshot("evolve")["sections"]["pointer"]

        assert pointer["schema"] == rm.SCHEMA_ABSENT
        assert pointer["state"] == rm.STATE_UNAVAILABLE
        assert pointer["rows"] is None
        assert "policy_versions" in pointer["missing"]
        assert "active_policy" in pointer["missing"]

    def test_a_missing_column_is_partial_not_absent(self):
        """The production shape of `gate_decisions`: the table exists, the
        Phase 4 column does not."""
        conn = memory_store._get_conn()
        conn.execute(
            "CREATE TABLE gate_decisions ("
            " id INTEGER PRIMARY KEY, date TEXT NOT NULL, "
            " candidate TEXT NOT NULL, promoted INTEGER NOT NULL, "
            " abstained INTEGER NOT NULL DEFAULT 0, n INTEGER, "
            " mean_diff REAL, t_stat REAL, reason TEXT, detail_json TEXT)")
        conn.commit()

        gates = _snapshot("evolve")["sections"]["gates"]

        assert gates["schema"] == rm.SCHEMA_PARTIAL
        assert gates["state"] == rm.STATE_UNAVAILABLE
        assert "gate_decisions.evidence_scope" in gates["missing"]
        assert "gate_decisions.validation_days" in gates["missing"]

    def test_completing_the_schema_flips_it_to_empty_then_present(self):
        conn = memory_store._get_conn()
        conn.execute(
            "CREATE TABLE gate_decisions ("
            " id INTEGER PRIMARY KEY, date TEXT NOT NULL, "
            " candidate TEXT NOT NULL, promoted INTEGER NOT NULL, "
            " abstained INTEGER NOT NULL DEFAULT 0, n INTEGER, "
            " validation_days INTEGER, evidence_scope TEXT, mean_diff REAL, "
            " t_stat REAL, reason TEXT, detail_json TEXT)")
        conn.commit()

        gates = _snapshot("evolve")["sections"]["gates"]
        assert gates["schema"] == rm.SCHEMA_COMPLETE
        assert gates["state"] == rm.STATE_EMPTY
        assert gates["rows"] == 0

        conn.execute(
            "INSERT INTO gate_decisions (date, candidate, promoted, abstained,"
            " n, validation_days, evidence_scope, reason, detail_json)"
            " VALUES ('2026-09-11', 'daily_playbook#1', 0, 1, 0, 0,"
            " 'baseline_only', '验证样本 0 < 20', '{}')")
        conn.commit()

        gates = _snapshot("evolve")["sections"]["gates"]
        assert gates["state"] == rm.STATE_PRESENT
        assert gates["rows"] == 1
        assert gates["value"]["abstained"] == 1
        assert gates["value"]["non_abstain"] == 0


class TestAbsentIsNotEmptyAndNeitherIsPartial:
    def test_the_note_differs_between_absent_and_partial(self):
        """One sentence for both would erase the distinction the page shows."""
        assert rm.ABSENT_NOTE != rm.PARTIAL_NOTE

        conn = memory_store._get_conn()
        conn.execute(
            "CREATE TABLE gate_decisions (id INTEGER PRIMARY KEY,"
            " date TEXT NOT NULL, candidate TEXT NOT NULL,"
            " promoted INTEGER NOT NULL, abstained INTEGER NOT NULL DEFAULT 0)")
        conn.commit()

        gates = _snapshot("evolve")["sections"]["gates"]
        assert gates["status_note"] == rm.PARTIAL_NOTE
        assert "落后" in gates["status_note"]

    def test_a_sections_own_note_cannot_hide_the_schema_state(self):
        """`note` says what the section is about; `status_note` says why it
        could not be read. One field for both would let a sentence about the
        domain bury a schema that is older than the code."""
        conn = memory_store._get_conn()
        conn.execute(
            "CREATE TABLE gate_decisions (id INTEGER PRIMARY KEY,"
            " date TEXT NOT NULL, candidate TEXT NOT NULL,"
            " promoted INTEGER NOT NULL, abstained INTEGER NOT NULL DEFAULT 0)")
        conn.commit()

        gates = _snapshot("evolve")["sections"]["gates"]
        assert gates["note"], "section 自己的说明不该为空"
        assert gates["note"] != gates["status_note"]

    def test_a_half_created_candidate_store_is_partial(self):
        """`learning_candidates` exists, `candidate_transitions` does not —
        the state the production database is actually in."""
        conn = memory_store._get_conn()
        conn.execute(
            "CREATE TABLE learning_candidates (id INTEGER PRIMARY KEY,"
            " status TEXT NOT NULL, entity_type TEXT NOT NULL,"
            " evidence_episode_ids TEXT)")
        conn.commit()

        cands = _snapshot("learn")["sections"]["candidates"]

        assert cands["schema"] == rm.SCHEMA_PARTIAL
        assert cands["missing"] == ["candidate_transitions"]


class TestEverySectionNamesASource:
    def test_no_section_is_sourceless(self):
        memory_store._get_conn()
        for name in rm.WORKSPACES:
            for section_name, sec in _snapshot(name)["sections"].items():
                assert sec["source"].strip(), f"{name}.{section_name} 没有来源"

    def test_the_payload_declares_what_it_asked_for(self):
        memory_store._get_conn()
        for name in rm.WORKSPACES:
            for label, sec in _snapshot(name)["sections"].items():
                assert sec["needs"], f"{name}.{label} 没有声明任何表"
                for need in sec["needs"]:
                    assert need["table"]
                    assert isinstance(need["columns"], list)

    def test_every_missing_entry_was_declared(self):
        """`missing` may not name something the section never asked for —
        otherwise the page shows a requirement nobody can trace back."""
        memory_store._get_conn()
        for name in rm.WORKSPACES:
            for label, sec in _snapshot(name)["sections"].items():
                declared = {need["table"] for need in sec["needs"]}
                declared |= {f"{need['table']}.{col}"
                             for need in sec["needs"] for col in need["columns"]}
                assert set(sec["missing"]) <= declared, f"{name}.{label}"

    def test_a_complete_schema_has_nothing_missing(self):
        memory_store._get_conn()
        for name in rm.WORKSPACES:
            for label, sec in _snapshot(name)["sections"].items():
                if sec["schema"] == rm.SCHEMA_COMPLETE:
                    assert sec["missing"] == [], f"{name}.{label}"
                if sec["state"] != rm.STATE_UNAVAILABLE:
                    assert sec["value"] is not None, f"{name}.{label}"
                    assert sec["rows"] is not None, f"{name}.{label}"

    def test_present_means_the_source_has_rows(self):
        memory_store._get_conn()
        for name in rm.WORKSPACES:
            for sec in _snapshot(name)["sections"].values():
                if sec["state"] == rm.STATE_PRESENT:
                    assert sec["rows"] > 0
                if sec["state"] == rm.STATE_EMPTY:
                    assert sec["rows"] == 0


class TestTheRoutesServeWhatTheReadModelsReturn:
    def test_the_three_paths_are_registered(self):
        from alpha_agents.server import app as server_app
        paths = {getattr(r, "path", None) for r in server_app.app.routes}
        assert {"/api/trade-workspace", "/api/learn-journal",
                "/api/evolve-lab"} <= paths

    @pytest.mark.parametrize("path,handler,name", [
        ("/api/trade-workspace", "get_trade_workspace", "trade"),
        ("/api/learn-journal", "get_learn_journal", "learn"),
        ("/api/evolve-lab", "get_evolve_lab", "evolve"),
    ])
    def test_the_endpoint_serialises_the_read_model(self, path, handler, name):
        from alpha_agents.server import app as server_app
        response = asyncio.run(getattr(server_app, handler)())
        body = json.loads(response.body)
        assert body["workspace"] == name
        assert set(body) == {"workspace", "generated_at", "states",
                             "sections", "code"}
        assert body["states"] == _snapshot(name)["states"]


class TestPortfolioIsTheTradeBook:
    def test_the_route_is_a_delegation_not_a_second_assembly(self):
        """Two assemblies of one book drift; the route may not be one of them."""
        import inspect
        from alpha_agents.server import app as server_app
        source = inspect.getsource(server_app.get_portfolio_api)
        assert "trade.book" in source
        assert "get_closed_positions" not in source

    def test_it_returns_the_same_payload_shape(self):
        response = asyncio.run(
            __import__("alpha_agents.server.app", fromlist=["app"])
            .get_portfolio_api())
        body = json.loads(response.body)
        assert set(body) == {"pending", "positions", "closed", "stats",
                             "theses", "capital", "traders"}


class TestTheEvolvePayloadCarriesTheGap:
    """The difference between "no data yet" and "this build cannot produce
    that data" is not in the database, so it travels in the payload."""

    def test_the_registered_producers_are_reported(self):
        from alpha_agents.evolution import shadow
        code = _snapshot("evolve")["code"]
        assert code["producers"] == sorted(shadow.PRODUCERS)
        assert code["baseline"] == shadow.BASELINE_NAME

    def test_this_build_registers_exactly_one_candidate_producer(self):
        """Phase 4's boundary, pinned mechanically — and it flipped.

        A promotion accepts only `candidate_policy` evidence. This test used to
        assert that *no* candidate was registered, so every verdict the build
        could produce was `baseline_only` and nothing was promotable. A
        candidate was registered on 2026-09-13, the test went red as its
        docstring asked it to, and what it pins now is the count: one candidate
        is what makes the promotion path reachable, and two would mean a verdict
        could describe a run the reader has no reason to think was chosen.
        """
        from alpha_agents.data import policy_registry as pr
        from alpha_agents.evolution import shadow
        code = _snapshot("evolve")["code"]
        assert code["candidate_producers"] == [shadow.CANDIDATE_NAME]
        assert code["reachable"] is True
        assert code["promotion_accepts"] == pr.SCOPE_CANDIDATE
        assert code["promotion_accepts"] == "candidate_policy"


class TestAnEmptyDatabaseIsALegalAnswer:
    def test_every_workspace_answers_without_raising(self):
        payloads = {name: _snapshot(name) for name in rm.WORKSPACES}
        assert len(payloads) == 3

    def test_trade_still_names_its_book_in_a_fresh_database(self):
        """`virtual_portfolio` comes from _SCHEMA, so it is always readable —
        the state is `empty`, which is a different sentence from `absent`."""
        book = _snapshot("trade")["sections"]["book"]
        assert book["schema"] == rm.SCHEMA_COMPLETE
        assert book["state"] == rm.STATE_EMPTY
        assert book["value"]["capital"]["total"] >= 0
