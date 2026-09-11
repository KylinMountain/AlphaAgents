"""The learning unit: one decision, and what came of it.

T1 of Phase 3. The claim being pinned is narrow and checkable: **a
decision is a row**. Not "the positions that ran to profit" — the
decisions that were refused, the orders that were cancelled, the
malformed intents that never named a stock. Before this table the book
could only express the ones that traded (``thesis → order → exits``
passes through rows a decision *produced*), which is a selection bias in
the learning data with a database behind it: "we decided 40 times and
filled 6" was unanswerable, and a low fill rate looked exactly like a
quiet week.

The tests are in five groups:

1. Coverage of the non-trades — a refusal, a cancel, a malformed intent
   each leave a complete episode with a readable verdict. This is §9's
   "selection and coverage".
2. One episode per decision — a fill, an add, a trim and a close all
   belong to the episode the order opened. Not four episodes, not zero.
3. The declaration is honest — every kind the schema allows is produced
   by running the real write paths, and ``expire`` is absent because
   nothing in this repository expires a pending order.
4. The record cannot be rewritten — events are append-only, an episode's
   identity and its frozen boundary are fixed, a closed episode cannot be
   reopened.
5. The read side — ``coverage`` and the two accessors.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from alpha_agents.data import attribution, episodes, intent as I
from alpha_agents.data import memory_store, portfolio as P
from alpha_agents.data import trader as TR
from alpha_agents.evolution.replay_mode import replay_as_of

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


# ── Helpers ────────────────────────────────────────────────────────────


def _db():
    return memory_store._get_conn()


def _write(d, name, body):
    (d / f"{name}.yaml").write_text(body, encoding="utf-8")


def _place(d, *, code="600000", order_date="2026-01-05", trader_id="slow",
           name="A"):
    """Place a pending order through the public wrapper."""
    _write(d, trader_id if trader_id == "fast" else "slow",
           f"id: {trader_id}\nname: {trader_id}\ncapital: 500000\n")
    return P.create_pending_order(
        code=code, name=name, theme="t", order_date=order_date,
        entry_low=9.0, entry_high=11.0, stop_loss=8.5,
        source="morning", reason="主线在流入", trader_id=trader_id)


def _fill(d, *, code="600000", trader_id="slow", fill_date="2026-01-05",
          price=10.0):
    order = [o for o in P.get_pending_orders(trader_id) if o["code"] == code][0]
    return P._fill_order(order, fill_price=price, fill_date=fill_date)


def _episodes():
    return [dict(r) for r in _db().execute(
        "SELECT * FROM episodes ORDER BY id").fetchall()]


def _kinds(episode_id):
    return [r["kind"] for r in _db().execute(
        "SELECT kind FROM episode_events WHERE episode_id = ? ORDER BY id",
        (episode_id,)).fetchall()]


def _verdict(episode_id):
    """The intent status behind this episode's ``intent`` event."""
    row = _db().execute(
        "SELECT i.status FROM episode_events v "
        "JOIN intents i ON i.id = v.ref_id "
        "WHERE v.episode_id = ? AND v.kind = ? ORDER BY v.id DESC LIMIT 1",
        (episode_id, episodes.INTENT)).fetchone()
    return row["status"] if row else None


# ── 1. The decisions that produced nothing ─────────────────────────────


class TestANonTradeIsStillADecision:
    def test_a_refused_order_leaves_a_complete_episode(
            self, store, traders_dir, theme):
        """The duplicate refusal is the cheapest real one: the first order
        is placed, the second is turned down by the same book. The second
        decision still happened."""
        assert _place(traders_dir) is not None
        assert _place(traders_dir) is None

        eps = _episodes()
        assert len(eps) == 2, "a refusal must not be invisible"
        refused = eps[1]
        assert refused["code"] == "600000"
        assert refused["status"] == episodes.CLOSED
        assert refused["order_id"] is None, "no order was written"
        assert refused["position_id"] is None, "it certainly never traded"
        assert refused["closed_at"] is not None
        assert _kinds(refused["id"]) == [episodes.INTENT]
        assert _verdict(refused["id"]) == I.REJECTED

    def test_a_malformed_intent_still_leaves_an_episode(self, store):
        """A shape refusal names no stock, so the episode has to allow a
        NULL code. Dropping these rows instead would remove exactly the
        malformed decisions from the coverage count — the bias this table
        exists to remove."""
        r = I.submit_intent(I.TradeIntent(action="nonsense", trader_id="slow"))
        assert r.accepted is False
        eps = _episodes()
        assert len(eps) == 1
        assert eps[0]["code"] is None
        assert eps[0]["status"] == episodes.CLOSED
        assert _verdict(eps[0]["id"]) == I.REJECTED

    def test_a_cancelled_order_leaves_a_complete_episode(
            self, store, traders_dir, theme):
        """The order existed, so the episode names it; it never became a
        position, so ``position_id`` stays NULL. That pair of facts is
        what "we decided and it did not trade" looks like."""
        oid = _place(traders_dir)
        P.cancel_order(oid, "主线走弱")

        eps = _episodes()
        assert len(eps) == 1
        ep = eps[0]
        assert ep["order_id"] == oid
        assert ep["position_id"] is None
        assert ep["status"] == episodes.CLOSED
        # The decision, its order, the decision to cancel it, the cancel.
        assert _kinds(ep["id"]) == [
            episodes.INTENT, episodes.ORDER, episodes.INTENT, episodes.CANCEL]

    def test_a_cancel_that_bypasses_the_door_is_still_recorded(
            self, store, traders_dir, theme):
        """Two cancels never pass through the intent door: the drawdown
        gate and the unaffordable lot inside ``_fill_order``. They are the
        ones a learning loop most needs to see, so the cancel event is
        written where the cancel happens rather than at the door."""
        _place(traders_dir)
        order = P.get_pending_orders("slow")[0]
        with patch.object(P, "get_available_capital", return_value=0.0), \
             patch.object(P, "_calc_shares", return_value=0), \
             patch.object(P, "_wanted_pct", return_value=0.0001):
            alert = P._fill_order(order, fill_price=10.0,
                                  fill_date="2026-01-05")
        assert alert["type"] == "cancelled"

        eps = _episodes()
        assert _kinds(eps[0]["id"]) == [
            episodes.INTENT, episodes.ORDER, episodes.CANCEL]
        assert eps[0]["status"] == episodes.CLOSED
        assert "资金不足" in _db().execute(
            "SELECT detail_json FROM episode_events WHERE kind = ?",
            (episodes.CANCEL,)).fetchone()["detail_json"]


# ── 2. One episode per decision ────────────────────────────────────────


class TestOneEpisodePerDecision:
    def test_a_whole_trade_is_one_episode(self, store, traders_dir, theme):
        """Open, fill, add, trim, close — one unit of learning. Split into
        four, no single episode would describe the trade; merged into the
        position row instead, the decisions would be invisible."""
        with replay_as_of("2026-01-05"):
            oid = _place(traders_dir, order_date="2026-01-05")
            _fill(traders_dir, fill_date="2026-01-05")
        with replay_as_of("2026-01-06"):
            assert P.add_to_position(oid, price=9.5, reason="补仓")
        with replay_as_of("2026-01-07"):
            assert P.close_position(oid, close_price=10.5,
                                    close_reason="减仓", shares=100)
            assert P.close_position(oid, close_price=11.0,
                                    close_reason="止盈触发")

        eps = _episodes()
        assert len(eps) == 1
        ep = eps[0]
        assert ep["order_id"] == oid
        assert ep["position_id"] == oid
        assert ep["status"] == episodes.CLOSED
        assert _kinds(ep["id"]) == [
            episodes.INTENT, episodes.ORDER, episodes.FILL,
            episodes.INTENT, episodes.ADD,
            episodes.INTENT, episodes.TRIM,
            episodes.INTENT, episodes.CLOSE]

    def test_a_pending_order_is_not_yet_a_position(
            self, store, traders_dir, theme):
        """``position_id`` is read as "this decision traded", so only a
        fill may set it. An unfilled order that claimed one would report a
        fill rate the book has not earned."""
        _place(traders_dir)
        ep = _episodes()[0]
        assert ep["order_id"] is not None
        assert ep["position_id"] is None

    def test_two_traders_on_one_stock_are_two_episodes(
            self, store, traders_dir, theme):
        """The comparison between books is the point of having two: one
        episode covering both would make each book's result the other's."""
        a = _place(traders_dir, trader_id="slow")
        b = _place(traders_dir, trader_id="fast")
        assert a is not None and b is not None and a != b

        eps = _episodes()
        assert len(eps) == 2
        assert {e["trader_id"] for e in eps} == {"slow", "fast"}
        assert {e["order_id"] for e in eps} == {a, b}

    def test_an_action_on_an_unknown_position_still_leaves_one_episode(
            self, store, traders_dir, theme):
        """The refusal is on the book, not on the record: the decision to
        sell position 999999 happened, and the episode says so with no
        instrument to name."""
        _write(traders_dir, "slow", "id: slow\nname: 回调派\ncapital: 500000\n")
        assert P.close_position(999_999, close_price=10.0,
                                close_reason="无中生有") is False
        eps = _episodes()
        assert len(eps) == 1
        assert eps[0]["code"] is None
        assert eps[0]["status"] == episodes.CLOSED
        assert _verdict(eps[0]["id"]) == I.REJECTED


# ── 3. The declaration is honest ───────────────────────────────────────


class TestEveryDeclaredKindHasAWriter:
    def test_the_schema_declares_exactly_the_kinds_the_code_writes(
            self, store, traders_dir, theme):
        """Run the real write paths and collect the kinds they produce.

        This is the mechanical form of "do not declare a state with no
        writer". It fails in both directions: a kind added to the CHECK
        constraint without a path that produces it, or a path deleted
        while the kind stays declared.
        """
        with replay_as_of("2026-01-05"):
            oid = _place(traders_dir, order_date="2026-01-05")
            _fill(traders_dir, fill_date="2026-01-05")
        with replay_as_of("2026-01-06"):
            P.add_to_position(oid, price=9.5, reason="补仓")
        with replay_as_of("2026-01-07"):
            P.close_position(oid, close_price=10.5, close_reason="减仓",
                             shares=100)
        other = None
        with replay_as_of("2026-01-08"):
            other = _place(traders_dir, code="600001", order_date="2026-01-08")
        P.cancel_order(other, "主线走弱")
        with replay_as_of("2026-01-09"):
            P.close_position(oid, close_price=11.0, close_reason="止盈触发")

        produced = set()
        for ep in _episodes():
            produced.update(_kinds(ep["id"]))
        assert produced == set(episodes.KINDS), (
            f"declared but never produced: "
            f"{sorted(set(episodes.KINDS) - produced)}; "
            f"produced but never declared: "
            f"{sorted(produced - set(episodes.KINDS))}")

    def test_expire_is_absent_because_nothing_expires(self):
        """§9 names holds and abstentions and this project's own plan named
        ``expire``; none of them has a write path, so none is declared.
        Re-adding the kind without a path would be a promise with no
        caller, which this repository has already shipped twice."""
        assert "expire" not in episodes.KINDS
        assert "hold" not in episodes.KINDS

    def test_the_pending_order_expiry_is_still_dead_code(self):
        """Why ``expire`` is absent, pinned so it cannot quietly become
        true: ``PENDING_EXPIRE_DAYS`` is written onto every order and
        ``days_pending`` is computed on every check, and neither has a
        reader anywhere in the package. If someone makes the expiry real,
        they will have to replace this test with one that produces the
        event — the right amount of friction.

        Comments and docstrings are ignored: they are allowed to *mention*
        the dead identifier, and this module's own schema comment does.
        """
        hits = []
        for path in (REPO / "alpha_agents").rglob("*.py"):
            for n, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith(("#", "--", '"', "'")):
                    continue
                if "days_pending" in line:
                    hits.append(f"{path.name}:{n}: {stripped}")
        assert len(hits) == 1 and "days_pending =" in hits[0], (
            f"days_pending is no longer only an unread assignment: {hits}. "
            "If the pending-order expiry became load-bearing, add "
            "episodes.EXPIRE, teach note_cancel's sibling to write it, and "
            "replace this test with one that produces the event.")

    def test_the_spelled_out_statuses_match_the_intent_module(self):
        """``episodes`` cannot import ``intent`` (intent imports it), so
        the two terminal statuses are spelled out there. This is the guard
        that keeps the duplication from drifting."""
        assert episodes._REFUSED == I.REJECTED
        assert episodes._TERMINAL_INTENT == I.INTENT_TERMINAL


# ── 4. The record cannot be rewritten ──────────────────────────────────


class TestTheRecordCannotBeRewritten:
    def test_an_event_cannot_be_edited_or_deleted(
            self, store, traders_dir, theme):
        _place(traders_dir)
        conn = _db()
        event_id = conn.execute(
            "SELECT id FROM episode_events").fetchone()["id"]
        with pytest.raises(Exception) as up:
            conn.execute("UPDATE episode_events SET kind = 'fill' WHERE id = ?",
                         (event_id,))
        assert "append-only" in str(up.value)
        with pytest.raises(Exception) as dp:
            conn.execute("DELETE FROM episode_events WHERE id = ?", (event_id,))
        assert "append-only" in str(dp.value)

    def test_an_episode_cannot_be_renamed(self, store, traders_dir, theme):
        """Reassigning an episode after the result is known is the shape of
        every hindsight bug."""
        _place(traders_dir)
        conn = _db()
        eid = _episodes()[0]["id"]
        for column, value in (("code", "600999"), ("trader_id", "other"),
                              ("opened_at", "2020-01-01")):
            with pytest.raises(Exception) as e:
                conn.execute(
                    f"UPDATE episodes SET {column} = ? WHERE id = ?",
                    (value, eid))
            assert "fixed" in str(e.value)

    def test_a_closed_episode_cannot_be_reopened(
            self, store, traders_dir, theme):
        oid = _place(traders_dir)
        P.cancel_order(oid, "主线走弱")
        conn = _db()
        eid = _episodes()[0]["id"]
        with pytest.raises(Exception) as e:
            conn.execute("UPDATE episodes SET status = 'open' WHERE id = ?",
                         (eid,))
        assert "fixed" in str(e.value)
        # And the closing date cannot be moved either.
        with pytest.raises(Exception) as e:
            conn.execute("UPDATE episodes SET closed_at = ? WHERE id = ?",
                         ("2030-01-01", eid))
        assert "fixed" in str(e.value)

    def test_the_frozen_boundary_cannot_be_repointed(
            self, store, traders_dir, theme):
        """A boundary re-pointed after the result is known is not a record
        of what the decision was allowed to know."""
        _place(traders_dir)
        conn = _db()
        ep = _episodes()[0]
        original = ep["decision_snapshot_id"]
        assert original is not None
        with pytest.raises(Exception) as e:
            conn.execute(
                "UPDATE episodes SET decision_snapshot_id = 999 WHERE id = ?",
                (ep["id"],))
        assert "fixed" in str(e.value)
        # And the writer helper is write-once: a later call is ignored
        # rather than moving the boundary.
        episodes.link_snapshot(conn, ep["id"], 999)
        assert _episodes()[0]["decision_snapshot_id"] == original

    def test_a_closed_episode_keeps_its_original_closing_date(
            self, store, traders_dir, theme):
        oid = _place(traders_dir)
        P.cancel_order(oid, "主线走弱")
        conn = _db()
        ep = _episodes()[0]
        episodes.close_episode(conn, ep["id"], at="2030-01-01")
        assert _episodes()[0]["closed_at"] == ep["closed_at"]


# ── 5. The frozen boundary, and the read side ──────────────────────────


class TestTheEpisodeHangsOnItsBoundary:
    def test_an_accepted_order_links_its_decision_snapshot(
            self, store, traders_dir, theme):
        """§9's "the decision boundary is frozen at the moment of
        deciding" is only joinable if the learning unit names it."""
        oid = _place(traders_dir, order_date="2026-01-05")
        ep = _episodes()[0]
        assert ep["information_cutoff"] == "2026-01-05"
        assert ep["decision_snapshot_id"] is not None

        conn = _db()
        snap = attribution.snapshot_by_id(conn, ep["decision_snapshot_id"])
        assert snap["order_id"] == oid
        assert snap["information_cutoff"] == "2026-01-05"
        assert attribution.verify_snapshot(conn, ep["decision_snapshot_id"])

    def test_an_open_now_links_its_boundary_too(
            self, store, traders_dir, theme):
        _write(traders_dir, "slow", "id: slow\nname: 回调派\ncapital: 500000\n")
        pid = P.open_position(code="600000", name="A", theme="t",
                              open_date="2026-01-05", open_price=10.0,
                              source="manual", reason="手填",
                              trader_id="slow")
        ep = _episodes()[0]
        assert ep["position_id"] == pid
        assert ep["order_id"] is None
        assert ep["decision_snapshot_id"] is not None

    def test_a_refused_order_has_no_boundary_to_name(
            self, store, traders_dir, theme):
        """NULL means "unknown basis", not "no basis": a decision turned
        down before an order was written never froze a boundary, and
        claiming one would be inventing the evidence."""
        _place(traders_dir)
        _place(traders_dir)
        assert _episodes()[1]["decision_snapshot_id"] is None


class TestCoverage:
    def test_it_counts_the_decisions_that_produced_nothing(
            self, store, traders_dir, theme):
        """The number the book could not produce before T1."""
        with replay_as_of("2026-01-05"):
            _place(traders_dir, order_date="2026-01-05")
            _place(traders_dir, order_date="2026-01-05")   # duplicate refusal
            _fill(traders_dir, fill_date="2026-01-05")
        other = _place(traders_dir, code="600001", order_date="2026-01-05")
        P.cancel_order(other, "主线走弱")

        cov = episodes.coverage(_db())
        assert cov["decisions"] == 3
        assert cov["verdicts"] == 3
        assert cov["refused"] == 1
        assert cov["traded"] == 1
        assert cov["cancelled"] == 1
        assert cov["no_verdict"] == 0
        assert cov["fill_rate"] == pytest.approx(0.3333, abs=1e-4)

    def test_it_scopes_to_one_trader(self, store, traders_dir, theme):
        with replay_as_of("2026-01-05"):
            _place(traders_dir, trader_id="slow", order_date="2026-01-05")
        _place(traders_dir, trader_id="fast", order_date="2026-01-05")
        assert episodes.coverage(_db())["decisions"] == 2
        assert episodes.coverage(_db(), trader_id="slow")["decisions"] == 1

    def test_a_decision_the_process_died_on_shows_up_as_no_verdict(
            self, store, traders_dir, theme):
        """Reproduce the crash the record exists to find: the episode was
        opened and the intent row written, and the run stopped before the
        rules answered. The intent row is the signal — the episode names
        it, and ``coverage`` counts it as unresolved rather than assuming
        either answer."""
        conn = _db()
        conn.execute("INSERT INTO intents (action, status, trader_id) "
                     "VALUES ('open', 'submitted', 'slow')")
        iid = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        eid = episodes.open_episode(conn, trader_id="slow", code="600000",
                                    at="2026-01-05")
        episodes.add_event(conn, eid, episodes.INTENT, at="2026-01-05",
                           ref_id=iid)
        conn.commit()

        cov = episodes.coverage(conn)
        assert cov["decisions"] == 1
        assert cov["verdicts"] == 0, "nobody answered this one"
        assert cov["no_verdict"] == 1
        assert cov["traded"] == 0

    def test_an_empty_book_reports_a_null_fill_rate(self, store):
        """No decisions is not a 0% fill rate; it is no evidence at all."""
        cov = episodes.coverage(_db())
        assert cov["decisions"] == 0
        assert cov["fill_rate"] is None


class TestTheAccessors:
    def test_events_come_back_in_the_order_they_happened(
            self, store, traders_dir, theme):
        oid = _place(traders_dir)
        _fill(traders_dir)
        ep = _episodes()[0]
        events = episodes.events_for(_db(), ep["id"])
        assert [e["kind"] for e in events] == [
            episodes.INTENT, episodes.ORDER, episodes.FILL]
        assert events[0]["at"] <= events[1]["at"] <= events[2]["at"]
        # ``ref_id`` names the row that carries the detail; no amount is
        # copied into the event, because a second copy of a P&L is a
        # second source of truth.
        assert events[1]["ref_id"] == oid
        assert events[1]["detail_json"] == "{}"

    def test_open_episodes_excludes_the_finished_ones(
            self, store, traders_dir, theme):
        a = _place(traders_dir, code="600000")
        b = _place(traders_dir, code="600001")
        P.cancel_order(b, "主线走弱")
        live = episodes.open_episodes(_db())
        assert [e["order_id"] for e in live] == [a]

    def test_get_episode_returns_none_for_an_unknown_id(self, store):
        assert episodes.get_episode(_db(), 999_999) is None

    def test_an_unknown_event_kind_is_refused_at_the_call_site(self, store):
        """A caller inventing a kind should be told which kinds exist, at
        the line it wrote — not get an IntegrityError naming a table."""
        conn = _db()
        eid = episodes.open_episode(conn, trader_id="slow", code="600000",
                                    at="2026-01-05")
        with pytest.raises(ValueError) as e:
            episodes.add_event(conn, eid, "expire", at="2026-01-05")
        assert "Unknown episode event kind" in str(e.value)
        assert "cancel" in str(e.value)
