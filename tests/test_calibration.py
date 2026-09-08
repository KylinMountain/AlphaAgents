"""Calibration is the signal that works at twenty samples instead of two hundred.

Every thesis contributes a row, not only the profitable ones, so "you said
70% eleven times and were right five" is readable long before a P&L edge
is. These tests pin the two things that would quietly ruin it: reporting a
bucket off one sample, and counting a correctly-reasoned loss as a blind
spot.
"""

import pytest

from alpha_agents.data import thesis as T
from alpha_agents.evolution import calibration as C


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


def closed(prob, status, conditions=None, close_kind="", code="000001"):
    tid = T.create(T.Thesis(code=code, name="X", theme="t", claim="c",
                            prob=prob, conviction=0.5,
                            conditions=conditions or [T.Condition("price_below", 10)]))
    T.close(tid, status, close_kind=close_kind)
    return tid


class TestCalibration:
    def test_buckets_by_stated_probability(self, store):
        for _ in range(4):
            closed(0.75, T.VALIDATED)
        for _ in range(2):
            closed(0.75, T.INVALIDATED)
        curve = C.calibration()
        row = next(r for r in curve if r["bucket"] == ">70%")
        assert row["n"] == 6
        assert row["hit_rate"] == pytest.approx(4 / 6)

    def test_a_thin_bucket_is_not_reported(self, store):
        """One sample at 70% says nothing and would teach the wrong lesson."""
        closed(0.75, T.INVALIDATED)
        assert [r for r in C.calibration() if r["bucket"] == ">70%"] == []

    def test_only_validated_counts_as_a_hit(self, store):
        """Expiring flat is not being right."""
        for _ in range(3):
            closed(0.6, T.EXPIRED)
        row = next(r for r in C.calibration() if r["bucket"] == "55–70%")
        assert row["hit_rate"] == 0.0

    def test_overconfidence_is_named_in_the_injection(self, store):
        for _ in range(5):
            closed(0.8, T.INVALIDATED)
        closed(0.8, T.VALIDATED)
        out = C.inject_calibration()
        assert "系统性高估" in out

    def test_underconfidence_is_named_too(self, store):
        for _ in range(5):
            closed(0.6, T.VALIDATED)
        assert "低估" in C.inject_calibration()


class TestBlindSpot:
    def test_a_predicted_failure_is_not_a_blind_spot(self, store):
        """It failed the way it said it would. The reasoning was fine."""
        closed(0.6, T.INVALIDATED, close_kind="price_below")
        stats = C.blind_spot_rate()
        assert stats["blind"] == 0
        assert stats["n"] == 1

    def test_an_unforeseen_failure_counts(self, store):
        closed(0.6, T.BLIND_SPOT)
        stats = C.blind_spot_rate()
        assert stats["blind"] == 1
        assert stats["rate"] == 1.0

    def test_wins_are_excluded_from_the_denominator(self, store):
        closed(0.6, T.VALIDATED)
        closed(0.6, T.BLIND_SPOT)
        assert C.blind_spot_rate()["n"] == 1

    def test_theses_with_no_conditions_are_counted(self, store):
        T.close(T.create(T.Thesis(code="X", prob=0.6, conditions=[])),
                T.EXPIRED)
        assert C.blind_spot_rate()["unguarded"] == 1

    def test_empty_history_is_not_an_error(self, store):
        assert C.blind_spot_rate() == {"n": 0}
        assert C.inject_calibration() == ""


class TestConditionUsefulness:
    def test_counts_written_against_fired(self, store):
        for i in range(3):
            closed(0.6, T.INVALIDATED, close_kind="price_below", code=f"00000{i}")
        rows = {r["kind"]: r for r in C.condition_usefulness()}
        assert rows["price_below"]["written"] == 3
        assert rows["price_below"]["fired"] == 3

    def test_a_condition_that_never_fires_is_flagged(self, store):
        for i in range(6):
            closed(0.6, T.EXPIRED,
                   conditions=[T.Condition("breadth_below", 0.2)],
                   code=f"0000{i}")
        out = C.inject_calibration()
        assert "从未触发的条件" in out
        assert "breadth_below" in out

    def test_a_rarely_written_condition_is_not_flagged(self, store):
        """Two uses is not evidence the threshold is wrong."""
        for i in range(2):
            closed(0.6, T.EXPIRED,
                   conditions=[T.Condition("breadth_below", 0.2)],
                   code=f"0000{i}")
        assert "从未触发的条件" not in C.inject_calibration()
