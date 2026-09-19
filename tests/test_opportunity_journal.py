"""Opportunity Journal preserves selected and unselected evidence honestly."""

import sqlite3

import pytest

from alpha_agents.data import memory_store
from alpha_agents.data import opportunity_journal as OJ


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


PANEL = [
    {"code": "600001", "name": "A", "change_pct": 3.0},
    {"code": "600002", "name": "B", "change_pct": 2.0},
    {"code": "600003", "name": "C", "change_pct": 1.0},
    {"code": "600004", "name": "D", "change_pct": 0.0},
]


def test_it_separates_selected_researched_and_ignored(store):
    set_id = OJ.record_decision(
        run_id="r1", trader_id="t", day="2026-09-19", phase="open",
        information_cutoff="2026-09-19 09:00:00", panel=PANEL,
        orders=[{"code": "600001", "reason": "best setup"}],
        refusals=[],
        research={"deep_dive_names": ["600001", "600002"]},
        parse_error=None, raw='{"orders": []}', conn=store)

    rows = {r["code"]: r for r in OJ.items(set_id, store)}
    assert rows["600001"]["status"] == "agent_selected"
    assert rows["600001"]["selected"] == 1
    assert rows["600002"]["status"] == "researched_not_selected"
    assert rows["600003"]["status"] == "offered_not_researched"


def test_parser_refusal_is_not_called_a_market_rejection(store):
    set_id = OJ.record_decision(
        run_id="r1", trader_id="t", day="2026-09-19", phase="open",
        information_cutoff="2026-09-19 09:00:00", panel=PANEL,
        orders=[], refusals=[
            {"code": "600003", "why": "bad_prices", "detail": "missing"}],
        research={"deep_dive_names": ["600003"]},
        parse_error=None, raw="x", conn=store)

    row = {r["code"]: r for r in OJ.items(set_id, store)}["600003"]
    assert row["status"] == "agent_attempt_refused"
    assert "bad_prices" in row["reason"]


def test_unreadable_reply_does_not_invent_rejections(store):
    set_id = OJ.record_decision(
        run_id="r1", trader_id="t", day="2026-09-19", phase="close",
        information_cutoff="2026-09-19 14:55:00", panel=PANEL,
        orders=[], refusals=[], research=None,
        parse_error="bad json", raw="not json", conn=store)

    assert {r["status"] for r in OJ.items(set_id, store)} == {
        "unreadable_decision"}


def test_history_is_append_only(store):
    set_id = OJ.record_decision(
        run_id="r1", trader_id="t", day="2026-09-19", phase="open",
        information_cutoff="2026-09-19 09:00:00", panel=PANEL,
        orders=[], refusals=[], research=None,
        parse_error=None, raw="{}", conn=store)

    with pytest.raises(sqlite3.DatabaseError):
        store.execute(
            "UPDATE opportunity_items SET status='agent_selected' "
            "WHERE opportunity_set_id=?", (set_id,))
