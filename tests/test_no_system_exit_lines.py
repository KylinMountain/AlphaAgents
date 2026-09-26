"""No price sells a position on the trader's behalf.

``check_positions(hard_only=True)`` used to close anything down
``HARD_STOP_PCT`` from cost or on an archived theme, and the order's own stop
and target closed it too. Each of those is a price deciding the exit, so the
trade's outcome measured the price rather than the trader. Now every trigger
reaches the trader as a signal and nothing is closed here (2026-09-26).
"""

from unittest.mock import patch

from alpha_agents.data.portfolio import check_positions
from tests.test_trailing_stop import TRADER, _row, _seed, book  # noqa: F401

TODAY = "2026-09-16"


def _check(price: float, code: str = "000510") -> list[dict]:
    return check_positions({code: price}, TODAY, hard_only=True,
                           trader_id=TRADER)


def test_a_deep_loss_stays_open(book):
    pid = _seed(book, open_price=10.0, stop_loss=None)
    _check(8.0)                               # −20%, far past the old 8% floor
    assert _row(book, pid)["status"] == "open"


def test_the_orders_stop_is_only_a_signal(book):
    pid = _seed(book, open_price=10.0, stop_loss=9.0, initial_stop_loss=9.0)
    alerts = _check(8.9)
    assert _row(book, pid)["status"] == "open"
    assert any(a.get("type") == "signal" and a.get("would_have") == "stopped"
               for a in alerts)


def test_the_orders_target_is_only_a_signal(book):
    pid = _seed(book, open_price=10.0, stop_loss=None)
    book.execute("UPDATE virtual_portfolio SET target_price = 11.0 WHERE id = ?",
                 (pid,))
    book.commit()
    alerts = _check(11.2)
    assert _row(book, pid)["status"] == "open"
    assert any(a.get("would_have") == "target_hit" for a in alerts)


def test_an_archived_theme_no_longer_forces_a_close(book):
    pid = _seed(book, open_price=10.0, stop_loss=None, theme="旧主线")
    with patch("alpha_agents.data.position_monitor.get_theme_by_name",
               return_value={"status": "archived", "strength": 0}):
        alerts = _check(10.1)
    assert _row(book, pid)["status"] == "open"
    assert any(a.get("type") == "signal" for a in alerts)
