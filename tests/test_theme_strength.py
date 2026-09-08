"""Theme strength is two numbers, and only one of them accumulates.

The intraday task re-scores every active theme every five minutes — 48
times between 09:30 and 15:00. When a single `strength` field absorbed
each of those deltas, a line with steady inflow reached the ceiling of 10
within half an hour and a weak one floored at 0, so the field tracked how
many cycles had elapsed rather than how strong anything was. That is what
cancelled 锡业股份's order for 金属铜 强度3 on a session that line ran
+2.0% against the market on 55億 of net inflow.
"""

import pytest

from alpha_agents.data.memory_store import get_theme_by_name, upsert_theme
from alpha_agents.pipeline.theme_manager import (
    evaluate_theme_signals, update_theme_strength,
)

TODAY = "2026-09-08"
TOMORROW = "2026-09-09"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    from alpha_agents.data import memory_store
    monkeypatch.setattr(memory_store, "MEMORY_DB_PATH",
                        tmp_path / "memory.db", raising=False)
    monkeypatch.setattr(memory_store._local, "conn", None, raising=False)
    yield
    conn = getattr(memory_store._local, "conn", None)
    if conn is not None:
        conn.close()
    memory_store._local.conn = None


def strong():
    """跑赢大盘 + 资金净流入 → bullish 2, bearish 0."""
    return evaluate_theme_signals(sector_name="金属铜", sector_change_pct=2.5,
                                  sector_fund_flow=55.4e8, market_change_pct=0.5)


def weak():
    """跑输大盘 + 资金净流出 → bearish 2, bullish 0."""
    return evaluate_theme_signals(sector_name="港口航运", sector_change_pct=-1.8,
                                  sector_fund_flow=-9e8, market_change_pct=0.5)


class TestSignalArithmetic:
    def test_strong_day_scores_plus_two(self):
        s = strong()
        assert s["bullish_signals"] - s["bearish_signals"] == 2

    def test_weak_day_scores_minus_two(self):
        s = weak()
        assert s["bullish_signals"] - s["bearish_signals"] == -2


class TestDailyGuard:
    def test_first_call_of_the_day_moves_strength(self, store):
        upsert_theme("金属铜", status="active", strength=5)
        update_theme_strength("金属铜", strong(), today=TODAY)
        t = get_theme_by_name("金属铜")
        assert t["strength"] == 7
        assert t["daily_score"] == 2
        assert t["last_scored_date"] == TODAY

    def test_forty_eight_cycles_move_it_once(self, store):
        """An intraday session's worth of calls, all on the same date."""
        upsert_theme("金属铜", status="active", strength=5)
        for _ in range(48):
            update_theme_strength("金属铜", strong(), today=TODAY)
        assert get_theme_by_name("金属铜")["strength"] == 7

    def test_the_next_day_moves_it_again(self, store):
        upsert_theme("金属铜", status="active", strength=5)
        update_theme_strength("金属铜", strong(), today=TODAY)
        update_theme_strength("金属铜", strong(), today=TOMORROW)
        t = get_theme_by_name("金属铜")
        assert t["strength"] == 9
        assert t["last_scored_date"] == TOMORROW

    def test_a_weak_day_still_subtracts(self, store):
        upsert_theme("港口航运", status="active", strength=6)
        update_theme_strength("港口航运", weak(), today=TODAY)
        assert get_theme_by_name("港口航运")["strength"] == 4

    def test_strength_stays_in_range(self, store):
        upsert_theme("金属铜", status="active", strength=9)
        update_theme_strength("金属铜", strong(), today=TODAY)
        assert get_theme_by_name("金属铜")["strength"] == 10


class TestDailyScoreIsLive:
    def test_later_cycles_still_refresh_today_score(self, store):
        """The gate is frozen for the day; the live read is not."""
        upsert_theme("金属铜", status="active", strength=5)
        update_theme_strength("金属铜", strong(), today=TODAY)
        update_theme_strength("金属铜", weak(), today=TODAY)
        t = get_theme_by_name("金属铜")
        assert t["daily_score"] == -2, "afternoon reversal must show"
        assert t["strength"] == 7, "but must not be charged twice"

    def test_status_does_not_flip_on_a_refresh(self, store):
        """Status follows strength, and strength did not move."""
        upsert_theme("金属铜", status="active", strength=5)
        update_theme_strength("金属铜", strong(), today=TODAY)
        for _ in range(10):
            update_theme_strength("金属铜", weak(), today=TODAY)
        assert get_theme_by_name("金属铜")["status"] == "active"


class TestRanking:
    def test_daily_score_ranks_today_strength_ordering_does_not(self, store):
        """A two-week-old line out-accumulates one that broke out today."""
        from alpha_agents.data.memory_store import get_active_themes

        upsert_theme("老主线", status="active", strength=9, daily_score=0)
        upsert_theme("今日爆发", status="watching", strength=2, daily_score=4)

        by_strength = [t["name"] for t in get_active_themes()]
        by_today = [t["name"] for t in get_active_themes(order_by="daily_score")]

        assert by_strength[0] == "老主线"
        assert by_today[0] == "今日爆发"


class TestArchived:
    def test_archived_themes_are_left_alone(self, store):
        upsert_theme("旧主线", status="archived", strength=0)
        update_theme_strength("旧主线", strong(), today=TODAY)
        t = get_theme_by_name("旧主线")
        assert t["strength"] == 0
        assert t["last_scored_date"] is None

    def test_unknown_theme_is_a_no_op(self, store):
        update_theme_strength("不存在的主线", strong(), today=TODAY)
        assert get_theme_by_name("不存在的主线") is None
