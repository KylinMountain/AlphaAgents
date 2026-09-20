"""Why a pending order finished, and what happens when it cannot.

Written on 2026-09-14 from two real orders that had waited days without
filling. Neither had failed for the reason the design assumed:

* ``#117`` (中石科技) was placed 2026-09-11 09:49 with a 95.5–98.0 zone and
  the price spent four sessions between 85.49 and 87.24 — it was never in
  the zone. It also held **no cash reservation**: the ``reservations``
  module landed at 22:58 that evening, thirteen hours after the order, and
  nothing backfilled the rows already written. Both finish paths refuse to
  run without a ``held`` row, so the order could be neither filled nor
  cancelled, and whichever rule reached it first raised out of
  ``check_pending_orders``' loop and took the rest of that book's cycle
  with it. One unfinishable row became an outage.
* ``#118`` (德福科技) was placed 14:27:42 with a 109.3–112.5 zone; the
  session's 112.02 happened *before* the service started, and afterwards
  the price never came back above 109.17. A real miss — but the system
  had no way to say so, because the metric for "the zone was never
  reached" counted only the opposite failure.

Each class below pins one of the four changes, with both halves: the
behaviour that must happen, and the behaviour that must not.
"""

from __future__ import annotations

import logging
from datetime import datetime

import pytest
from unittest.mock import patch

from alpha_agents.data import memory_store, reservations as R
from alpha_agents.data import portfolio as P
from alpha_agents.data import thesis as T
from alpha_agents.data import trader as TR


REPO_DATE = "2026-01-05"


# ── Fixtures ───────────────────────────────────────────────────────────


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
    # The horizon is declared explicitly and set to 4 so that no test below
    # can pass against the wrong source by coincidence: 4 differs from both
    # the last-resort constant (2) and the dataclass default (5).
    (d / "slow.yaml").write_text(
        "id: slow\nname: 回调派\ncapital: 500000\ndefault_horizon_days: 4\n",
        encoding="utf-8")
    monkeypatch.setattr(TR, "TRADERS_DIR", d, raising=False)
    return d


@pytest.fixture()
def theme():
    """A theme that admits and holds: active, strong, and unscored.

    ``trend_score`` is deliberately absent. The gate passes an unmeasurable
    line on purpose (see ``theme_gate``), and pinning that here keeps these
    tests about the pending-order lifecycle rather than about the gate.

    Patched in three namespaces because each module bound the lookup at its
    own import: ``memory_store`` (resolve_theme's theme list), ``portfolio``
    (the existence check and the thesis pre-check) and ``theme_gate`` (the
    cancel bar).
    """
    row = {"name": "t", "status": "active", "strength": 6,
           "daily_score": 1, "core_stocks": "[]"}
    with patch("alpha_agents.data.memory_store.get_active_themes",
               return_value=[row]), \
         patch("alpha_agents.data.memory_store.get_theme_by_name",
               return_value=row), \
         patch("alpha_agents.data.portfolio.get_theme_by_name",
               return_value=row), \
         patch("alpha_agents.data.theme_gate.get_theme_by_name",
               return_value=row), \
         patch("alpha_agents.data.sentiment_cycle.get_sentiment_cycle",
               return_value={"phase": "修复",
                             "strategy": {"max_exposure_pct": 100}}):
        yield row


# ── Helpers ────────────────────────────────────────────────────────────


def _db():
    return memory_store._get_conn()


def _status(order_id):
    return _db().execute(
        "SELECT status, close_reason FROM virtual_portfolio WHERE id = ?",
        (order_id,)).fetchone()


def _seed_pending(*, order_id=None, code="600000", name="A", theme="t",
                  order_date=REPO_DATE, entry_low=9.0, entry_high=11.0,
                  expire_days=2, trader_id="slow", held=False):
    """Write a pending order row directly.

    Needed rather than ``create_pending_order`` wherever the point of the
    test *is* the missing piece — an order written before reservations
    existed cannot be produced by the create path any more, which is the
    whole reason #117 sat there unfinishable.
    """
    conn = _db()
    cur = conn.execute(
        "INSERT INTO virtual_portfolio "
        "(code, name, theme, order_date, open_date, open_price, entry_low, "
        " entry_high, expire_days, status, source, reason, trader_id) "
        "VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, 'pending', 'morning', '测试', ?)",
        (code, name, theme, order_date, order_date, entry_low, entry_high,
         expire_days, trader_id))
    conn.commit()
    oid = cur.lastrowid
    if held:
        R.reserve_for_order(conn, order_id=oid, trader_id=trader_id, code=code,
                            amount=1000.0, reason="test")
        conn.commit()
    return oid


class TestLiquidityCapacityIsExecution:
    def test_capacity_truncates_actual_fill_shares(
            self, store, traders_dir, theme):
        oid = _seed_pending(held=True)
        alerts = P.check_pending_orders(
            {"600000": 10.0}, today=REPO_DATE, trader_id="slow",
            capacity_shares_by_code={"600000": 200})
        assert [row["type"] for row in alerts] == ["filled"]
        row = _db().execute(
            "SELECT status,shares FROM virtual_portfolio WHERE id=?",
            (oid,)).fetchone()
        assert row["status"] == "open"
        assert row["shares"] == 200

    def test_capacity_below_one_lot_refuses_fill(
            self, store, traders_dir, theme):
        oid = _seed_pending(held=True)
        alerts = P.check_pending_orders(
            {"600000": 10.0}, today=REPO_DATE, trader_id="slow",
            capacity_shares_by_code={"600000": 50})
        assert alerts == [{
            "type": "cancelled",
            "code": "600000",
            "name": "A",
            "reason": "流动性容量不足",
        }]
        row = _status(oid)
        assert row["status"] == "cancelled"
        assert "流动性容量不足" in row["close_reason"]

    def test_capacity_argument_refuses_invalid_share_limit(
            self, store, traders_dir, theme):
        _seed_pending(held=True)
        with pytest.raises(ValueError, match="max_shares"):
            P.check_pending_orders(
                {"600000": 10.0}, today=REPO_DATE, trader_id="slow",
                capacity_shares_by_code={"600000": -1})


def _seed_cancelled(close_reason, *, code="600000", trader_id="slow",
                    order_date=None):
    conn = _db()
    conn.execute(
        "INSERT INTO virtual_portfolio "
        "(code, name, theme, order_date, status, close_reason, close_date, "
        " expire_days, trader_id) "
        "VALUES (?, 'A', 't', ?, 'cancelled', ?, ?, 2, ?)",
        (code, order_date or datetime.now().strftime("%Y-%m-%d"),
         close_reason, order_date or datetime.now().strftime("%Y-%m-%d"),
         trader_id))
    conn.commit()


def _thesis(code="600000", *, horizon_days=5, conditions=(), trader_id="slow"):
    return T.create(T.Thesis(code=code, name="A", theme="t", claim="测试论点",
                             horizon_days=horizon_days, conviction=0.7,
                             trader_id=trader_id,
                             conditions=list(conditions)))


# ── B. An order with no reservation can be finished ────────────────────


class TestAnOrderWithNoReservationCanStillFinish:
    def test_the_missing_row_is_adopted_and_said_out_loud(
            self, store, traders_dir, theme, caplog):
        """#117's exact state: pending, no reservation, price not in zone.

        The order stays pending — a bookkeeping omission is not the bet's
        fault — but it now holds the cash the create path was supposed to
        earmark, and the warning names the row.
        """
        oid = _seed_pending(held=False)
        assert R.reservation_for_order(_db(), oid) is None, "the premise"

        with caplog.at_level(logging.WARNING,
                             logger="alpha_agents.data.portfolio"):
            alerts = P.check_pending_orders(
                {"600000": 5.0}, today=REPO_DATE, trader_id="slow")

        assert alerts == [], "nothing about the bet changed, in either direction"
        assert _status(oid)["status"] == "pending"

        row = R.reservation_for_order(_db(), oid)
        assert row is not None and row["state"] == R.HELD
        assert row["amount"] > 0
        assert any("carried no cash reservation" in r.getMessage()
                   for r in caplog.records), \
            "a silent repair would leave the next orphan undiagnosed"

    def test_adoption_happens_once_however_many_cycles_run(
            self, store, traders_dir, theme):
        """``reserve_for_order`` is idempotent, and the loop must rely on
        that rather than on its own bookkeeping: the same order is checked
        every five minutes."""
        oid = _seed_pending(held=False)
        P.check_pending_orders({"600000": 5.0}, today=REPO_DATE,
                               trader_id="slow")
        first = R.reservation_for_order(_db(), oid)
        P.check_pending_orders({"600000": 5.0}, today=REPO_DATE,
                               trader_id="slow")
        second = R.reservation_for_order(_db(), oid)
        assert (second["id"], second["amount"]) == (first["id"], first["amount"])

    def test_an_adopted_order_can_then_be_filled(self, store, traders_dir,
                                                theme):
        """The bug in one line: before the repair, ``consume_reservation``
        raised on this order and the fill never happened."""
        oid = _seed_pending(held=False)
        P.check_pending_orders({"600000": 5.0}, today=REPO_DATE,
                               trader_id="slow")           # adopts

        alerts = P.check_pending_orders({"600000": 10.0}, today=REPO_DATE,
                                        trader_id="slow")
        assert [a["type"] for a in alerts] == ["filled"]
        assert _status(oid)["status"] == "open"
        assert R.reservation_for_order(_db(), oid)["state"] == R.CONSUMED

    def test_an_adopted_order_can_then_be_cancelled(self, store, traders_dir,
                                                   theme):
        """And the other end. Both paths needed the row; one pass can
        adopt and cancel, because the repair lands before the rules."""
        oid = _seed_pending(held=False, entry_low=9.0, entry_high=11.0)
        alerts = P.check_pending_orders({"600000": 12.0}, today=REPO_DATE,
                                        trader_id="slow")
        assert [a["type"] for a in alerts] == ["cancelled"]
        assert "涨走" in alerts[0]["reason"]
        assert _status(oid)["status"] == "cancelled"
        assert R.reservation_for_order(_db(), oid)["state"] == R.RELEASED

    def test_a_reservation_in_a_terminal_state_skips_the_row_not_the_cycle(
            self, store, traders_dir, theme, caplog):
        """The outage half.

        A consumed reservation on a still-pending order means the order was
        finished twice. Nothing here can guess which half is real, so the
        row is left exactly as it is and reported every cycle — but the
        order queued behind it still gets its turn.
        """
        broken = _seed_pending(code="600001", held=True)
        conn = _db()
        R.consume_reservation(conn, broken, actual_cost=100.0)
        conn.commit()
        healthy = _seed_pending(code="600002", entry_low=9.0, entry_high=11.0,
                                held=True)

        with caplog.at_level(logging.ERROR,
                             logger="alpha_agents.data.portfolio"):
            alerts = P.check_pending_orders(
                {"600001": 5.0, "600002": 10.0}, today=REPO_DATE,
                trader_id="slow")

        # It must not be filled, cancelled, or repaired by guesswork.
        assert _status(broken)["status"] == "pending"
        assert R.reservation_for_order(_db(), broken)["state"] == R.CONSUMED
        assert any("reservation is consumed" in r.getMessage()
                   for r in caplog.records)

        # And the order behind it must still have been served. This is the
        # assertion that fails when the bad row raises out of the loop.
        assert [a["type"] for a in alerts] == ["filled"]
        assert alerts[0]["code"] == "600002"
        assert _status(healthy)["status"] == "open"

    def test_a_properly_reserved_order_is_not_touched_by_the_repair(
            self, store, traders_dir, theme):
        """The pair for the whole class: a healthy order is not re-reserved,
        not re-flagged, and not cancelled for having been looked at."""
        oid = _seed_pending(held=True)
        before = R.reservation_for_order(_db(), oid)
        alerts = P.check_pending_orders({"600000": 5.0}, today=REPO_DATE,
                                        trader_id="slow")
        after = R.reservation_for_order(_db(), oid)
        assert alerts == []
        assert _status(oid)["status"] == "pending"
        assert (after["id"], after["amount"], after["state"]) == \
            (before["id"], before["amount"], before["state"])


# ── C. The thesis is re-read every cycle ───────────────────────────────


class TestTheThesisIsReReadEveryCycle:
    def test_a_thesis_that_died_while_the_price_waited_cancels_the_order(
            self, store, traders_dir, theme):
        """#117 declared '跌破 94.00 则突破失败' and traded at 85.49 for four
        sessions with nobody looking, because 85.49 never entered 95.5–98.0
        and the check only ran on the way in."""
        oid = _seed_pending(entry_low=95.5, entry_high=98.0, held=True)
        tid = _thesis(conditions=[T.Condition("price_below", 94.0)])

        alerts = P.check_pending_orders({"600000": 85.49}, today=REPO_DATE,
                                        trader_id="slow")

        assert [a["type"] for a in alerts] == ["cancelled"]
        assert "论点" in alerts[0]["reason"]
        closed = _status(oid)
        assert closed["status"] == "cancelled"
        assert "论点在成交前已失效" in closed["close_reason"]
        # The thesis is not merely ignored — it is closed, and which
        # condition fired is on the record.
        got = T.get_by_id(tid)
        assert got.status == T.INVALIDATED
        assert got.close_kind == "price_below"

    def test_being_outside_the_zone_is_not_itself_a_reason_to_cancel(
            self, store, traders_dir, theme):
        """The pair, and the line the change must not cross.

        85.49 is just as far below the zone as in the test above; the only
        difference is that the thesis is still intact. Making the check
        per-cycle must not become a rule that a price below the zone is a
        cancel — that would be a new exit rule wearing a bug fix, and it
        would kill every order the market moved away from."""
        oid = _seed_pending(entry_low=95.5, entry_high=98.0, held=True)
        tid = _thesis(conditions=[T.Condition("price_below", 80.0)])

        alerts = P.check_pending_orders({"600000": 85.49}, today=REPO_DATE,
                                        trader_id="slow")

        assert alerts == []
        assert _status(oid)["status"] == "pending"
        assert T.get_by_id(tid).status == T.ACTIVE
        assert R.reservation_for_order(_db(), oid)["state"] == R.HELD

    def test_an_order_with_no_thesis_is_not_cancelled_by_the_new_check(
            self, store, traders_dir, theme):
        """An order placed before theses existed has no conditions to
        break; the check must read that as silence, not as a failure."""
        oid = _seed_pending(entry_low=95.5, entry_high=98.0, held=True)
        alerts = P.check_pending_orders({"600000": 85.49}, today=REPO_DATE,
                                        trader_id="slow")
        assert alerts == []
        assert _status(oid)["status"] == "pending"


# ── D. expire_days is read at last ─────────────────────────────────────


class TestTheDeclaredHorizonIsTheExpiry:
    def test_the_order_dies_on_its_horizon_and_not_before(
            self, store, traders_dir, theme):
        """``expire_days`` was written on every order and read by nothing;
        the theme branch skipped past the only check that mentioned it."""
        oid = _seed_pending(expire_days=3, held=True)

        # Day 2 of 3: still the setup's own time. Paired with the next line,
        # this is what stops "expires" from meaning "cancels on sight".
        assert P.check_pending_orders({"600000": 5.0}, today="2026-01-07",
                                      trader_id="slow") == []
        assert _status(oid)["status"] == "pending"

        alerts = P.check_pending_orders({"600000": 5.0}, today="2026-01-08",
                                        trader_id="slow")
        assert [a["type"] for a in alerts] == ["cancelled"]
        assert "未到价" in alerts[0]["reason"]

        closed = _status(oid)
        assert closed["status"] == "cancelled"
        assert "到期未到价" in closed["close_reason"]
        assert R.reservation_for_order(_db(), oid)["state"] == R.RELEASED

    def test_an_order_whose_price_arrived_before_its_horizon_is_filled(
            self, store, traders_dir, theme):
        """The pair: the expiry is a deadline, not a veto. On the same day
        the zone is reached, the fill wins — the check runs after the
        trigger test only for orders the price never reached."""
        oid = _seed_pending(expire_days=3, held=True)
        alerts = P.check_pending_orders({"600000": 10.0}, today="2026-01-08",
                                        trader_id="slow")
        assert [a["type"] for a in alerts] == ["filled"]
        assert _status(oid)["status"] == "open"

    def test_the_expiry_is_the_theses_own_horizon(self, store, traders_dir,
                                                  theme):
        """A three-day setup whose price has not arrived by day three has
        not been unlucky — it has not happened, and the agent said three
        days when it wrote the thing down."""
        tid = _thesis(horizon_days=3)
        oid = P.create_pending_order(
            code="600000", name="A", theme="t", order_date=REPO_DATE,
            entry_low=9.0, entry_high=11.0, stop_loss=8.5, source="morning",
            reason="主线在流入", trader_id="slow", thesis_id=tid)
        assert oid is not None
        got = _db().execute(
            "SELECT expire_days FROM virtual_portfolio WHERE id = ?",
            (oid,)).fetchone()["expire_days"]
        assert got == 3
        assert got not in (P.PENDING_EXPIRE_DAYS, 4), \
            "3 differs from both fallbacks, so 3 can only come from the thesis"

    def test_an_order_without_a_thesis_falls_back_to_the_traders_default(
            self, store, traders_dir, theme):
        """The pair. ``slow`` declares 4; an order carrying no thesis has
        nothing else to inherit, and 4 is what it gets — not the constant."""
        oid = P.create_pending_order(
            code="600000", name="A", theme="t", order_date=REPO_DATE,
            entry_low=9.0, entry_high=11.0, stop_loss=8.5, source="morning",
            reason="主线在流入", trader_id="slow")
        got = _db().execute(
            "SELECT expire_days FROM virtual_portfolio WHERE id = ?",
            (oid,)).fetchone()["expire_days"]
        assert got == 4
        assert got != P.PENDING_EXPIRE_DAYS


# ── E. Both directions of a missed entry are countable ─────────────────


class TestBothDirectionsOfAMissedEntryAreCounted:
    def test_a_zone_the_price_never_reached_is_countable(self, store):
        """The metric counted only 介入价太低 — the zone below the market and
        the price running away up — so every reading of it argued for
        higher zones, and the breakout book, whose characteristic failure
        is the opposite one, could not be described at all."""
        from alpha_agents.data.portfolio_risk import entry_quality

        _seed_cancelled("挂单到期未到价（挂2天，期限3天）")   # #118's shape
        _seed_cancelled("价格已涨走(12.00远超介入上限11.00)", code="600001")

        q = entry_quality(days=30, trader_id="slow")
        assert q["missed_never_came"] == 1
        assert q["missed_ran_away"] == 1
        assert q["missed_right"] == 2, "the headline stays the sum of both"
        assert q["missed_rate"] == 100.0
        assert q["by_reason"]["价格未到"] == 1, \
            "the label existed for three weeks with no producer"

    def test_the_reasons_the_code_writes_all_reach_a_label(self, store):
        """Ties D to E: the strings the cancel paths write are what make the
        labels reachable. If a reason is reworded and the table is not, the
        bucket goes quiet and nothing else in the system notices."""
        from alpha_agents.data.portfolio_risk import classify_cancellation

        assert classify_cancellation("挂单到期未到价（挂2天，期限3天）") == "价格未到"
        assert classify_cancellation("价格已涨走(12.00远超介入上限11.00)") == "介入价太低"
        assert classify_cancellation("资金不足(需1000元/手, 可用232元)") == "资金分配"
        assert classify_cancellation("论点在成交前已失效: 股价跌破 94.00") == \
            "论点先于价格失效"
        # And the label with no writer is gone rather than left as a decoy.
        assert classify_cancellation("当日未成交") == "其他"

    def test_the_advice_speaks_to_both_mistakes(self, store):
        """A lesson that only ever names the run-away direction coaches
        every book toward higher entry zones — a strategy change disguised
        as feedback."""
        from alpha_agents.data.portfolio_risk import inject_entry_quality

        _seed_cancelled("挂单到期未到价（挂2天，期限3天）")
        _seed_cancelled("价格已涨走(12.00远超介入上限11.00)", code="600001")
        text = inject_entry_quality(days=30, trader_id="slow")
        assert "价格涨走" in text and "价格从未到达" in text
        assert "上移" in text and "拉回" in text

    def test_the_never_arrived_half_does_not_prescribe_the_other_lesson(
            self, store):
        """The pair, and the bug it was written against: with only the
        never-arrived direction on the record, the prompt must not tell the
        book to move its zones up. Before the fix this case produced no
        advice at all — the count it needed did not exist."""
        from alpha_agents.data.portfolio_risk import inject_entry_quality

        _seed_cancelled("挂单到期未到价（挂2天，期限3天）")
        text = inject_entry_quality(days=30, trader_id="slow")
        assert "价格从未到达" in text
        assert "上移" not in text, \
            "one direction's advice must not be given for the other's failure"


class TestEveryCancelReasonReachesItsLabel:
    """The mapping, driven through the paths that write the reasons.

    Found while writing the class above: five of the eight reasons the
    cancel paths emit were landing in 其他 — every theme cancel and the
    thesis cancel among them — because the keys had been written against
    reasons somebody imagined rather than the ones the loops produce. A
    bucket that is always empty is not a quiet quarter; it is a blind spot
    with a count printed next to it, which is precisely what "价格未到"
    had been for three weeks.

    Each test drives a real cancel and asks what its stored reason
    classifies as, so the mapping cannot drift from the wording without
    this file going red.
    """

    @staticmethod
    def _cancel(oid, *, theme_row, extra=(), price=5.0):
        """Run one cycle and hand back the reason the row was left with."""
        from contextlib import ExitStack
        with ExitStack() as stack:
            stack.enter_context(patch(
                "alpha_agents.data.portfolio.get_theme_by_name",
                return_value=theme_row))
            stack.enter_context(patch(
                "alpha_agents.data.theme_gate.get_theme_by_name",
                return_value=theme_row))
            for target, kwargs in extra:
                stack.enter_context(patch(target, **kwargs))
            alerts = P.check_pending_orders({"600000": price}, today=REPO_DATE,
                                            trader_id="slow")
        assert [a["type"] for a in alerts] == ["cancelled"], alerts
        return _status(oid)["close_reason"]

    def test_a_retired_line(self, store, traders_dir, theme):
        from alpha_agents.data.portfolio_risk import classify_cancellation
        oid = _seed_pending(held=True)
        reason = self._cancel(oid, theme_row={"name": "t", "strength": 6,
                                              "status": "declining"})
        assert "declining" in reason
        assert classify_cancellation(reason) == "主线先于价格失效"

    def test_a_line_whose_score_collapsed(self, store, traders_dir, theme):
        """The cancel that killed ~100 of 117 orders. If this one does not
        reach its bucket, the entry-quality report cannot say why the book
        is empty."""
        from alpha_agents.data.portfolio_risk import classify_cancellation
        oid = _seed_pending(held=True)
        reason = self._cancel(
            oid, theme_row={"name": "t", "status": "active", "strength": 6,
                            "trend_score": 0.05},
            extra=[("alpha_agents.data.scoring.in_force_decision_params",
                    {"return_value": {"theme_gate": {"cancel_score": 0.35}}})])
        assert "明显走弱" in reason
        assert classify_cancellation(reason) == "主线先于价格失效"

    def test_a_line_that_is_not_tracked(self, store, traders_dir, theme):
        from alpha_agents.data.portfolio_risk import classify_cancellation
        oid = _seed_pending(held=True)
        reason = self._cancel(oid, theme_row=None)
        assert "不存在" in reason
        assert classify_cancellation(reason) == "主线不存在"

    def test_a_thesis_that_died_before_the_price_arrived(
            self, store, traders_dir, theme):
        from alpha_agents.data.portfolio_risk import classify_cancellation
        oid = _seed_pending(entry_low=95.5, entry_high=98.0, held=True)
        _thesis(conditions=[T.Condition("price_below", 94.0)])
        reason = self._cancel(oid, theme_row=theme, price=85.49)
        assert "成交前已失效" in reason
        assert classify_cancellation(reason) == "论点先于价格失效"

    def test_a_corrupt_row_names_no_bucket(self, store, traders_dir, theme):
        """The pair for 其他: it stays for what it is for — a row nobody can
        classify — and is not quietly filled with a plausible label."""
        from alpha_agents.data.portfolio_risk import classify_cancellation
        oid = _seed_pending(held=True, order_date="not-a-date")
        P.check_pending_orders({"600000": 5.0}, today=REPO_DATE,
                               trader_id="slow")
        reason = _status(oid)["close_reason"]
        assert reason == "日期解析失败"
        assert classify_cancellation(reason) == "其他"
