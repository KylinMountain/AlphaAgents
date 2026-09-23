"""The trader's close-of-day market read, and the watchlist the market grades."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from unittest.mock import patch

import pandas as pd
import pytest

from alpha_agents.data import memory_store
from alpha_agents.evolution import market_review as MR


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_store, "MEMORY_DB_PATH", tmp_path / "memory.db",
                        raising=False)
    monkeypatch.setattr(memory_store._local, "conn", None, raising=False)
    c = memory_store._get_conn()
    yield c
    c.close()
    memory_store._local.conn = None


@pytest.fixture()
def hist():
    h = sqlite3.connect(":memory:")
    h.execute("CREATE TABLE daily_kline (code TEXT, date TEXT, open REAL, "
              "high REAL, low REAL, close REAL)")
    closes = {  # AAA is watched; BBB and CCC are the rest of the market
        "600001": [10.0, 11.0, 11.0, 12.0],
        "600002": [10.0, 10.0, 10.1, 10.2],
        "600003": [10.0, 10.0, 9.9, 10.0],
    }
    days = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]
    for code, cs in closes.items():
        for d, c in zip(days, cs):
            h.execute("INSERT INTO daily_kline VALUES (?,?,?,?,?,?)",
                      (code, d, c, c, c, c))
    yield h
    h.close()


def _store(conn, day="2026-01-05", codes=("600001",)):
    MR.ensure(conn)
    conn.execute(
        "INSERT INTO market_reviews (trader_id, date, facts, review_json) VALUES "
        "('default', ?, 'f', ?)",
        (day, json.dumps({"market": "普涨", "themes": "算力",
                          "watchlist": [{"code": c, "name": "甲", "why": "资金最强",
                                         "buy_if": "回踩不破10", "drop_if": "跌破9.8"}
                                        for c in codes]}, ensure_ascii=False)))
    conn.commit()


class TestTheWatchlistIsGradedByTheMarket:
    def test_one_and_three_session_excess(self, conn, hist):
        _store(conn)
        [g] = MR.grade(conn, hist, "default")
        # AAA +10% next day; market median of the three that day is 0%.
        assert g["d1"] == 10.0 and g["d1_excess"] == 10.0
        # 3 sessions: AAA +20%, BBB +2%, CCC 0% → median +2%.
        assert g["d3"] == 20.0 and g["d3_excess"] == 18.0

    def test_a_replay_morning_grades_only_seen_sessions(self, conn, hist):
        _store(conn)
        [g] = MR.grade(conn, hist, "default", before="2026-01-07")
        assert "d1" in g and "d3" not in g
        assert MR.grade(conn, hist, "default", before="2026-01-06") == []

    def test_the_grade_line_counts_winners(self, conn, hist):
        _store(conn)
        assert "次日跑赢大盘 1/1" in MR.grade_line(MR.grade(conn, hist, "default"))


class TestTheMorningReadsIt:
    def test_yesterdays_read_and_watchlist(self, conn, hist):
        _store(conn)
        text = MR.inject(conn, hist, "default", before="2026-01-06")
        assert "【你昨天收盘后写的盘面复盘（2026-01-05）】" in text
        assert "买入条件：回踩不破10" in text

    def test_nothing_from_the_same_day(self, conn, hist):
        _store(conn)
        assert MR.inject(conn, hist, "default", before="2026-01-05") == ""


class TestWriting:
    def test_an_unreadable_reply_writes_nothing(self, conn, monkeypatch):
        import agents

        class _R:
            final_output = "今天还行"

        async def run(agent, message, max_turns=2):
            return _R()
        monkeypatch.setattr(agents.Runner, "run", staticmethod(run))
        got = asyncio.run(MR.write(conn, trader_id="default", date="2026-01-05",
                                   facts="宽度", model="stub-model"))
        assert got is None
        assert conn.execute("SELECT COUNT(*) FROM market_reviews").fetchone()[0] == 0

    def test_a_bad_code_is_dropped_and_the_list_is_capped(self):
        reply = json.dumps({"market": "m", "themes": "t", "watchlist": [
            {"code": "abc"}] + [{"code": f"60000{i}"} for i in range(8)]})
        got = MR._parse(reply)
        assert len(got["watchlist"]) == MR.MAX_WATCH
        assert all(w["code"].isdigit() for w in got["watchlist"])


class TestTheFactsComeFromTheSnapshots:
    def test_breadth_boards_and_ladder(self):
        from alpha_agents.pipeline.tasks import close_review as CR
        up = pd.DataFrame([{"代码": "600001", "名称": "甲", "连板数": 3,
                            "所属行业": "电子"},
                           {"代码": "600002", "名称": "乙", "连板数": 1,
                            "所属行业": "电子"}])
        boards = [{"captured_at": "2026-01-05 15:00", "sector_name": "PCB",
                   "change_pct": 1.4, "net_flow_yi": 21.2, "leader": "甲",
                   "leader_change_pct": 20.0}]
        with patch("alpha_agents.data.snapshot_store.read_latest_breadth",
                   return_value={"captured_at": "2026-01-05 15:00", "advances": 1755,
                                 "declines": 3362, "flat": 99, "limit_up": 51,
                                 "limit_down": 16}), \
             patch("alpha_agents.data.snapshot_store.read_latest_sector_flow",
                   return_value=boards), \
             patch("alpha_agents.data.snapshot_store.read_limit_pool",
                   return_value=up):
            text = CR.market_facts("2026-01-05")
        assert "上涨 1755" in text and "PCB +1.40%" in text
        assert "领涨甲(+20.0%)" in text
        assert "甲(600001) 3板" in text and "电子 2只" in text

    def test_yesterdays_snapshot_is_not_todays_market(self):
        from alpha_agents.pipeline.tasks import close_review as CR
        with patch("alpha_agents.data.snapshot_store.read_latest_breadth",
                   return_value={"captured_at": "2026-01-04 15:00"}), \
             patch("alpha_agents.data.snapshot_store.read_latest_sector_flow",
                   return_value=[]), \
             patch("alpha_agents.data.snapshot_store.read_limit_pool",
                   return_value=None):
            assert CR.market_facts("2026-01-05") == ""
