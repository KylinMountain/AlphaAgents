"""The attribution chain, the frozen boundary, and outcome separation.

Design §4 makes the execution chain explicit —
``thesis_id → order_id → fill_id → ledger_entry_id`` — and §8 requires a
decision's information boundary to be recorded when it is made and never
rewritten. Both are the same idea: ownership as a stored fact.

The failure these tests exist for is specific. Binding a fill to "the
first live thesis with the same stock code" is not an ownership relation.
It happened to work in the sequential single-trader case, which is exactly
why it survived: with two traders on one stock, whichever order filled
second could take the first trader's idea, after which the monitor
evaluated one book's thesis against the other book's cost basis.
"""

from unittest.mock import patch

import pytest
import sqlite3

from alpha_agents.data import attribution as A
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
FAST = "id: fast\nname: 突破派\ncapital: 300000\ndefault_size_pct: 0.05\n"


def _conn():
    from alpha_agents.data import memory_store
    return memory_store._get_conn()


def _thesis(code="600000", trader_id="slow", prob=0.6):
    return T.create(T.Thesis(
        code=code, name="A", theme="t", claim="涨", prob=prob,
        conviction=0.2, trader_id=trader_id,
        conditions=[T.Condition("price_below", 9.0, "止损位")]))


class TestTheChainIsStored:
    """Every hop is a column, so nothing has to be re-derived."""

    def test_an_order_names_its_thesis_and_the_chain_resolves(
            self, store, traders_dir, theme):
        write(traders_dir, "slow", SLOW)
        tid = _thesis()
        oid = P.create_pending_order(code="600000", name="A", theme="t",
                                     order_date="2026-01-05",
                                     entry_low=9.0, entry_high=11.0,
                                     trader_id="slow", thesis_id=tid)
        assert oid

        chain = A.resolve_chain(_conn(), oid)
        assert chain["thesis_id"] == tid
        assert chain["code"] == "600000"
        assert chain["trader_id"] == "slow"
        assert chain["exits"] == []
        assert chain["snapshot_id"] is not None

    def test_a_filled_order_binds_the_thesis_that_named_it(
            self, store, traders_dir, theme):
        write(traders_dir, "slow", SLOW)
        tid = _thesis()
        oid = P.create_pending_order(code="600000", name="A", theme="t",
                                     order_date="2026-01-05",
                                     entry_low=9.0, entry_high=11.0,
                                     trader_id="slow", thesis_id=tid)
        P.check_pending_orders({"600000": 10.0}, "2026-01-06",
                               trader_id="slow")
        row = _conn().execute(
            "SELECT vp.id AS position_id, vp.thesis_id AS order_thesis, "
            "vp.status AS position_status FROM theses th "
            "JOIN virtual_portfolio vp ON vp.id = th.position_id "
            "WHERE th.id = ?", (tid,)).fetchone()
        assert row["position_status"] == "open"
        assert row["position_id"] == oid
        assert row["order_thesis"] == tid

    def test_an_exit_leg_carries_the_thesis(self, store, traders_dir, theme):
        write(traders_dir, "slow", SLOW)
        tid = _thesis()
        oid = P.create_pending_order(code="600000", name="A", theme="t",
                                     order_date="2026-01-05",
                                     entry_low=9.0, entry_high=11.0,
                                     trader_id="slow", thesis_id=tid)
        P.check_pending_orders({"600000": 10.0}, "2026-01-06",
                               trader_id="slow")
        assert P.close_position(oid, close_price=10.5,
                                close_reason="止盈触发")

        chain = A.resolve_chain(_conn(), oid)
        assert len(chain["exits"]) == 1
        assert chain["exits"][0]["thesis_id"] == tid

    def test_an_order_without_an_idea_keeps_an_empty_link(
            self, store, traders_dir, theme):
        """A manual order has no thesis, and none is invented for it."""
        write(traders_dir, "slow", SLOW)
        oid = P.create_pending_order(code="600000", name="A", theme="t",
                                     order_date="2026-01-05",
                                     entry_low=9.0, entry_high=11.0,
                                     trader_id="slow")
        assert A.resolve_chain(_conn(), oid)["thesis_id"] is None


class TestBindingIsScopedToTheBook:
    """Two traders on one stock must not swap ideas."""

    def test_the_fill_binds_its_own_thesis_not_the_first_live_one(
            self, store, traders_dir, theme):
        """The regression. ``fast`` fills first; ``slow``'s idea is live and
        unfilled at that moment, and the old lookup — every live thesis on
        the code, first unfilled wins — handed fast's position to slow's
        thesis."""
        write(traders_dir, "slow", SLOW)
        write(traders_dir, "fast", FAST)
        slow_thesis = _thesis(trader_id="slow")
        fast_thesis = _thesis(trader_id="fast")

        fast_order = P.create_pending_order(
            code="600000", name="A", theme="t", order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, trader_id="fast",
            thesis_id=fast_thesis)
        P.check_pending_orders({"600000": 10.0}, "2026-01-06",
                               trader_id="fast")

        conn = _conn()
        fast_row = conn.execute("SELECT position_id FROM theses WHERE id=?",
                                (fast_thesis,)).fetchone()
        slow_row = conn.execute("SELECT position_id FROM theses WHERE id=?",
                                (slow_thesis,)).fetchone()
        assert fast_row["position_id"] == fast_order
        assert slow_row["position_id"] is None

    def test_a_thesis_from_another_book_is_refused_before_the_order_exists(
            self, store, traders_dir, theme):
        write(traders_dir, "slow", SLOW)
        write(traders_dir, "fast", FAST)
        slow_thesis = _thesis(trader_id="slow")
        oid = P.create_pending_order(
            code="600000", name="A", theme="t", order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, trader_id="fast",
            thesis_id=slow_thesis)
        assert oid is None
        assert P.get_open_positions("fast") == []

    def test_a_thesis_about_another_stock_is_refused(
            self, store, traders_dir, theme):
        write(traders_dir, "slow", SLOW)
        other = _thesis(code="000001", trader_id="slow")
        oid = P.create_pending_order(
            code="600000", name="A", theme="t", order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, trader_id="slow",
            thesis_id=other)
        assert oid is None

    def test_ownership_is_read_from_storage(
            self, store, traders_dir, theme):
        write(traders_dir, "slow", SLOW)
        tid = _thesis()
        conn = _conn()
        assert A.valid_thesis(conn, tid, "600000", "slow")
        assert not A.valid_thesis(conn, tid, "600000", "fast")
        assert not A.valid_thesis(conn, tid, "000001", "slow")
        assert not A.valid_thesis(conn, 0, "600000", "slow")
        assert not A.valid_thesis(conn, "7", "600000", "slow")
        assert A.valid_thesis(conn, None, "600000", "slow")


class TestTheBoundaryIsFrozen:
    """Invariant 4: the decision-time information boundary cannot move."""

    def test_an_order_freezes_what_it_was_allowed_to_know(
            self, store, traders_dir, theme):
        write(traders_dir, "slow", SLOW)
        tid = _thesis()
        oid = P.create_pending_order(
            code="600000", name="A", theme="t", order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, stop_loss=8.5,
            source="morning", reason="主线在流入", trader_id="slow",
            thesis_id=tid)
        snap = A.snapshot_for_order(_conn(), oid)
        assert snap["thesis_id"] == tid
        assert snap["order_id"] == oid
        assert snap["information_cutoff"] == "2026-01-05"
        assert snap["decided_at"] == "2026-01-05"
        # The declared inputs are stored verbatim, not summarised.
        assert snap["payload"]["entry_low"] == 9.0
        assert snap["payload"]["stop_loss"] == 8.5
        assert snap["payload"]["reason"] == "主线在流入"
        assert snap["trader_id"] == "slow"
        assert len(snap["content_hash"]) == 64

    def test_a_snapshot_cannot_be_updated_or_deleted(
            self, store, traders_dir, theme):
        """Enforced by the database, not by everyone remembering."""
        write(traders_dir, "slow", SLOW)
        oid = P.create_pending_order(code="600000", name="A", theme="t",
                                     order_date="2026-01-05",
                                     entry_low=9.0, entry_high=11.0,
                                     trader_id="slow")
        conn = _conn()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE decision_snapshots SET payload_json='{}' "
                         "WHERE order_id=?", (oid,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM decision_snapshots WHERE order_id=?",
                         (oid,))
        conn.rollback()
        # The boundary is still there and still says what it said.
        snap = A.snapshot_for_order(conn, oid)
        assert snap["payload"]["entry_low"] == 9.0

    def test_the_hash_is_deterministic_and_covers_the_boundary(
            self, store, traders_dir, theme):
        write(traders_dir, "slow", SLOW)
        a = P.create_pending_order(code="600000", name="A", theme="t",
                                   order_date="2026-01-05",
                                   entry_low=9.0, entry_high=11.0,
                                   trader_id="slow")
        b = P.create_pending_order(code="000001", name="B", theme="t",
                                   order_date="2026-01-05",
                                   entry_low=9.0, entry_high=11.0,
                                   trader_id="slow")
        conn = _conn()
        sa, sb = A.snapshot_for_order(conn, a), A.snapshot_for_order(conn, b)
        assert sa["content_hash"] != sb["content_hash"]
        # Recomputing from the stored fields reproduces the stored hash, so
        # a rewritten snapshot is detectable even if a trigger were dropped.
        recomputed = A._hash({
            "trader_id": sa["trader_id"], "code": sa["code"],
            "information_cutoff": sa["information_cutoff"],
            "decided_at": sa["decided_at"], "thesis_id": sa["thesis_id"],
            "order_id": sa["order_id"], "prediction_id": sa["prediction_id"],
            "policy_ref": sa["policy_ref"], "model_ref": sa["model_ref"],
            "sources": sa["sources"], "payload": sa["payload"],
            "supersedes_id": sa["supersedes_id"],
        })
        assert recomputed == sa["content_hash"]

    def test_a_superseding_snapshot_leaves_the_original_readable(
            self, store, traders_dir, theme):
        write(traders_dir, "slow", SLOW)
        oid = P.create_pending_order(code="600000", name="A", theme="t",
                                     order_date="2026-01-05",
                                     entry_low=9.0, entry_high=11.0,
                                     trader_id="slow")
        conn = _conn()
        first = A.snapshot_for_order(conn, oid)
        A.freeze(conn, trader_id="slow", code="600000",
                 information_cutoff="2026-01-05", decided_at="2026-01-06",
                 payload={"action": "reaffirm"}, order_id=oid,
                 supersedes_id=first["id"])
        conn.commit()

        history = A.snapshot_history(conn, oid)
        assert [s["id"] for s in history] == [first["id"], first["id"] + 1]
        assert history[0]["payload"] == first["payload"]
        assert A.snapshot_for_order(conn, oid)["payload"] == {"action": "reaffirm"}


class TestOutcomesDoNotOverwrite:
    """§9: forecast, trade and process results are three separate facts.

    One episode can hold a correct forecast and a losing trade at the same
    time. Collapsing them — which the close path used to do by writing the
    realised return into ``predictions.hit`` — destroys the forecast score
    and invents a trading result out of a price move.
    """

    def _open_and_close(self, store, traders_dir, theme, exit_price,
                        prediction_id, trader_id="slow"):
        write(traders_dir, "slow", SLOW)
        tid = _thesis(trader_id=trader_id)
        oid = P.create_pending_order(
            code="600000", name="A", theme="t", order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, trader_id=trader_id,
            thesis_id=tid, prediction_id=prediction_id)
        assert oid
        P.check_pending_orders({"600000": 10.0}, "2026-01-06",
                               trader_id=trader_id)
        assert P.close_position(oid, close_price=exit_price,
                                close_reason="止损触发")
        return tid, oid

    def _prediction(self, trader_id="slow", *, hit, week_return):
        from alpha_agents.data import memory_store
        pid = memory_store.save_prediction(
            date="2026-01-05", report_type="morning", code="600000",
            name="A", direction="bullish", confidence="high",
            theme_line="t", entry_price=10.0, reason="r",
            trader_id=trader_id)
        conn = _conn()
        conn.execute("UPDATE predictions SET hit=?, week_return=?, "
                     "scored_at='2026-01-12' WHERE id=?",
                     (hit, week_return, pid))
        conn.commit()
        return pid

    def test_a_correct_forecast_survives_a_losing_trade(
            self, store, traders_dir, theme):
        pid = self._prediction(hit=1, week_return=3.0)
        tid, oid = self._open_and_close(store, traders_dir, theme, 9.2, pid)

        conn = _conn()
        forecast = A.forecast_outcome(conn, pid)
        trade = A.trade_outcome(conn, tid)
        assert forecast["hit"] == 1              # the forecast was right
        assert forecast["week_return"] == 3.0
        assert trade["net_amount"] < 0           # the trade lost money
        assert trade["fills"] == 1

    def test_a_winning_trade_does_not_upgrade_a_wrong_forecast(
            self, store, traders_dir, theme):
        pid = self._prediction(hit=0, week_return=-2.0)
        tid, oid = self._open_and_close(store, traders_dir, theme, 10.0, pid)

        conn = _conn()
        forecast = A.forecast_outcome(conn, pid)
        trade = A.trade_outcome(conn, tid)
        assert forecast["hit"] == 0
        assert forecast["week_return"] == -2.0
        assert trade["fills"] == 1               # it still traded

    def test_an_unfilled_thesis_has_no_trading_result(
            self, store, traders_dir, theme):
        """No fill is not a flat return; only one of the two is evidence."""
        write(traders_dir, "slow", SLOW)
        tid = _thesis()
        trade = A.trade_outcome(_conn(), tid)
        assert trade["fills"] == 0
        assert trade["net_amount"] == 0.0
        assert trade["return_pct"] is None
        assert trade["opened"] is False

    def test_the_trade_outcome_comes_from_the_ledger_not_the_forecast(
            self, store, traders_dir, theme):
        """A profitable forecast does not create a trade result."""
        pid = self._prediction(hit=1, week_return=8.0)
        write(traders_dir, "slow", SLOW)
        tid = _thesis()
        trade = A.trade_outcome(_conn(), tid)
        assert trade["fills"] == 0
        assert A.forecast_outcome(_conn(), pid)["hit"] == 1

    def test_a_missing_forecast_reads_as_absent_not_as_a_miss(
            self, store):
        assert A.forecast_outcome(_conn(), 999999) is None
