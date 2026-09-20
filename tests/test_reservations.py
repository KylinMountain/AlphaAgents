"""The reservation lifecycle on its own, plus the wiring through portfolio.

The first two classes pin the module's contract in isolation: insert
held once (idempotent), consume on fill, release on cancel, refuse
double-application, and the ``unconsumed_total`` formula. The wiring
class then exercises the same lifecycle through ``create_pending_order``,
``_fill_order`` and ``_cancel_order`` and checks that
``get_available_capital`` reflects each step — which is the bug this
slice exists to fix.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from alpha_agents.data import memory_store, reservations as R


@pytest.fixture()
def conn():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "memory.db"
        original = memory_store.MEMORY_DB_PATH
        memory_store.MEMORY_DB_PATH = db
        if hasattr(memory_store._local, "conn"):
            del memory_store._local.conn
        c = memory_store._get_conn()
        yield c
        c.close()
        memory_store._local.conn = None
        memory_store.MEMORY_DB_PATH = original


def _seed_order(conn, order_id=1, trader_id="t", code="600000"):
    """Create the parent row reservations attach to.

    Reservations reference virtual_portfolio.id; the reservations table
    has no FK in the schema, but the wiring is meaningless without a
    parent, and these tests should not reach into portfolio's setup.
    """
    conn.execute(
        "INSERT INTO virtual_portfolio "
        "(id, code, name, order_date, status, trader_id) "
        "VALUES (?, ?, 'X', '2026-01-05', 'pending', ?)",
        (order_id, code, trader_id),
    )
    conn.commit()


class TestReserveIsIdempotent:
    def test_a_first_reserve_holds_the_amount(self, conn):
        _seed_order(conn)
        rid = R.reserve_for_order(conn, order_id=1, trader_id="t",
                                 code="600000", amount=1000.0)
        assert rid > 0
        row = R.reservation_for_order(conn, 1)
        assert row["state"] == R.HELD
        assert row["amount"] == 1000.0
        assert row["consumed_amount"] == 0.0

    def test_a_second_reserve_is_a_no_op_not_an_error(self, conn):
        _seed_order(conn)
        first = R.reserve_for_order(conn, order_id=1, trader_id="t",
                                    code="600000", amount=1000.0)
        second = R.reserve_for_order(conn, order_id=1, trader_id="t",
                                     code="600000", amount=2000.0)
        assert first == second
        assert R.reservation_for_order(conn, 1)["amount"] == 1000.0, \
            "the first amount is the record; the second is dropped"

    def test_reject_zero_or_nonfinite_amounts(self, conn):
        _seed_order(conn)
        for bad in (0, -1, float("inf"), float("nan")):
            with pytest.raises(ValueError):
                R.reserve_for_order(conn, order_id=1, trader_id="t",
                                    code="600000", amount=bad)


class TestConsumeTransitionsOnce:
    def test_consume_marks_held_as_consumed_at_actual_cost(self, conn):
        _seed_order(conn)
        R.reserve_for_order(conn, order_id=1, trader_id="t",
                            code="600000", amount=1000.0)
        rid, released = R.consume_reservation(conn, 1, actual_cost=800.0)
        assert rid > 0
        assert released == 200.0, "the over-reserve is what the kernel returns"
        row = R.reservation_for_order(conn, 1)
        assert row["state"] == R.CONSUMED
        assert row["consumed_amount"] == 800.0
        # The over-reserve is given back by shrinking the row to the actual
        # cost. The first version kept ``amount`` intact and let
        # ``unconsumed_total`` keep counting it, on the promise that
        # reconciliation would release it later — but reconciliation is
        # read-only here and never did: eight fills locked 647k of a 1M pot
        # and the agent was shown 可用 −41,624 while the equity curve had
        # the cash.
        assert row["amount"] == 800.0, \
            "the row shrinks to the actual cost; the over-reserve is back"

    def test_a_second_consume_is_loud(self, conn):
        """A double-consume would mean two fills shared one reservation."""
        _seed_order(conn)
        R.reserve_for_order(conn, order_id=1, trader_id="t",
                            code="600000", amount=1000.0)
        R.consume_reservation(conn, 1, actual_cost=800.0)
        with pytest.raises(R.ReservationStateError, match="consumed"):
            R.consume_reservation(conn, 1, actual_cost=800.0)

    def test_consume_without_a_reserve_is_an_error(self, conn):
        _seed_order(conn)
        with pytest.raises(R.ReservationStateError, match="No cash_reserve"):
            R.consume_reservation(conn, 1, actual_cost=100.0)

    def test_an_actual_cost_above_the_backstop_clamps_release_to_zero(
            self, conn):
        """Should not happen in normal sizing, but be safe rather than
        silently add a negative cash flow."""
        _seed_order(conn)
        R.reserve_for_order(conn, order_id=1, trader_id="t",
                            code="600000", amount=100.0)
        _, released = R.consume_reservation(conn, 1, actual_cost=200.0)
        assert released == 0.0


class TestReleaseTransitionsOnce:
    def test_release_returns_the_full_backstop(self, conn):
        _seed_order(conn)
        R.reserve_for_order(conn, order_id=1, trader_id="t",
                            code="600000", amount=1000.0)
        rid = R.release_reservation(conn, 1, reason="cancelled: theme weak")
        assert rid > 0
        row = R.reservation_for_order(conn, 1)
        assert row["state"] == R.RELEASED
        assert row["released_at"] is not None
        assert row["reason"] == "cancelled: theme weak"

    def test_release_of_a_consumed_row_is_an_error(self, conn):
        """A consumed reservation is money already spent; releasing it
        would be a phantom refund."""
        _seed_order(conn)
        R.reserve_for_order(conn, order_id=1, trader_id="t",
                            code="600000", amount=1000.0)
        R.consume_reservation(conn, 1, actual_cost=900.0)
        with pytest.raises(R.ReservationStateError):
            R.release_reservation(conn, 1, reason="late cancel")

    def test_release_without_a_reserve_is_an_error(self, conn):
        _seed_order(conn)
        with pytest.raises(R.ReservationStateError):
            R.release_reservation(conn, 1, reason="nothing held")


class TestReservationKinds:
    def test_theme_risk_hold_does_not_reduce_cash(self, conn):
        _seed_order(conn)
        R.reserve_for_order(
            conn, order_id=1, trader_id="t", code="600000",
            amount=1000.0, kind=R.CASH_RESERVE)
        R.reserve_for_order(
            conn, order_id=1, trader_id="t", code="600000",
            amount=5000.0, kind=R.theme_risk_kind("AI"))
        assert R.unconsumed_total(conn, "t") == 1000.0
        assert R.held_total(
            conn, "t", kind=R.theme_risk_kind("AI")) == 5000.0

    def test_terminal_release_clears_all_held_kinds(self, conn):
        _seed_order(conn)
        for kind in (
                R.CASH_RESERVE,
                R.theme_risk_kind("AI"),
                R.theme_risk_kind("算力")):
            R.reserve_for_order(
                conn, order_id=1, trader_id="t", code="600000",
                amount=1000.0, kind=kind)
        released = R.release_all_held_for_order(
            conn, 1, "cancelled")
        assert len(released) == 3
        states = conn.execute(
            "SELECT kind,state FROM reservations WHERE order_id=1 "
            "ORDER BY kind").fetchall()
        assert {row["state"] for row in states} == {R.RELEASED}


class TestUnconsumedTotal:
    def test_held_counts_its_full_amount(self, conn):
        _seed_order(conn, order_id=1)
        _seed_order(conn, order_id=2, code="000001")
        R.reserve_for_order(conn, order_id=1, trader_id="t",
                            code="600000", amount=1000.0)
        R.reserve_for_order(conn, order_id=2, trader_id="t",
                            code="000001", amount=400.0)
        assert R.unconsumed_total(conn, "t") == 1400.0

    def test_a_consumed_row_binds_nothing(self, conn):
        _seed_order(conn, order_id=1)
        R.reserve_for_order(conn, order_id=1, trader_id="t",
                            code="600000", amount=1000.0)
        R.consume_reservation(conn, 1, actual_cost=700.0)
        assert R.unconsumed_total(conn, "t") == 0.0, \
            "the fill's cost lives in invested; counting the row here too " \
            "would charge the position twice"

    def test_released_counts_nothing(self, conn):
        _seed_order(conn, order_id=1)
        _seed_order(conn, order_id=2)
        R.reserve_for_order(conn, order_id=1, trader_id="t",
                            code="600000", amount=1000.0)
        R.reserve_for_order(conn, order_id=2, trader_id="t",
                            code="000001", amount=400.0)
        R.release_reservation(conn, 1, reason="cancelled")
        assert R.unconsumed_total(conn, "t") == 400.0

    def test_unconsumed_total_is_scoped_to_the_trader(self, conn):
        _seed_order(conn, order_id=1, trader_id="a")
        _seed_order(conn, order_id=2, trader_id="b")
        R.reserve_for_order(conn, order_id=1, trader_id="a",
                            code="600000", amount=1000.0)
        R.reserve_for_order(conn, order_id=2, trader_id="b",
                            code="000001", amount=500.0)
        assert R.unconsumed_total(conn, "a") == 1000.0
        assert R.unconsumed_total(conn, "b") == 500.0
        assert R.unconsumed_total(conn, "a") + R.unconsumed_total(conn, "b") \
            == 1500.0

    def test_two_traders_on_the_same_stock_do_not_share_holds(self, conn):
        """The Phase 1 cross-trader attribution fix is preserved here:
        each trader's reservation is a row keyed on trader_id."""
        _seed_order(conn, order_id=1, trader_id="a", code="600000")
        _seed_order(conn, order_id=2, trader_id="b", code="600000")
        R.reserve_for_order(conn, order_id=1, trader_id="a",
                            code="600000", amount=1000.0)
        R.reserve_for_order(conn, order_id=2, trader_id="b",
                            code="600000", amount=800.0)
        assert R.unconsumed_total(conn, "a") == 1000.0
        assert R.unconsumed_total(conn, "b") == 800.0


# ── Wiring through the portfolio path ──────────────────────────────────

from unittest.mock import patch  # noqa: E402

from alpha_agents.data import portfolio as P  # noqa: E402
from alpha_agents.data import portfolio_risk_reservations as PRR  # noqa: E402
from alpha_agents.data import reservations as R  # noqa: E402
from alpha_agents.data import trader as TR  # noqa: E402
from alpha_agents.data import trade_ledger  # noqa: E402
from alpha_agents.data.portfolio_exit import SLIPPAGE_RATE  # noqa: E402
from alpha_agents.data.portfolio import trader_capital  # noqa: E402


@pytest.fixture()
def store(tmp_path, monkeypatch):
    p = tmp_path / "memory.db"
    monkeypatch.setattr(memory_store, "MEMORY_DB_PATH", p, raising=False)
    monkeypatch.setattr(memory_store._local, "conn", None, raising=False)
    yield
    c = getattr(memory_store._local, "conn", None)
    if c is not None:
        c.close()
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


def _write_trader(d, name, body):
    (d / f"{name}.yaml").write_text(body, encoding="utf-8")


SLOW = "id: slow\nname: 回调派\ncapital: 500000\n"


def _conn():
    return memory_store._get_conn()


class TestPlanRiskSizing:
    def test_existing_hard_stop_budget_is_the_floor_for_risk_distance(self):
        capital = 500_000.0
        near = PRR.plan_risk_amount_cap(
            10.0, 9.5, capital, 0.10,
            hard_stop_pct=P.HARD_STOP_PCT, lot_size=100)
        at_hard = PRR.plan_risk_amount_cap(
            10.0, 9.2, capital, 0.10,
            hard_stop_pct=P.HARD_STOP_PCT, lot_size=100)
        assert near == at_hard == 50_000.0

    def test_wider_declared_stop_shrinks_notional(self):
        capital = 500_000.0
        got = PRR.plan_risk_amount_cap(
            10.0, 8.0, capital, 0.10,
            hard_stop_pct=P.HARD_STOP_PCT, lot_size=100)
        assert got == 20_000.0
        assert got < capital * 0.10

    def test_risk_cap_can_refuse_even_one_lot(self):
        # 100-share lot costs 20k, but this tiny capital/risk budget cannot
        # support it. The one-lot fallback must not override the risk gate.
        got = PRR.plan_risk_amount_cap(
            200.0, 160.0, 10_000.0, 0.10,
            hard_stop_pct=P.HARD_STOP_PCT, lot_size=100)
        assert got == 0.0


class TestAtomicThemeRisk:
    def test_second_order_cannot_double_spend_shared_theme_headroom(
            self, store, traders_dir, theme, monkeypatch):
        _write_trader(traders_dir, "slow", SLOW)
        # Existing approved constants define each backstop at ~10% of capital.
        # Tighten only the fixture's theme cap so one fits and two do not.
        monkeypatch.setattr(P, "MAX_THEME_PCT", 0.15)
        first = P.create_pending_order(
            code="600000", name="A", theme="t",
            risk_themes=["shared"], order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, stop_loss=8.5,
            source="morning", reason="A", trader_id="slow")
        second = P.create_pending_order(
            code="000001", name="B", theme="t",
            risk_themes=["shared"], order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, stop_loss=8.5,
            source="morning", reason="B", trader_id="slow")

        assert first is not None
        assert second is None
        kind = R.theme_risk_kind("shared")
        assert R.held_total(_conn(), "slow", kind=kind) > 0
        assert _conn().execute(
            "SELECT COUNT(*) FROM virtual_portfolio "
            "WHERE trader_id='slow' AND status='pending'").fetchone()[0] == 1

    def test_fill_releases_theme_holds_but_consumes_cash(
            self, store, traders_dir, theme):
        _write_trader(traders_dir, "slow", SLOW)
        oid = P.create_pending_order(
            code="600000", name="A", theme="t",
            risk_themes=["shared"], order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, stop_loss=8.5,
            source="morning", reason="A", trader_id="slow")
        P._fill_order(
            P.get_pending_orders("slow")[0],
            fill_price=10.0, fill_date="2026-01-05")
        rows = _conn().execute(
            "SELECT kind,state FROM reservations WHERE order_id=?",
            (oid,)).fetchall()
        state = {row["kind"]: row["state"] for row in rows}
        assert state[R.CASH_RESERVE] == R.CONSUMED
        assert state[R.theme_risk_kind("t")] == R.RELEASED
        assert state[R.theme_risk_kind("shared")] == R.RELEASED

    def test_cancel_releases_cash_and_theme_holds(
            self, store, traders_dir, theme):
        _write_trader(traders_dir, "slow", SLOW)
        oid = P.create_pending_order(
            code="600000", name="A", theme="t",
            risk_themes=["shared"], order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, stop_loss=8.5,
            source="morning", reason="A", trader_id="slow")
        P._cancel_order(oid, "cancel")
        rows = _conn().execute(
            "SELECT state FROM reservations WHERE order_id=?",
            (oid,)).fetchall()
        assert {row["state"] for row in rows} == {R.RELEASED}


class TestReservationsFollowTheOrderLifecycle:
    def test_creating_a_pending_order_earmarks_cash(
            self, store, traders_dir, theme):
        """The order row and the held row are written together; available
        drops by the backstop, not by the (still-unknown) eventual cost."""
        _write_trader(traders_dir, "slow", SLOW)
        before = P.get_available_capital("slow")
        oid = P.create_pending_order(
            code="600000", name="A", theme="t", order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, stop_loss=8.5,
            source="morning", reason="主线在流入", trader_id="slow")
        assert oid is not None
        # The backstop is MAX_POSITION_PCT * capital * (1 + slippage).
        from alpha_agents.data.portfolio import MAX_POSITION_PCT
        from alpha_agents.data.portfolio_exit import SLIPPAGE_RATE
        backstop = 500_000 * MAX_POSITION_PCT * (1 + SLIPPAGE_RATE)
        assert P.get_available_capital("slow") == before - backstop
        held = _conn().execute(
            "SELECT state, amount FROM reservations WHERE order_id = ?",
            (oid,)).fetchone()
        assert held["state"] == R.HELD
        assert held["amount"] == backstop

    def test_filling_converts_the_hold_to_actual_cost(
            self, store, traders_dir, theme):
        """The reservation transitions held → consumed at the actual cost,
        and the over-reserve goes straight back to available: the row
        shrinks to the actual cost, the consumed row stays as
        reconciliation evidence, and nothing of it binds cash."""
        _write_trader(traders_dir, "slow", SLOW)
        oid = P.create_pending_order(
            code="600000", name="A", theme="t", order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, stop_loss=8.5,
            source="morning", reason="主线在流入", trader_id="slow")
        backstop = trader_capital("slow") * 0.10 * (1 + SLIPPAGE_RATE)
        after_reserve = P.get_available_capital("slow")
        assert after_reserve == pytest.approx(
            trader_capital("slow") - backstop)
        P._fill_order(
            P.get_pending_orders("slow")[0],
            fill_price=10.0, fill_date="2026-01-05")
        row = _conn().execute(
            "SELECT state, amount, consumed_amount FROM reservations "
            "WHERE order_id = ?", (oid,)).fetchone()
        position = _conn().execute(
            "SELECT shares, open_price FROM virtual_portfolio WHERE id = ?",
            (oid,)).fetchone()
        assert row["state"] == R.CONSUMED
        expected_actual = (position["shares"] * position["open_price"]
                           * (1 + SLIPPAGE_RATE))
        assert row["consumed_amount"] == pytest.approx(expected_actual)
        assert row["amount"] == pytest.approx(expected_actual), \
            "the over-reserve was released at the fill, not parked for a " \
            "reconciliation that never ran"
        # What remains bound is the position's own cost, no more: the
        # available figure answers "cash minus the filled position minus
        # still-pending backstops", not "cash minus a phantom 100k per
        # order that already filled". ``invested`` reads
        # ``open_price * shares`` without the slippage leg, so the
        # expected remainder uses that basis.
        invested = position["shares"] * position["open_price"]
        assert P.get_available_capital("slow") == pytest.approx(
            trader_capital("slow") - invested)

    def test_cancelling_a_pending_order_returns_the_full_backstop(
            self, store, traders_dir, theme):
        _write_trader(traders_dir, "slow", SLOW)
        before = P.get_available_capital("slow")
        oid = P.create_pending_order(
            code="600000", name="A", theme="t", order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, stop_loss=8.5,
            source="morning", reason="主线在流入", trader_id="slow")
        assert P.get_available_capital("slow") < before
        P._cancel_order(oid, "主线走弱")
        assert P.get_available_capital("slow") == before
        row = _conn().execute(
            "SELECT state FROM reservations WHERE order_id = ?",
            (oid,)).fetchone()
        assert row["state"] == R.RELEASED

    def test_a_second_pending_order_cannot_use_the_cash_of_the_first(
            self, store, traders_dir, theme):
        """The S2 bug fix in one line: two pending orders on different
        stocks each earmark the backstop, and ``get_available_capital``
        reflects both holds — not just one of them."""
        _write_trader(traders_dir, "slow", SLOW)
        before = P.get_available_capital("slow")
        oid_a = P.create_pending_order(
            code="600000", name="A", theme="t", order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, stop_loss=8.5,
            source="morning", reason="A 主题", trader_id="slow")
        after_first = P.get_available_capital("slow")
        oid_b = P.create_pending_order(
            code="000001", name="B", theme="t", order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, stop_loss=8.5,
            source="morning", reason="B 主题", trader_id="slow")
        after_second = P.get_available_capital("slow")

        assert oid_a is not None and oid_b is not None
        # Each hold is the same backstop; the second subtracts again.
        assert after_second == after_first - (before - after_first)
        # And both rows exist independently on different orders.
        rows = _conn().execute(
            "SELECT order_id, state FROM reservations "
            "WHERE order_id IN (?, ?) AND kind=? ORDER BY order_id",
            (oid_a, oid_b, R.CASH_RESERVE)).fetchall()
        assert [(r["order_id"], r["state"]) for r in rows] == \
            [(oid_a, R.HELD), (oid_b, R.HELD)]

    def test_a_double_cancel_does_not_release_twice(
            self, store, traders_dir, theme):
        """The wiring is the only thing that calls release; two rule
        paths in one pass both calling cancel must not double-release."""
        _write_trader(traders_dir, "slow", SLOW)
        before = P.get_available_capital("slow")
        oid = P.create_pending_order(
            code="600000", name="A", theme="t", order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, stop_loss=8.5,
            source="morning", reason="A 主题", trader_id="slow")
        P._cancel_order(oid, "第一遍")
        P._cancel_order(oid, "第二遍")  # state machine no-op
        # Available restored exactly once, not twice.
        assert P.get_available_capital("slow") == before
        row = _conn().execute(
            "SELECT state, reason FROM reservations WHERE order_id = ?",
            (oid,)).fetchone()
        assert row["state"] == R.RELEASED
        assert row["reason"] == "第一遍", \
            "the first finisher's reason is the record"
