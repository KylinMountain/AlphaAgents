"""G1 — probabilistic forecasts graded on Brier and factor residual."""

import math
import sqlite3

import pytest

from alpha_agents.data import policy_registry as PR
from alpha_agents.data import scoring
from alpha_agents.data.scoring import (
    _ols_residual, brier_score, confidence_to_prob, log_score, summarize_scores,
)


def _sources(decision: dict) -> dict:
    """A complete source set whose pointer-controlled block is ``decision``."""
    return {
        "prompts": {}, "model": {}, "retrieval": {}, "rules": {},
        "knowledge": {"snapshot_id": None}, "decision": decision,
    }

#: A market that trades Monday to Friday. The five dates are real ones —
#: 2026-09-07 Mon through 2026-09-11 Fri — because the D10 bug is about the
#: weekend sitting inside a calendar horizon, and a made-up weekday list
#: would not have caught it.
_WEEK = ("2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11")

_DAILY_KLINE = """
CREATE TABLE daily_kline (
    code TEXT NOT NULL, date TEXT NOT NULL, open REAL, high REAL, low REAL,
    close REAL, volume INTEGER, turnover_rate REAL, change_pct REAL,
    PRIMARY KEY (code, date));
CREATE INDEX idx_kline_date ON daily_kline(date);
"""


@pytest.fixture()
def market(tmp_path, monkeypatch):
    """A ``market_history.db`` for ``scoring`` to read, and its connection.

    A real file, not ``:memory:``: ``scoring._connect`` opens the path
    read-only if it exists, so the predicate can only be exercised through
    storage.
    """
    path = tmp_path / "market_history.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(_DAILY_KLINE)
    monkeypatch.setattr(scoring, "DB_PATH", path)
    yield conn
    conn.close()


def _bars(conn, dates, *, codes=("600000",), close=10.0):
    for date in dates:
        for code in codes:
            conn.execute(
                "INSERT INTO daily_kline (code, date, close, volume, "
                "turnover_rate) VALUES (?, ?, ?, 1000, 1.0)",
                (code, date, close))
    conn.commit()


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


class TestEvidenceWindowClosed:
    """Has the market traded the forecast window shut?

    This is the question the calendar cannot answer, and answering it wrong is
    D10: a 5-trading-day window was declared as ``date + 5`` calendar days, so
    the due test handed over forecasts whose forward return did not exist yet —
    and the evaluator censored them, writing "the horizon arrived and the
    evidence did not" for calls nobody had waited on.
    """

    def test_five_calendar_days_are_four_trading_days(self, market):
        """The exact shape: a Mon-Fri week is five dates, and from Tuesday it
        is four — one short of a five-day window's six bars."""
        _bars(market, _WEEK)
        assert scoring.evidence_window_closed("2026-09-07", 5) is False
        assert scoring.evidence_window_closed("2026-09-08", 5) is False
        # One fewer day of horizon is what those four bars actually support.
        assert scoring.evidence_window_closed("2026-09-08", 3) is True

    def test_the_entry_bar_is_the_first_bar_of_the_window(self, market):
        """``_forward_return`` reads ``horizon + 1`` rows starting at the
        entry, so five bars support a four-day horizon, not five."""
        _bars(market, _WEEK)
        assert scoring.evidence_window_closed("2026-09-07", 4) is True
        assert scoring.evidence_window_closed("2026-09-07", 5) is False

    def test_one_more_bar_closes_a_five_day_window(self, market):
        _bars(market, _WEEK + ("2026-09-14",))
        assert scoring.evidence_window_closed("2026-09-07", 5) is True

    def test_it_reads_the_market_not_the_stock(self, market):
        """A stock with no bars at all must not look like an open window.

        The window belongs to the calendar the comparison runs on. If it were
        read from the stock's own series, every suspended name would sit
        "not yet ripe" for ever instead of being censored honestly.
        """
        _bars(market, _WEEK, codes=("600001",))
        assert scoring.evidence_window_closed("2026-09-07", 4) is True
        assert scoring.evidence_window_closed("2026-09-07", 5) is False

    def test_it_agrees_with_the_grader_it_guards(self, market):
        """The predicate exists to describe ``_forward_return``'s limit.

        If the two can disagree, the guard is decoration: a closed window that
        returns no number is a censored forecast, and an open one that returns
        a number is a window that closed early.
        """
        _bars(market, _WEEK)
        conn = scoring._connect()
        try:
            assert scoring._forward_return(
                conn, "600000", "2026-09-07", 5) is None
            assert scoring._forward_return(
                conn, "600000", "2026-09-07", 4) is not None
        finally:
            conn.close()

    def test_an_absent_archive_is_not_a_closed_window(self, tmp_path,
                                                     monkeypatch):
        """A refusal to claim, not a claim. With no archive we cannot
        establish that the window shut, so the caller keeps the forecast
        pending rather than censoring it on a guess."""
        monkeypatch.setattr(scoring, "DB_PATH", tmp_path / "nothing.db")
        assert scoring.evidence_window_closed("2026-09-07", 5) is False

    def test_an_archive_without_a_kline_table_is_not_closed(self, market):
        market.execute("DROP TABLE daily_kline")
        market.commit()
        assert scoring.evidence_window_closed("2026-09-07", 5) is False


class TestWindowProgress:
    """The counts behind the boolean, so "not ripe yet" can be *said*.

    ``evidence_window_closed`` answers a yes/no question, and the Learn page
    needs more than that: how far along is each batch, how many trading days
    are left. If the page re-derived that from its own arithmetic, the two
    copies would eventually disagree — one of them would call a window shut
    that the other keeps open, and the grader would be the wrong one. So the
    counts live here, next to the predicate, and the predicate reads them.
    """

    def test_the_counts_match_the_boolean(self, market):
        """have/need/remaining are the predicate, stated as numbers."""
        _bars(market, _WEEK)
        progress = scoring.window_progress("2026-09-07", 4)
        assert progress["closed"] is True
        assert progress["have"] == 5
        assert progress["need"] == 5
        assert progress["remaining"] == 0

        progress = scoring.window_progress("2026-09-07", 5)
        assert progress["closed"] is False
        assert progress["have"] == 5
        assert progress["need"] == 6
        assert progress["remaining"] == 1

    def test_a_refusal_is_none_not_a_count(self, tmp_path, monkeypatch):
        """An unreadable archive is not "0 days done" — nothing is knowable.

        ``remaining: 5`` would read as a patient countdown; the honest value
        for "we cannot see the market" is no value at all, so the page can
        refuse to display a progress bar over an archive that is not there.
        """
        monkeypatch.setattr(scoring, "DB_PATH", tmp_path / "nothing.db")
        assert scoring.window_progress("2026-09-07", 5) is None


class TestTheMappingComesFromThePointer:
    """The mapping is policy, so the version in force decides it.

    Reading the module constants at decision time is what made ``promote``
    inert: the registry refuses to move the pointer to a version the live
    configuration does not hash to, so a permitted promotion was always one
    whose numbers were already running. Two answers to "which mapping is the
    trader using" is one answer too many — they stop agreeing the moment the
    pointer moves, and the one that is read wins.
    """

    def test_nothing_in_force_uses_the_code_defaults(self):
        assert (scoring.in_force_decision_params()
                == scoring.DEFAULT_DECISION_PARAMS)
        assert (confidence_to_prob("high")
                == scoring.DEFAULT_DECISION_PARAMS["confidence_priors"]["high"])

    def test_the_version_in_force_decides(self):
        bolder = {**scoring.DEFAULT_DECISION_PARAMS,
                  "confidence_priors": {"high": 0.72, "medium": 0.60,
                                        "low": 0.45}}
        version = PR.freeze(sources=_sources(bolder), created_by="kylin",
                            reason="a bolder mapping")
        assert confidence_to_prob("high") == pytest.approx(0.58), \
            "frozen is not the same thing as in force"
        PR.install(version_id=version, actor="kylin", reason="first policy")
        assert confidence_to_prob("high") == pytest.approx(0.72)
        # The evidence-count branch moves with the same block.
        assert confidence_to_prob(dims_passed=3) == pytest.approx(0.56)

    def test_an_uninstalled_version_changes_nothing(self):
        PR.freeze(sources=_sources({**scoring.DEFAULT_DECISION_PARAMS,
                                    "dim_step": 0.09}),
                  created_by="kylin", reason="never installed")
        assert confidence_to_prob(dims_passed=4) == pytest.approx(0.60)

    def test_an_unknown_version_reads_the_defaults(self):
        assert scoring.decision_params_of(None) == scoring.DEFAULT_DECISION_PARAMS
        assert (scoring.decision_params_of(4242)
                == scoring.DEFAULT_DECISION_PARAMS)

    def test_a_partial_block_reads_over_the_defaults(self):
        """An undeclared parameter is a default, not a missing decision.

        The alternative — raising — stops a decision on the trading path, and
        the version's record still says exactly what it declared: the merge
        happens in one place and the next freeze writes the block back whole.
        """
        version = PR.freeze(sources=_sources({"dim_step": 0.09}),
                            created_by="kylin", reason="one key moved")
        params = scoring.decision_params_of(version)
        assert params["dim_step"] == 0.09
        assert params["dim_base"] == scoring.DEFAULT_DECISION_PARAMS["dim_base"]
        assert (params["confidence_priors"]
                == scoring.DEFAULT_DECISION_PARAMS["confidence_priors"])
