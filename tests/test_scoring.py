"""G1 — probabilistic forecasts graded on Brier and factor residual."""

import math

import pytest

from alpha_agents.data.scoring import (
    _ols_residual, brier_score, confidence_to_prob, log_score, summarize_scores,
)


class TestBrierScore:
    """Bounded proper scoring rule; 0.25 is the p=0.5 reference."""

    @pytest.mark.parametrize("prob,outcome,expected", [
        (1.0, True, 0.0),      # perfect
        (0.0, False, 0.0),     # perfect
        (0.5, True, 0.25),     # shrug
        (0.5, False, 0.25),
        (0.9, True, 0.01),
        (0.9, False, 0.81),    # confident and wrong is the expensive case
    ])
    def test_known_values(self, prob, outcome, expected):
        assert brier_score(prob, outcome) == pytest.approx(expected)

    def test_confident_error_costs_more_than_a_shrug(self):
        assert brier_score(0.95, False) > brier_score(0.5, False)

    def test_probability_is_clamped(self):
        assert brier_score(1.7, True) == 0.0
        assert brier_score(-0.4, False) == 0.0


class TestLogScore:
    def test_perfect_call_scores_near_zero(self):
        assert log_score(1.0, True) < 1e-5

    def test_confident_error_is_finite_not_infinite(self):
        # p=0 that lands would be -log(0); the clip keeps it usable.
        assert math.isfinite(log_score(0.0, True))
        assert log_score(0.0, True) > 10

    def test_harsher_than_brier_on_confident_errors(self):
        assert log_score(0.99, False) > brier_score(0.99, False)


class TestConfidenceToProb:
    """Seeds must sit near 0.5 — the system has no track record yet."""

    def test_ordinal_labels_are_modest(self):
        for label in ("high", "medium", "low"):
            assert 0.49 <= confidence_to_prob(label) <= 0.60

    def test_high_beats_medium_beats_low(self):
        assert (confidence_to_prob("high") > confidence_to_prob("medium")
                > confidence_to_prob("low"))

    def test_unknown_label_is_a_coin_flip(self):
        assert confidence_to_prob("???") == 0.50
        assert confidence_to_prob(None) == 0.50

    def test_dimension_count_is_monotonic(self):
        probs = [confidence_to_prob(dims_passed=d) for d in range(5)]
        assert probs == sorted(probs)
        assert probs[0] < 0.5 < probs[4]

    def test_dimension_count_wins_over_label(self):
        # 0/4 dims must not come out as a confident call.
        assert confidence_to_prob("high", dims_passed=0) < 0.5

    def test_output_stays_inside_sane_bounds(self):
        for d in (-5, 0, 4, 99):
            assert 0.35 <= confidence_to_prob(dims_passed=d) <= 0.75


class TestOlsResidual:
    def test_recovers_a_planted_residual(self):
        # y = 2 + 3x with one point sitting 5 above the line.
        xs = [[float(i)] for i in range(40)]
        ys = [2 + 3 * i for i in range(40)]
        resid = _ols_residual(ys, xs, y0=2 + 3 * 10 + 5, x0=[10.0])
        assert resid == pytest.approx(5.0, abs=0.2)

    def test_point_on_the_line_has_no_residual(self):
        xs = [[float(i)] for i in range(40)]
        ys = [2 + 3 * i for i in range(40)]
        assert _ols_residual(ys, xs, y0=2 + 3 * 10, x0=[10.0]) == pytest.approx(0.0, abs=0.2)

    def test_too_few_points_returns_none(self):
        assert _ols_residual([1.0, 2.0], [[1.0], [2.0]], 1.0, [1.0]) is None

    def test_no_factors_returns_none(self):
        assert _ols_residual([1.0] * 20, [[]] * 20, 1.0, []) is None


class TestSummarizeScores:
    def _rows(self, pairs):
        return [{"brier": brier_score(p, o), "outcome": o,
                 "excess_return": 1.0 if o else -1.0,
                 "residual_alpha": 0.5 if o else -0.5}
                for p, o in pairs]

    def test_empty_input(self):
        assert summarize_scores([]) == {"n": 0}
        assert summarize_scores([{"brier": None}]) == {"n": 0}

    def test_informative_probabilities_score_positive_skill(self):
        rows = self._rows([(0.9, True)] * 8 + [(0.1, False)] * 8)
        assert summarize_scores(rows)["brier_skill"] > 0.9

    def test_constant_probability_has_no_skill(self):
        # p=0.5 everywhere is exactly the reference forecast.
        rows = self._rows([(0.5, True)] * 5 + [(0.5, False)] * 5)
        assert summarize_scores(rows)["brier_skill"] == pytest.approx(0.0)

    def test_backwards_probabilities_score_negative_skill(self):
        rows = self._rows([(0.9, False)] * 6 + [(0.1, True)] * 6)
        assert summarize_scores(rows)["brier_skill"] < -1.0

    def test_reports_medians_and_counts(self):
        s = summarize_scores(self._rows([(0.6, True)] * 3 + [(0.6, False)]))
        assert s["n"] == 4
        assert s["hit_rate"] == pytest.approx(0.75)
        assert s["median_excess"] == pytest.approx(1.0)
        assert s["n_with_residual"] == 4

    def test_missing_residuals_are_counted_separately(self):
        rows = self._rows([(0.6, True), (0.6, False)])
        rows[0]["residual_alpha"] = None
        s = summarize_scores(rows)
        assert s["n"] == 2 and s["n_with_residual"] == 1
