"""A theme's core stocks are its members ranked by money, not by stock code.

On 2026-09-23 PCB概念's "core" was TCL科技, 铜陵有色, 格力电器 — the first
semantic-search matches, in code order — and 28 of 32 themes had none.
"""

from __future__ import annotations

import sqlite3

import pytest

from alpha_agents import config
from alpha_agents.data import theme_members as TM


@pytest.fixture()
def dbs(tmp_path, monkeypatch):
    stocks = tmp_path / "stocks.db"
    s = sqlite3.connect(stocks)
    s.executescript("""
        CREATE TABLE concepts (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE concept_stocks (concept_id INTEGER, stock_code TEXT);
        CREATE TABLE stocks (code TEXT, name TEXT, is_st INTEGER);
        INSERT INTO concepts VALUES (1, 'PCB概念');
        INSERT INTO concept_stocks VALUES (1,'000100'),(1,'002436'),(1,'002916'),(1,'600000');
        INSERT INTO stocks VALUES ('000100','TCL科技',0),('002436','兴森科技',0),
                                  ('002916','深南电路',0),('600000','某ST',1);
    """)
    s.commit(); s.close()
    snaps = tmp_path / "market_snapshots.db"
    f = sqlite3.connect(snaps)
    f.execute("CREATE TABLE stock_fund_flow_daily (trade_date TEXT, code TEXT, net_amount REAL)")
    rows = []
    for d in ("20260917", "20260918", "20260919", "20260922", "20260923"):
        rows += [(d, "000100", -100.0), (d, "002436", 1800.0),
                 (d, "002916", 600.0), (d, "600000", 9999.0)]
    rows.append(("20260901", "000100", 1e9))  # outside the 5-session window
    f.executemany("INSERT INTO stock_fund_flow_daily VALUES (?,?,?)", rows)
    f.commit(); f.close()
    monkeypatch.setattr(config, "DB_PATH", stocks, raising=False)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path, raising=False)


def test_ranked_by_recent_money_leader_first(dbs):
    got = TM.core_stocks("PCB概念")
    assert [s["name"] for s in got] == ["兴森科技", "深南电路", "TCL科技"]
    assert got[0]["role"] == "龙头" and got[1]["role"] == "核心"


def test_st_members_are_left_out(dbs):
    assert "某ST" not in [s["name"] for s in TM.core_stocks("PCB概念")]


def test_a_theme_that_is_not_a_concept_has_none(dbs):
    assert TM.core_stocks("盘中异动造的名字") == []
