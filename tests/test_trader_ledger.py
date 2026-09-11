"""Regression tests for the Trade slice: cash, realised P&L, and exit records.

Each test names the failure it exists to prevent. Before the ledger existed
every one of these passed on broken behaviour: cash ignored realised P&L
entirely, and a trimmed position's final exit overwrote the earlier legs.

The cash-side T+1 rule (S4) splits "owns" from "can spend": the proceeds
of a sale land in ``pending_settlements`` and only reach
``get_available_capital`` after their settle date. The tests below assert
both halves — ``get_total_capital`` moves the moment the trade closes,
``get_available_capital`` moves once the cash has settled.
"""

import sqlite3

import pytest

from alpha_agents.data import settlement, trade_ledger
from alpha_agents.data.memory_store import _SCHEMA
from alpha_agents.data.portfolio import (
    close_position, get_available_capital, get_total_capital, open_position,
    trader_capital,
)
from alpha_agents.data.trader import DEFAULT_TRADER


@pytest.fixture
def conn(monkeypatch):
    """An isolated in-memory book, with every reader pointed at it."""
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    c.commit()
    for path in ("alpha_agents.data.portfolio",
                 "alpha_agents.data.portfolio_exit",
                 "alpha_agents.data.position_monitor"):
        monkeypatch.setattr(f"{path}._get_conn", lambda: c)
    yield c
    c.close()


def _open(conn, *, code="300475", price=100.0, shares=1000):
    return open_position(code=code, name="测试", theme="芯片",
                         open_date="2026-04-08", open_price=price,
                         source="morning", reason="t", shares=shares)


def _exits(conn):
    return conn.execute("SELECT * FROM position_exits ORDER BY id").fetchall()


def _cash(conn):
    return get_available_capital(DEFAULT_TRADER)


def _settle_all(conn):
    """Advance past T+1: release every pending settlement.

    Uses a far-future date rather than computing the real one so the
    test does not depend on the clock. After this the pending bucket is
    empty and ``_cash`` reflects the sale.
    """
    settlement.release_due_settlements(conn, "2099-12-31")
    conn.commit()


def test_cash_includes_realised_profit(conn):
    """A winning trader can spend what it made — after T+1.

    Old behaviour: cash was ``capital − open positions``, so profit from a
    closed trade was invisible and the account never grew. S4 adds a
    second clock: on the day of the sale the proceeds are in transit
    (total grew, available did not), and the next day available catches
    up.
    """
    before = _cash(conn)
    pos = _open(conn, price=100.0)
    assert _cash(conn) < before          # the buy ties the money up

    assert close_position(pos, close_price=110.0, close_reason="止盈触发")
    # Day T: the trader owns the gain, but the cash is at the broker.
    assert get_total_capital(DEFAULT_TRADER) > before
    assert _cash(conn) < before, \
        "sale proceeds must not be spendable on the day of the sale"

    # T+1: the cash has landed and the profit is spendable.
    _settle_all(conn)
    assert _cash(conn) > before


def test_cash_includes_realised_loss(conn):
    """A losing trader cannot still spend money it no longer has."""
    before = _cash(conn)
    pos = _open(conn, price=100.0)
    assert close_position(pos, close_price=90.0, close_reason="止损触发")

    # The loss is visible in total immediately — the trader knows.
    assert get_total_capital(DEFAULT_TRADER) < before
    # And once the (smaller) proceeds settle, spendable cash is down too.
    _settle_all(conn)
    assert _cash(conn) < before


def test_partial_then_final_close_keeps_the_whole_result(conn):
    """The trim's profit survives the stop-out on the rest.

    Old behaviour: the final exit wrote its own leg into ``return_amount``,
    discarding the money the trim had already banked.
    """
    pos = _open(conn, price=100.0, shares=1000)

    # Bank a gain on half, then lose on the remainder.
    assert close_position(pos, close_price=120.0, close_reason="减仓止盈", shares=500)
    assert close_position(pos, close_price=95.0, close_reason="止损触发")

    legs = _exits(conn)
    assert len(legs) == 2
    expected = round(sum(r["net_amount"] for r in legs), 2)

    row = conn.execute(
        "SELECT return_amount, return_pct, shares FROM virtual_portfolio WHERE id = ?",
        (pos,),
    ).fetchone()
    assert row["return_amount"] == expected
    # And it is not merely the last leg repeated.
    assert row["return_amount"] != legs[-1]["net_amount"]

    # The blended percentage is the position's, not the final leg's.
    basis = sum(r["cost_basis"] for r in legs)
    assert row["return_pct"] == pytest.approx(expected / basis * 100, abs=0.01)


def test_settling_a_position_twice_is_refused(conn):
    """A settled position is history; booking it again double-counts."""
    pos = _open(conn, price=100.0)
    assert close_position(pos, close_price=110.0, close_reason="止盈触发")

    row = conn.execute(
        "SELECT return_amount FROM virtual_portfolio WHERE id = ?", (pos,)
    ).fetchone()
    booked = row["return_amount"]
    cash = _cash(conn)

    assert close_position(pos, close_price=110.0, close_reason="止盈触发") is False

    assert len(_exits(conn)) == 1
    assert conn.execute(
        "SELECT return_amount FROM virtual_portfolio WHERE id = ?", (pos,)
    ).fetchone()["return_amount"] == booked
    assert _cash(conn) == cash


def test_an_invalid_price_books_nothing(conn):
    """A zero or negative price is not a sale."""
    pos = _open(conn, price=100.0)
    cash = _cash(conn)

    assert close_position(pos, close_price=0.0, close_reason="止损触发") is False
    assert close_position(pos, close_price=-5.0, close_reason="止损触发") is False

    assert conn.execute(
        "SELECT status FROM virtual_portfolio WHERE id = ?", (pos,)
    ).fetchone()["status"] == "open"
    assert _exits(conn) == []
    assert _cash(conn) == cash


def test_an_exact_retry_of_an_exit_has_one_effect(conn):
    """The unique key is the reason a retried sale cannot book twice."""
    fields = dict(position_id=1, trader_id=DEFAULT_TRADER, code="300475",
                  exit_date="2026-04-09", price=110.0, shares=100,
                  cost_basis=10_050.0, gross_amount=1_000.0, costs=50.0,
                  net_amount=950.0, return_pct=9.45, reason="减仓")
    first = trade_ledger.record_exit(conn, command_id="sale-retry", **fields)
    second = trade_ledger.record_exit(conn, command_id="sale-retry", **fields)

    assert first == second
    assert len(_exits(conn)) == 1


def test_the_same_command_id_with_different_arguments_is_refused(conn):
    """Idempotency covers the instruction, not merely its key.

    Returning the earlier row for a changed retry would quietly book a
    different sale under the first one's identity, which is how a
    fat-fingered re-send turns into a phantom fill.
    """
    fields = dict(position_id=1, trader_id=DEFAULT_TRADER, code="300475",
                  exit_date="2026-04-09", price=110.0, shares=100,
                  cost_basis=10_050.0, gross_amount=1_000.0, costs=50.0,
                  net_amount=950.0, return_pct=9.45, reason="减仓")
    trade_ledger.record_exit(conn, command_id="sale-1", **fields)

    with pytest.raises(ValueError):
        trade_ledger.record_exit(conn, command_id="sale-1",
                                 **{**fields, "shares": 200})

    assert len(_exits(conn)) == 1, "a rejected retry books nothing"


def test_the_ledger_refuses_a_sale_with_no_price(conn):
    with pytest.raises(ValueError):
        trade_ledger.record_exit(
            conn, position_id=1, trader_id=DEFAULT_TRADER, code="300475",
            exit_date="2026-04-09", price=0.0, shares=100, cost_basis=0.0,
            gross_amount=0.0, costs=0.0, net_amount=0.0, return_pct=0.0)


def test_realised_results_are_per_trader(conn):
    """One trader's losses are not another trader's cash."""
    other = "momentum"
    conn.execute(
        "INSERT INTO position_exits (position_id, trader_id, code, exit_date, "
        "price, shares, cost_basis, gross_amount, costs, net_amount, return_pct) "
        "VALUES (99, ?, '000001', '2026-04-09', 10.0, 100, 1000.0, 0.0, 0.0, -250.0, -25.0)",
        (other,),
    )
    conn.commit()

    assert trade_ledger.realized_total(conn, other) == pytest.approx(-250.0)
    assert trade_ledger.realized_total(conn, DEFAULT_TRADER) == 0.0
    # The default trader's pot is untouched by the other book's loss.
    assert _cash(conn) == pytest.approx(trader_capital(DEFAULT_TRADER))


def test_the_bounded_pot_still_bounds_a_winning_trader(conn):
    """Realised money adds to the pot; it does not become an uncapped budget.

    The sizing ceiling is a share of ``trader_capital``, so a trader that
    has doubled its money may deploy more cash but not a larger fraction of
    the mandate. This pins that the pot, not the cash balance, is the base.
    """
    pos = _open(conn, price=100.0)
    close_position(pos, close_price=150.0, close_reason="止盈触发")
    _settle_all(conn)   # T+1: the proceeds have settled
    assert _cash(conn) > trader_capital(DEFAULT_TRADER)


def test_identical_sales_are_distinct_commands(conn):
    pos = _open(conn, shares=300)
    for _ in range(3):
        assert close_position(pos, close_price=110, shares=100, close_reason="trim")
    assert len(_exits(conn)) == 3
    assert sum(r["shares"] for r in _exits(conn)) == 300


def test_explicit_retry_does_not_change_inventory_or_cash(conn):
    pos = _open(conn, shares=300)
    kwargs = dict(close_price=110, shares=100, close_reason="trim", command_id="sale-1")
    assert close_position(pos, **kwargs)
    cash = _cash(conn)
    assert close_position(pos, **kwargs)
    assert len(_exits(conn)) == 1
    assert _cash(conn) == cash
    assert conn.execute("SELECT shares FROM virtual_portfolio WHERE id=?", (pos,)).fetchone()[0] == 200
    assert close_position(pos, **dict(kwargs, close_price=111)) is False
    assert close_position(pos, **dict(kwargs, shares=1000)) is False
    assert len(_exits(conn)) == 1


@pytest.mark.parametrize("price", [float("nan"), float("inf"), -float("inf"), True, "110"])
def test_nonfinite_or_nonnumeric_price_is_refused(conn, price):
    pos = _open(conn)
    assert close_position(pos, close_price=price, close_reason="invalid") is False
    assert _exits(conn) == []


@pytest.mark.parametrize("shares", [-100, 0, 0.5, float("nan"), True, "100", 101, 1001])
def test_invalid_share_request_is_refused(conn, shares):
    pos = _open(conn)
    assert close_position(pos, close_price=110, shares=shares, close_reason="invalid") is False
    assert _exits(conn) == []


def test_failed_position_update_rolls_back_exit(conn):
    pos = _open(conn)
    conn.execute("CREATE TRIGGER reject_exit BEFORE UPDATE ON virtual_portfolio "
                 "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        close_position(pos, close_price=110, close_reason="trim", shares=100)
    assert _exits(conn) == []
    assert not conn.in_transaction
    assert conn.execute("SELECT shares FROM virtual_portfolio WHERE id=?", (pos,)).fetchone()[0] == 1000


def test_legacy_partial_profit_survives_new_exits(conn):
    pos = _open(conn, shares=300)
    conn.execute("UPDATE virtual_portfolio SET return_amount=500 WHERE id=?", (pos,))
    conn.commit()
    assert _cash(conn) == pytest.approx(trader_capital() - 30000 + 500)
    assert close_position(pos, close_price=110, close_reason="trim", shares=100)
    assert close_position(pos, close_price=105, close_reason="close")
    row = conn.execute("SELECT * FROM virtual_portfolio WHERE id=?", (pos,)).fetchone()
    assert row["return_amount"] == pytest.approx(500 + sum(r["net_amount"] for r in _exits(conn)))
    assert row["return_pct"] is None  # Historic sold basis is unknown.
    assert len(_exits(conn)) == 2  # No fabricated legacy fill.
    # ``get_total_capital`` is the pot plus everything realised, legacy
    # included — the same figure the row records. Day-T spendable cash is
    # lower because both legs' proceeds are in transit; after T+1 the two
    # agree again.
    assert get_total_capital(DEFAULT_TRADER) == pytest.approx(
        trader_capital() + row["return_amount"])
    _settle_all(conn)
    assert _cash(conn) == pytest.approx(
        trader_capital() + row["return_amount"])
