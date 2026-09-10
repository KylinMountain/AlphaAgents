"""今天这个市场能不能做 —— 选票之前的那个问题。

系统一直在存涨停/炸板/跌停三个池子，一天上万行，然后把原始行丢给
agent，指望它自己想出「炸板率」这个概念。真实交易员的经验有很大一部分
就是知道该做哪些除法；这些测试盯住那几个除法本身，以及一件更重要的
事——数据缺失时不能被读成「市场冷清」。
"""

import json
import sqlite3

import pytest

from alpha_agents.tools import limit_ladder as L


@pytest.fixture()
def snaps(tmp_path, monkeypatch):
    db = tmp_path / "snap.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE limit_pool_snapshots(
            captured_at TEXT, code TEXT, pool_type TEXT, name TEXT,
            change_pct REAL, turnover_rate REAL, seal_amount_yi REAL,
            first_seal_time TEXT, break_count INTEGER,
            consecutive_limits INTEGER, sector TEXT);
        CREATE TABLE all_quote_snapshots(
            captured_at TEXT, code TEXT, name TEXT, price REAL,
            change_pct REAL);
    """)
    monkeypatch.setattr(L, "_DB", db, raising=False)
    yield conn
    conn.close()


def seal(conn, day, code, pool="up", boards=1, seal_yi=1.0,
         first="092500", breaks=0, sector="电力"):
    conn.execute("INSERT INTO limit_pool_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (f"{day} 14:55", code, pool, f"名{code}", 10.0, 5.0,
                  seal_yi, first, breaks, boards, sector))
    conn.commit()


class TestTheDivisionsNobodyWasMaking:
    def test_break_rate_counts_attempts_not_just_seals(self, snaps):
        """炸板率的分母是「封板尝试」，不是「涨停数」——否则一个
        全是炸板的日子会算出 100% 以上。"""
        for i in range(3):
            seal(snaps, "2026-09-10", f"00000{i}")
        for i in range(2):
            seal(snaps, "2026-09-10", f"10000{i}", pool="broken")
        d = json.loads(L.get_limit_ladder_fn("2026-09-10"))
        assert d["涨停"] == 3 and d["炸板"] == 2
        assert d["炸板率"] == 40.0

    def test_the_ladder_shows_its_shape(self, snaps):
        seal(snaps, "2026-09-10", "000001", boards=4)
        seal(snaps, "2026-09-10", "000002", boards=2)
        seal(snaps, "2026-09-10", "000003", boards=1)
        d = json.loads(L.get_limit_ladder_fn("2026-09-10"))
        assert d["最高板"] == 4
        assert d["梯队"] == {"4板": 1, "2板": 1, "1板": 1}

    def test_early_seals_are_separated_from_late_ones(self, snaps):
        """9:25 一字和 14:00 才封是两种东西，第二天完全不同命运。"""
        seal(snaps, "2026-09-10", "000001", first="092500")
        seal(snaps, "2026-09-10", "000002", first="140000")
        d = json.loads(L.get_limit_ladder_fn("2026-09-10"))
        assert d["早盘封板占比"] == 50.0

    def test_one_stock_is_counted_once_however_many_snapshots(self, snaps):
        """快照每 5 分钟写一遍，一只涨停股一天几十行。"""
        for t in ("09:35", "10:35", "14:55"):
            snaps.execute(
                "INSERT INTO limit_pool_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (f"2026-09-10 {t}", "000001", "up", "X", 10.0, 5.0, 1.0,
                 "092500", 0, 2, "电力"))
        snaps.commit()
        assert json.loads(L.get_limit_ladder_fn("2026-09-10"))["涨停"] == 1


class TestMoneyEffect:
    def test_it_reads_yesterdays_winners_today(self, snaps):
        """唯一直接回答「跟进能不能赚钱」的数字。"""
        seal(snaps, "2026-09-09", "000001")
        seal(snaps, "2026-09-09", "000002")
        seal(snaps, "2026-09-10", "000003")
        for code, chg in (("000001", -4.0), ("000002", 2.0)):
            snaps.execute("INSERT INTO all_quote_snapshots VALUES (?,?,?,?,?)",
                          ("2026-09-10 14:55", code, "X", 10.0, chg))
        snaps.commit()
        me = json.loads(L.get_limit_ladder_fn("2026-09-10"))["赚钱效应"]
        assert me["available"] and me["n"] == 2
        assert me["avg_change_pct"] == -1.0
        assert me["positive_pct"] == 50.0

    def test_a_missing_baseline_says_so_rather_than_guessing(self, snaps):
        seal(snaps, "2026-09-10", "000001")
        me = json.loads(L.get_limit_ladder_fn("2026-09-10"))["赚钱效应"]
        assert me["available"] is False and me["reason"]


class TestAbsenceIsNotCalm:
    def test_no_data_is_reported_as_no_data(self, snaps):
        """空池子必须读作「没采集到」，不能读作「今天市场冷清」——
        后者会让 agent 在数据缺失的日子做出自信的判断。"""
        d = json.loads(L.get_limit_ladder_fn("2026-09-10"))
        assert "error" in d and "不要据此判断市场冷清" in d["error"]

    def test_a_broken_database_returns_an_error_not_an_exception(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(L, "_DB", tmp_path / "nope.db", raising=False)
        assert "error" in json.loads(L.get_limit_ladder_fn("2026-09-10"))
