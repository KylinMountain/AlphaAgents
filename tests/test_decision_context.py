"""G6 — a prediction records what was knowable when it was made."""

import json
import sqlite3
from datetime import datetime

import pytest

from alpha_agents.data.decision_context import (
    CONTEXT_VERSION, build_decision_context, merge_features, replay_day,
)


class TestBuildDecisionContext:
    def test_records_task_and_timestamp(self):
        ctx = build_decision_context(task="morning_scan",
                                     decided_at=datetime(2026, 9, 7, 6, 30))
        assert ctx["task"] == "morning_scan"
        assert ctx["decided_at"] == "2026-09-07 06:30:00"
        assert ctx["v"] == CONTEXT_VERSION

    def test_news_window_bounds_are_derived_from_the_hours(self):
        ctx = build_decision_context(
            task="morning_scan", news_window_hours=16, news_count=400,
            decided_at=datetime(2026, 9, 7, 6, 30),
        )
        w = ctx["news_window"]
        assert w["since"] == "2026-09-06 14:30:00"
        assert w["until"] == "2026-09-07 06:30:00"
        assert w["hours"] == 16 and w["count"] == 400

    def test_monday_window_reaches_over_the_weekend(self):
        ctx = build_decision_context(
            task="morning_scan", news_window_hours=66,
            decided_at=datetime(2026, 9, 7, 6, 30),   # Monday
        )
        assert ctx["news_window"]["since"].startswith("2026-09-04")

    def test_no_window_when_hours_absent(self):
        assert "news_window" not in build_decision_context(task="x")

    def test_themes_are_reduced_to_name_and_strength(self):
        themes = [{"name": "AI算力", "strength": 8, "core_stocks": "[...]",
                   "notes": "很长的备注" * 50}]
        ctx = build_decision_context(task="x", themes=themes)
        assert ctx["themes"] == [{"name": "AI算力", "strength": 8}]

    def test_theme_list_is_capped(self):
        themes = [{"name": f"T{i}", "strength": i} for i in range(20)]
        assert len(build_decision_context(task="x", themes=themes)["themes"]) == 8

    def test_regime_and_sentiment_are_carried(self):
        ctx = build_decision_context(task="x", market_regime="weak",
                                     sentiment_phase="退潮")
        assert ctx["market_regime"] == "weak"
        assert ctx["sentiment_phase"] == "退潮"

    def test_extra_fields_merge_in(self):
        ctx = build_decision_context(task="x", extra={"has_anomaly": True})
        assert ctx["has_anomaly"] is True

    def test_absent_optionals_are_omitted_not_nulled(self):
        ctx = build_decision_context(task="x")
        for key in ("themes", "market_regime", "sentiment_phase", "news_window"):
            assert key not in ctx


class TestMergeFeatures:
    def test_context_is_nested_under_ctx(self):
        merged = merge_features({"theme": "AI"}, {"task": "morning_scan"})
        assert merged["theme"] == "AI"          # flat namespace preserved
        assert merged["_ctx"]["task"] == "morning_scan"

    def test_handles_missing_sides(self):
        assert merge_features(None, None) == {}
        assert merge_features({"a": 1}, None) == {"a": 1}
        assert merge_features(None, {"task": "x"}) == {"_ctx": {"task": "x"}}

    def test_does_not_mutate_the_input(self):
        feats = {"theme": "AI"}
        merge_features(feats, {"task": "x"})
        assert "_ctx" not in feats


@pytest.fixture
def store(tmp_path, monkeypatch):
    from alpha_agents.data import memory_store as ms
    from alpha_agents.data import snapshot_store as ss

    mem = sqlite3.connect(str(tmp_path / "memory.db"), check_same_thread=False)
    mem.row_factory = sqlite3.Row
    mem.executescript(ms._SCHEMA)
    mem.commit()
    monkeypatch.setattr(ms, "_get_conn", lambda: mem)

    snap = sqlite3.connect(str(tmp_path / "snap.db"), check_same_thread=False)
    snap.row_factory = sqlite3.Row
    snap.executescript(ss._SCHEMA)
    snap.commit()
    monkeypatch.setattr(ss, "_get_conn", lambda: snap)

    yield ms, ss
    mem.close()
    snap.close()


class TestReplayDay:
    """The acceptance test for G6: visible information → recommendation."""

    def _seed(self, ms, ss):
        ss.save_news("金十数据", [
            {"title": "夜间政策", "summary": "s", "time": "2026-09-07 02:00:00"},
            {"title": "开盘前消息", "summary": "s", "time": "2026-09-07 06:00:00"},
            {"title": "窗口外的旧闻", "summary": "s", "time": "2026-09-05 10:00:00"},
        ])
        ctx = build_decision_context(
            task="morning_scan", news_window_hours=16, news_count=2,
            themes=[{"name": "AI算力", "strength": 8}],
            market_regime="strong",
            decided_at=datetime(2026, 9, 7, 6, 30),
        )
        ms.save_prediction(
            date="2026-09-07", report_type="morning", code="300308",
            name="中际旭创", direction="bullish", confidence="high",
            theme_line="AI算力", entry_price=150.0, reason="r",
            prob=0.6, features=merge_features({"dims_passed": 3}, ctx),
        )

    def test_replays_the_pick_with_its_context(self, store):
        ms, ss = store
        self._seed(ms, ss)
        got = replay_day("2026-09-07")
        assert got["replayable"] is True
        assert len(got["picks"]) == 1

        pick = got["picks"][0]
        assert pick["code"] == "300308" and pick["prob"] == 0.6
        assert pick["context"]["task"] == "morning_scan"
        assert pick["context"]["market_regime"] == "strong"
        assert pick["features"] == {"dims_passed": 3}   # _ctx split back out

    def test_recovers_the_news_that_was_visible(self, store):
        ms, ss = store
        self._seed(ms, ss)
        got = replay_day("2026-09-07")
        titles = {n["title"] for n in got["visible_news"]}
        assert titles == {"夜间政策", "开盘前消息"}
        assert "窗口外的旧闻" not in titles   # outside the recorded window

    def test_outcome_columns_come_back_once_scored(self, store):
        ms, ss = store
        self._seed(ms, ss)
        pid = ms.get_predictions_due_for_scoring("2026-09-30", 5)[0]["id"]
        ms.save_prediction_score(pid, {
            "brier": 0.16, "log_score": 0.5, "excess_return": 1.3,
            "residual_alpha": 0.8, "scored_at": "2026-09-14 16:00:00",
            "outcome": True,
        })
        pick = replay_day("2026-09-07")["picks"][0]
        assert pick["brier"] == 0.16 and pick["hit"] == 1
        assert pick["residual_alpha"] == 0.8

    def test_report_type_filter(self, store):
        ms, ss = store
        self._seed(ms, ss)
        assert len(replay_day("2026-09-07", "morning")["picks"]) == 1
        assert replay_day("2026-09-07", "intraday")["picks"] == []

    def test_day_with_no_picks_is_not_replayable(self, store):
        got = replay_day("2026-01-01")
        assert got["picks"] == [] and got["replayable"] is False

    def test_legacy_row_without_context_is_flagged_unreplayable(self, store):
        ms, _ss = store
        ms.save_prediction(
            date="2026-09-07", report_type="morning", code="000001",
            name="X", direction="bullish", confidence="low",
            theme_line="", entry_price=None, reason="legacy",
        )
        got = replay_day("2026-09-07")
        assert got["picks"][0]["context"] == {}
        assert got["replayable"] is False

    def test_malformed_features_do_not_raise(self, store):
        ms, _ss = store
        ms.save_prediction(
            date="2026-09-07", report_type="morning", code="000002",
            name="X", direction="bullish", confidence="low",
            theme_line="", entry_price=None, reason="r",
        )
        from alpha_agents.data.memory_store import _get_conn
        conn = _get_conn()
        conn.execute("UPDATE predictions SET features_json = '{bad json'")
        conn.commit()
        assert replay_day("2026-09-07")["picks"][0]["features"] == {}
