"""The state machine at the write path, not just in the graph.

The pure logic of the graph lives in ``tests/test_order_state.py``. This
file proves the *writes* go through it: a second cancel of a terminal
row is a logged no-op rather than a history rewrite, and the same
guarantee holds for ``_fill_order`` when a row is no longer pending.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from alpha_agents.data import attribution as A
from alpha_agents.data import order_state as OS
from alpha_agents.data import portfolio as P
from alpha_agents.data import thesis as T
from alpha_agents.data import trader as TR


@pytest.fixture()
def store(tmp_path, monkeypatch):
    from alpha_agents.data import memory_store
    monkeypatch.setattr(memory_store, "MEMORY_DB_PATH",
                        tmp_path / "memory.db", raising=False)
    monkeypatch.setattr(memory_store._local, "conn", None, raising=False)
    yield
    conn = getattr(memory_store._local, "conn", None)
    if conn is not None:
        conn.close()
    memory_store._local.conn = None


@pytest.fixture()
def traders_dir(tmp_path, monkeypatch):
    d = tmp_path / "traders"
    d.mkdir()
    monkeypatch.setattr(TR, "TRADERS_DIR", d, raising=False)
    return d


@pytest.fixture()
def theme():
    row = {"name": "t", "status": "active", "strength": 6,
           "daily_score": 1, "core_stocks": "[]"}
    with patch("alpha_agents.data.memory_store.get_active_themes",
               return_value=[row]), \
         patch("alpha_agents.data.memory_store.get_theme_by_name",
               return_value=row), \
         patch("alpha_agents.data.portfolio.get_theme_by_name",
               return_value=row), \
         patch("alpha_agents.data.sentiment_cycle.get_sentiment_cycle",
               return_value={"phase": "修复",
                             "strategy": {"max_exposure_pct": 100}}):
        yield row


def write(d, name, body):
    (d / f"{name}.yaml").write_text(body, encoding="utf-8")


SLOW = "id: slow\nname: 回调派\ncapital: 500000\n"


def _conn():
    from alpha_agents.data import memory_store
    return memory_store._get_conn()


def _thesis():
    return T.create(T.Thesis(
        code="600000", name="A", theme="t", claim="涨", prob=0.6,
        conviction=0.2, trader_id="slow",
        conditions=[T.Condition("price_below", 9.0, "止损位")]))


class TestCancelsRespectTerminality:
    def test_a_second_cancel_of_a_cancelled_row_is_a_no_op(
            self, store, traders_dir, theme, caplog):
        """The first finisher owns the reason; the second finisher logs off."""
        write(traders_dir, "slow", SLOW)
        oid = P.create_pending_order(
            code="600000", name="A", theme="t", order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, stop_loss=8.5,
            source="morning", reason="主线在流入", trader_id="slow")
        P._cancel_order(oid, "第一次取消：主线走弱")
        # Second cancel from a different rule path.
        with caplog.at_level("INFO"):
            P._cancel_order(oid, "第二次取消：价格涨走")

        row = _conn().execute(
            "SELECT status, close_reason FROM virtual_portfolio WHERE id = ?",
            (oid,)).fetchone()
        assert row["status"] == OS.CANCELLED
        assert row["close_reason"] == "第一次取消：主线走弱", \
            "the first finisher's reason is the record; the second is dropped"
        assert any("cancel ignored" in r.getMessage() for r in caplog.records)

    def test_a_cancel_of_a_filled_row_is_also_a_no_op(
            self, store, traders_dir, theme, caplog):
        """A stop on a stopped row, a stop on a filled row — same principle."""
        write(traders_dir, "slow", SLOW)
        oid = P.create_pending_order(
            code="600000", name="A", theme="t", order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, stop_loss=8.5,
            source="morning", reason="主线在流入", trader_id="slow")
        P._fill_order(
            P.get_pending_orders("slow")[0],
            fill_price=10.0, fill_date="2026-01-05")

        with caplog.at_level("INFO"):
            P._cancel_order(oid, "迟到的撤单")

        row = _conn().execute(
            "SELECT status, close_reason FROM virtual_portfolio WHERE id = ?",
            (oid,)).fetchone()
        assert row["status"] == OS.OPEN
        assert row["close_reason"] is None
        assert any("cancel ignored" in r.getMessage() for r in caplog.records)

    def test_a_cancel_of_an_unknown_order_is_logged_and_safe(
            self, store, traders_dir, theme):
        write(traders_dir, "slow", SLOW)
        # Must not raise; nothing to cancel is a logged warning, not a crash.
        P._cancel_order(999_999, "无中生有")
        assert _conn().execute(
            "SELECT COUNT(*) FROM virtual_portfolio WHERE id = ?",
            (999_999,)).fetchone()[0] == 0


class TestFillsRefuseNonPendingRows:
    def test_filling_a_cancelled_row_is_reported_not_silently_written(
            self, store, traders_dir, theme):
        """A racing cancel wins; the fill does not overwrite its history."""
        write(traders_dir, "slow", SLOW)
        oid = P.create_pending_order(
            code="600000", name="A", theme="t", order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, stop_loss=8.5,
            source="morning", reason="主线在流入", trader_id="slow")
        P._cancel_order(oid, "已撤单")

        result = P._fill_order(
            {"id": oid, "code": "600000", "name": "A", "theme": "t",
             "trader_id": "slow", "entry_low": 9.0, "entry_high": 11.0,
             "stop_loss": 8.5, "thesis_id": None},
            fill_price=10.0, fill_date="2026-01-05")

        assert result is not None
        assert result["type"] == "cancelled"
        assert "cancelled" in result["reason"]
        row = _conn().execute(
            "SELECT status FROM virtual_portfolio WHERE id = ?",
            (oid,)).fetchone()
        assert row["status"] == OS.CANCELLED, \
            "the fill did not overwrite a row the cancel had already finished"

    def test_filling_an_open_row_is_reported_not_silently_overwritten(
            self, store, traders_dir, theme):
        """An open row passed to ``_fill_order`` is the wrong domain — a
        racing fill would silently rewrite open_date / open_price / shares
        on a position the monitor is already evaluating."""
        write(traders_dir, "slow", SLOW)
        oid = P.create_pending_order(
            code="600000", name="A", theme="t", order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, stop_loss=8.5,
            source="morning", reason="主线在流入", trader_id="slow")
        P._fill_order(
            P.get_pending_orders("slow")[0],
            fill_price=10.0, fill_date="2026-01-05")
        before = _conn().execute(
            "SELECT status, open_price, shares FROM virtual_portfolio "
            "WHERE id = ?", (oid,)).fetchone()

        result = P._fill_order(
            {"id": oid, "code": "600000", "name": "A", "theme": "t",
             "trader_id": "slow", "entry_low": 9.0, "entry_high": 11.0,
             "stop_loss": 8.5, "thesis_id": None},
            fill_price=999.0, fill_date="2026-09-11")

        assert result is not None
        assert result["type"] == "cancelled"
        after = _conn().execute(
            "SELECT status, open_price, shares FROM virtual_portfolio "
            "WHERE id = ?", (oid,)).fetchone()
        assert (before["status"], before["open_price"], before["shares"]) == \
               (after["status"], after["open_price"], after["shares"]), \
            "a rejected fill must not mutate an open position"


class TestClosesRespectTheGraph:
    def test_close_uses_the_state_machine(self, store, traders_dir, theme):
        """A sanity pin: ``close_position`` asserts its transition, not just
        its shape. If the assertion disappears, the test should fail by
        *expected* exception, not by regression."""
        write(traders_dir, "slow", SLOW)
        oid = P.create_pending_order(
            code="600000", name="A", theme="t", order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, stop_loss=8.5,
            source="morning", reason="主线在流入", trader_id="slow")
        P._fill_order(
            P.get_pending_orders("slow")[0],
            fill_price=10.0, fill_date="2026-01-05")

        # Calling close_position directly with a stop reason. No ``shares``
        # argument means sell all — a full close, which is the only kind
        # the status machine closes the row on.
        from alpha_agents.data.portfolio_exit import close_position
        ok = close_position(oid, close_price=11.0, close_reason="止损触发")
        assert ok is True
        row = _conn().execute(
            "SELECT status FROM virtual_portfolio WHERE id = ?",
            (oid,)).fetchone()
        assert row["status"] == OS.STOPPED

    def test_a_close_attempt_on_a_terminal_row_returns_false(
            self, store, traders_dir, theme):
        """The state machine and the early return cooperate: a closed row
        cannot be closed again, and the function reports it instead of
        rewriting history."""
        write(traders_dir, "slow", SLOW)
        oid = P.create_pending_order(
            code="600000", name="A", theme="t", order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, stop_loss=8.5,
            source="morning", reason="主线在流入", trader_id="slow")
        P._fill_order(
            P.get_pending_orders("slow")[0],
            fill_price=10.0, fill_date="2026-01-05")
        from alpha_agents.data.portfolio_exit import close_position
        close_position(oid, close_price=11.0, close_reason="止损触发")
        ok_again = close_position(oid, close_price=12.0,
                                  close_reason="重复关闭")
        assert ok_again is False
        # The first close's data is unchanged: a second close did not
        # retcon shares or close_price.
        row = _conn().execute(
            "SELECT status, close_price, close_reason "
            "FROM virtual_portfolio WHERE id = ?",
            (oid,)).fetchone()
        assert row["status"] == OS.STOPPED
        assert row["close_price"] == 11.0
        assert row["close_reason"] == "止损触发"
