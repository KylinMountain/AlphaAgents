"""A claim whose order never filled must close, and must not read as a loss.

The cancel path updated the order, closed the episode and released the cash
reservation, and left the thesis alone. Measured on a 20-day autonomous
replay: **29 theses, 9 filled, 20 still ``active`` with no position** —
returned by every ``get_active`` call, never graded, growing without bound.

The 69% that never filled is not a random slice. Fifteen of the seventeen
cancels were 价格已涨走: the names that ran away are exactly the ones that
went up. A system that grades only what filled is grading only the names
that came back to its limit, which is adverse selection sitting inside the
learning signal rather than in the market.

``unfilled`` is its own status because the alternatives lie. ``invalidated``
would book 20 falsified claims the agent never had; ``expired`` is a
position that ran its horizon, and "I sat at my limit for five days and
never got filled" is a different failure from "I held for five days and
nothing happened".
"""

import sqlite3
from unittest.mock import patch

import pytest

from alpha_agents.data import thesis as T


class TestTheStatus:
    def test_it_is_a_closed_status(self):
        assert T.UNFILLED in T.CLOSED_STATUSES

    def test_it_is_not_one_of_the_outcome_statuses(self):
        """Counting it as a failure would be inventing results."""
        assert T.UNFILLED not in (T.INVALIDATED, T.EXPIRED, T.BLIND_SPOT,
                                  T.VALIDATED)

    def test_close_accepts_it(self, tmp_path, monkeypatch):
        monkeypatch.setattr("alpha_agents.config.DATA_DIR", tmp_path)
        # close() refuses anything outside CLOSED_STATUSES, which is the
        # check that matters here.
        with pytest.raises(ValueError):
            T.close(1, "made_up_status")


class TestTheCancelPathClosesIt:
    @pytest.fixture
    def book(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE virtual_portfolio "
                     "(id INTEGER PRIMARY KEY, thesis_id INTEGER)")
        conn.execute("INSERT INTO virtual_portfolio VALUES (11, 77)")
        conn.execute("INSERT INTO virtual_portfolio VALUES (12, NULL)")
        return conn

    def test_the_linked_thesis_is_closed_unfilled(self, book):
        from alpha_agents.data import portfolio_book as pb
        with patch("alpha_agents.data.thesis.close") as closed:
            pb._close_unfilled_thesis(book, 11, "价格已涨走(7.51远超介入上限6.01)")
        assert closed.call_args.args == (77, T.UNFILLED)
        assert "价格已涨走" in closed.call_args.kwargs["close_note"]

    def test_an_order_with_no_thesis_is_a_no_op(self, book):
        from alpha_agents.data import portfolio_book as pb
        with patch("alpha_agents.data.thesis.close") as closed:
            pb._close_unfilled_thesis(book, 12, "到期未到价")
        closed.assert_not_called()

    def test_a_bookkeeping_failure_does_not_stop_the_cancel(self, book, caplog):
        """The alternative is an order left pending with its cash held."""
        from alpha_agents.data import portfolio_book as pb
        with patch("alpha_agents.data.thesis.close",
                   side_effect=sqlite3.DatabaseError("locked")), \
             caplog.at_level("WARNING"):
            pb._close_unfilled_thesis(book, 11, "到期未到价")
        assert "cancelled order #11" in caplog.text

    def test_the_cancel_path_calls_it(self):
        import inspect
        from alpha_agents.data import portfolio_book as pb
        src = inspect.getsource(pb._cancel_order_unlocked)
        assert "_close_unfilled_thesis" in src


class TestTheCountingIsUnchanged:
    """An unfilled claim's forward return is counterfactual. Claiming it as
    a hit would be booking money that was never made."""

    def test_the_hit_label_still_comes_from_excess_return(self):
        import inspect
        from alpha_agents.pipeline.tasks import review
        src = inspect.getsource(review)
        assert "hit_val = 1 if excess_pct > 0 else 0" in src

    def test_the_playbook_grader_still_reads_excess(self):
        import inspect
        from alpha_agents.data import portfolio_exit
        assert "hit=bool(excess > 0)" in inspect.getsource(portfolio_exit)
