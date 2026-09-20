"""Selection shadow freezes forward opportunity evidence at a sample floor."""

import sqlite3

import pytest

from alpha_agents.data import memory_store, opportunity_journal as OJ
from alpha_agents.data import policy_registry as PR, scoring
from alpha_agents.evolution import selection_shadow as SS


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(
        memory_store, "MEMORY_DB_PATH", tmp_path / "memory.db", raising=False)
    monkeypatch.setattr(memory_store._local, "conn", None, raising=False)
    conn = memory_store._get_conn()
    yield conn
    current = getattr(memory_store._local, "conn", None)
    if current is not None:
        current.close()
    memory_store._local.conn = None


def _sources(share):
    decision = dict(scoring.DEFAULT_DECISION_PARAMS)
    decision["selection_rank"] = {"change_share": share}
    return {
        "prompts": {"t1_decide.md": "same"},
        "model": {"agent_model": "same"},
        "retrieval": {"budget": 400},
        "rules": {"minimum_samples": 20},
        "knowledge": {"snapshot_id": None},
        "decision": decision,
    }


def _history():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE daily_kline (code TEXT, date TEXT, open REAL, high REAL, "
        "low REAL, close REAL, volume INTEGER, PRIMARY KEY(code,date))")
    dates = [
        "2025-12-01", "2025-12-02", "2025-12-03", "2025-12-04",
        "2025-12-05", "2025-12-08", "2025-12-09", "2025-12-10",
        "2025-12-11", "2025-12-12", "2025-12-15", "2025-12-16",
        "2025-12-17", "2025-12-18", "2025-12-19", "2025-12-22",
        "2025-12-23", "2025-12-24", "2025-12-25", "2025-12-26",
        "2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08",
        "2026-01-09", "2026-01-12",
    ]
    paths = {
        "600001": [10, 10.1, 10.2, 10.3, 10.4, 10.5],
        "600002": [10, 10.5, 11.0, 11.5, 12.0, 12.5],
        "600003": [10, 9.8, 9.6, 9.4, 9.2, 9.0],
        "600004": [10, 10.4, 10.8, 11.2, 11.6, 12.0],
    }
    for code, values in paths.items():
        for day in dates[:20]:
            conn.execute(
                "INSERT INTO daily_kline VALUES (?,?,?,?,?,?,?)",
                (code, day, 10, 10.1, 9.9, 10, 1_000_000))
        for day, close in zip(dates[20:], values):
            conn.execute(
                "INSERT INTO daily_kline VALUES (?,?,?,?,?,?,?)",
                (code, day, 10 if day == dates[20] else close,
                 close * 1.01, close * 0.99, close, 1_000_000))
    conn.commit()
    return conn


def _versions(store):
    parent = PR.freeze(
        sources=_sources(0.5), created_by="test",
        reason="parent", frozen_at="2026-01-01")
    PR.install(version_id=parent, actor="test", reason="seed")
    variant = PR.freeze(
        sources=_sources(0.8), parent_id=parent, created_by="test",
        reason="challenger", frozen_at="2026-01-02")
    return parent, variant


def _set(store, day="2026-01-05"):
    return OJ.record_decision(
        run_id="r", trader_id="t", day=day, phase="open",
        information_cutoff=f"{day} 09:00:00",
        panel=[
            {"code": "600001", "change_pct": 4.0, "turnover_rate": 1.0},
            {"code": "600002", "change_pct": 1.0, "turnover_rate": 9.0},
        ],
        orders=[{"code": "600001", "reason": "pick"}],
        refusals=[], research={"deep_dive_names": ["600001"]},
        parse_error=None, raw="{}",
        context={
            "ranking_day": "2025-12-26",
            "panel_limit": 2,
            "candidate_pool": [
                {"code": "600001", "change_pct": 4.0, "turnover_rate": 1.0,
                 "change_rank": 0, "turnover_rank": 3},
                {"code": "600003", "change_pct": 3.0, "turnover_rate": 2.0,
                 "change_rank": 1, "turnover_rank": 2},
                {"code": "600004", "change_pct": 2.0, "turnover_rate": 8.0,
                 "change_rank": 2, "turnover_rank": 1},
                {"code": "600002", "change_pct": 1.0, "turnover_rate": 9.0,
                 "change_rank": 3, "turnover_rank": 0},
            ],
        },
        conn=store)


def test_run_scores_only_forward_parent_sets_and_seals(store, monkeypatch):
    parent, variant = _versions(store)
    monkeypatch.setattr(SS.clock, "today", lambda: "2026-01-04")
    run = SS.open_run(
        parent_version_id=parent, variant_version_id=variant,
        minimum_sets=1, conn=store)
    _set(store)
    history = _history()

    got = SS.process(run_id=run, history_conn=history, conn=store)
    assert got["sample_count"] == 1
    assert got["sealed"] is True
    summary = SS.summary(run, store)
    assert summary["seal"]["summary"]["promotion_eligible"] is False
    assert summary["seal"]["summary"]["sample_count"] == 1


def test_sealed_run_never_grows(store, monkeypatch):
    parent, variant = _versions(store)
    monkeypatch.setattr(SS.clock, "today", lambda: "2026-01-04")
    run = SS.open_run(
        parent_version_id=parent, variant_version_id=variant,
        minimum_sets=1, conn=store)
    _set(store)
    history = _history()
    SS.process(run_id=run, history_conn=history, conn=store)
    before = SS.summary(run, store)["sample_count"]

    _set(store, day="2026-01-06")
    got = SS.process(run_id=run, history_conn=history, conn=store)
    assert got["new_rows"] == 0
    assert SS.summary(run, store)["sample_count"] == before


def test_unrelated_gene_is_refused(store):
    parent, _ = _versions(store)
    decision = dict(scoring.DEFAULT_DECISION_PARAMS)
    decision["confidence_priors"] = {
        **scoring.DEFAULT_DECISION_PARAMS["confidence_priors"],
        "high": 0.7,
    }
    variant = PR.freeze(
        sources={**_sources(0.5), "decision": decision},
        parent_id=parent, created_by="test",
        reason="wrong gene", frozen_at="2026-01-02")
    with pytest.raises(SS.SelectionShadowError, match="confidence_priors"):
        SS.open_run(
            parent_version_id=parent, variant_version_id=variant,
            conn=store)


def test_open_run_refuses_caller_supplied_backdated_start(store):
    parent, variant = _versions(store)
    with pytest.raises(
            SS.SelectionShadowError, match="writer-controlled"):
        SS.open_run(
            parent_version_id=parent,
            variant_version_id=variant,
            opened_on="2020-01-01",
            conn=store)


def test_process_stops_exactly_at_preregistered_sample_floor(
        store, monkeypatch):
    parent, variant = _versions(store)
    monkeypatch.setattr(SS.clock, "today", lambda: "2026-01-04")
    run = SS.open_run(
        parent_version_id=parent, variant_version_id=variant,
        minimum_sets=2, conn=store)

    # Three eligible future sets arrive before the next process() call.
    for day in ("2026-01-05", "2026-01-06", "2026-01-07"):
        _set(store, day=day)
    history = _history()

    got = SS.process(run_id=run, history_conn=history, conn=store)
    assert got["sealed"] is True
    assert got["sample_count"] == 2
    assert got["new_rows"] == 2
    assert store.execute(
        "SELECT COUNT(*) FROM selection_shadow_rows WHERE run_id=?",
        (run,)).fetchone()[0] == 2

    # The third matured set remains outside this sealed experiment.
    total_sets = store.execute(
        "SELECT COUNT(*) FROM opportunity_sets WHERE day>'2026-01-04'"
    ).fetchone()[0]
    assert total_sets == 3
    assert SS.summary(run, store)["sample_count"] == 2


def test_seal_refuses_legacy_overfilled_unsealed_run(store, monkeypatch):
    parent, variant = _versions(store)
    monkeypatch.setattr(SS.clock, "today", lambda: "2026-01-04")
    run = SS.open_run(
        parent_version_id=parent, variant_version_id=variant,
        minimum_sets=1, conn=store)
    # Simulate a pre-fix database already contaminated past its floor.
    SS.init_schema(store)
    for index in (1, 2):
        payload = {
            "variant_minus_parent_mean": 0.1,
            "behavior_changed_sets": 1,
        }
        store.execute(
            "INSERT INTO selection_shadow_rows "
            "(run_id,opportunity_set_id,day,result_json,result_hash,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (run, 10_000 + index, f"2026-01-0{4 + index}",
             SS._dump(payload), SS._hash(payload), "2026-01-10"))
    store.commit()

    with pytest.raises(
            SS.SelectionShadowError, match="already exceeds"):
        SS.process(run_id=run, history_conn=_history(), conn=store)
