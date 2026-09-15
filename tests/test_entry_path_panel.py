"""The entry-path scaffolding has to agree with the production scorer.

Three things can go wrong quietly in ``scripts/evaluate_entry_path.py``:

* the cross-section can be built in a shape ``theme_score`` does not read, and
  then every board silently fails to match — an empty panel that looks like
  "no effect" rather than like a bug;
* the units can be wrong (元 vs 亿元), which shifts every percentile without
  changing a single rank;
* the metric can be raw instead of cross-sectional, which reports a rising
  market as a working gate.

The first is checked against the **production** function rather than a copy of
it. There is no stub of ``theme_score`` anywhere in this file, on purpose: a
stub would encode this file's belief about the shape, which is the thing under
test.
"""

import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from alpha_agents.pipeline import theme_manager
from scripts.evaluate_entry_path import (
    _METRIC_COLUMNS as METRIC_COLUMNS, add_forward_returns, band_comparison,
    cross_section_for, load_board_flow, quantile_separation,
)

GATE = {"w_flow": 0.45, "w_rel": 0.35, "w_confirm": 0.20,
        "admit_score": 0.50, "cancel_score": 0.35}


def _frame(rows):
    """A day's boards as the loader would hand them to the scorer."""
    return pd.DataFrame(rows, columns=[
        "ts_code", "board", "scope", "change_pct", "net_flow_yi", "close", "date"])


def _day(n=12, date="2025-06-02"):
    return _frame([
        (f"BK{i:04d}.DC", f"板块{i}", "concept" if i % 2 else "industry",
         float(i), float(i) * 10.0, 100.0 + i, pd.Timestamp(date))
        for i in range(n)
    ])


class TestTheShapeTheScorerReads:
    def test_rows_carry_exactly_the_production_keys(self):
        rows = cross_section_for(_day(3))
        assert len(rows) == 3
        assert set(rows[0]) == {"concept", "scope", "change_pct", "net_flow_yi"}
        assert isinstance(rows[0]["net_flow_yi"], float)
        assert isinstance(rows[0]["change_pct"], float)

    def test_the_production_scorer_consumes_them(self):
        """The integration guard.

        If ``theme_cross_section``'s keys or units ever change, this fails here
        instead of silently scoring nothing on real data.
        """
        rows = cross_section_for(_day(12))
        got = theme_manager.theme_score(rows, "板块5", 0, gate=GATE)
        assert got is not None, "the scorer could not name a board we just built"
        assert got["of"] == len(rows), "the scorer saw a different frame size"
        assert 0.0 <= got["score"] <= 1.0
        assert 1 <= got["rank"] <= len(rows)

    def test_an_unknown_board_is_none_and_not_a_zero(self):
        """``None`` means "cannot see it"; a zero would be a claim about flow."""
        rows = cross_section_for(_day(6))
        assert theme_manager.theme_score(rows, "根本不存在的板块", 0,
                                        gate=GATE) is None


class TestUnitsAndWhichBoardsAreKept:
    def _db(self, tmp_path: Path) -> Path:
        path = tmp_path / "tushare.db"
        con = sqlite3.connect(path)
        con.execute(
            "CREATE TABLE moneyflow_concept_dc (ts_code TEXT, name TEXT, "
            "trade_date TEXT, content_type TEXT, pct_change REAL, "
            "net_amount REAL, close REAL)")
        con.executemany(
            "INSERT INTO moneyflow_concept_dc VALUES (?,?,?,?,?,?,?)", [
                ("BK0001.DC", "半导体", "20250901", "概念", 2.5, 1_500_000_000.0, 1000.0),
                ("BK0002.DC", "银行", "20250901", "行业", -0.5, -200_000_000.0, 900.0),
                ("BK0003.DC", "上海板块", "20250901", "地域", 1.0, 50_000_000.0, 800.0),
                ("BK0004.DC", "无名", "20250901", None, 1.0, 10_000_000.0, 700.0),
            ])
        con.commit()
        con.close()
        return path

    def test_yuan_becomes_yi(self, tmp_path):
        frame = load_board_flow(self._db(tmp_path), "2025-09-01", "2025-09-01")
        got = {r.board: r.net_flow_yi for r in frame.itertuples()}
        assert got["半导体"] == pytest.approx(15.0)
        assert got["银行"] == pytest.approx(-2.0)

    def test_region_boards_stay_out_of_the_frame(self, tmp_path):
        """A percentile is taken over the frame it is given.

        ``theme_cross_section`` feeds concepts and industries only, so a 地域
        board here would shift every other board's percentile — an error that
        changes the answer without changing any single row's own numbers.
        """
        frame = load_board_flow(self._db(tmp_path), "2025-09-01", "2025-09-01")
        assert sorted(frame["scope"]) == ["concept", "industry"]
        assert "上海板块" not in set(frame["board"])
        assert "无名" not in set(frame["board"])

    def test_a_missing_database_is_an_error_not_an_empty_panel(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="external input"):
            load_board_flow(tmp_path / "nope.db", "2025-01-01", "2025-02-01")


class TestForwardReturnsAndTheirExcess:
    def test_the_window_is_shifted_within_each_board(self):
        """Two boards with different histories must not borrow each other's rows.

        Board A has four days, board B three. A shift taken over the whole
        frame would hand B's first row to A's last one.
        """
        rows = []
        for date, a, b in (("2025-06-02", 100.0, 200.0), ("2025-06-03", 110.0, 190.0),
                           ("2025-06-04", 121.0, 180.0), ("2025-06-05", 133.1, None)):
            rows.append(("A.DC", "甲", "concept", 0.0, 0.0, a, pd.Timestamp(date)))
            if b is not None:
                rows.append(("B.DC", "乙", "concept", 0.0, 0.0, b, pd.Timestamp(date)))
        out = add_forward_returns(_frame(rows), horizon=1)
        by = {(r.ts_code, str(r.date.date())): r.fwd for r in out.itertuples()}
        assert by[("A.DC", "2025-06-02")] == pytest.approx(0.10)
        assert by[("B.DC", "2025-06-02")] == pytest.approx(-0.05)

    def test_an_incomplete_window_stays_nan_rather_than_becoming_zero(self):
        rows = [("A.DC", "甲", "concept", 0.0, 0.0, 100.0, pd.Timestamp("2025-06-02")),
                ("A.DC", "甲", "concept", 0.0, 0.0, 101.0, pd.Timestamp("2025-06-03"))]
        out = add_forward_returns(_frame(rows), horizon=5)
        assert out["fwd"].isna().all()

    def test_excess_is_measured_against_the_same_dates_boards(self):
        """The reason raw returns are not the headline metric.

        Two boards on one date: +10% and 0%. The date's median is +5%, so the
        excess is ±5%. Without this step a market-wide rise reads as skill.
        """
        rows = [("A.DC", "甲", "concept", 0.0, 0.0, 100.0, pd.Timestamp("2025-06-02")),
                ("B.DC", "乙", "concept", 0.0, 0.0, 100.0, pd.Timestamp("2025-06-02")),
                ("A.DC", "甲", "concept", 0.0, 0.0, 110.0, pd.Timestamp("2025-06-03")),
                ("B.DC", "乙", "concept", 0.0, 0.0, 100.0, pd.Timestamp("2025-06-03"))]
        out = add_forward_returns(_frame(rows), horizon=1)
        got = {r.ts_code: r.excess for r in out.itertuples()
               if str(r.date.date()) == "2025-06-02"}
        assert got["A.DC"] == pytest.approx(0.05)
        assert got["B.DC"] == pytest.approx(-0.05)


class TestTheMetricFlagMapsToARealColumn:
    def test_every_choice_selects_a_column_the_pipeline_produces(self):
        """The cheapest possible guard, against a crash that happened.

        ``--metric raw`` was passed straight to pandas as a column name and
        died with ``KeyError: ['raw']`` — the flag names the *reading*, not the
        column. Both sides are checked here so adding a reading without adding
        its column fails in the suite rather than at 2am on real data.
        """
        rows = [("A.DC", "甲", "concept", 0.0, 0.0, 100.0, pd.Timestamp("2025-06-02")),
                ("A.DC", "甲", "concept", 0.0, 0.0, 110.0, pd.Timestamp("2025-06-03"))]
        columns = set(add_forward_returns(_frame(rows), horizon=1).columns)
        assert set(METRIC_COLUMNS) == {"excess", "raw"}
        assert set(METRIC_COLUMNS.values()) <= columns, (
            "a --metric choice selects a column the pipeline never builds")


class TestTheTwoReadings:
    def _panel(self, scores, outcomes, date="2025-06-02", value="excess"):
        return pd.DataFrame({
            "date": [pd.Timestamp(date)] * len(scores),
            "ts_code": [f"BK{i:04d}.DC" for i in range(len(scores))],
            "board": [f"板块{i}" for i in range(len(scores))],
            "scope": ["concept"] * len(scores),
            "score": list(scores),
            value: list(outcomes),
        })

    def test_quintiles_are_averaged_per_date_not_pooled(self):
        """A 500-board date must not outvote a 5-board one.

        Date A separates cleanly; date B is flat. Equal weight means the answer
        sits between them, not on whichever date had more boards.
        """
        big = self._panel(range(10), [i / 100.0 for i in range(10)],
                          date="2025-06-02")
        small = self._panel([0, 0.1, 0.2, 0.3, 0.4], [0.0] * 5,
                            date="2025-06-03")
        seps = quantile_separation(pd.concat([big, small], ignore_index=True))
        assert list(seps["bucket"]) == [0, 1, 2, 3, 4]
        assert (seps["n_dates"] == 2).all()
        assert seps["median"].iloc[-1] > seps["median"].iloc[0]

    def test_a_flat_panel_shows_no_separation(self):
        """The control: if this could invent a slope, no slope would mean nothing."""
        seps = quantile_separation(self._panel(range(10), [0.01] * 10))
        assert seps["median"].nunique() == 1

    def test_a_date_with_too_few_boards_is_dropped_not_forced_into_quintiles(self):
        """Cutting 4 boards into 5 buckets would invent structure."""
        thin = self._panel(range(4), [0.0, 0.01, 0.02, 0.03])
        assert quantile_separation(thin).empty

    def test_the_band_comparison_counts_both_sides_and_its_dates(self):
        panel = self._panel([0.9, 0.4, 0.2, 0.1], [0.05, 0.01, -0.01, -0.03])
        got = band_comparison(panel, GATE)
        assert got["n_dates"] == 1
        assert got["n_above_admit"] == 1          # 0.9 >= 0.50
        assert got["n_band"] == 1                 # 0.40 is between 0.35 and 0.50
        assert got["n_below_cancel"] == 2         # 0.20 and 0.10 are under 0.35
        assert got["dates_above_admit"] == 1
        # above: median([0.05]) = 0.05
        # below: median([-0.01, -0.03]) = -0.02   → 0.05 − (−0.02) = 0.07
        assert got["gap_above_minus_below"] == pytest.approx(0.07)
