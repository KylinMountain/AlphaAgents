"""There is no system exit line; only the trader's own orders execute.

With ``AGENT_EXIT_DECISIONS`` on, ``check_positions(hard_only=True)`` used to
close anything down ``HARD_STOP_PCT`` from cost and anything on an archived
theme, whatever the trader said. Both were exits the system chose, so the
outcome of such a trade measured the rule. Now the only levels that close a
position without asking are the stop and target the trader wrote on the
order — read from ``initial_stop_loss``, never from ``stop_loss``, which the
trailing and bearish rules rewrite.
"""

from unittest.mock import patch

from alpha_agents.data.portfolio import check_positions
from tests.test_trailing_stop import TRADER, _row, _seed, book  # noqa: F401

TODAY = "2026-09-16"


def _check(price: float, code: str = "000510") -> list[dict]:
    return check_positions({code: price}, TODAY, hard_only=True,
                           trader_id=TRADER)


def test_a_deep_loss_without_an_own_stop_stays_open(book):
    pid = _seed(book, open_price=10.0, stop_loss=None)
    alerts = _check(8.0)                      # −20%, far past the old 8% floor
    assert _row(book, pid)["status"] == "open"
    assert not [a for a in alerts if a.get("type") in ("stopped", "expired")]


def test_the_traders_own_stop_executes(book):
    pid = _seed(book, open_price=10.0, stop_loss=9.0, initial_stop_loss=9.0)
    alerts = _check(8.9)
    row = _row(book, pid)
    assert row["status"] != "open"
    assert "自设止损" in row["close_reason"]
    assert any(a.get("type") == "stopped" for a in alerts)


def test_the_traders_own_target_executes(book):
    pid = _seed(book, open_price=10.0, stop_loss=None)
    book.execute("UPDATE virtual_portfolio SET target_price = 11.0 WHERE id = ?",
                 (pid,))
    book.commit()
    _check(11.2)
    row = _row(book, pid)
    assert row["status"] != "open"
    assert "自设止盈" in row["close_reason"]


def test_a_system_trailed_stop_is_only_a_signal(book):
    """``stop_loss`` 9.5 is above the trader's own 8.0: that gap is the
    system's rule. Crossing it tells the trader; it does not sell."""
    pid = _seed(book, open_price=10.0, stop_loss=9.5, initial_stop_loss=8.0)
    alerts = _check(9.2)
    assert _row(book, pid)["status"] == "open"
    assert any(a.get("type") == "signal" and a.get("would_have") == "stopped"
               for a in alerts)


def test_an_archived_theme_no_longer_forces_a_close(book):
    pid = _seed(book, open_price=10.0, stop_loss=None, theme="旧主线")
    with patch("alpha_agents.data.position_monitor.get_theme_by_name",
               return_value={"status": "archived", "strength": 0}):
        alerts = _check(10.1)
    assert _row(book, pid)["status"] == "open"
    assert any(a.get("type") == "signal" for a in alerts)
