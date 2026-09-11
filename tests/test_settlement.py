"""T+1 settlement — share-side lots and cash-side pending.

The two clocks behave differently but share a calendar trick: the
``settle_date`` is open_date (or exit_date) plus one calendar day, and
``settle_date <= today`` is the test for "already settled". That works
across weekends because Saturday <= Monday is True and Sunday <= Monday
is True, so a Friday-buy is sellable on Monday the same way the broker
reports it.

This file pins the contract before wiring the call sites — every
portfolio.py and portfolio_exit.py path that touches T+1 calls into
this module, so the module's behaviour must be observable on its own.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from alpha_agents.data import memory_store, settlement as S


@pytest.fixture()
def conn():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "memory.db"
        original = memory_store.MEMORY_DB_PATH
        memory_store.MEMORY_DB_PATH = db
        if hasattr(memory_store._local, "conn"):
            del memory_store._local.conn
        c = memory_store._get_conn()
        memory_store.MEMORY_DB_PATH = original
        yield c
        c.close()
        memory_store._local.conn = None


def _seed_position(c, *, position_id, code="600000", trader_id="slow",
                   status="open", shares=1000, open_price=10.0,
                   open_date="2026-01-05"):
    c.execute(
        "INSERT INTO virtual_portfolio "
        "(id, code, name, theme, order_date, open_date, open_price, "
        " shares, status, trader_id) "
        "VALUES (?, ?, 'X', 't', ?, ?, ?, ?, ?, ?)",
        (position_id, code, open_date, open_date, open_price, shares,
         status, trader_id))
    c.commit()


# ── next_settle_date (utility) ────────────────────────────────────────


class TestNextSettleDate:
    def test_adds_one_calendar_day(self):
        assert S.next_settle_date("2026-01-05") == "2026-01-06"

    def test_crosses_month_boundary(self):
        assert S.next_settle_date("2026-01-31") == "2026-02-01"

    def test_crosses_year_boundary(self):
        assert S.next_settle_date("2026-12-31") == "2027-01-01"

    def test_weekend_fill_settles_on_weekend_but_sellable_monday(self):
        """Friday fill → settle_date = Saturday; Saturday <= Monday = True.

        This is why the lot filter uses ``settle_date <= today`` rather
        than a trading-day calendar: a Saturday settle_date being <= a
        Monday today is exactly the broker's behaviour on the same
        shares.
        """
        assert S.next_settle_date("2026-01-09") == "2026-01-10"  # Fri → Sat


# ── create_lot (write path) ─────────────────────────────────────────────


class TestCreateLot:
    def test_create_lot_inserts_a_row(self, conn):
        _seed_position(conn, position_id=1)
        lid = S.create_lot(conn, position_id=1, trader_id="slow",
                           code="600000", shares=1000,
                           open_date="2026-01-05", open_price=10.0)
        assert lid > 0
        row = conn.execute(
            "SELECT * FROM settlement_lots WHERE id = ?", (lid,)).fetchone()
        assert row["shares"] == 1000
        assert row["remaining_shares"] == 1000
        assert row["settle_date"] == "2026-01-06"
        assert row["open_price"] == 10.0
        assert row["source"] == "initial"

    def test_create_lot_records_source_for_adds(self, conn):
        """The source column lets the audit log distinguish initial
        fills from later adds — both create the same row, but the
        intent is different and a future reconciliation could care."""
        _seed_position(conn, position_id=1)
        lid = S.create_lot(conn, position_id=1, trader_id="slow",
                           code="600000", shares=500,
                           open_date="2026-01-06", open_price=10.0,
                           source="add")
        row = conn.execute(
            "SELECT source FROM settlement_lots WHERE id = ?",
            (lid,)).fetchone()
        assert row["source"] == "add"

    def test_create_lot_rejects_zero_or_negative_shares(self, conn):
        """A zero-share lot is a different bug from a no-fill — make
        the caller fix it."""
        _seed_position(conn, position_id=1)
        with pytest.raises(ValueError):
            S.create_lot(conn, position_id=1, trader_id="slow",
                         code="600000", shares=0,
                         open_date="2026-01-05", open_price=10.0)


# ── sellable_shares (read path) ────────────────────────────────────────


class TestSellableShares:
    def test_lot_not_yet_settled_is_not_sellable(self, conn):
        _seed_position(conn, position_id=1)
        S.create_lot(conn, position_id=1, trader_id="slow",
                     code="600000", shares=1000,
                     open_date="2026-01-05", open_price=10.0)
        # Today is fill day; settle_date is tomorrow; not sellable.
        assert S.sellable_shares(conn, 1, "2026-01-05") == 0

    def test_lot_is_sellable_one_day_after_fill(self, conn):
        _seed_position(conn, position_id=1)
        S.create_lot(conn, position_id=1, trader_id="slow",
                     code="600000", shares=1000,
                     open_date="2026-01-05", open_price=10.0)
        # Today is the day after fill; settle_date <= today → sellable.
        assert S.sellable_shares(conn, 1, "2026-01-06") == 1000

    def test_only_remaining_shares_count(self, conn):
        """A lot whose remaining_shares has been drawn down reports the
        smaller number, not the original. The FIFO consume path is
        what decrements this; we test the read side here."""
        _seed_position(conn, position_id=1)
        S.create_lot(conn, position_id=1, trader_id="slow",
                     code="600000", shares=1000,
                     open_date="2026-01-05", open_price=10.0)
        # Manually draw down to mirror what consume_lots_fifo would do.
        conn.execute(
            "UPDATE settlement_lots SET remaining_shares = 400 "
            "WHERE position_id = 1")
        conn.commit()
        assert S.sellable_shares(conn, 1, "2026-01-10") == 400

    def test_no_lots_returns_zero(self, conn):
        """A position with no lots returns 0 — caller should fall back
        to ``open_date < today`` semantics for legacy rows."""
        _seed_position(conn, position_id=1)
        assert S.sellable_shares(conn, 1, "2026-01-10") == 0


# ── consume_lots_fifo (write path) ─────────────────────────────────────


class TestConsumeLotsFifo:
    def test_consume_from_a_single_lot(self, conn):
        _seed_position(conn, position_id=1)
        S.create_lot(conn, position_id=1, trader_id="slow",
                     code="600000", shares=1000,
                     open_date="2026-01-05", open_price=10.0)
        consumed = S.consume_lots_fifo(conn, position_id=1,
                                       shares_to_sell=400,
                                       today="2026-01-06")
        assert len(consumed) == 1
        assert consumed[0]["shares"] == 400
        assert S.sellable_shares(conn, 1, "2026-01-06") == 600

    def test_consume_fifo_across_lots(self, conn):
        """Two lots bought on different days: consume draws from the
        earlier one first."""
        _seed_position(conn, position_id=1)
        S.create_lot(conn, position_id=1, trader_id="slow",
                     code="600000", shares=400,
                     open_date="2026-01-05", open_price=10.0)  # settle 01-06
        S.create_lot(conn, position_id=1, trader_id="slow",
                     code="600000", shares=600,
                     open_date="2026-01-06", open_price=10.0)  # settle 01-07
        consumed = S.consume_lots_fifo(conn, position_id=1,
                                       shares_to_sell=500,
                                       today="2026-01-08")
        # 400 from lot 1, 100 from lot 2.
        assert len(consumed) == 2
        assert consumed[0]["shares"] == 400
        assert consumed[1]["shares"] == 100
        # Lot 1 empty, lot 2 has 500 remaining.
        rows = conn.execute(
            "SELECT id, remaining_shares FROM settlement_lots "
            "WHERE position_id = 1 ORDER BY id"
        ).fetchall()
        assert rows[0]["remaining_shares"] == 0
        assert rows[1]["remaining_shares"] == 500

    def test_consume_skips_unsettled_lots(self, conn):
        """An unsettled lot cannot be drawn from, even if it is earlier
        in the FIFO ordering — its settle_date > today."""
        _seed_position(conn, position_id=1)
        S.create_lot(conn, position_id=1, trader_id="slow",
                     code="600000", shares=400,
                     open_date="2026-01-05", open_price=10.0)  # settle 01-06
        S.create_lot(conn, position_id=1, trader_id="slow",
                     code="600000", shares=600,
                     open_date="2026-01-07", open_price=10.0)  # settle 01-08
        # today = 2026-01-06: only the first lot is settled.
        consumed = S.consume_lots_fifo(conn, position_id=1,
                                       shares_to_sell=200,
                                       today="2026-01-06")
        assert len(consumed) == 1
        assert consumed[0]["shares"] == 200

    def test_consume_raises_when_not_enough_settled(self, conn):
        """The write-side backstop: if even the settled lots cannot
        cover the request, refuse rather than silently dip into
        unsettled ones."""
        _seed_position(conn, position_id=1)
        S.create_lot(conn, position_id=1, trader_id="slow",
                     code="600000", shares=400,
                     open_date="2026-01-05", open_price=10.0)  # settle 01-06
        S.create_lot(conn, position_id=1, trader_id="slow",
                     code="600000", shares=600,
                     open_date="2026-01-07", open_price=10.0)  # settle 01-08
        # today = 2026-01-06: only 400 settled; ask for 500 → refuse.
        with pytest.raises(ValueError, match="settled"):
            S.consume_lots_fifo(conn, position_id=1,
                                shares_to_sell=500, today="2026-01-06")

    def test_consume_with_no_lots_raises(self, conn):
        """A legacy position with no lots cannot be sold through the
        lot path. The ``close_position`` flow falls back to the legacy
        ``open_date < today`` check before getting here."""
        _seed_position(conn, position_id=1)
        with pytest.raises(ValueError, match="settled shares"):
            S.consume_lots_fifo(conn, position_id=1,
                                shares_to_sell=100, today="2026-01-06")

    def test_consume_zero_or_negative_raises(self, conn):
        _seed_position(conn, position_id=1)
        S.create_lot(conn, position_id=1, trader_id="slow",
                     code="600000", shares=1000,
                     open_date="2026-01-05", open_price=10.0)
        with pytest.raises(ValueError):
            S.consume_lots_fifo(conn, position_id=1,
                                shares_to_sell=0, today="2026-01-06")
        with pytest.raises(ValueError):
            S.consume_lots_fifo(conn, position_id=1,
                                shares_to_sell=-100, today="2026-01-06")


# ── Pending settlement (cash-side) ─────────────────────────────────────


class TestPendingSettlement:
    def _seed_exit(self, conn, *, exit_id=1, position_id=1, trader_id="slow"):
        conn.execute(
            "INSERT INTO position_exits "
            "(id, position_id, trader_id, code, exit_date, price, "
            " shares, cost_basis, gross_amount, costs, net_amount, "
            " return_pct, reason, command_id) "
            "VALUES (?, ?, ?, '600000', '2026-01-10', 11.0, 500, "
            " 5000.0, 5500.0, 10.0, 5490.0, 9.8, '', ?)",
            (exit_id, position_id, trader_id, f"cmd-{exit_id}"))
        conn.commit()

    def test_record_pending_inserts_a_row(self, conn):
        self._seed_exit(conn, exit_id=1)
        pid = S.record_pending(conn, exit_id=1, trader_id="slow",
                               code="600000", net_amount=5490.0,
                               exit_date="2026-01-10")
        row = conn.execute(
            "SELECT * FROM pending_settlements WHERE id = ?",
            (pid,)).fetchone()
        assert row["net_amount"] == 5490.0
        assert row["settle_date"] == "2026-01-11"
        assert row["released"] == 0

    def test_record_pending_is_idempotent_per_exit(self, conn):
        """A retry of the same exit returns the existing row instead
        of inserting a second one — the close path uses command_ids
        for retry-safety, and a double pending would inflate the cash
        bucket."""
        self._seed_exit(conn, exit_id=1)
        first = S.record_pending(conn, exit_id=1, trader_id="slow",
                                  code="600000", net_amount=5490.0,
                                  exit_date="2026-01-10")
        second = S.record_pending(conn, exit_id=1, trader_id="slow",
                                   code="600000", net_amount=5490.0,
                                   exit_date="2026-01-10")
        assert first == second
        count = conn.execute(
            "SELECT COUNT(*) FROM pending_settlements WHERE exit_id = 1"
        ).fetchone()[0]
        assert count == 1

    def test_unreleased_pending_sums_only_unreleased(self, conn):
        self._seed_exit(conn, exit_id=1)
        self._seed_exit(conn, exit_id=2)
        S.record_pending(conn, exit_id=1, trader_id="slow",
                         code="600000", net_amount=5490.0,
                         exit_date="2026-01-10")
        S.record_pending(conn, exit_id=2, trader_id="slow",
                         code="600000", net_amount=3000.0,
                         exit_date="2026-01-11")
        # Mark exit 1 as released.
        conn.execute(
            "UPDATE pending_settlements SET released = 1 WHERE exit_id = 1"
        )
        conn.commit()
        assert S.unreleased_pending_total(conn, "slow") == 3000.0

    def test_release_due_settlements_flips_rows(self, conn):
        self._seed_exit(conn, exit_id=1)
        self._seed_exit(conn, exit_id=2)
        S.record_pending(conn, exit_id=1, trader_id="slow",
                         code="600000", net_amount=1000.0,
                         exit_date="2026-01-10")  # settle 01-11
        S.record_pending(conn, exit_id=2, trader_id="slow",
                         code="600000", net_amount=2000.0,
                         exit_date="2026-01-12")  # settle 01-13
        # Today = 01-12: only row 1 is due.
        released_count = S.release_due_settlements(conn, "2026-01-12")
        assert released_count == 1
        # Idempotent: a second call on the same day flips nothing new.
        assert S.release_due_settlements(conn, "2026-01-12") == 0
        # Today = 01-13: row 2 also clears.
        assert S.release_due_settlements(conn, "2026-01-13") == 1
        # Both released → unreleased total is now zero.
        assert S.unreleased_pending_total(conn, "slow") == 0.0

    def test_pending_summary(self, conn):
        self._seed_exit(conn, exit_id=1)
        S.record_pending(conn, exit_id=1, trader_id="slow",
                         code="600000", net_amount=5490.0,
                         exit_date="2026-01-10")
        s = S.pending_summary(conn, "slow")
        assert s["unreleased_total"] == 5490.0
        assert s["unreleased_count"] == 1
        assert s["released_total"] == 0.0
        assert s["released_count"] == 0


# ── has_lots / lot_count (diagnostic) ──────────────────────────────────


class TestHasLots:
    def test_returns_false_for_position_with_no_lots(self, conn):
        _seed_position(conn, position_id=1)
        assert S.has_lots(conn, 1) is False

    def test_returns_true_after_a_lot_is_created(self, conn):
        _seed_position(conn, position_id=1)
        S.create_lot(conn, position_id=1, trader_id="slow",
                     code="600000", shares=1000,
                     open_date="2026-01-05", open_price=10.0)
        assert S.has_lots(conn, 1) is True

    def test_lot_count_counts_lots(self, conn):
        _seed_position(conn, position_id=1)
        S.create_lot(conn, position_id=1, trader_id="slow",
                     code="600000", shares=500,
                     open_date="2026-01-05", open_price=10.0)
        S.create_lot(conn, position_id=1, trader_id="slow",
                     code="600000", shares=500,
                     open_date="2026-01-06", open_price=10.0)
        assert S.lot_count(conn, 1) == 2


# ── Wiring: the portfolio path calls into the module ───────────────────

from unittest.mock import patch  # noqa: E402

from alpha_agents.data import portfolio as P  # noqa: E402
from alpha_agents.data import portfolio_exit as PE  # noqa: E402
from alpha_agents.data import trader as TR  # noqa: E402
from alpha_agents.data.portfolio_exit import SLIPPAGE_RATE  # noqa: E402


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_store, "MEMORY_DB_PATH",
                        tmp_path / "memory.db", raising=False)
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


SLOW = "id: slow\nname: 回调派\ncapital: 500000\n"


def _write(d, name, body):
    (d / f"{name}.yaml").write_text(body, encoding="utf-8")


def _db():
    return memory_store._get_conn()


class TestThePortfolioPathCreatesAndConsumesLots:
    """The three write sites — fill, add, close — each keep the lot
    table honest. Without this class the module tests could all pass
    while nothing in production ever called into it."""

    def test_a_fill_creates_one_lot(self, store, traders_dir, theme):
        _write(traders_dir, "slow", SLOW)
        oid = P.create_pending_order(
            code="600000", name="A", theme="t", order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, stop_loss=8.5,
            source="morning", reason="主线在流入", trader_id="slow")
        P._fill_order(P.get_pending_orders("slow")[0],
                      fill_price=10.0, fill_date="2026-01-05")
        lots = _db().execute(
            "SELECT * FROM settlement_lots WHERE position_id = ?",
            (oid,)).fetchall()
        assert len(lots) == 1
        assert lots[0]["source"] == "initial"
        assert lots[0]["settle_date"] == "2026-01-06"
        assert lots[0]["remaining_shares"] == lots[0]["shares"]

    def test_a_fill_is_not_sellable_the_same_day(self, store, traders_dir,
                                                 theme):
        """T+1 through the read path: the lot exists but its settle_date
        is tomorrow, so today's sellable count is zero."""
        _write(traders_dir, "slow", SLOW)
        oid = P.create_pending_order(
            code="600000", name="A", theme="t", order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, stop_loss=8.5,
            source="morning", reason="主线在流入", trader_id="slow")
        P._fill_order(P.get_pending_orders("slow")[0],
                      fill_price=10.0, fill_date="2026-01-05")
        assert S.sellable_shares(_db(), oid, "2026-01-05") == 0
        assert S.sellable_shares(_db(), oid, "2026-01-06") > 0

    def test_an_add_creates_a_second_lot(self, store, traders_dir, theme):
        """The add's shares settle one day after the add, not one day
        after the original fill — this is the case the old
        ``open_date < today`` check could not express."""
        _write(traders_dir, "slow", SLOW)
        oid = P.create_pending_order(
            code="600000", name="A", theme="t", order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, stop_loss=8.5,
            source="morning", reason="主线在流入", trader_id="slow")
        P._fill_order(P.get_pending_orders("slow")[0],
                      fill_price=10.0, fill_date="2026-01-05")
        added = P.add_to_position(oid, price=9.5, reason="补仓")
        assert added is not None
        lots = _db().execute(
            "SELECT source, shares FROM settlement_lots "
            "WHERE position_id = ? ORDER BY id", (oid,)).fetchall()
        assert len(lots) == 2
        assert lots[1]["source"] == "add"
        assert lots[1]["shares"] == added["shares"]

    def test_a_close_consumes_lots_and_records_pending(
            self, store, traders_dir, theme):
        """Selling draws the lot down and parks the proceeds in
        ``pending_settlements`` for T+1."""
        _write(traders_dir, "slow", SLOW)
        oid = P.create_pending_order(
            code="600000", name="A", theme="t", order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, stop_loss=8.5,
            source="morning", reason="主线在流入", trader_id="slow")
        P._fill_order(P.get_pending_orders("slow")[0],
                      fill_price=10.0, fill_date="2026-01-05")
        held = _db().execute(
            "SELECT shares FROM virtual_portfolio WHERE id = ?",
            (oid,)).fetchone()["shares"]
        half = (held // 200) * 100   # a legal partial size in lots
        assert PE.close_position(oid, close_price=11.0,
                                 close_reason="减仓止盈", shares=half)
        # The lot drew down by exactly the sold quantity.
        row = _db().execute(
            "SELECT remaining_shares FROM settlement_lots "
            "WHERE position_id = ?", (oid,)).fetchone()
        assert row["remaining_shares"] == held - half
        # And the proceeds are pending, not available.
        pend = S.pending_summary(_db(), "slow")
        assert pend["unreleased_count"] == 1
        assert pend["unreleased_total"] > 0
        assert S.unreleased_pending_total(_db(), "slow") > 0

    def test_pending_is_the_proceeds_not_the_pnl(
            self, store, traders_dir, theme):
        """The pending amount is what the broker holds — cost basis
        coming back plus the gain — not just the gain. Recording the
        P&L alone would credit the returned capital a day early."""
        _write(traders_dir, "slow", SLOW)
        oid = P.create_pending_order(
            code="600000", name="A", theme="t", order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, stop_loss=8.5,
            source="morning", reason="主线在流入", trader_id="slow")
        P._fill_order(P.get_pending_orders("slow")[0],
                      fill_price=10.0, fill_date="2026-01-05")
        assert PE.close_position(oid, close_price=11.0,
                                 close_reason="止盈触发")
        pend = _db().execute(
            "SELECT net_amount FROM pending_settlements "
            "WHERE trader_id = 'slow'").fetchone()["net_amount"]
        exit_row = _db().execute(
            "SELECT cost_basis, net_amount FROM position_exits "
            "WHERE position_id = ?", (oid,)).fetchone()
        expected = round(exit_row["cost_basis"] + exit_row["net_amount"], 2)
        assert pend == pytest.approx(expected)
        # Sanity: the proceeds exceed the P&L on a small move — proves
        # we did not accidentally store only the gain.
        assert pend > exit_row["net_amount"]

    def test_releasing_settlements_returns_cash_to_available(
            self, store, traders_dir, theme):
        """Before release, available excludes the proceeds; after, it
        includes them. This is the end-to-end statement of the whole
        cash-side rule."""
        _write(traders_dir, "slow", SLOW)
        oid = P.create_pending_order(
            code="600000", name="A", theme="t", order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, stop_loss=8.5,
            source="morning", reason="主线在流入", trader_id="slow")
        P._fill_order(P.get_pending_orders("slow")[0],
                      fill_price=10.0, fill_date="2026-01-05")
        PE.close_position(oid, close_price=11.0, close_reason="止盈触发")
        before = P.get_available_capital("slow")
        S.release_due_settlements(_db(), "2099-12-31")
        _db().commit()
        after = P.get_available_capital("slow")
        assert after > before, \
            "released proceeds must raise available cash"
        assert S.unreleased_pending_total(_db(), "slow") == 0.0


class TestTheMonitorSeesOnlySettledShares:
    """``check_positions`` gates on settled shares now, not on a date
    column. A position inserted with no lots (a legacy row) still falls
    back to ``open_date < today`` so rows that pre-date the cutover are
    governed the same way they always were."""

    def _insert_open(self, *, code, open_date, shares=1000,
                     open_price=10.0, trader_id="slow"):
        c = _db()
        c.execute(
            "INSERT INTO virtual_portfolio (code, name, theme, order_date, "
            " open_date, open_price, shares, stop_loss, status, source, "
            " reason, trader_id) "
            "VALUES (?, 'A', 't', ?, ?, ?, ?, 8.5, 'open', 'morning', 'x', ?)",
            (code, open_date, open_date, open_price, shares, trader_id))
        c.commit()
        return c.execute("SELECT id FROM virtual_portfolio WHERE code = ?",
                         (code,)).fetchone()["id"]

    def test_legacy_position_with_no_lots_still_uses_open_date(
            self, store, theme):
        """No lot row → fall back to open_date < today, which is the
        pre-S4 behaviour legacy rows depend on."""
        self._insert_open(code="600000", open_date="2026-01-05")
        alerts = P.check_positions(realtime_prices={"600000": 1.0},
                                   today="2026-01-06")
        # Price far below stop and the legacy date gate passes → the
        # monitor evaluated it (it produced a stopped alert).
        assert len(alerts) == 1
        assert alerts[0]["type"] == "stopped"

    def test_legacy_position_bought_today_is_skipped(self, store, theme):
        self._insert_open(code="600000", open_date="2026-01-06")
        alerts = P.check_positions(realtime_prices={"600000": 1.0},
                                   today="2026-01-06")
        assert alerts == []

    def test_a_position_with_an_unsettled_lot_is_skipped(
            self, store, traders_dir, theme):
        """A position whose only lot was created today is not eligible
        even though ``open_date`` equals today — the lot rule and the
        date rule agree here, and the lot rule is what actually runs."""
        _write(traders_dir, "slow", SLOW)
        oid = P.create_pending_order(
            code="600000", name="A", theme="t", order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, stop_loss=8.5,
            source="morning", reason="主线在流入", trader_id="slow")
        P._fill_order(P.get_pending_orders("slow")[0],
                      fill_price=10.0, fill_date="2026-01-05")
        # Today is the fill day; the lot settles tomorrow.
        alerts = P.check_positions(realtime_prices={"600000": 1.0},
                                   today="2026-01-05")
        assert alerts == [], "an unsettled lot must not be monitored"

    def test_a_position_one_day_old_is_monitored(self, store, traders_dir,
                                                 theme):
        _write(traders_dir, "slow", SLOW)
        P.create_pending_order(
            code="600000", name="A", theme="t", order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, stop_loss=8.5,
            source="morning", reason="主线在流入", trader_id="slow")
        P._fill_order(P.get_pending_orders("slow")[0],
                      fill_price=10.0, fill_date="2026-01-05")
        # Next day: the lot has settled and the monitor evaluates it.
        alerts = P.check_positions(realtime_prices={"600000": 1.0},
                                   today="2026-01-06")
        assert len(alerts) == 1
        assert alerts[0]["type"] == "stopped"