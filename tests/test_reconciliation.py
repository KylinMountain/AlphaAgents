"""Reconciliation: invariants the book must keep to itself.

Each test starts from a clean database, builds a small but
representative set of rows, then either asserts the run is clean or
inserts a deliberate inconsistency and asserts the run names it.

The point is not to test that the SQL runs — it is to prove the
invariants cover every way the book's three tables (virtual_portfolio,
position_exits, reservations) can drift apart, and that a clean book
stays clean.

All eight invariants are exercised here:

    1. orphan_exit              (major)
    2. exit_trader_mismatch      (critical)
    3. exit_math_inconsistent    (major)
    4. orphan_reservation        (critical)
    5. missing_reservation       (critical)
    6. stale_reservation         (major)
    7. consumed_amount_mismatch  (critical)
    8. oversold_position         (major)
"""

from __future__ import annotations

import json
import uuid

import pytest

from alpha_agents.data import memory_store, reconciliation as REC
from alpha_agents.data.portfolio_exit import SLIPPAGE_RATE


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture()
def store(tmp_path, monkeypatch):
    p = tmp_path / "memory.db"
    monkeypatch.setattr(memory_store, "MEMORY_DB_PATH", p, raising=False)
    monkeypatch.setattr(memory_store._local, "conn", None, raising=False)
    yield
    conn = getattr(memory_store._local, "conn", None)
    if conn is not None:
        conn.close()
    memory_store._local.conn = None


def _conn():
    return memory_store._get_conn()


def _seed_position(conn, *, position_id, status="open", code="600000",
                   shares=1000, open_price=10.0, trader_id="slow",
                   open_date="2026-01-05", order_date="2026-01-05"):
    """Insert a virtual_portfolio row with the fields the invariants read."""
    conn.execute(
        "INSERT INTO virtual_portfolio "
        "(id, code, name, theme, order_date, open_date, open_price, "
        " shares, status, trader_id) "
        "VALUES (?, ?, 'X', 't', ?, ?, ?, ?, ?, ?)",
        (position_id, code, order_date, open_date, open_price, shares,
         status, trader_id))
    conn.commit()


def _seed_reservation(conn, *, order_id, trader_id="slow", code="600000",
                       amount=1000.0, state="consumed",
                       consumed_amount=0.0, kind="cash_reserve"):
    conn.execute(
        "INSERT INTO reservations "
        "(order_id, trader_id, code, kind, amount, consumed_amount, "
        " state, reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'test')",
        (order_id, trader_id, code, kind, amount, consumed_amount, state))
    conn.commit()


def _seed_exit(conn, *, position_id, trader_id="slow", code="600000",
               exit_date="2026-01-10", price=11.0, shares=500,
               cost_basis=5000.0, gross_amount=5500.0,
               costs=10.0, net_amount=5490.0, command_id=None):
    """Insert a position_exits row with internally consistent math."""
    if command_id is None:
        command_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO position_exits "
        "(position_id, trader_id, code, exit_date, price, shares, "
        " cost_basis, gross_amount, costs, net_amount, return_pct, "
        " reason, command_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?)",
        (position_id, trader_id, code, exit_date, price, shares,
         cost_basis, gross_amount, costs, net_amount,
         (net_amount / cost_basis - 1) * 100 if cost_basis else 0.0,
         command_id))
    conn.commit()
    return command_id


# ── Clean book ───────────────────────────────────────────────────────────


class TestCleanBook:
    """A book built through the production path reports zero diffs.

    The fixtures below exercise every state the eight invariants check:
    an open position with a consumed reservation matching its open
    cost, a closed position with a released reservation, a pending order
    with a held reservation. A clean reconciliation is the precondition
    for every other test in this file: if it fails, the diffs in the
    later tests are noise from the setup, not from the tampering.
    """

    def test_an_empty_book_is_clean(self, store):
        result = REC.reconcile()
        assert result.status == "clean"
        assert result.diffs == []

    def test_a_well_built_book_is_clean(self, store):
        """Open + closed + pending, each with the matching reservation
        state, produces a clean run and a populated summary."""
        # Open position with consumed reservation matching open cost.
        _seed_position(_conn(), position_id=1, shares=1000, open_price=10.0,
                       status="open")
        _seed_reservation(_conn(), order_id=1, state="consumed",
                          amount=10000.0,
                          consumed_amount=round(1000 * 10.0
                                                 * (1 + SLIPPAGE_RATE), 2))
        # Pending order with held reservation.
        _seed_position(_conn(), position_id=2, shares=0, status="pending")
        _seed_reservation(_conn(), order_id=2, state="held",
                          amount=10000.0)
        # Closed position with released reservation.
        _seed_position(_conn(), position_id=3, shares=1000, open_price=10.0,
                       status="cancelled")
        _seed_reservation(_conn(), order_id=3, state="released",
                          amount=10000.0)

        result = REC.reconcile()
        assert result.status == "clean", \
            f"well-built book reported diffs: {result.diffs}"
        # Summary must reflect the open position's invested capital.
        assert "slow" in result.summary
        assert result.summary["slow"]["invested_capital"] == 10000.0
        assert result.summary["slow"]["n_open"] == 1
        assert result.summary["slow"]["n_pending"] == 1
        assert result.summary["slow"]["n_closed"] == 1
        # Reservation breakdown: 1 held, 1 consumed, 1 released.
        assert result.summary["slow"]["reservation_held"] == 10000.0
        assert result.summary["slow"]["reservation_released"] == 10000.0

    def test_run_is_persisted_in_the_audit_log(self, store):
        """A successful run writes one row to reconciliation_runs and
        none to reconciliation_diffs — the audit log itself is part of
        the contract."""
        _seed_position(_conn(), position_id=1, shares=1000, open_price=10.0)
        _seed_reservation(_conn(), order_id=1, state="consumed",
                          amount=10000.0,
                          consumed_amount=round(1000 * 10.0
                                                 * (1 + SLIPPAGE_RATE), 2))

        result = REC.reconcile()

        run_row = _conn().execute(
            "SELECT * FROM reconciliation_runs WHERE id = ?",
            (result.run_id,)).fetchone()
        assert run_row is not None
        assert run_row["status"] == "clean"
        assert run_row["diff_count"] == 0
        diff_count = _conn().execute(
            "SELECT COUNT(*) FROM reconciliation_diffs WHERE run_id = ?",
            (result.run_id,)).fetchone()[0]
        assert diff_count == 0


# ── Each invariant ───────────────────────────────────────────────────────


class TestInvariantsReportDeliberateTampering:
    """Each test inserts one specific kind of inconsistency and asserts
    the run reports it with the right severity and the right detail.
    """

    def test_orphan_exit_is_reported(self, store):
        """A position_exits row pointing to a non-existent position_id
        is flagged major: the realised P&L is appended to nothing."""
        conn = _conn()
        conn.execute(
            "INSERT INTO position_exits "
            "(position_id, trader_id, code, exit_date, price, shares, "
            " cost_basis, gross_amount, costs, net_amount, return_pct, "
            " reason, command_id) "
            "VALUES (?, 'slow', '600000', '2026-01-10', 11.0, 500, "
            " 5000.0, 5500.0, 10.0, 5490.0, 9.8, '', ?)",
            (999, uuid.uuid4().hex))
        conn.commit()

        result = REC.reconcile()
        kinds = [d.invariant for d in result.diffs]
        assert "orphan_exit" in kinds
        d = next(d for d in result.diffs if d.invariant == "orphan_exit")
        assert d.severity == REC.MAJOR
        assert d.position_id == 999
        assert d.trader_id == "slow"

    def test_exit_trader_mismatch_is_reported(self, store):
        """An exit attributed to one trader when the position belongs
        to another is critical: realised P&L would be booked against the
        wrong book."""
        _seed_position(_conn(), position_id=1, shares=1000, open_price=10.0,
                       trader_id="slow")
        _seed_reservation(_conn(), order_id=1, trader_id="slow",
                          state="consumed", amount=10000.0,
                          consumed_amount=round(1000 * 10.0
                                                 * (1 + SLIPPAGE_RATE), 2))
        # Exit says trader='fast' but position is 'slow'.
        _seed_exit(_conn(), position_id=1, trader_id="fast")

        result = REC.reconcile()
        d = next(d for d in result.diffs
                 if d.invariant == "exit_trader_mismatch")
        assert d.severity == REC.CRITICAL
        assert d.detail["exit_trader"] == "fast"
        assert d.detail["portfolio_trader"] == "slow"

    def test_exit_math_inconsistency_is_reported(self, store):
        """A net_amount that does not equal gross − costs is a friction
        math bug; major because the inputs are recoverable."""
        _seed_position(_conn(), position_id=1, shares=1000, open_price=10.0)
        _seed_reservation(_conn(), order_id=1, state="consumed",
                          amount=10000.0,
                          consumed_amount=round(1000 * 10.0
                                                 * (1 + SLIPPAGE_RATE), 2))
        _seed_exit(_conn(), position_id=1, gross_amount=5500.0,
                   costs=10.0, net_amount=9999.0)  # net should be 5490

        result = REC.reconcile()
        d = next(d for d in result.diffs
                 if d.invariant == "exit_math_inconsistent")
        assert d.severity == REC.MAJOR
        assert d.detail["expected_net"] == 5490.0
        assert d.detail["actual_net"] == 9999.0

    def test_orphan_reservation_is_reported(self, store):
        """A reservation with no matching virtual_portfolio row is
        critical — cash earmarked for an order that was never written."""
        _seed_reservation(_conn(), order_id=999, amount=50000.0,
                          state="held")

        result = REC.reconcile()
        d = next(d for d in result.diffs
                 if d.invariant == "orphan_reservation")
        assert d.severity == REC.CRITICAL
        assert d.reservation_id is not None
        assert d.detail["amount"] == 50000.0

    def test_missing_reservation_is_reported(self, store):
        """An open position with shares > 0 but no reservation is
        critical: the earmark has been lost, so a new order can spend
        the same cash."""
        _seed_position(_conn(), position_id=1, shares=1000, open_price=10.0,
                       status="open")
        # Deliberately no reservation row.

        result = REC.reconcile()
        d = next(d for d in result.diffs
                 if d.invariant == "missing_reservation")
        assert d.severity == REC.CRITICAL
        assert d.position_id == 1

    def test_stale_reservation_is_reported(self, store):
        """A reservation still in state='held' for a position whose
        status is terminal is major: the cash has already returned to
        the pot in practice but the row still subtracts from available."""
        _seed_position(_conn(), position_id=1, status="cancelled")
        _seed_reservation(_conn(), order_id=1, state="held", amount=10000.0)

        result = REC.reconcile()
        d = next(d for d in result.diffs
                 if d.invariant == "stale_reservation")
        assert d.severity == REC.MAJOR
        assert d.detail["position_status"] == "cancelled"

    def test_consumed_amount_mismatch_is_reported(self, store):
        """A consumed reservation whose amount disagrees with the
        matching position's actual fill cost is critical: the
        reservation was consumed at a different rate than the position
        was opened, eroding the meaning of the cash ledger."""
        _seed_position(_conn(), position_id=1, shares=1000, open_price=10.0,
                       status="open")
        # 1000 * 10 * 1.0005 = 10005.00, but we claim 12000.
        _seed_reservation(_conn(), order_id=1, state="consumed",
                          amount=12000.0, consumed_amount=12000.0)

        result = REC.reconcile()
        d = next(d for d in result.diffs
                 if d.invariant == "consumed_amount_mismatch")
        assert d.severity == REC.CRITICAL
        assert d.detail["expected_consumed"] == round(
            1000 * 10.0 * (1 + SLIPPAGE_RATE), 2)
        assert d.detail["actual_consumed"] == 12000.0

    def test_consumed_against_unfilled_position_is_reported(self, store):
        """A reservation in state='consumed' against a position that
        has no open_price or shares is structural — the comparison
        can't even be made, so the diff says so explicitly."""
        _seed_position(_conn(), position_id=1, status="pending",
                       shares=0, open_price=None)
        _seed_reservation(_conn(), order_id=1, state="consumed",
                          amount=10000.0, consumed_amount=10000.0)

        result = REC.reconcile()
        d = next(d for d in result.diffs
                 if d.invariant == "consumed_amount_mismatch")
        assert d.detail["reason"] == (
            "consumed reservation references an unfilled position")

    def test_oversold_position_is_reported(self, store):
        """Exits whose total shares exceed the position's share count
        is major: a position sold past zero."""
        _seed_position(_conn(), position_id=1, shares=1000, open_price=10.0,
                       status="open")
        _seed_reservation(_conn(), order_id=1, state="consumed",
                          amount=10000.0,
                          consumed_amount=round(1000 * 10.0
                                                 * (1 + SLIPPAGE_RATE), 2))
        _seed_exit(_conn(), position_id=1, shares=1500)  # > 1000

        result = REC.reconcile()
        d = next(d for d in result.diffs
                 if d.invariant == "oversold_position")
        assert d.severity == REC.MAJOR
        assert d.detail["shares_held"] == 1000
        assert d.detail["total_sold"] == 1500


# ── Run aggregation & API ───────────────────────────────────────────────


class TestRunAggregation:
    def test_multiple_tamperings_are_all_reported(self, store):
        """A single run with several inconsistencies reports all of
        them — the runner never short-circuits on the first diff."""
        _seed_position(_conn(), position_id=1, shares=1000, open_price=10.0)
        _seed_reservation(_conn(), order_id=1, state="consumed",
                          amount=12000.0, consumed_amount=12000.0)
        _seed_exit(_conn(), position_id=1, gross_amount=5500.0,
                   costs=10.0, net_amount=9999.0)

        result = REC.reconcile()
        invariants = {d.invariant for d in result.diffs}
        assert "exit_math_inconsistent" in invariants
        assert "consumed_amount_mismatch" in invariants

    def test_summary_records_per_trader_derived_totals(self, store):
        """The summary recorded in the run captures every per-trader
        value the next operator will need to compare against the
        previous run."""
        _seed_position(_conn(), position_id=1, shares=1000, open_price=10.0,
                       trader_id="slow", status="open")
        _seed_reservation(_conn(), order_id=1, trader_id="slow",
                          state="consumed", amount=10000.0,
                          consumed_amount=round(1000 * 10.0
                                                 * (1 + SLIPPAGE_RATE), 2))
        _seed_position(_conn(), position_id=2, shares=500, open_price=20.0,
                       trader_id="fast", status="open")
        _seed_reservation(_conn(), order_id=2, trader_id="fast",
                          state="consumed", amount=10000.0,
                          consumed_amount=round(500 * 20.0
                                                 * (1 + SLIPPAGE_RATE), 2))

        result = REC.reconcile()
        assert result.summary["slow"]["invested_capital"] == 10000.0
        assert result.summary["fast"]["invested_capital"] == 10000.0

    def test_get_latest_run_returns_the_most_recent(self, store):
        _seed_position(_conn(), position_id=1, shares=1000, open_price=10.0)
        _seed_reservation(_conn(), order_id=1, state="consumed",
                          amount=10000.0,
                          consumed_amount=round(1000 * 10.0
                                                 * (1 + SLIPPAGE_RATE), 2))
        first = REC.reconcile()
        second = REC.reconcile()

        latest = REC.get_latest_run()
        assert latest is not None
        assert latest["id"] == second.run_id

        diffs = REC.get_diffs_for_run(first.run_id)
        assert isinstance(diffs, list)

    def test_dirty_run_writes_diffs_to_the_audit_log(self, store):
        """The diffs table is not just an in-memory return value — the
        run also persists each diff with the run_id so an operator can
        query the history without re-running reconciliation."""
        _seed_position(_conn(), position_id=1, shares=1000, open_price=10.0,
                       status="open")
        # No reservation → missing_reservation.

        result = REC.reconcile()
        rows = _conn().execute(
            "SELECT * FROM reconciliation_diffs WHERE run_id = ?",
            (result.run_id,)).fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row["invariant"] == "missing_reservation"
        assert row["severity"] == REC.CRITICAL
        assert row["position_id"] == 1

    def test_diff_to_dict_round_trip(self, store):
        """The CLI serializer produces a JSON-safe dict; the detail
        must come through as a real object, not a string."""
        _seed_position(_conn(), position_id=1, shares=1000, open_price=10.0)
        _seed_reservation(_conn(), order_id=1, state="consumed",
                          amount=12000.0, consumed_amount=12000.0)

        result = REC.reconcile()
        d = next(dd for dd in result.diffs
                 if dd.invariant == "consumed_amount_mismatch")
        dct = REC.diff_to_dict(d)
        # Detail is a dict, JSON-encodable.
        json.dumps(dct)
        assert dct["invariant"] == "consumed_amount_mismatch"
        assert dct["severity"] == REC.CRITICAL