"""历史主题评分：让主题门在回放里真的能被触发。

## 它修的是什么

回放的买入路径过主题门（`data/theme_gate.theme_admits`），门的判据是
`theme_lines.trend_score`。建 `WALK-PLACEHOLDER` 合成主题时那一列是 NULL，
而 NULL 是门**刻意放行**的分支。

后果不是"回放跑不了"，是**回放里主题门永远不生效**：policy variant 目前
唯一能改的基因位点（`theme_gate.w_rel ∓0.05`）在回放中不参与任何决策，
所以 `counterfactual_change_rate` 必然是 0——不是学习没用，是唯一能变的
参数没被读到。"RSI golden path 跑不通"的根因在这里。

## 两个不能妥协的性质

1. **读历史，不读现值。** `theme_lines.trend_score` 是单列现值、每个周期
   被重写。回放读它 = 2026-09-14 的决策用了 09-18 算出来的分。
2. **没源就不写。** `sector_flow_snapshots` 只有 2026-09-08 起的数据。
   更早的日期必须**拒绝**，而不是拿后一天的分数回填——那是把未来写进历史。

这些测试用自建的小快照库，因为 conftest 禁止测试读生产 `data/`
（`StorageIsolationError`），而自建数据才能断言"截断停在哪一天"。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import rebuild_theme_scores as R  # noqa: E402

from alpha_agents.data import theme_gate as TG  # noqa: E402
from alpha_agents.evolution import replay_mode  # noqa: E402

DAYS = ["2026-09-08", "2026-09-09", "2026-09-10"]


@pytest.fixture(autouse=True)
def _reset_as_of():
    yield
    replay_mode.set_replay_as_of(None)


def _board_rows(day: str, hot: float, cold: float) -> list[dict]:
    """Two boards, one clearly ahead, plus quiet filler.

    The filler matters: `_percentile` is taken over the whole frame, so a
    two-row frame makes every score 0.25/0.75 and a percentile bug invisible.
    """
    rows = [{"concept": "热门概念", "scope": "concept",
             "net_flow_yi": hot, "change_pct": hot, "company_count": 30,
             "last_seen": f"{day} 15:30"},
            {"concept": "冷门概念", "scope": "concept",
             "net_flow_yi": cold, "change_pct": cold, "company_count": 20,
             "last_seen": f"{day} 15:30"}]
    for i in range(8):
        mid = (hot + cold) / 2
        rows.append({"concept": f"填充{i}", "scope": "industry",
                     "net_flow_yi": mid, "change_pct": mid,
                     "company_count": 10,
                     "last_seen": f"{day} 15:30"})
    return rows


class TestTheScoreIsRebuiltFromTheRealFrame:
    def test_a_hot_board_scores_above_a_cold_one(self):
        rows = _board_rows(DAYS[0], hot=50.0, cold=-20.0)
        conn = _FakeSnapshots(rows)
        scored = R.score_day(conn, DAYS[0], R._cut_for(DAYS[0]),
                             ["热门概念", "冷门概念"])
        assert scored["热门概念"]["score"] > scored["冷门概念"]["score"]

    def test_the_percentile_is_taken_over_the_whole_frame(self):
        """Not over the matched rows: a percentile of two is not a percentile.
        The filler rows are half the frame, so a hot board cannot reach 1.0
        without outranking them."""
        rows = _board_rows(DAYS[0], hot=50.0, cold=-20.0)
        scored = R.score_day(_FakeSnapshots(rows), DAYS[0],
                             R._cut_for(DAYS[0]), ["热门概念"])
        s = scored["热门概念"]
        assert s["board_of"] == len(rows)
        assert 0.0 < s["flow_pct"] <= 1.0

    def test_an_unmatched_theme_is_omitted_not_zeroed(self):
        """Production returns None for a board it cannot name, and the caller
        records a decay. Writing 0.0 would claim the line had no money, which
        is a different fact."""
        scored = R.score_day(_FakeSnapshots(_board_rows(DAYS[0], 1.0, -1.0)),
                             DAYS[0], R._cut_for(DAYS[0]), ["不存在的主题"])
        assert scored == {}

    def test_the_score_matches_productions_formula(self):
        """Same weights, same inputs, same answer. Two formulas would put the
        replay's gate on a different scale from production's, and the gap
        would look like a behaviour change."""
        from alpha_agents.data import scoring
        gate = scoring.DEFAULT_DECISION_PARAMS["theme_gate"]
        rows = _board_rows(DAYS[0], hot=30.0, cold=-10.0)
        s = R.score_day(_FakeSnapshots(rows), DAYS[0], R._cut_for(DAYS[0]),
                        ["热门概念"])["热门概念"]
        expected = (gate["w_flow"] * s["flow_pct"]
                    + gate["w_rel"] * s["rel_pct"]
                    + gate["w_confirm"] * s["confirm"])
        assert s["score"] == pytest.approx(round(expected, 4))

    def test_confirm_is_recorded_as_absent_rather_than_guessed(self):
        """`strength` (confirmed sessions) is not in the snapshot. Guessing it
        would put an invented number into a gate; leaving it 0 and saying so
        makes the reconstruction auditable and strictly conservative."""
        s = R.score_day(_FakeSnapshots(_board_rows(DAYS[0], 30.0, -10.0)),
                        DAYS[0], R._cut_for(DAYS[0]),
                        ["热门概念"])["热门概念"]
        assert s["confirm"] == 0.0

    def test_the_score_varies_by_day(self):
        """A reconstruction that returned the same number every day would be
        a constant wearing a history's clothes."""
        first = R.score_day(
            _FakeSnapshots(_board_rows(DAYS[0], 50.0, -20.0)), DAYS[0],
            R._cut_for(DAYS[0]), ["热门概念"])["热门概念"]["score"]
        second = R.score_day(
            _FakeSnapshots(_board_rows(DAYS[1], -30.0, 40.0)), DAYS[1],
            R._cut_for(DAYS[1]), ["热门概念"])["热门概念"]["score"]
        assert first != second


class TestItRefusesWhatItCannotKnow:
    def test_a_window_before_the_source_is_refused(self, tmp_path):
        """`sector_flow_snapshots` starts 2026-09-08. An earlier request must
        fail loudly: filling 2026-08 with a September score is the future
        written into the past, which is worse than having no score."""
        (tmp_path / "market_snapshots.db").touch()
        (tmp_path / "memory.db").touch()
        with pytest.raises(SystemExit) as e:
            R.rebuild(tmp_path, start="2026-08-18", end="2026-09-14",
                      names=["x"])
        assert R.EARLIEST_REBUILDABLE in str(e.value)

    def test_the_earliest_rebuildable_day_is_the_data_boundary(self):
        """Pinned as a constant with a reason, because it is a fact about the
        store rather than a setting."""
        assert R.EARLIEST_REBUILDABLE == "2026-09-08"


class TestTheGateReadsHistoryUnderReplay:
    def test_a_replay_with_no_history_row_passes(self):
        """The gate's documented 'unmeasurable' branch. A day outside the
        reconstruction genuinely has no score, and inventing one would be the
        fabrication this table exists to avoid."""
        replay_mode.set_replay_as_of("2020-01-01")
        assert TG._score_as_of("任何主题", {"trend_score": 0.99}) is None

    def test_a_replay_never_falls_back_to_the_live_column(self, tmp_path,
                                                          monkeypatch):
        """The live column is the present. Under a replay it is the future,
        and reading it is the exact leak the history table closes — so a
        lookup failure returns None rather than `row['trend_score']`."""
        replay_mode.set_replay_as_of("2026-09-09")
        monkeypatch.setattr(TG, "get_theme_by_name",
                            lambda name: {"trend_score": 0.99,
                                          "status": "active"})
        # No history table exists in the empty sandbox → unmeasurable, and
        # crucially not 0.99.
        assert TG._score_as_of("任何主题",
                               {"trend_score": 0.99}) is None

    def test_live_mode_still_reads_the_live_column(self):
        """Production's live column *is* today's truth; the history table must
        not shadow it when there is no replay in progress."""
        replay_mode.set_replay_as_of(None)
        assert TG._score_as_of("任何主题", {"trend_score": 0.42}) == 0.42


class TestTheRowIsDatedNotOverwritten:
    def test_the_schema_has_a_dated_history_table(self):
        from alpha_agents.data import memory_schema
        assert "theme_score_history" in memory_schema._SCHEMA
        assert "UNIQUE(theme, as_of)" in memory_schema._SCHEMA

    def test_the_unique_key_is_theme_and_day(self):
        """Overwriting one column per theme is what made history impossible;
        the pair is what makes a day's answer retrievable on its own."""
        from alpha_agents.data import memory_schema
        sql = memory_schema._SCHEMA
        i = sql.find("CREATE TABLE IF NOT EXISTS theme_score_history")
        assert i != -1
        block = sql[i:i + 1200]
        assert "theme TEXT NOT NULL" in block
        assert "as_of TEXT NOT NULL" in block


class _FakeSnapshots:
    """A stand-in for the snapshot connection, in the **query's** row shape.

    `_cross_section` reads `sector_name` / `scope` / `net_flow_yi` /
    `change_pct` / `company_count`, so that is what these rows carry — a fake
    that returned the cross-section's shape instead would test nothing about
    the code that builds it.
    """

    def __init__(self, rows: list[dict]):
        self._rows = [{
            "sector_name": r["concept"],
            "scope": r["scope"],
            "net_flow_yi": r["net_flow_yi"],
            "change_pct": r["change_pct"],
            "company_count": r["company_count"],
            "last_seen": r["last_seen"],
        } for r in rows]

    def execute(self, sql: str, args=()):
        assert "sector_flow_snapshots" in sql
        return _Rows(self._rows)


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return [dict(r) for r in self._rows]
