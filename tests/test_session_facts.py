"""The session as a trader reads it, from daily bars alone.

The 2026-01 replays had no limit-up pool on 0 of 30 days, so "emotion or
money" could not be asked. These pin the reconstruction from bars.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from alpha_agents.evolution import session_facts as SF

DAYS = ["2026-01-05", "2026-01-06", "2026-01-07"]


@pytest.fixture()
def hist():
    h = sqlite3.connect(":memory:")
    h.execute("CREATE TABLE daily_kline (code TEXT, date TEXT, open REAL, "
              "high REAL, low REAL, close REAL)")

    def put(code, closes, highs=None):
        highs = highs or closes
        for d, c, hi in zip(DAYS, closes, highs):
            h.execute("INSERT INTO daily_kline VALUES (?,?,?,?,?,?)",
                      (code, d, c, hi, c, c))
    put("600001", [10.0, 11.0, 12.1])           # main board: 2 limit-ups in a row
    put("300001", [10.0, 10.5, 12.6])           # ChiNext: +20% is its limit
    put("600002", [10.0, 10.0, 10.5], highs=[10, 10, 11.0])  # hit 11.0, closed 10.5: broken
    put("600003", [10.0, 10.0, 9.0])            # limit-down
    for i in range(4, 16):                      # the rest of a concept, flat
        put(f"6000{i:02d}", [10.0, 10.0, 10.0])
    yield h
    h.close()


MEMBERS = {"算力": ["600001", "300001", "600002"] + [f"6000{i:02d}" for i in range(4, 12)],
           "地产": ["600003"] + [f"6000{i:02d}" for i in range(4, 14)]}


def test_limit_ups_streaks_and_broken_come_from_bars(hist):
    f = SF.compute(hist, day="2026-01-07", members=MEMBERS)
    m = f["market"]
    assert m["limit_up"] == 2, "600001 at 10%, 300001 at 20%"
    assert m["broken"] == 1 and m["limit_down"] == 1
    assert m["max_streak"] == 2
    assert m["ladder"][0][1] == "600001"


def test_a_board_carries_its_evidence_and_what_we_did(hist):
    f = SF.compute(hist, day="2026-01-07", members=MEMBERS,
                   names={"300001": "甲"},  # +20% leads the board
                   news_titles=["算力租赁再获订单", "甲公司公告", "无关"],
                   ours={"算力": SF.SEEN})
    top = f["top"][0]
    assert top["name"] == "算力" and top["limit_up"] == 2
    assert top["news"] == 2, "names the board or its leader"
    assert top["ours"] == SF.SEEN
    assert "——你今天：看到没选" in SF.render(f)
    unseen = next(b for b in f["top"] + f["bottom"] if b["name"] == "地产")
    assert unseen["ours"] == SF.UNSEEN


def test_nothing_after_the_day_is_read(hist):
    """A replay's market history holds the future; the day's facts must not."""
    f = SF.compute(hist, day="2026-01-06", members=MEMBERS)
    assert f["market"]["max_streak"] == 1


def test_exposure_line_names_the_cost_of_being_out():
    line = SF.exposure_line([("d1", 3.0), ("d2", 0.0)], 6.8, 0.02)
    assert "平均仓位 1.5%，空仓 1 天" in line and "+6.8%" in line


class TestTheHandbookSeesWhatWasMissed:
    def test_the_rewrite_is_handed_the_misses(self, tmp_path, monkeypatch, hist):
        from alpha_agents import config
        from alpha_agents.data import memory_store
        from alpha_agents.evolution import close_day, handbook, market_review

        monkeypatch.setattr(config, "DATA_DIR", tmp_path, raising=False)
        monkeypatch.setattr(memory_store, "MEMORY_DB_PATH", tmp_path / "m.db",
                            raising=False)
        monkeypatch.setattr(memory_store._local, "conn", None, raising=False)
        conn = memory_store._get_conn()

        async def write(conn, *, trader_id, date, facts, model, context=""):
            market_review.ensure(conn)
            conn.execute(
                "INSERT INTO market_reviews (trader_id, date, facts, review_json) "
                "VALUES (?, ?, ?, ?)", (trader_id, date, facts,
                '{"boards": [{"name": "算力", "driver": "情绪", '
                '"why_missed": "没看到", "next_time": "看涨停扩散"}], "watchlist": []}'))
            return {"boards": []}
        seen = {}

        async def consolidate(conn, trader_id, *, as_of, model, trader=None,
                              opportunity=""):
            seen["opportunity"] = opportunity
            return True
        monkeypatch.setattr(market_review, "write", write)
        monkeypatch.setattr(handbook, "consolidate", consolidate)
        got = asyncio.run(close_day.review_day(
            conn, hist, trader_id="default", trader=None, day="2026-01-07",
            model=None, facts_text="事实", exposure_text="平均仓位 3.2%"))
        assert got["market_review"] == 1 and got["handbook"] == 1
        assert "平均仓位 3.2%" in seen["opportunity"]
        assert "算力（情绪）：没看到｜下次：看涨停扩散" in seen["opportunity"]
        conn.close()
        memory_store._local.conn = None
