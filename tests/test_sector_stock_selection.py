"""Frozen B-arm directions and isolated stock-choice contracts."""

import json
import sqlite3

import pytest

from alpha_agents.agents import sector_stock_selector as S
from alpha_agents.data import frozen_direction_archive as F
from alpha_agents.data import memory_store
from alpha_agents.data import theme_opportunity_journal as J


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


def _snapshot(sector_id, rank):
    return {
        "sector_id": sector_id,
        "rank": rank,
        "membership_snapshot_id": "m1",
        "membership_hash": "h1",
    }


def test_export_and_replay_direction_choice_by_exact_world(store, tmp_path):
    J.record(
        run_id="B-run", trader_id="t", day="2026-01-05", phase="open",
        information_cutoff="2026-01-05 09:00:00",
        architecture="sector_first_v0",
        snapshots=[_snapshot("AI", 1), _snapshot("存储", 2)],
        shortlist=["AI", "存储"], selected=["AI"], conn=store)

    payload = F.export(run_id="B-run", conn=store)
    path = tmp_path / "directions.json"
    F.write(path, payload)
    loaded = F.load(path)
    got = F.decision_for(
        loaded,
        day="2026-01-05",
        information_cutoff="2026-01-05 09:00:00",
        membership_snapshot_id="m1",
        membership_hash="h1",
        shortlist=["AI", "存储"],
    )
    assert got["selected"] == ["AI"]
    assert got["source_run_id"] == "B-run"


def test_frozen_direction_refuses_changed_shortlist(store, tmp_path):
    J.record(
        run_id="B-run", trader_id="t", day="2026-01-05", phase="open",
        information_cutoff="2026-01-05 09:00:00",
        architecture="sector_first_v0",
        snapshots=[_snapshot("AI", 1), _snapshot("存储", 2)],
        shortlist=["AI", "存储"], selected=["AI"], conn=store)
    payload = F.export(run_id="B-run", conn=store)
    path = tmp_path / "directions.json"
    F.write(path, payload)
    with pytest.raises(F.FrozenDirectionError, match="shortlist changed"):
        F.decision_for(
            F.load(path),
            day="2026-01-05",
            information_cutoff="2026-01-05 09:00:00",
            membership_snapshot_id="m1",
            membership_hash="h1",
            shortlist=["存储", "AI"],
        )


def test_archive_hash_tampering_is_refused(store, tmp_path):
    J.record(
        run_id="B-run", trader_id="t", day="2026-01-05", phase="open",
        information_cutoff="2026-01-05 09:00:00",
        architecture="sector_first_v0",
        snapshots=[_snapshot("AI", 1)],
        shortlist=["AI"], selected=["AI"], conn=store)
    payload = F.export(run_id="B-run", conn=store)
    payload["days"]["2026-01-05"]["selected"] = []
    path = tmp_path / "directions.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(F.FrozenDirectionError, match="hash mismatch"):
        F.load(path)


def test_simple_selector_is_transparent_panel_prefix():
    panel = [
        {"code": "600001"},
        {"code": "600002"},
        {"code": "600003"},
    ]
    got = S.simple(panel, picks=2)
    assert [row["code"] for row in got["stocks"]] == ["600001", "600002"]
    assert {row["reason"] for row in got["stocks"]} == {
        "transparent_panel_order"}


def test_llm_stock_parser_never_creates_trade_terms():
    got = S.parse(
        '{"stocks":[{"code":"600001","reason":"best",'
        '"counterevidence":"extended","entry_low":9.9}]}',
        {"600001"}, picks=1)
    assert got["stocks"] == [{
        "code": "600001",
        "reason": "best",
        "counterevidence": "extended",
    }]
    assert "entry_low" not in got["stocks"][0]


def test_llm_stock_parser_requires_primary_theme_from_frozen_relations():
    got = S.parse(
        '{"stocks":[{"code":"600001","primary_theme":"算力",'
        '"reason":"best","counterevidence":"extended"}]}',
        {"600001"}, picks=1,
        offered_themes={"600001": ["AI", "算力"]},
    )
    assert got["parse_error"] is None
    assert got["stocks"][0]["primary_theme"] == "算力"

    missing = S.parse(
        '{"stocks":[{"code":"600001","reason":"best"}]}',
        {"600001"}, picks=1,
        offered_themes={"600001": ["AI", "算力"]},
    )
    assert missing["stocks"] == []
    assert missing["refused"][0]["why"] == "missing_primary_theme"

    outside = S.parse(
        '{"stocks":[{"code":"600001","primary_theme":"机器人","reason":"best"}]}',
        {"600001"}, picks=1,
        offered_themes={"600001": ["AI", "算力"]},
    )
    assert outside["stocks"] == []
    assert outside["refused"][0]["why"] == "invalid_primary_theme"


def test_simple_selector_carries_deterministic_primary_theme():
    panel = [{"code": "600001", "primary_theme": "AI"}]
    got = S.simple(panel, picks=1)
    assert got["stocks"][0]["primary_theme"] == "AI"
    assert got["research_trace"] == []
