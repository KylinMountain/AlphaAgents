"""Per-trade reviews: numbers from bars, words from the trader, read back.

The case is 三六零 from the 2026-01 default-trader replay: bought 13.19, the
session high on the buy day +6.9%, closed −4.8%. The review has to say that
the peak came on the buy day and that 11.7 points were handed back — the
number the trader had never been shown.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from alpha_agents.data import memory_store
from alpha_agents.evolution import trade_review as TR


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
    rows = [  # 601360: T-1 up 3.3%, peak on the buy day, stopped two days on
        ("601360", "2026-01-14", 12.3, 12.8, 12.2, 12.40),
        ("601360", "2026-01-15", 12.4, 12.9, 12.3, 12.81),
        ("601360", "2026-01-16", 13.19, 14.10, 13.0, 13.30),
        ("601360", "2026-01-19", 13.2, 13.3, 12.5, 12.56),
    ]
    h.executemany("INSERT INTO daily_kline VALUES (?,?,?,?,?,?)", rows)
    yield h
    h.close()


def _close(conn, pid=1, code="601360", open_date="2026-01-16",
           close_date="2026-01-19", entry=13.19, ret=-4.78, trader="default"):
    conn.execute(
        "INSERT INTO virtual_portfolio (id, code, name, theme, order_date, "
        "open_date, open_price, shares, status, close_date, close_price, "
        "return_pct, reason, close_reason, trader_id) VALUES "
        "(?, ?, '三六零', 'AI', ?, ?, ?, 100, 'stopped', ?, 12.56, ?, "
        "'大资金低换手吸纳', '跌破止损', ?)",
        (pid, code, open_date, open_date, entry, close_date, ret, trader))
    conn.commit()


def _pos(conn, pid=1):
    return dict(conn.execute("SELECT * FROM virtual_portfolio WHERE id=?",
                             (pid,)).fetchone())


class TestTheFactsAreTheBars:
    def test_peak_session_and_giveback(self, conn, hist):
        _close(conn)
        f = TR.facts(_pos(conn), hist)
        assert f["peak_session"] == 0, "the high came on the buy day"
        assert f["peak_pct"] == pytest.approx(6.9, abs=0.01)
        assert f["giveback_pp"] == pytest.approx(6.9 + 4.78, abs=0.01)
        assert f["t1_change_pct"] == pytest.approx(3.31, abs=0.01)
        assert f["worst_pct"] < 0
        assert "买入当天" in TR.facts_line(f)

    def test_a_close_without_its_days_bar_waits(self, conn, hist):
        """Live 15:30: today's K-line lands at 17:30."""
        _close(conn, close_date="2026-01-20")
        assert TR.facts(_pos(conn), hist) is None


class TestWritingAReview:
    def test_no_model_still_stores_the_facts(self, conn, hist):
        _close(conn)
        n = asyncio.run(TR.review_closed(conn, hist, trader_id="default",
                                         as_of="2026-01-19", model=None))
        assert n == 1
        block = TR.inject(conn, "default")
        assert "吐回" in block and "1/1 笔的最高点就在买入当天" in block

    def test_a_position_is_reviewed_once(self, conn, hist):
        _close(conn)
        for _ in range(3):
            asyncio.run(TR.review_closed(conn, hist, trader_id="default",
                                         as_of="2026-01-19", model=None))
        assert conn.execute("SELECT COUNT(*) FROM trade_reviews").fetchone()[0] == 1

    def test_the_traders_words_are_shown_with_the_numbers(self, conn, hist, monkeypatch):
        _close(conn)

        async def words(f, *, model, trader=None, rules=""):
            return {"verdict": "错", "right": "", "wrong": "买入当天+6.9%没走",
                    "next_time": "买入当天冲高超过1个ATR先兑现一半"}
        monkeypatch.setattr(TR, "write_words", words)
        asyncio.run(TR.review_closed(conn, hist, trader_id="default",
                                     as_of="2026-01-19", model=object()))
        block = TR.inject(conn, "default")
        assert "下次：买入当天冲高超过1个ATR先兑现一半" in block
        assert "做错：买入当天+6.9%没走" in block

    def test_an_unreadable_reply_is_no_words_not_an_error(self):
        assert TR._parse("我觉得还行") == TR.NO_WORDS


class TestReadingItBack:
    def test_a_replay_morning_reads_only_earlier_closes(self, conn, hist):
        _close(conn)
        asyncio.run(TR.review_closed(conn, hist, trader_id="default",
                                     as_of="2026-01-19", model=None))
        assert TR.inject(conn, "default", before="2026-01-19") == ""
        assert TR.inject(conn, "default", before="2026-01-20") != ""

    def test_one_traders_reviews_are_its_own(self, conn, hist):
        _close(conn, trader="pullback")
        asyncio.run(TR.review_closed(conn, hist, trader_id="pullback",
                                     as_of="2026-01-19", model=None))
        assert TR.inject(conn, "default") == ""
        assert TR.inject(conn, "pullback") != ""

    def test_the_morning_context_carries_the_reviews(self, conn, hist):
        from alpha_agents.evolution.context_builder import build_morning_context
        _close(conn)
        asyncio.run(TR.review_closed(conn, hist, trader_id="default",
                                     as_of="2026-01-19", model=None))
        ctx = build_morning_context(themes=[], stats="", trader_id="default",
                                    knowledge="")
        assert "【你自己的逐笔复盘】" in ctx
