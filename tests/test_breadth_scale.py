"""``breadth_below`` must mean one thing, and the agent must be told which.

Three defects sharing one root, found by reading the values an autonomous
replay actually wrote:

1. ``_CONDITIONS`` described the quantity as "市场涨跌比" with an empty unit.
   Nothing said whether the number was a 0–1 fraction or a percentage, so the
   agent guessed both: of eleven conditions written in one run, seven were on
   a 0–1 scale (0.35 … 0.6) and four were 30, 40, 50 and 1.0. On the fraction
   the evaluator actually reads, every one of those four is permanently true.
2. ``_SANITY`` carried no bound for the kind, so all four were stored.
3. The two producers fed different quantities. ``walk_forward`` computed
   ``up / len(bars)`` — bounded, 0–1 — while ``intraday_monitor`` read
   ``ad_ratio``, which is ``advances / declines``: unbounded, and 999 on a
   day with no decliners. The same thesis therefore exited at a different
   market in the backtest than in production.

The bound comes from the data, not from taste. Over the 116 trading days of
2026-H1 the up-fraction ran 0.056..0.939, median 0.444 — so a threshold above
0.5 fires on most days and one below 0.1 never fired at all.
"""

import json

import pytest

from alpha_agents.data import thesis as T


class TestScaleErrorsAreDropped:
    """The four values one real run produced on the wrong scale."""

    @pytest.mark.parametrize("value", [30, 40, 50, 1.0])
    def test_a_percentage_scale_value_is_refused(self, value):
        assert T.validate_condition(
            {"kind": "breadth_below", "value": value, "note": "大盘转弱"}) is None

    @pytest.mark.parametrize("value", [0.35, 0.45, 0.5])
    def test_a_fraction_scale_value_survives(self, value):
        cond = T.validate_condition(
            {"kind": "breadth_below", "value": value, "note": "大盘转弱"})
        assert cond is not None and cond.value == value

    def test_a_threshold_that_fires_on_most_days_is_refused(self):
        """0.6 is below the measured p75 of 0.658: it exits nearly always."""
        assert T.validate_condition(
            {"kind": "breadth_below", "value": 0.6, "note": "x"}) is None

    def test_the_drop_is_logged_with_the_offending_value(self, caplog):
        with caplog.at_level("WARNING"):
            T.validate_condition({"kind": "breadth_below", "value": 50})
        assert "breadth_below" in caplog.text and "50" in caplog.text


class TestTheAgentIsToldTheScale:
    def test_the_vocabulary_names_the_quantity_and_its_range(self):
        line = next(ln for ln in T.prompt_vocabulary().splitlines()
                    if "breadth_below" in ln)
        assert "占比" in line, "the quantity must be named a fraction"
        assert "0–1" in line, "the scale must be stated, not guessed"

    def test_the_vocabulary_carries_the_base_rate(self):
        """A threshold is only meaningful against how often it fires."""
        line = next(ln for ln in T.prompt_vocabulary().splitlines()
                    if "breadth_below" in ln)
        assert "0.44" in line

    def test_the_vocabulary_no_longer_says_涨跌比(self):
        """That phrase is the advance/decline ratio — the wrong quantity."""
        line = next(ln for ln in T.prompt_vocabulary().splitlines()
                    if "breadth_below" in ln)
        assert "涨跌比" not in line


class TestBothProducersFeedTheSameQuantity:
    def _view(self, monkeypatch, payload):
        from alpha_agents.pipeline.tasks import intraday_monitor as im
        monkeypatch.setattr(im, "get_concept_ranking_fn",
                            lambda **kw: json.dumps({"gainers": [], "losers": []}))
        monkeypatch.setattr(im, "get_market_breadth_fn",
                            lambda: json.dumps(payload))
        return im._market_view()

    def test_production_reports_the_up_fraction(self, monkeypatch):
        out = self._view(monkeypatch, {"advances": 1200, "declines": 3300,
                                       "flat": 500, "total": 5000,
                                       "ad_ratio": 0.36})
        assert out["breadth_ratio"] == 0.24, "1200/5000, not advances/declines"

    def test_the_unbounded_ratio_is_not_used(self, monkeypatch):
        """ad_ratio is 999 when nothing fell; as a breadth_ratio that would
        keep every thesis alive through the one session it should not."""
        out = self._view(monkeypatch, {"advances": 4800, "declines": 0,
                                       "flat": 200, "total": 5000,
                                       "ad_ratio": 999})
        assert out["breadth_ratio"] == 0.96

    def test_a_payload_without_the_counts_measures_nothing(self, monkeypatch):
        """Missing input must not read as a breadth of zero, which would fire
        every condition at once."""
        out = self._view(monkeypatch, {"ad_ratio": 0.8})
        assert "breadth_ratio" not in out

    def test_the_replay_and_production_agree_on_one_day(self, monkeypatch):
        """The replay's formula, side by side with production's."""
        bars = {f"{i:06d}": {"change_pct": 1.0 if i < 1200 else -1.0}
                for i in range(5000)}
        up = sum(1 for r in bars.values() if (r.get("change_pct") or 0) > 0)
        replay = round(up / len(bars), 4)
        out = self._view(monkeypatch, {"advances": 1200, "declines": 3800,
                                       "flat": 0, "total": 5000})
        assert out["breadth_ratio"] == replay
