"""One table, one definition of a hit.

``playbooks.hit_rate`` is what the evolution loop reads to decide whether a
pattern works, and it had two writers with two definitions:

- ``review.py`` graded on excess return, and says why in its own comment:
  absolute direction scores the whole book as hits on an up day and misses
  on a down day.
- ``portfolio_exit.py`` graded on the raw return — the exact thing that
  comment warns against — and it is the caller that fires on **real closes**,
  the trades with money on them. So the strongest evidence carried the weaker
  definition, and a rising tape taught every playbook that it worked.

The benchmark leg can be missing. Then nothing is recorded: an unmeasured
trade is a gap in the evidence; a wrongly measured one is a false lesson.
"""

import sqlite3
from unittest.mock import patch

import pytest

from alpha_agents.data import scoring


class TestTheHelper:
    def test_the_market_leg_is_subtracted(self):
        with patch.object(scoring, "_market_forward_return", return_value=2.0), \
             patch.object(scoring, "_connect", return_value=sqlite3.connect(":memory:")):
            assert scoring.excess_over_market(8.0, "2026-01-09", sessions=5) == 6.0

    def test_a_stock_that_rose_less_than_the_market_is_negative(self):
        """The case the raw-return grader called a hit."""
        with patch.object(scoring, "_market_forward_return", return_value=5.0), \
             patch.object(scoring, "_connect", return_value=sqlite3.connect(":memory:")):
            assert scoring.excess_over_market(3.0, "2026-01-09", sessions=5) == -2.0

    def test_no_benchmark_leg_yields_none_not_the_raw_return(self):
        with patch.object(scoring, "_market_forward_return", return_value=None), \
             patch.object(scoring, "_connect", return_value=sqlite3.connect(":memory:")):
            assert scoring.excess_over_market(8.0, "2026-01-09", sessions=5) is None

    @pytest.mark.parametrize("sessions", [0, -1, None])
    def test_an_impossible_window_measures_nothing(self, sessions):
        assert scoring.excess_over_market(8.0, "2026-01-09", sessions=sessions) is None

    def test_a_missing_database_measures_nothing(self):
        with patch.object(scoring, "_connect", return_value=None):
            assert scoring.excess_over_market(8.0, "2026-01-09", sessions=5) is None


class TestTheCloseSiteUsesIt:
    def _source(self):
        import inspect

        from alpha_agents.data import portfolio_exit
        return inspect.getsource(portfolio_exit)

    def test_the_raw_return_definition_is_gone(self):
        assert "hit = 1 if return_pct > 0" not in self._source(), (
            "the absolute-return grader is back")

    def test_the_close_site_grades_on_excess(self):
        src = self._source()
        assert "excess_over_market" in src
        assert "hit=bool(excess > 0)" in src

    def test_both_graders_now_name_the_same_quantity(self):
        import inspect

        from alpha_agents.data import portfolio_exit
        from alpha_agents.pipeline.tasks import review
        assert "excess" in inspect.getsource(portfolio_exit)
        assert "excess_pct" in inspect.getsource(review)
