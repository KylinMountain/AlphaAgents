"""The trader's close-of-day market read, and the watchlist the market grades."""

from __future__ import annotations

import asyncio
import json
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


def _store(conn, day="2026-01-05"):
    MR.ensure(conn)
    conn.execute(
        "INSERT INTO market_reviews (trader_id, date, facts, review_json) VALUES "
        "('default', ?, 'f', ?)",
        (day, json.dumps({"market": "普涨", "themes": "算力", "boards": [
            {"name": "算力", "kind": "错过", "driver": "资金",
             "verdict": "看到了但嫌涨多了没选", "lesson": "资金连续3天流入就先买半仓"}]},
            ensure_ascii=False)))
    conn.commit()


class TestTheMorningReadsItAsYesterdays:
    def test_labelled_as_yesterdays_diagnosis_not_todays_list(self, conn):
        _store(conn)
        text = MR.inject(conn, "default", before="2026-01-06")
        assert "【昨日复盘（2026-01-05 收盘后你自己写的）】" in text
        assert "不是今天的买入名单" in text
        assert "◆ [错过] 算力" in text and "教训：资金连续3天流入就先买半仓" in text
        assert "观察" not in text

    def test_nothing_from_the_same_day(self, conn):
        _store(conn)
        assert MR.inject(conn, "default", before="2026-01-05") == ""

    def test_an_old_review_still_reads(self, conn):
        MR.ensure(conn)
        conn.execute("INSERT INTO market_reviews (trader_id, date, facts, review_json) "
                     "VALUES ('default', '2026-01-05', 'f', ?)",
                     ('{"boards": [{"name": "PCB", "why_missed": "没看到", '
                      '"next_time": "看涨停扩散"}], "watchlist": [{"code": "600001"}]}',))
        text = MR.inject(conn, "default", before="2026-01-06")
        assert "诊断：没看到｜教训：看涨停扩散" in text and "600001" not in text


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

    def test_no_watchlist_and_an_unknown_kind_is_blank(self):
        reply = json.dumps({"market": "m", "themes": "t",
                            "boards": [{"name": "算力 +3.2%", "kind": "瞎猜"}],
                            "watchlist": [{"code": "600001"}]})
        got = MR._parse(reply)
        assert "watchlist" not in got
        assert got["boards"][0]["name"] == "算力" and got["boards"][0]["kind"] == ""

    def test_an_unescaped_quote_in_a_text_field_is_repaired(self):
        reply = ('{"market":"m","themes":"属于"老题材+新催化"，质量更高",'
                 '"boards":[{"name":"化工","kind":"错过"}]}')
        got = MR._parse(reply)
        assert got == {"market": "m", "themes": "属于\"老题材+新催化\"，质量更高",
                       "boards": [{"name": "化工", "kind": "错过", "driver": "",
                                   "evidence": "", "morning": "", "verdict": "",
                                   "lesson": ""}]}

    def test_the_days_record_reaches_the_model(self, conn, monkeypatch):
        import agents
        seen = {}

        class _R:
            final_output = '{"market": "m", "boards": []}'

        async def run(agent, message, max_turns=2):
            seen["message"] = message
            return _R()
        monkeypatch.setattr(agents.Runner, "run", staticmethod(run))
        asyncio.run(MR.write(conn, trader_id="default", date="2026-01-05",
                             facts="宽度", model="stub-model",
                             record="早盘选中的方向：\n- 算力：资金流入"))
        assert "## 你今天的记录" in seen["message"]
        assert "算力：资金流入" in seen["message"]


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


class TestBoardNamesMatchTheConceptTable:
    """The 2026-01 replay's review named "脑机接口 +10.67%": no concept is
    called that, so none of the boards it named reached the shortlist."""

    @pytest.mark.parametrize("raw,want", [
        ("脑机接口 +10.67%", "脑机接口"),
        ("海南自贸区 -2.93%", "海南自贸区"),
        ("PCB概念（+1.42%）", "PCB概念"),
        ("共封装光学(CPO)", "共封装光学(CPO)"),
        ("中国AI 50", "中国AI 50"),   # a concept's own number stays
    ])
    def test_the_move_is_stripped(self, raw, want):
        assert MR.board_name(raw) == want
