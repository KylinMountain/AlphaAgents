"""The intent layer: one door into the book, and the record that it was used.

The point of S5 is not a new feature — it is that every action which
changes the book now passes through ``submit_intent`` and leaves a row
saying what was asked, on what evidence, and whether it was accepted.
These tests pin three things:

1. The lifecycle: a row is written at 'submitted' and always ends at
   'accepted' or 'rejected'. A row stuck at 'submitted' means the process
   died mid-action, and there is a reader for exactly that.
2. The dispatch: all six actions route to their implementation and the
   result comes back through the wrapper unchanged, so the compatibility
   wrappers have not altered behaviour.
3. The guard rail: no module outside the two state-machine-governed
   writers may touch ``virtual_portfolio.status`` directly. That is the
   mechanical form of "业务路径不直接 UPDATE 持仓状态".
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest

from alpha_agents.data import intent as I
from alpha_agents.data import memory_store
from alpha_agents.data import portfolio as P
from alpha_agents.data import trader as TR

REPO = Path(__file__).resolve().parent.parent


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


def _rows():
    return [dict(r) for r in _db().execute(
        "SELECT * FROM intents ORDER BY id").fetchall()]


SLOW = "id: slow\nname: 回调派\ncapital: 500000\n"


def _write(d, name, body):
    (d / f"{name}.yaml").write_text(body, encoding="utf-8")


# ── Lifecycle ──────────────────────────────────────────────────────────


class TestIntentLifecycle:
    def test_a_malformed_intent_is_recorded_and_rejected(self, store):
        """A shape problem is a recorded refusal, not an exception —
        callers get one kind of answer."""
        r = I.submit_intent(I.TradeIntent(action="nonsense", trader_id="x"))
        assert r.accepted is False
        assert "unknown action" in r.reject_reason
        rows = _rows()
        assert len(rows) == 1
        assert rows[0]["status"] == I.REJECTED
        assert rows[0]["action"] == "nonsense"

    def test_a_position_action_without_a_position_is_refused(self, store):
        r = I.submit_intent(I.TradeIntent(action=I.ADD, price=10.0,
                                          trader_id="x"))
        assert r.accepted is False
        assert "position_id" in r.reject_reason

    def test_a_bad_price_is_refused(self, store):
        for bad in (0.0, -1.0, float("nan"), None, "10"):
            r = I.submit_intent(I.TradeIntent(
                action=I.CLOSE, position_id=1, price=bad, trader_id="x"))
            assert r.accepted is False, f"{bad!r} should be refused"
            assert "finite positive price" in r.reject_reason

    def test_a_bad_share_count_is_refused(self, store):
        for bad in (0, -100, 1.5, True):
            r = I.submit_intent(I.TradeIntent(
                action=I.OPEN, code="600000", shares=bad, trader_id="x"))
            assert r.accepted is False, f"{bad!r} should be refused"

    def test_no_row_is_ever_left_at_submitted(self, store):
        """The invariant the whole table exists for: a decision either
        completed or was refused. 'submitted' is only ever a transient."""
        I.submit_intent(I.TradeIntent(action="bogus", trader_id="x"))
        I.submit_intent(I.TradeIntent(action=I.CLOSE, position_id=1,
                                      price=float("nan"), trader_id="x"))
        statuses = {r["status"] for r in _rows()}
        assert statuses <= {I.ACCEPTED, I.REJECTED}
        assert I.never_decided(_db()) == []

    def test_the_evidence_is_frozen_on_the_row(self, store):
        I.submit_intent(I.TradeIntent(
            action=I.CLOSE, position_id=7, price=float("nan"),
            reason="止损触发", source="morning", thesis_id=3,
            information_cutoff="2026-01-05", policy_ref="rule:hard_stop",
            trader_id="slow"))
        row = _rows()[0]
        assert row["policy_ref"] == "rule:hard_stop"
        assert row["information_cutoff"] == "2026-01-05"
        assert row["position_id"] == 7
        ev = row["evidence_json"]
        assert "止损触发" in ev and "morning" in ev


class TestIntentTransitionGraph:
    def test_submitted_may_become_accepted_or_rejected(self):
        for target in (I.ACCEPTED, I.REJECTED):
            assert I.assert_intent_transition(I.SUBMITTED, target) == target

    def test_terminal_states_are_terminal(self):
        for current in (I.ACCEPTED, I.REJECTED):
            for target in (I.SUBMITTED, I.ACCEPTED, I.REJECTED):
                with pytest.raises(I.IllegalIntentTransition):
                    I.assert_intent_transition(current, target)

    def test_an_unknown_state_is_refused(self):
        with pytest.raises(I.IllegalIntentTransition):
            I.assert_intent_transition("halfway", I.ACCEPTED)


# ── Dispatch through the wrappers ──────────────────────────────────────


class TestEveryActionGoesThroughTheDoor:
    """Each public wrapper must leave an intent row. If one of them
    stops routing through ``submit_intent``, its test here fails."""

    def test_open_records_an_intent_and_returns_the_order_id(
            self, store, traders_dir, theme):
        _write(traders_dir, "slow", SLOW)
        oid = P.create_pending_order(
            code="600000", name="A", theme="t", order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, stop_loss=8.5,
            source="morning", reason="主线在流入", trader_id="slow")
        assert oid is not None
        rows = _rows()
        assert len(rows) == 1
        assert rows[0]["action"] == I.OPEN
        assert rows[0]["status"] == I.ACCEPTED
        assert rows[0]["result_json"] and str(oid) in rows[0]["result_json"]

    def test_a_refused_open_is_recorded_as_rejected(
            self, store, traders_dir, theme):
        """A duplicate order comes back as ``None`` exactly as before —
        and now there is a row saying the attempt happened."""
        _write(traders_dir, "slow", SLOW)
        P.create_pending_order(
            code="600000", name="A", theme="t", order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, stop_loss=8.5,
            source="morning", reason="主线在流入", trader_id="slow")
        second = P.create_pending_order(
            code="600000", name="A", theme="t", order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, stop_loss=8.5,
            source="morning", reason="重复", trader_id="slow")
        assert second is None
        rows = _rows()
        assert len(rows) == 2
        assert rows[1]["status"] == I.REJECTED
        assert rows[1]["reject_reason"]

    def test_open_now_records_an_intent(self, store, traders_dir, theme):
        _write(traders_dir, "slow", SLOW)
        pid = P.open_position(code="600000", name="A", theme="t",
                              open_date="2026-01-05", open_price=10.0,
                              source="manual", reason="x", shares=500,
                              trader_id="slow")
        assert pid is not None
        rows = _rows()
        assert rows[0]["action"] == I.OPEN_NOW
        assert rows[0]["status"] == I.ACCEPTED

    def test_add_records_an_intent(self, store, traders_dir, theme):
        _write(traders_dir, "slow", SLOW)
        oid = P.create_pending_order(
            code="600000", name="A", theme="t", order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, stop_loss=8.5,
            source="morning", reason="x", trader_id="slow")
        P._fill_order(P.get_pending_orders("slow")[0],
                      fill_price=10.0, fill_date="2026-01-05")
        added = P.add_to_position(oid, price=9.5, reason="补仓")
        assert added is not None
        actions = [r["action"] for r in _rows()]
        assert I.ADD in actions

    def test_close_records_a_close_intent(self, store, traders_dir, theme):
        _write(traders_dir, "slow", SLOW)
        oid = P.create_pending_order(
            code="600000", name="A", theme="t", order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, stop_loss=8.5,
            source="morning", reason="x", trader_id="slow")
        P._fill_order(P.get_pending_orders("slow")[0],
                      fill_price=10.0, fill_date="2026-01-05")
        from alpha_agents.data.portfolio_exit import close_position
        assert close_position(oid, close_price=11.0, close_reason="止盈触发")
        row = [r for r in _rows() if r["action"] == I.CLOSE][0]
        assert row["status"] == I.ACCEPTED

    def test_a_partial_close_records_a_trim_intent(self, store, traders_dir,
                                                   theme):
        _write(traders_dir, "slow", SLOW)
        oid = P.create_pending_order(
            code="600000", name="A", theme="t", order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, stop_loss=8.5,
            source="morning", reason="x", trader_id="slow")
        P._fill_order(P.get_pending_orders("slow")[0],
                      fill_price=10.0, fill_date="2026-01-05")
        from alpha_agents.data.portfolio_exit import close_position
        assert close_position(oid, close_price=11.0, close_reason="减仓",
                              shares=100)
        assert any(r["action"] == I.TRIM for r in _rows())

    def test_cancel_records_an_intent(self, store, traders_dir, theme):
        """Covers both the business cancel and the automated one —
        ``check_pending_orders`` cancels through the same wrapper."""
        _write(traders_dir, "slow", SLOW)
        oid = P.create_pending_order(
            code="600000", name="A", theme="t", order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, stop_loss=8.5,
            source="morning", reason="x", trader_id="slow")
        P._cancel_order(oid, "手动撤销")
        row = [r for r in _rows() if r["action"] == I.CANCEL][0]
        assert row["status"] == I.ACCEPTED

    def test_every_declared_action_reaches_an_implementation(
            self, store, traders_dir, theme):
        """Drive each declared action through the dispatcher.

        The first form of this test grepped the source for the action
        string — which the constant definition (``OPEN = "open"``)
        satisfies by itself, so it could never fail. This one submits a
        shape-valid intent per action and asserts that what stops it is
        only ever business logic, never a missing branch: ``_dispatch``
        raises ``ValueError('no dispatch for action …')`` for an unbound
        action, and ``submit_intent`` deliberately re-raises, so a
        declared-but-unimplemented action fails here loudly instead of
        being recorded as an ordinary refusal.
        """
        _write(traders_dir, "slow", SLOW)
        minimal = {
            I.OPEN: dict(code="600000", name="A", theme="t",
                         order_date="2026-01-05"),
            # A distinct code: an already-filled position for the same
            # code would be a business refusal, and this test is not
            # about business refusals.
            I.OPEN_NOW: dict(code="600001", name="B", theme="t",
                             order_date="2026-01-05", price=10.0),
            I.ADD: dict(position_id=999_999, price=10.0, reason="x"),
            I.TRIM: dict(position_id=999_999, price=10.0, shares=100,
                         reason="x"),
            I.CLOSE: dict(position_id=999_999, price=10.0, reason="x"),
            I.CANCEL: dict(position_id=999_999, reason="x"),
        }
        assert set(minimal) == set(I.ALL_ACTIONS), (
            "a declared action has no case here — add one, and a dispatch "
            "branch for it")

        for action, kwargs in sorted(minimal.items()):
            r = I.submit_intent(I.TradeIntent(
                action=action, trader_id="slow", **kwargs))
            assert "no dispatch for action" not in (r.reject_reason or ""), \
                f"{action} is declared but has no dispatch branch"

    def test_the_wrapper_return_values_are_unchanged(
            self, store, traders_dir, theme):
        """A close of a non-open position is still ``False`` and still
        books nothing — the wrapper did not turn a refusal into an
        exception or into a success."""
        _write(traders_dir, "slow", SLOW)
        from alpha_agents.data.portfolio_exit import close_position
        assert close_position(999_999, close_price=10.0,
                              close_reason="无中生有") is False
        assert _db().execute(
            "SELECT COUNT(*) FROM position_exits").fetchone()[0] == 0


# ── The guard rail ─────────────────────────────────────────────────────


class TestOnlyTheStateMachineWritesStatus:
    """The mechanical form of "业务路径不直接 UPDATE 持仓状态".

    Two modules may write ``virtual_portfolio.status``, and both must do
    it through ``order_state.assert_transition``: ``portfolio`` (fill and
    cancel) and ``portfolio_exit`` (close). Everything else — the intent
    layer, the monitors, the pipeline tasks, the agents — has to go
    through a wrapper. A new direct writer fails this test, which is the
    point.
    """

    #: Modules allowed to write the status column, and the reason.
    ALLOWED = {
        "portfolio.py": "the fill and cancel transitions",
        "portfolio_exit.py": "the close transition",
    }

    def _status_writers(self) -> dict:
        """Files whose SQL writes virtual_portfolio.status."""
        found: dict[str, int] = {}
        pattern = re.compile(
            r"UPDATE\s+virtual_portfolio\s+SET[^\"']*\bstatus\b",
            re.IGNORECASE)
        for path in (REPO / "alpha_agents").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            n = len(pattern.findall(text))
            if n:
                found[path.name] = n
        return found

    def test_only_the_two_governed_writers_touch_status(self):
        writers = self._status_writers()
        assert set(writers) == set(self.ALLOWED), (
            "virtual_portfolio.status is written by an unexpected module. "
            f"found: {sorted(writers)}; allowed: {sorted(self.ALLOWED)}. "
            "Route the new write through a public wrapper (which goes "
            "through submit_intent) or through order_state.assert_transition.")

    def test_each_writer_asserts_its_transition(self):
        for name in self.ALLOWED:
            text = (REPO / "alpha_agents" / "data" / name).read_text(
                encoding="utf-8")
            assert "order_state.assert_transition" in text, (
                f"{name} writes status without asserting the transition")

    def test_the_intent_module_writes_no_position_rows(self):
        """The door must not become a fifth implementation."""
        text = (REPO / "alpha_agents" / "data" / "intent.py").read_text(
            encoding="utf-8")
        assert "UPDATE virtual_portfolio" not in text
        assert "INSERT INTO virtual_portfolio" not in text

    def test_no_monitor_or_task_writes_position_rows_directly(self):
        """Pipeline tasks and monitors may only read the book; a write
        here would escape both the intent trail and the state machine."""
        offenders = []
        for sub in ("pipeline", "agents", "server", "tools"):
            for path in (REPO / "alpha_agents" / sub).rglob("*.py"):
                text = path.read_text(encoding="utf-8")
                if re.search(r"UPDATE\s+virtual_portfolio", text,
                             re.IGNORECASE):
                    offenders.append(str(path.relative_to(REPO)))
        assert offenders == [], (
            f"these modules write the book directly: {offenders}. "
            "They must go through a portfolio wrapper (submit_intent).")


# ── Read side ──────────────────────────────────────────────────────────


class TestReadSide:
    def test_history_filters_by_action_and_trader(self, store):
        I.submit_intent(I.TradeIntent(action="bogus", trader_id="a"))
        I.submit_intent(I.TradeIntent(action="bogus", trader_id="b"))
        assert len(I.history(_db())) == 2
        assert len(I.history(_db(), trader_id="a")) == 1
        assert len(I.history(_db(), action="bogus")) == 2
        assert I.history(_db(), action=I.OPEN) == []

    def test_never_decided_finds_a_row_left_mid_action(self, store):
        """Manufacture the crash it exists to find: a row at 'submitted'
        with no terminal status."""
        conn = _db()
        conn.execute(
            "INSERT INTO intents (action, status, trader_id) "
            "VALUES ('open', 'submitted', 'slow')")
        conn.commit()
        stuck = I.never_decided(conn)
        assert len(stuck) == 1
        assert stuck[0]["action"] == "open"


class TestTheAutomatedTopUpGoesThroughTheDoor:
    """``_check_add_position`` used to carry its own sizing and its own
    ``UPDATE virtual_portfolio``. It now delegates, which is worth two
    tests: the observable behaviour (a lot is created, the stop is
    recomputed) and the fact that it is the *rule* layer deciding while
    the write path does the writing.
    """

    def _open_and_drop(self, traders_dir):
        _write(traders_dir, "slow", SLOW)
        oid = P.create_pending_order(
            code="600000", name="A", theme="t", order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, stop_loss=8.5,
            source="morning", reason="主线在流入", trader_id="slow")
        P._fill_order(P.get_pending_orders("slow")[0],
                      fill_price=10.0, fill_date="2026-01-05")
        return oid

    def test_the_rule_add_creates_a_settlement_lot(
            self, store, traders_dir, theme):
        """The gap this closed: the old duplicate implementation updated
        ``virtual_portfolio`` without touching ``settlement_lots``, so a
        rule-added tranche was instantly sellable despite A-share T+1."""
        oid = self._open_and_drop(traders_dir)
        lots_before = _db().execute(
            "SELECT COUNT(*) FROM settlement_lots WHERE position_id = ?",
            (oid,)).fetchone()[0]

        alerts = P.check_positions(realtime_prices={"600000": 9.4},
                                   today="2026-01-06")
        assert any(a["type"] == "add_position" for a in alerts), \
            f"expected the top-up rule to fire, got {alerts}"

        lots = _db().execute(
            "SELECT source, shares FROM settlement_lots "
            "WHERE position_id = ? ORDER BY id", (oid,)).fetchall()
        assert len(lots) == lots_before + 1
        assert lots[-1]["source"] == "add"
        # And the rule-add left an intent, like every other action.
        assert any(r["action"] == I.ADD and r["status"] == I.ACCEPTED
                   for r in _rows())

    def test_the_rule_add_keeps_the_stop_distance(
            self, store, traders_dir, theme):
        """Averaging down must not widen the risk per share: the stop
        moves up with the new average, holding its original percentage
        below cost."""
        oid = self._open_and_drop(traders_dir)
        before = _db().execute(
            "SELECT open_price, stop_loss FROM virtual_portfolio WHERE id = ?",
            (oid,)).fetchone()
        old_pct = (before["open_price"] - before["stop_loss"]) / before["open_price"]

        P.check_positions(realtime_prices={"600000": 9.4},
                          today="2026-01-06")

        after = _db().execute(
            "SELECT open_price, stop_loss, shares FROM virtual_portfolio "
            "WHERE id = ?", (oid,)).fetchone()
        assert after["open_price"] < before["open_price"], \
            "the average should have come down"
        assert after["shares"] > 0
        new_pct = (after["open_price"] - after["stop_loss"]) / after["open_price"]
        assert new_pct == pytest.approx(old_pct, abs=1e-3), \
            "the stop must keep the same distance below the new average"

    def test_an_agent_add_does_not_move_the_stop(
            self, store, traders_dir, theme):
        """``recalc_stop`` is the rule's policy, not the agent's: an
        agent-initiated add must leave the stop where the agent put it."""
        oid = self._open_and_drop(traders_dir)
        before = _db().execute(
            "SELECT stop_loss FROM virtual_portfolio WHERE id = ?",
            (oid,)).fetchone()["stop_loss"]
        added = P.add_to_position(oid, price=9.4, reason="agent add")
        assert added is not None
        assert added.get("stop_loss") is None
        after = _db().execute(
            "SELECT stop_loss FROM virtual_portfolio WHERE id = ?",
            (oid,)).fetchone()["stop_loss"]
        assert after == before