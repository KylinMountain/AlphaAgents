"""Evaluating theses every cycle, with no model in the loop.

The properties worth pinning: a fired condition closes and records *which*
one, a horizon that runs out distinguishes played-out from went-nowhere,
and a position that vanished under the hard floor becomes a blind_spot
rather than being quietly forgotten. That last one is the statistic the
whole design exists to produce — how often it lost money for a reason it
never wrote down.
"""

from datetime import datetime
from unittest.mock import patch

import pytest

from alpha_agents.data import thesis as T
from alpha_agents.pipeline.tasks import thesis_monitor as M


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


def a_thesis(conditions, horizon=5, code="000962"):
    return T.Thesis(code=code, name="东方钽业", theme="小金属概念",
                    claim="资金持续流入", horizon_days=horizon,
                    prob=0.6, conviction=0.7, conditions=conditions,
                    position_id=1)


def a_position(**kw):
    base = {"id": 1, "code": "000962", "name": "东方钽业",
            "open_price": 51.80, "theme": "小金属概念",
            "peak_return_pct": 4.0, "holding_days": 2}
    base.update(kw)
    return base


NOON = datetime(2026, 9, 9, 11, 0)


class TestFiring:
    """A crossed invalidation wakes the agent. It does not close anything.

    The old wiring closed the position here, on the reasoning that a claim
    its own stated invalidation had broken was not a claim the agent should
    re-argue. That turned a number written on entry day into a mechanical
    stop — the agent never looked at the stock again — and it destroyed the
    one observation worth having: whether the agent honours its own
    commitment when the line is actually crossed.
    """

    def test_a_fired_condition_does_not_close_the_position(self, store):
        T.create(a_thesis([T.Condition("price_below", 50.0)]))
        with patch.object(M, "get_open_positions", return_value=[a_position()]), \
             patch.object(M, "close_position", return_value=True) as close:
            M.check_all({"000962": 49.5}, now=NOON)
        close.assert_not_called()
        assert len(T.get_active()) == 1, "the thesis stays alive until answered"

    def test_a_fired_condition_produces_a_signal_naming_the_condition(self, store):
        T.create(a_thesis([T.Condition("price_below", 50.0, "跌破50就是结构破了")]))
        with patch.object(M, "get_open_positions", return_value=[a_position()]), \
             patch.object(M, "close_position", return_value=True):
            out = M.check_all({"000962": 49.5}, now=NOON)

        assert out["closed"] == []
        sig = out["signals"][0]
        assert sig["type"] == "signal", "build_context only reads type=signal"
        assert sig["kind"] == "price_below"
        assert sig["code"] == "000962"

    def test_the_signal_quotes_the_agents_own_words(self, store):
        """The note is why this is a commitment and not a threshold."""
        T.create(a_thesis([T.Condition("price_below", 50.0, "跌破50就是结构破了")]))
        with patch.object(M, "get_open_positions", return_value=[a_position()]), \
             patch.object(M, "close_position", return_value=True):
            out = M.check_all({"000962": 49.5}, now=NOON)
        assert "跌破50就是结构破了" in out["signals"][0]["reason"]
        assert "还持有吗" in out["signals"][0]["reason"]

    def test_the_trigger_is_recorded_as_a_checkpoint(self, store):
        tid = T.create(a_thesis([T.Condition("price_below", 50.0)]))
        with patch.object(M, "get_open_positions", return_value=[a_position()]), \
             patch.object(M, "close_position", return_value=True):
            M.check_all({"000962": 49.5}, now=NOON)
        points = T.get_by_id(tid).checkpoints
        assert points[-1]["verdict"] == T.TRIGGERED
        assert points[-1]["kind"] == "price_below"

    def test_a_repeat_crossing_tells_the_agent_it_is_a_repeat(self, store):
        """Deliberately not suppressed: a trader at the line for the third
        time, having overridden itself twice, is in a different situation."""
        T.create(a_thesis([T.Condition("price_below", 50.0)]))
        with patch.object(M, "get_open_positions", return_value=[a_position()]), \
             patch.object(M, "close_position", return_value=True):
            first = M.check_all({"000962": 49.5}, now=NOON)
            second = M.check_all({"000962": 49.0}, now=NOON)
        assert "第 1 次" not in first["signals"][0]["reason"]
        assert "第 2 次触及" in second["signals"][0]["reason"]
        assert "前 1 次你都选择了继续持有" in second["signals"][0]["reason"]

    def test_nothing_fires_means_no_close(self, store):
        T.create(a_thesis([T.Condition("price_below", 40.0)]))
        with patch.object(M, "get_open_positions", return_value=[a_position()]), \
             patch.object(M, "close_position") as close:
            out = M.check_all({"000962": 53.24}, now=NOON)
        close.assert_not_called()
        assert out["closed"] == []
        assert len(T.get_active()) == 1

    def test_theme_data_comes_from_the_theme_table(self, store):
        from alpha_agents.data.memory_store import upsert_theme
        upsert_theme("小金属概念", status="active", strength=2, daily_score=-1)
        T.create(a_thesis([T.Condition("theme_strength_below", 4)]))
        with patch.object(M, "get_open_positions", return_value=[a_position()]), \
             patch.object(M, "close_position", return_value=True):
            out = M.check_all({"000962": 53.24}, now=NOON)
        assert out["signals"][0]["kind"] == "theme_strength_below"

    def test_sector_rank_is_passed_through(self, store):
        T.create(a_thesis([T.Condition("theme_rank_worse_than", 20)]))
        with patch.object(M, "get_open_positions", return_value=[a_position()]), \
             patch.object(M, "close_position", return_value=True):
            out = M.check_all({"000962": 53.24},
                              sector_ranks={"小金属概念": 44}, now=NOON)
        assert out["signals"][0]["kind"] == "theme_rank_worse_than"


class TestHorizon:
    def test_a_thesis_that_played_out_is_validated(self, store):
        T.create(a_thesis([], horizon=3))
        pos = a_position(holding_days=3)
        with patch.object(M, "get_open_positions", return_value=[pos]), \
             patch.object(M, "close_position", return_value=True):
            out = M.check_all({"000962": 55.0}, now=NOON)   # +6.2%
        assert out["closed"][0]["type"] == "thesis_validated"
        assert T.get_closed()[0].status == T.VALIDATED

    def test_a_thesis_that_went_nowhere_expires(self, store):
        """Drifting 0.4% over the horizon is not being right."""
        T.create(a_thesis([], horizon=3))
        pos = a_position(holding_days=3)
        with patch.object(M, "get_open_positions", return_value=[pos]), \
             patch.object(M, "close_position", return_value=True):
            out = M.check_all({"000962": 52.0}, now=NOON)   # +0.4%
        assert out["closed"][0]["type"] == "thesis_expired"

    def test_before_the_horizon_nothing_happens(self, store):
        T.create(a_thesis([], horizon=5))
        with patch.object(M, "get_open_positions",
                          return_value=[a_position(holding_days=2)]), \
             patch.object(M, "close_position") as close:
            M.check_all({"000962": 52.0}, now=NOON)
        close.assert_not_called()

    def test_a_fired_condition_defers_the_horizon_close(self, store):
        """A crossed line is a question for the agent, and it is asked first.

        Closing on the horizon in the same pass would answer the question by
        ending the position, which is the behaviour this design removed.
        """
        T.create(a_thesis([T.Condition("price_below", 60.0)], horizon=1))
        with patch.object(M, "get_open_positions",
                          return_value=[a_position(holding_days=9)]), \
             patch.object(M, "close_position", return_value=True) as close:
            out = M.check_all({"000962": 53.0}, now=NOON)
        close.assert_not_called()
        assert out["closed"] == []
        assert out["signals"][0]["kind"] == "price_below"


class TestNarrative:
    def test_queued_only_inside_the_review_window(self, store):
        T.create(a_thesis([T.Condition("narrative", None, "关税豁免若不续期")]))
        with patch.object(M, "get_open_positions", return_value=[a_position()]), \
             patch.object(M, "close_position"):
            early = M.check_all({"000962": 53.0},
                                now=datetime(2026, 9, 9, 10, 0))
            due = M.check_all({"000962": 53.0},
                              now=datetime(2026, 9, 9, 14, 3))
            late = M.check_all({"000962": 53.0},
                               now=datetime(2026, 9, 9, 14, 40))
        assert early["narrative_due"] == []
        assert len(due["narrative_due"]) == 1
        assert late["narrative_due"] == []

    def test_a_narrative_never_closes_anything(self, store):
        T.create(a_thesis([T.Condition("narrative", None, "x")]))
        with patch.object(M, "get_open_positions", return_value=[a_position()]), \
             patch.object(M, "close_position") as close:
            M.check_all({"000962": 30.0}, now=datetime(2026, 9, 9, 14, 3))
        close.assert_not_called()


class TestBlindSpot:
    def test_a_position_closed_by_the_floor_becomes_a_blind_spot(self, store):
        T.create(a_thesis([T.Condition("price_below", 40.0)]))
        with patch.object(M, "get_open_positions", return_value=[]):
            orphans = M.settle_orphans({"000962": 47.0})
        assert len(orphans) == 1
        closed = T.get_closed()[0]
        assert closed.status == T.BLIND_SPOT
        assert "一条也没触发" in closed.close_note

    def test_a_warned_exit_is_not_a_blind_spot(self, store):
        """The agent wrote the line, was woken when it broke, and closed.

        That is the opposite of a blind spot, and marking it blind would
        poison the one failure statistic this design leans on — in the
        autonomous arm there is no hard floor, so every agent sale would
        otherwise be filed as "I never saw it coming".
        """
        tid = T.create(a_thesis([T.Condition("price_below", 40.0)]))
        T.add_checkpoint(tid, "股价跌破 40.0 | 浮动-3.1% 持仓2天",
                         T.TRIGGERED, kind="price_below")
        with patch.object(M, "get_open_positions", return_value=[]):
            M.settle_orphans({"000962": 47.0})
        closed = T.get_closed()[0]
        assert closed.status == T.INVALIDATED
        assert closed.close_kind == "price_below"
        assert "agent 在自己声明的条件触发后平仓" in closed.close_note

    def test_the_blind_spot_note_no_longer_asserts_a_hard_floor(self, store):
        """--autonomous switches the floor off, so the old note named a
        mechanism that could not have run."""
        T.create(a_thesis([T.Condition("price_below", 40.0)]))
        with patch.object(M, "get_open_positions", return_value=[]):
            M.settle_orphans({"000962": 47.0})
        assert "风控硬线" not in T.get_closed()[0].close_note

    def test_the_replay_settles_orphans_too(self):
        """Only book_manager called it, so a replay left one active thesis
        behind for every agent sale."""
        import inspect
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import walk_forward as wf
        assert "settle_orphans" in inspect.getsource(wf._check_theses)

    def test_a_live_position_is_left_alone(self, store):
        T.create(a_thesis([T.Condition("price_below", 40.0)]))
        with patch.object(M, "get_open_positions", return_value=[a_position()]):
            assert M.settle_orphans({"000962": 53.0}) == []
        assert len(T.get_active()) == 1

    def test_a_thesis_with_no_position_yet_is_not_a_blind_spot(self, store):
        """An order still pending has no position id — not a failure."""
        th = a_thesis([T.Condition("price_below", 40.0)])
        th.position_id = None
        T.create(th)
        with patch.object(M, "get_open_positions", return_value=[]):
            assert M.settle_orphans({}) == []
        assert len(T.get_active()) == 1


class TestSkips:
    def test_a_thesis_with_no_price_is_skipped(self, store):
        T.create(a_thesis([T.Condition("price_below", 99.0)]))
        with patch.object(M, "get_open_positions", return_value=[a_position()]), \
             patch.object(M, "close_position") as close:
            M.check_all({}, now=NOON)
        close.assert_not_called()

    def test_a_failed_close_leaves_the_thesis_active(self, store):
        """The two must not drift apart: no close, no status change."""
        T.create(a_thesis([T.Condition("price_below", 99.0)]))
        with patch.object(M, "get_open_positions", return_value=[a_position()]), \
             patch.object(M, "close_position", return_value=False):
            out = M.check_all({"000962": 53.0}, now=NOON)
        assert out["closed"] == []
        assert len(T.get_active()) == 1
