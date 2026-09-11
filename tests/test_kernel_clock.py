"""The trading kernel's time boundary: what day is it, and what may it know.

S6 is about the difference between "the kernel takes a date parameter"
and "the kernel cannot obtain a date from anywhere else". The first was
already true; the second was not, and three writes reached for the wall
clock anyway. Under ``evolution.replay_mode`` those three dated a March
decision in September, which is not a cosmetic problem — the T+1 window
is then measured from a date the replay has not reached, and the
``settle_date`` written into ``settlement_lots`` is one no later replay
day can ever satisfy, so the position looks permanently unsellable.

The tests are in three groups:

1. ``clock.today()`` — replay-aware, and deliberately *not*
   ``effective_eod_cut_date``: a morning replay of 3/20 knows it is 3/20
   even though the EOD price data it may read stops at 3/19.
2. The guards. A fill dated after the clock is refused, and so is one
   that cites information from after the act. Both raise: a look-ahead
   is a fault, not a trade the rules declined.
3. The kernel actually writes inside the replayed window — the close
   books its exit on the replay day, not on the real one. This is the
   group that fails if ``clock.today()`` is reverted to the wall clock.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from alpha_agents.data import clock, intent as I
from alpha_agents.data import memory_store, portfolio as P
from alpha_agents.data import trader as TR
from alpha_agents.evolution.replay_mode import (
    effective_eod_cut_date, get_replay_as_of, replay_as_of,
)


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


def _db():
    return memory_store._get_conn()


SLOW = "id: slow\nname: 回调派\ncapital: 500000\n"


def _write(d, name, body):
    (d / f"{name}.yaml").write_text(body, encoding="utf-8")


def _place(traders_dir, *, order_date):
    _write(traders_dir, "slow", SLOW)
    return P.create_pending_order(
        code="600000", name="A", theme="t", order_date=order_date,
        entry_low=9.0, entry_high=11.0, stop_loss=8.5,
        source="morning", reason="主线在流入", trader_id="slow")


# ── 1. The clock ───────────────────────────────────────────────────────


class TestTheKernelClock:
    def test_live_mode_is_the_local_date(self):
        assert get_replay_as_of() is None
        assert clock.today() == date.today().isoformat()

    def test_replay_mode_is_the_replay_date(self):
        with replay_as_of("2026-03-20"):
            assert clock.today() == "2026-03-20"

    def test_a_morning_replay_is_still_that_day(self):
        """The distinction the module exists for.

        At 06:30 on 3/20 the kernel may only read price data up to 3/19
        (the EOD cut), but it *is* 3/20 — an order placed pre-open is
        dated 3/20 and its T+1 lot settles 3/21. Feeding the data cut
        into the kernel's own date would date every pre-open fill one day
        early and desynchronise every lot from its order.
        """
        with replay_as_of("2026-03-20 06:30"):
            assert effective_eod_cut_date() == "2026-03-19"
            assert clock.today() == "2026-03-20"

    def test_the_clock_does_not_leak_when_the_context_exits(self):
        with replay_as_of("2026-03-20"):
            assert clock.today() == "2026-03-20"
        assert clock.today() == date.today().isoformat()


# ── 2. The guards ──────────────────────────────────────────────────────


class TestTheFillGuards:
    def test_a_fill_dated_after_the_clock_is_refused(
            self, store, traders_dir, theme):
        oid = _place(traders_dir, order_date="2026-03-20")
        order = P.get_pending_orders("slow")[0]
        with pytest.raises(clock.LookAheadError) as e:
            P._fill_order(order, fill_price=10.0, fill_date="2027-01-01")
        assert "after the kernel clock" in str(e.value)
        # And nothing was written: the order is still pending.
        assert _db().execute(
            "SELECT status FROM virtual_portfolio WHERE id = ?",
            (oid,)).fetchone()["status"] == "pending"

    def test_the_replay_instant_bounds_the_fill(
            self, store, traders_dir, theme):
        """The bug S6 fixes, stated as a test: replaying 3/20 must not be
        able to book a 3/21 fill. Before the kernel had a clock this
        succeeded — the date came from the argument, and the guard that
        would have questioned it did not exist."""
        with replay_as_of("2026-03-20"):
            _place(traders_dir, order_date="2026-03-20")
        order = P.get_pending_orders("slow")[0]
        with replay_as_of("2026-03-20"):
            with pytest.raises(clock.LookAheadError):
                P._fill_order(order, fill_price=10.0, fill_date="2026-03-21")
        # A fill on the replay day itself is fine.
        with replay_as_of("2026-03-20"):
            assert P._fill_order(order, fill_price=10.0,
                                 fill_date="2026-03-20")

    def test_acting_on_information_from_after_the_act_is_refused(
            self, store, traders_dir, theme):
        """``information_cutoff`` was written faithfully and read by
        nothing. An order whose decision cites 3/25 cannot be filled on
        3/22: at the moment of the trade the declared basis did not exist
        yet."""
        _place(traders_dir, order_date="2026-03-25")
        order = P.get_pending_orders("slow")[0]
        assert I.cutoff_for_order(_db(), order["id"]) == "2026-03-25"
        with pytest.raises(clock.LookAheadError) as e:
            P._fill_order(order, fill_price=10.0, fill_date="2026-03-22")
        assert "postdates the act" in str(e.value)

    def test_deciding_before_acting_is_the_normal_case(
            self, store, traders_dir, theme):
        """The direction of the check matters: a cutoff *earlier* than
        the act is exactly right — decide on yesterday's close, act
        today. Only a cutoff later than the act is impossible."""
        _place(traders_dir, order_date="2026-03-19")
        order = P.get_pending_orders("slow")[0]
        assert P._fill_order(order, fill_price=10.0, fill_date="2026-03-20")

    def test_an_order_with_no_declared_cutoff_is_not_a_violation(
            self, store, traders_dir, theme):
        """Unknown is not the same as unbounded. An order written outside
        the door — a pre-S6 legacy row, or a hand-inserted one — makes no
        claim, and a guard that fired on it would make every such row
        unfillable. The declaration has to be *absent* for this to mean
        anything, so it is removed rather than never made: the fixture
        places the order through the door, which records a cutoff."""
        _place(traders_dir, order_date="2026-03-20")
        order = P.get_pending_orders("slow")[0]
        # The row the wrapper wrote is the only declaration there is.
        _db().execute("DELETE FROM intents")
        _db().commit()
        assert I.cutoff_for_order(_db(), order["id"]) is None
        assert P._fill_order(order, fill_price=10.0, fill_date="2026-03-20")


class TestTheAttributionBoundary:
    def test_a_cutoff_after_the_clock_is_refused(self, store):
        """Invariant 4 held on the honour system until S6: the column was
        never read, so claiming to have seen next week cost nothing."""
        from alpha_agents.data import attribution
        with pytest.raises(clock.LookAheadError) as e:
            attribution.freeze(
                _db(), trader_id="slow", code="600000",
                information_cutoff="2099-01-01", decided_at="2026-03-20",
                payload={"action": "open"})
        assert "information_cutoff" in str(e.value)

    def test_a_cutoff_after_the_replay_instant_is_refused(self, store):
        from alpha_agents.data import attribution
        with replay_as_of("2026-03-20"):
            with pytest.raises(clock.LookAheadError):
                attribution.freeze(
                    _db(), trader_id="slow", code="600000",
                    information_cutoff="2026-03-25",
                    decided_at="2026-03-20", payload={"action": "open"})

    def test_a_past_cutoff_still_freezes(self, store):
        from alpha_agents.data import attribution
        sid = attribution.freeze(
            _db(), trader_id="slow", code="600000",
            information_cutoff="2026-03-19", decided_at="2026-03-20",
            payload={"action": "open"})
        assert sid


# ── 3. The kernel writes inside the replayed window ────────────────────


class TestTheBookIsDatedInTheReplayWindow:
    def test_the_links_are_columns_not_json(self, store, traders_dir, theme):
        """``intents.order_id`` existed and was never written — the same
        class of defect as a field never read. The fill path reads it
        back, so it has to be a real column."""
        oid = _place(traders_dir, order_date="2026-03-20")
        row = _db().execute(
            "SELECT order_id, information_cutoff FROM intents "
            "WHERE action = 'open' ORDER BY id").fetchone()
        assert row["order_id"] == oid
        assert row["information_cutoff"] == "2026-03-20"
        assert I.cutoff_for_order(_db(), oid) == "2026-03-20"

    def test_a_fill_creates_a_lot_dated_in_the_replay_window(
            self, store, traders_dir, theme):
        with replay_as_of("2026-03-20"):
            oid = _place(traders_dir, order_date="2026-03-20")
            P._fill_order(P.get_pending_orders("slow")[0],
                          fill_price=10.0, fill_date="2026-03-20")
        lot = _db().execute(
            "SELECT open_date, settle_date FROM settlement_lots "
            "WHERE position_id = ?", (oid,)).fetchone()
        assert lot["open_date"] == "2026-03-20"
        assert lot["settle_date"] == "2026-03-21"

    def test_a_close_is_booked_on_the_replay_day(
            self, store, traders_dir, theme):
        """The sharpest form of the bug: without a kernel clock this exit
        is dated the real September day, inside a March replay, and it is
        the ledger — not a log line — that carries the wrong date."""
        with replay_as_of("2026-03-20"):
            oid = _place(traders_dir, order_date="2026-03-20")
            P._fill_order(P.get_pending_orders("slow")[0],
                          fill_price=10.0, fill_date="2026-03-20")

        from alpha_agents.data.portfolio_exit import close_position
        with replay_as_of("2026-03-23"):      # the following Monday
            assert close_position(oid, close_price=11.0,
                                  close_reason="止盈触发")

        ex = _db().execute(
            "SELECT exit_date FROM position_exits WHERE position_id = ?",
            (oid,)).fetchone()
        assert ex["exit_date"] == "2026-03-23"
        assert ex["exit_date"] != date.today().isoformat()
        # The proceeds park until T+1 *of the replay*, not of today.
        pend = _db().execute(
            "SELECT settle_date FROM pending_settlements WHERE trader_id = ?",
            ("slow",)).fetchone()
        assert pend["settle_date"] == "2026-03-24"

    def test_an_add_dates_its_lot_in_the_replay_window(
            self, store, traders_dir, theme):
        with replay_as_of("2026-03-20"):
            oid = _place(traders_dir, order_date="2026-03-20")
            P._fill_order(P.get_pending_orders("slow")[0],
                          fill_price=10.0, fill_date="2026-03-20")
        with replay_as_of("2026-03-24"):
            added = P.add_to_position(oid, price=9.4, reason="补仓")
        assert added is not None
        lot = _db().execute(
            "SELECT open_date, settle_date, source FROM settlement_lots "
            "WHERE position_id = ? ORDER BY id DESC LIMIT 1",
            (oid,)).fetchone()
        assert lot["source"] == "add"
        assert lot["open_date"] == "2026-03-24"
        assert lot["settle_date"] == "2026-03-25"

    def test_an_intent_defaults_its_order_date_to_the_clock(
            self, store, traders_dir, theme):
        """The third leak: an intent submitted without a date fell back to
        ``intent._today()``, which was the wall clock. The compatibility
        wrapper makes ``order_date`` required, so the default is reached
        by submitting the intent directly — which is the path the intent
        layer exposes for exactly this."""
        _write(traders_dir, "slow", SLOW)
        with replay_as_of("2026-03-20"):
            r = I.submit_intent(I.TradeIntent(
                action=I.OPEN, code="600000", name="A", theme="t",
                entry_low=9.0, entry_high=11.0, stop_loss=8.5,
                source="morning", reason="x", trader_id="slow"))
        assert r.accepted, r.reject_reason
        row = _db().execute(
            "SELECT order_date, open_date FROM virtual_portfolio WHERE id = ?",
            (r.result,)).fetchone()
        assert row["order_date"] == "2026-03-20"


# ── 4. The pipeline that feeds the kernel ──────────────────────────────


class TestThePipelineTakesItsDateFromTheKernel:
    """The production paths that hand a *date* to the kernel are
    ``intraday_monitor`` (via ``check_pending_orders(today=…)``, which
    becomes the fill date) and the order-placement calls in
    ``morning_scan`` and ``intraday_monitor`` (``order_date``, which is
    also the information cutoff frozen against the order). All of them
    read the wall clock. Under replay the kernel now refuses those dates
    rather than mis-dating the book, so this is the difference between a
    replayed cycle running and a replayed cycle raising — worth a guard.
    It is a one-line contract in each module, so the guard is a source
    check rather than a behavioural one: driving ``run_intraday_monitor``
    needs network, quotes and the whole morning context.
    """

    def _src(self, name: str) -> str:
        repo = Path(__file__).resolve().parent.parent
        return (repo / "alpha_agents" / "pipeline" / "tasks" /
                name).read_text(encoding="utf-8")

    def test_the_business_dates_come_from_the_clock(self):
        assert "today_str = clock.today()" in self._src("intraday_monitor.py")
        assert "today = clock.today()" in self._src("intraday_monitor.py")
        assert "today = clock.today()" in self._src("morning_scan.py")

    def test_no_business_date_is_left_on_the_wall_clock(self):
        for name in ("intraday_monitor.py", "morning_scan.py"):
            src = self._src(name)
            for bad in ("today_str = now.strftime", "today = time.strftime"):
                assert bad not in src, (
                    f"{name} still derives its business date from {bad!r}; "
                    "the kernel will refuse the date it produces under "
                    "replay")


class TestALookAheadIsNotSwallowedByTheAuditHandler:
    """``_create_pending_order_impl`` and ``_open_position_impl`` wrap the
    ``attribution.freeze`` call in ``except Exception`` on purpose — a
    missing audit must not stop a trade. ``LookAheadError`` is a
    ``ValueError``, so without an explicit re-raise it would land in that
    handler and a future-dated decision boundary would be demoted to a
    warning line: the trade goes through, every log looks healthy, and
    the learning data carries the leak. Re-raise first.
    """

    def test_the_order_path_refuses_rather_than_warns(self, store,
                                                      traders_dir, theme):
        _write(traders_dir, "slow", SLOW)
        with replay_as_of("2026-03-20"):
            with pytest.raises(clock.LookAheadError):
                P.create_pending_order(
                    code="600000", name="A", theme="t",
                    order_date="2026-09-12", entry_low=9.0, entry_high=11.0,
                    stop_loss=8.5, source="morning", reason="x",
                    trader_id="slow")

    def test_the_position_path_refuses_rather_than_warns(self, store,
                                                         traders_dir, theme):
        _write(traders_dir, "slow", SLOW)
        with replay_as_of("2026-03-20"):
            with pytest.raises(clock.LookAheadError):
                P.open_position(code="600000", name="A", theme="t",
                                open_date="2026-09-12", open_price=10.0,
                                source="manual", reason="x", shares=500,
                                trader_id="slow")
