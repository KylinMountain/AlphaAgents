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
    def test_a_fired_condition_closes_and_records_which(self, store):
        tid = T.create(a_thesis([T.Condition("price_below", 50.0)]))
        with patch.object(M, "get_open_positions", return_value=[a_position()]), \
             patch.object(M, "close_position", return_value=True) as close:
            out = M.check_all({"000962": 49.5}, now=NOON)

        assert close.call_args.kwargs["close_reason"].startswith("论点失效")
        assert out["closed"][0]["type"] == "thesis_invalidated"
        closed = T.get_closed()[0]
        assert closed.status == T.INVALIDATED
        assert closed.close_kind == "price_below"
        assert T.get_active() == []

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
        assert out["closed"][0]["type"] == "thesis_invalidated"

    def test_sector_rank_is_passed_through(self, store):
        T.create(a_thesis([T.Condition("theme_rank_worse_than", 20)]))
        with patch.object(M, "get_open_positions", return_value=[a_position()]), \
             patch.object(M, "close_position", return_value=True):
            out = M.check_all({"000962": 53.24},
                              sector_ranks={"小金属概念": 44}, now=NOON)
        assert out["closed"]


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

    def test_a_fired_condition_beats_the_horizon(self, store):
        """Exiting on a reason is more informative than exiting on a date."""
        T.create(a_thesis([T.Condition("price_below", 60.0)], horizon=1))
        with patch.object(M, "get_open_positions",
                          return_value=[a_position(holding_days=9)]), \
             patch.object(M, "close_position", return_value=True):
            out = M.check_all({"000962": 53.0}, now=NOON)
        assert out["closed"][0]["type"] == "thesis_invalidated"


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
        assert "没有任何列出的失效条件触发" in closed.close_note

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
