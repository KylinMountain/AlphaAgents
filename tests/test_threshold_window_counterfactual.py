"""①C 贴阈值窗口：让"位点惰性"从窗口产物里分离出来。

## 这个实验回答什么

2026-09-17 计划的路 1 实测（5 天 × 4 主题）：w_rel ±0.05 没有翻转任何
准入，结论写成了"位点惰性"。但惰性有两种成因——位移真的永远够不着阈值，
或者**那 5 天恰好没有贴近阈值的评分**。
scripts/threshold_window_counterfactual.py 对重建窗口里的**每一个**
(日, 主题) 跑同一步反事实，把两种成因分开。

## 测试钉住的性质

1. 翻转判定**只来自** single_step_counterfactual——扫描器不许有第二份
   公式，否则"扫描发现的翻转"与"反事实承认的翻转"可以是两回事。
2. 同一帧只计一次证据，重复的日期要**指认**它重复了谁，而不是静默丢弃。
3. 重建口径（confirm=0）必须随结果声明：它让评分系统性偏低，但两臂相同，
   所以只影响"生产会不会准入"的解读，不影响翻转判定。
4. 结果可复算：同输入同输出，没有任何采样。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import threshold_window_counterfactual as TWC  # noqa: E402

from alpha_agents.data.scoring import DEFAULT_DECISION_PARAMS  # noqa: E402

DAY = "2026-09-15"
DAY2 = "2026-09-16"


def _frame(theme_flow_rank_below: int, theme_change_rank_below: int,
           day: str = DAY) -> list[dict]:
    """A 10-row board whose theme sits at chosen percentile ranks.

    _percentile is mid-rank over ties and the theme's own row is in its
    frame, so with 10 distinct values a rank of r rows below lands on
    (r + 0.5) / 10: rank 4 is 0.45, rank 9 is 0.95 -- the values the
    flip-band fixtures below are built from.
    """
    flows = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
    changes = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    theme_flow = flows[theme_flow_rank_below]
    theme_change = changes[theme_change_rank_below]
    others_flow = [v for v in flows if v != theme_flow]
    others_change = [v for v in changes if v != theme_change]
    rows = [{"concept": "贴阈概念", "scope": "concept",
             "net_flow_yi": theme_flow, "change_pct": theme_change,
             "company_count": 30, "last_seen": f"{day} 15:30"}]
    for i, (f, c) in enumerate(zip(others_flow, others_change)):
        rows.append({"concept": f"填充{i}", "scope": "industry",
                     "net_flow_yi": f, "change_pct": c,
                     "company_count": 10, "last_seen": f"{day} 15:30"})
    return rows


class _DayAwareSnapshots:
    """Rows per day, in the query's shape: two days with equal boards must
    come back equal for the deduplication to have something to find. The
    days query selects one column, so it gets one-tuples back, the way a
    real cursor would."""

    def __init__(self, by_day: dict[str, list[dict]]):
        # Boards are declared in the cross-section's shape (concept/...) and
        # converted to the raw query's shape (sector_name/...), the same
        # hand-off _cross_section performs on real rows.
        self._by_day = {
            day: [{"sector_name": r["concept"], "scope": r["scope"],
                   "net_flow_yi": r["net_flow_yi"],
                   "change_pct": r["change_pct"],
                   "company_count": r["company_count"],
                   "last_seen": r["last_seen"]} for r in rows]
            for day, rows in by_day.items()
        }

    def execute(self, sql: str, args=()):
        assert "sector_flow_snapshots" in sql
        if "DISTINCT" in sql:
            start, end = args
            return _Rows([(d,) for d in self._by_day if start <= d <= end])
        day = args[1]
        return _Rows(self._by_day.get(day, []))


class _Rows:
    """Both consumption styles the scan uses: the days query iterates the
    cursor (as a real sqlite3 cursor supports) and _cross_section calls
    fetchall(), so the fake has to stand in for both."""

    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)

    def fetchall(self):
        return [dict(r) for r in self._rows]


def _scan(by_day, names=("贴阈概念",)):
    return TWC.scan(_DayAwareSnapshots(by_day), names=list(names),
                    start=DAY, end=DAY2, delta=0.05)


class TestTheScanFindsFlipsTheWindowWasBuiltFor:
    def test_a_score_one_step_above_the_bar_flips_down(self):
        """flow pct 0.45, change pct 0.95 (mid-rank over 10 distinct values):
        0.45x0.45 + 0.35x0.95 = 0.535 -- admitted at w_rel=0.35, refused at
        0.30 (0.4875). The band is exactly one weight-step wide, which is
        what 'threshold-adjacent' means."""
        got = _scan({DAY: _frame(4, 9), DAY2: _frame(4, 9)})
        assert [f["direction"] for f in got["flips"]] == ["down"]
        flip = got["flips"][0]
        assert flip["theme"] == "贴阈概念" and flip["day"] == DAY
        assert flip["score"] == pytest.approx(0.535, abs=1e-6)
        assert flip["flip_margin"] == pytest.approx(0.0475, abs=1e-6)

    def test_a_score_one_step_below_the_bar_flips_up(self):
        """flow pct 0.35, change pct 0.95: 0.45x0.35 + 0.35x0.95 = 0.49 --
        refused at the baseline, admitted by the +0.05 arm (0.5375)."""
        got = _scan({DAY: _frame(3, 9), DAY2: _frame(3, 9)})
        assert [f["direction"] for f in got["flips"]] == ["up"]
        assert got["flips"][0]["score_b"] == pytest.approx(0.5375, abs=1e-6)

    def test_a_score_far_from_the_bar_flips_in_neither_direction(self):
        """0.76 and its 0.7125 arm are both admitted: four comparable pairs
        across the two days, zero flips. Distance from the bar is the null
        result -- and the 2026-09-17 window was made of exactly these."""
        got = _scan({DAY: _frame(9, 9), DAY2: _frame(9, 8)})
        assert got["flips"] == []
        assert got["comparable_pairs"] == 4

    def test_the_flip_verdict_is_the_shared_tools_verdict(self):
        """The scanner must not carry a second copy of the admission
        formula: whatever single_step_counterfactual refuses to judge,
        the scanner counts as refused rather than judging itself."""
        from alpha_agents.evolution.causal_trace import (
            single_step_counterfactual,
        )
        rows = _frame(4, 9)
        board = next(r for r in rows if r["concept"] == "贴阈概念")
        gate = DEFAULT_DECISION_PARAMS["theme_gate"]
        board_row = {**board,
                     "frame_flows": [r["net_flow_yi"] for r in rows],
                     "frame_changes": [r["change_pct"] for r in rows],
                     "confirm": 0.0}
        tool = single_step_counterfactual(
            theme="贴阈概念", day=DAY, board_row=board_row,
            params_a=gate, params_b={**gate, "w_rel": gate["w_rel"] - 0.05})
        got = _scan({DAY: rows, DAY2: rows})
        assert tool["changed"] is True
        assert got["flips"][0]["score"] == tool["a"]["score"]
        assert got["flips"][0]["flip_margin"] == tool["flip_margin"]


class TestTheWindowIsCountedHonestly:
    def test_an_identical_frame_is_counted_once_and_named(self):
        """2026-09-12 (a Saturday) re-states 09-11's close frame. Counting it
        twice would double the evidence for one market day; skipping it
        silently would look exactly like a day the market had no boards."""
        rows = _frame(4, 9)
        got = _scan({DAY: rows, DAY2: [dict(r) for r in rows]})
        assert got["window"]["unique_frames"] == 1
        dup = [d for d in got["days"] if d["day"] == DAY2][0]
        assert dup["duplicate_of"] == DAY and dup["scored"] == 0
        assert len(got["flips"]) == 1, "one frame, one piece of evidence"

    def test_a_theme_the_board_does_not_name_is_counted(self):
        """An unmatched name is a denominator fact ('the board could not see
        it'), not a silent shrinkage of the theme list."""
        got = _scan({DAY: _frame(4, 9), DAY2: _frame(3, 9)},
                    names=("贴阈概念", "不存在的主题"))
        assert got["unmatched_pairs"] == 2
        assert got["comparable_pairs"] == 4


class TestTheResultIsRecomputable:
    def test_the_same_input_yields_the_same_result(self):
        """The acceptance criterion says the flip must be re-derivable from
        the same inputs. A scan with any sampling in it could not promise
        that, so equality here is the cheap half of the proof."""
        by_day = {DAY: _frame(5, 9), DAY2: _frame(4, 9)}
        first = _scan(by_day)
        second = _scan(by_day)
        assert first == second

    def test_the_scale_is_declared_with_the_result(self):
        """confirm=0 is a rebuild-scale fact: identical for both arms, so it
        cannot move a flip verdict, but it does move the absolute scores --
        and an undeclared scale is how two gates end up incomparable."""
        got = _scan({DAY: _frame(4, 9), DAY2: _frame(4, 9)})
        assert got["scale"]["gate"] == DEFAULT_DECISION_PARAMS["theme_gate"]
        assert got["scale"]["delta"] == 0.05
        assert got["scale"]["confirm"] == 0.0
        assert "confirm" in got["scale"]["note"]


class TestTheEntrypointGuardsTheBoundary:
    def test_a_window_before_the_source_is_refused(self):
        """"Same refusal as rebuild_theme_scores: sector_flow history starts
        2026-09-08, and filling earlier days from later ones writes the
        future into the past."""
        with pytest.raises(SystemExit) as e:
            TWC.main(["--start", "2026-08-01"])
        assert TWC.EARLIEST_REBUILDABLE in str(e.value)
