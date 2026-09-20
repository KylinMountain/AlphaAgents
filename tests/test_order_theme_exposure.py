"""Overlapping theme exposure is side-car attribution, not duplicate orders."""

import sqlite3

import pytest

from alpha_agents.data import memory_store
from alpha_agents.data import order_theme_exposure as E


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


def _order(store, *, code, theme, status, shares=0, order_id=None):
    cur = store.execute(
        "INSERT INTO virtual_portfolio "
        "(id,code,name,theme,order_date,entry_low,entry_high,stop_loss,"
        "status,shares,trader_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (order_id, code, code, theme, "2026-01-05", 9.0, 10.0, 8.0,
         status, shares, "t"),
    )
    return int(order_id or cur.lastrowid)


def test_one_position_counts_fully_in_each_overlapping_theme(store):
    order_id = _order(
        store, code="600001", theme="AI", status="open", shares=1000)
    E.record(
        order_id=order_id, primary_theme="AI",
        supporting_themes=["算力"], source="sector_first_v0", conn=store)

    got = E.snapshot(
        trader_id="t", price_map={"600001": 10.0},
        equity=100000.0, conn=store)
    assert got["theme_exposure_pct"] == {"AI": 10.0, "算力": 10.0}
    assert got["max_theme_cluster_exposure_pct"] == 10.0
    assert got["overlap_orders"] == 1
    assert got["complete"] is True


def test_pending_order_counts_held_reservation(store):
    order_id = _order(
        store, code="600002", theme="存储", status="pending")
    store.execute(
        "INSERT INTO reservations "
        "(order_id,trader_id,code,kind,amount,state) "
        "VALUES (?,?,?,?,?,?)",
        (order_id, "t", "600002", "cash_reserve", 20000.0, "held"),
    )
    E.record(
        order_id=order_id, primary_theme="存储",
        supporting_themes=[], source="sector_first_v0", conn=store)

    got = E.snapshot(
        trader_id="t", price_map={}, equity=100000.0, conn=store)
    assert got["max_theme_cluster_exposure_pct"] == 20.0
    assert got["complete"] is True


def test_pending_exposure_uses_cash_hold_not_risk_hold(store):
    order_id = _order(
        store, code="600005", theme="AI", status="pending")
    store.executemany(
        "INSERT INTO reservations "
        "(order_id,trader_id,code,kind,amount,state) "
        "VALUES (?,?,?,?,?,?)",
        [
            (order_id, "t", "600005", "cash_reserve", 20000.0, "held"),
            (order_id, "t", "600005", "theme_risk:AI", 90000.0, "held"),
        ],
    )
    E.record(
        order_id=order_id, primary_theme="AI",
        supporting_themes=[], source="sector_first_v0", conn=store)

    got = E.snapshot(
        trader_id="t", price_map={}, equity=100000.0, conn=store)
    assert got["theme_exposure_pct"] == {"AI": 20.0}
    assert got["complete"] is True


def test_missing_mark_is_unverified_not_zero(store):
    order_id = _order(
        store, code="600003", theme="AI", status="open", shares=1000)
    E.record(
        order_id=order_id, primary_theme="AI",
        source="sector_first_v0", conn=store)
    got = E.snapshot(
        trader_id="t", price_map={}, equity=100000.0, conn=store)
    assert got["complete"] is False
    assert got["missing"][0]["reason"] == "missing_open_mark_or_shares"


def test_sidecar_is_append_only(store):
    order_id = _order(
        store, code="600004", theme="AI", status="pending")
    E.record(
        order_id=order_id, primary_theme="AI",
        supporting_themes=["算力"], source="test", conn=store)
    with pytest.raises(sqlite3.DatabaseError):
        store.execute(
            "UPDATE order_theme_exposures SET theme='x' WHERE order_id=?",
            (order_id,))
