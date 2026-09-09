"""说了做没做——唯一不受市场噪声影响的信号。

一笔交易的盈亏里技能占的方差极小：单笔标准差约 6%，真实 edge 可能 1%，
要区分开需要上百笔，而市场状态几个月就换一次。所以从盈亏学习在这个领域
几乎不可能。

「我写了跌破 49.5 就走，价格到 49.2 我还拿着」——这个矛盾完全在系统内部
判定，零噪声，一个事件一个样本，立刻可读。而且它是纪律问题不是判断问题：
一个写得出好论点却不执行的 agent，想得再对也没用。
"""

from unittest.mock import patch

import pytest

from alpha_agents.data import thesis as T
from alpha_agents.evolution import consistency as C


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


def a_thesis(conditions, code="000962", **kw):
    base = dict(code=code, name="东方钽业", theme="", claim="c",
                prob=0.6, conviction=0.5, conditions=conditions,
                position_id=1)
    base.update(kw)
    return T.create(T.Thesis(**base))


POS = [{"id": 1, "code": "000962", "name": "东方钽业", "open_price": 50.0,
        "peak_return_pct": 0.0, "holding_days": 2, "theme": ""}]


class TestBrokenPromises:
    def test_a_true_condition_on_a_live_position_is_flagged(self, store):
        a_thesis([T.Condition("price_below", 49.5, "跌破止损位")])
        with patch.object(C, "get_open_positions", return_value=POS):
            out = C.broken_promises({"000962": 49.2})
        assert len(out) == 1
        assert out[0]["kind"] == "price_below"
        assert "49.5" in out[0]["condition"]

    def test_a_condition_that_is_not_true_is_not_a_promise_broken(self, store):
        a_thesis([T.Condition("price_below", 49.5)])
        with patch.object(C, "get_open_positions", return_value=POS):
            assert C.broken_promises({"000962": 53.0}) == []

    def test_a_thesis_with_no_position_is_skipped(self, store):
        """An order still pending has nothing to have failed to sell."""
        a_thesis([T.Condition("price_below", 99.0)], position_id=None)
        with patch.object(C, "get_open_positions", return_value=POS):
            assert C.broken_promises({"000962": 49.2}) == []

    def test_a_missing_price_is_not_a_verdict(self, store):
        a_thesis([T.Condition("price_below", 99.0)])
        with patch.object(C, "get_open_positions", return_value=POS):
            assert C.broken_promises({}) == []

    def test_theme_conditions_read_the_live_theme(self, store):
        from alpha_agents.data.memory_store import upsert_theme
        upsert_theme("小金属概念", status="active", strength=5, daily_score=-2)
        a_thesis([T.Condition("theme_daily_score_below", 0)], theme="小金属概念")
        pos = [{**POS[0], "theme": "小金属概念"}]
        with patch.object(C, "get_open_positions", return_value=pos):
            assert len(C.broken_promises({"000962": 53.0})) == 1


class TestUnexplainedExits:
    def test_a_fired_condition_counts_as_explained(self, store):
        tid = a_thesis([T.Condition("price_below", 49.5)])
        T.close(tid, T.INVALIDATED, close_kind="price_below")
        assert C.unexplained_exits() == []

    def test_a_blind_spot_is_explained_by_being_one(self, store):
        """It already carries the most informative label there is."""
        tid = a_thesis([T.Condition("price_below", 49.5)])
        T.close(tid, T.BLIND_SPOT, close_note="风控硬线")
        assert C.unexplained_exits() == []

    def test_an_agent_note_counts_as_explained(self, store):
        tid = a_thesis([T.Condition("price_below", 49.5)])
        T.close(tid, T.EXPIRED, close_note="主线转弱，先走")
        assert C.unexplained_exits() == []

    def test_silence_is_flagged(self, store):
        tid = a_thesis([T.Condition("price_below", 49.5)])
        T.close(tid, T.EXPIRED)
        out = C.unexplained_exits()
        assert len(out) == 1 and out[0]["code"] == "000962"


class TestSizingFollowsConviction:
    def _spread(self, pairs):
        for i, (prob, size) in enumerate(pairs):
            T.create(T.Thesis(code=f"00000{i}", prob=prob, size_pct=size,
                              conditions=[T.Condition("price_below", 1)]))

    def test_thin_history_says_nothing(self, store):
        self._spread([(0.6, 0.03), (0.7, 0.05)])
        assert C.sizing_follows_conviction() == {"n": 2}

    def test_identical_sizes_are_flat(self, store):
        self._spread([(0.55, 0.03), (0.6, 0.03), (0.7, 0.03), (0.8, 0.03)])
        assert C.sizing_follows_conviction()["flat"] is True

    def test_sizing_up_with_confidence_is_neither_flat_nor_backwards(self, store):
        self._spread([(0.55, 0.01), (0.6, 0.02), (0.7, 0.04), (0.8, 0.06)])
        got = C.sizing_follows_conviction()
        assert not got["flat"] and not got["backwards"]
        assert got["high_conf_avg"] > got["low_conf_avg"]

    def test_betting_more_when_less_sure_is_backwards(self, store):
        """Worse than ignoring the probability: it is inverted."""
        self._spread([(0.55, 0.06), (0.6, 0.05), (0.7, 0.02), (0.8, 0.01)])
        assert C.sizing_follows_conviction()["backwards"] is True


class TestInjection:
    def test_empty_history_says_nothing(self, store):
        assert C.inject_consistency({}) == ""

    def test_a_broken_promise_leads_the_report(self, store):
        a_thesis([T.Condition("price_below", 49.5, "跌破止损位")])
        with patch.object(C, "get_open_positions", return_value=POS):
            out = C.inject_consistency({"000962": 49.2})
        assert "说了没做" in out
        assert "纪律问题" in out

    def test_flat_sizing_is_named(self, store):
        for i, p in enumerate((0.55, 0.6, 0.7, 0.8)):
            T.create(T.Thesis(code=f"00000{i}", prob=p, size_pct=0.03,
                              conditions=[T.Condition("price_below", 1)]))
        assert "仓位不随信心变化" in C.inject_consistency({})
