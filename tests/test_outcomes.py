"""Outcome labels: a lifecycle, three kinds, and the rules §9 forbids.

T2 of Phase 3. ``predictions.hit`` is a writable cell on the source row,
and §9 asks for two things a mutable cell cannot express — a label no
later event can overwrite, and a way to tell afterwards which version of
the evaluator produced it. So labels are append-only rows with a state,
an ``evaluator_version`` and an ``available_at``; a correction appends a
revision naming the row it corrects.

The tests are in five groups:

1. The state machine. ``pending → matured | censored`` legal,
   ``matured → revised`` legal and additive, ``matured → pending``
   refused and inert, terminal states final.
2. Immutability and chain shape — by trigger, by unique index, and the
   structural check the triggers cannot express.
3. The three prohibitions of §9, each as a rule rather than a sentence: a
   close must not rewrite a forecast, a correct forecast is not realised
   profit, and a process violation is not waived because it made money.
4. Declared horizons — a 3-day forecast matures in 3 days, and a row that
   never declared one is labelled as un-declared instead of assumed.
5. The sweeps, and the episode link that makes a label attributable.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from alpha_agents.data import clock, episodes, intent as I, memory_store
from alpha_agents.data import outcomes as O
from alpha_agents.data import portfolio as P
from alpha_agents.data import thesis as T
from alpha_agents.data import trader as TR
from alpha_agents.evolution import outcome_labels as OL


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
    (d / "slow.yaml").write_text(
        "id: slow\nname: 回调派\ncapital: 500000\n", encoding="utf-8")
    return d


@pytest.fixture()
def theme():
    """The book refuses an order naming an untracked theme line.

    A leftover rule from a run where every idea had to trace to a theme;
    these tests are about labels, not about that gate, so the one theme
    the orders here name is stubbed in.
    """
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


def _prediction(*, date="2026-01-05", code="600000", prob=0.6,
                horizon_days=3, trader_id="slow") -> int:
    return memory_store.save_prediction(
        date, "morning", code, "A", "看多", "medium", "t", 10.0,
        "资金持续流入，主力净买入 1.2 亿", {}, prob, trader_id,
        horizon_days=horizon_days)


def _due(as_of: str) -> list[dict]:
    return memory_store.get_predictions_due_for_scoring(as_of, 5)


def _score(outcome=True, *, prob=0.6, horizon=3) -> dict:
    """A stand-in for ``scoring.score_prediction``'s result.

    Hand-made on purpose: the labeller's contract is "here is the score",
    and building market history to produce one would test the price
    pipeline instead of the label.
    """
    return {
        "code": "600000", "horizon_days": horizon, "prob": prob,
        "outcome": outcome,
        "brier": round((prob - int(outcome)) ** 2, 4),
        "log_score": -0.51, "excess_return": 2.4 if outcome else -2.4,
        "residual_alpha": 1.1 if outcome else -1.1,
        "scored_at": "2026-01-08 16:00:00",
    }


def _thesis(**kw):
    base = dict(code="600000", name="A", theme="t",
                claim="小金属资金持续流入，主力净买入 1.2 亿",
                horizon_days=5, prob=0.62, conviction=0.7, size_pct=0.03,
                conditions=[T.Condition("theme_daily_score_below", 0),
                            T.Condition("drawdown_from_peak", 5)])
    base.update(kw)
    return T.Thesis(**base)


def _open_position(*, trader_id="slow", order_date="2026-01-05") -> int:
    pid = P.open_position(code="600000", name="A", theme="t",
                          open_date=order_date, open_price=10.0,
                          stop_loss=9.0, target_price=12.0, source="manual",
                          reason="资金持续流入，主力净买入 1.2 亿",
                          trader_id=trader_id)
    assert pid is not None
    return pid


# ── 1. The state machine ───────────────────────────────────────────────


class TestTheStateMachine:
    def test_pending_matures(self, store):
        pid = _prediction()
        first = O.declare(_db(), kind=O.FORECAST, subject_type="prediction",
                          subject_id=pid)
        assert O.get(_db(), first)["state"] == O.PENDING
        second = O.resolve(_db(), first, state=O.MATURED)
        assert O.get(_db(), second)["state"] == O.MATURED
        assert O.get(_db(), second)["supersedes_id"] == first

    def test_pending_can_be_censored_instead(self, store):
        pid = _prediction()
        first = O.declare(_db(), kind=O.FORECAST, subject_type="prediction",
                          subject_id=pid)
        second = O.resolve(_db(), first, state=O.CENSORED)
        assert O.get(_db(), second)["state"] == O.CENSORED

    def test_matured_can_be_revised_and_the_old_row_is_kept(self, store):
        """§9: corrections append. The superseded row stays readable, or
        there is no way to see that a label changed at all."""
        pid = _prediction()
        first = O.declare(_db(), kind=O.FORECAST, subject_type="prediction",
                          subject_id=pid)
        matured = O.resolve(_db(), first, state=O.MATURED,
                            evidence={"brier": 0.2})
        revised = O.resolve(_db(), matured, state=O.REVISED,
                            evidence={"brier": 0.31})
        chain = O.history(_db(), O.FORECAST, "prediction", pid)
        assert [r["state"] for r in chain] == [O.PENDING, O.MATURED, O.REVISED]
        assert chain[1]["evidence"] == {"brier": 0.2}, "not overwritten"

    def test_matured_cannot_go_back_to_pending(self, store):
        pid = _prediction()
        first = O.declare(_db(), kind=O.FORECAST, subject_type="prediction",
                          subject_id=pid)
        matured = O.resolve(_db(), first, state=O.MATURED)
        before = O.history(_db(), O.FORECAST, "prediction", pid)
        with pytest.raises(O.IllegalOutcomeTransition) as e:
            O.resolve(_db(), matured, state=O.PENDING)
        assert "never by going back" in str(e.value) or \
            "Illegal outcome transition" in str(e.value)
        assert O.history(_db(), O.FORECAST, "prediction", pid) == before, \
            "a refused transition must not have written anything"

    def test_matured_cannot_become_censored(self, store):
        """A resolved label is not un-resolved by losing its evidence.
        Correcting it is a revision, which records both states."""
        pid = _prediction()
        first = O.declare(_db(), kind=O.FORECAST, subject_type="prediction",
                          subject_id=pid)
        matured = O.resolve(_db(), first, state=O.MATURED)
        with pytest.raises(O.IllegalOutcomeTransition) as e:
            O.resolve(_db(), matured, state=O.CENSORED)
        assert "matured" in str(e.value) and "censored" in str(e.value)

    def test_a_censored_label_is_corrected_by_revision(self, store):
        pid = _prediction()
        first = O.declare(_db(), kind=O.FORECAST, subject_type="prediction",
                          subject_id=pid)
        censored = O.resolve(_db(), first, state=O.CENSORED)
        revised = O.resolve(_db(), censored, state=O.REVISED)
        assert O.get(_db(), revised)["state"] == O.REVISED
        assert O.current(_db(), O.FORECAST, "prediction", pid)["id"] == revised

    def test_a_revised_label_can_be_revised_again_but_not_matured(self, store):
        pid = _prediction()
        first = O.declare(_db(), kind=O.FORECAST, subject_type="prediction",
                          subject_id=pid)
        a = O.resolve(_db(), first, state=O.MATURED)
        b = O.resolve(_db(), a, state=O.REVISED)
        c = O.resolve(_db(), b, state=O.REVISED)
        assert O.get(_db(), c)["state"] == O.REVISED
        with pytest.raises(O.IllegalOutcomeTransition):
            O.resolve(_db(), c, state=O.MATURED)

    def test_an_unknown_state_is_refused_with_its_name(self):
        with pytest.raises(O.IllegalOutcomeTransition) as e:
            O.assert_outcome_transition("guessed", O.MATURED)
        assert "guessed" in str(e.value)

    def test_an_unknown_kind_or_subject_is_refused_before_sql(self, store):
        with pytest.raises(ValueError, match="Unknown outcome kind"):
            O.declare(_db(), kind="vibes", subject_type="prediction",
                      subject_id=1)
        with pytest.raises(ValueError, match="subject_id"):
            O.declare(_db(), kind=O.FORECAST, subject_type="prediction",
                      subject_id=0)


# ── 2. Immutability and chain shape ────────────────────────────────────


class TestTheChainCannotBeRewrittenOrForked:
    def test_a_label_cannot_be_updated_or_deleted(self, store):
        pid = _prediction()
        oid = O.declare(_db(), kind=O.FORECAST, subject_type="prediction",
                        subject_id=pid)
        conn = _db()
        with pytest.raises(Exception) as up:
            conn.execute("UPDATE outcomes SET state = 'matured' WHERE id = ?",
                         (oid,))
        assert "append-only" in str(up.value)
        with pytest.raises(Exception) as dp:
            conn.execute("DELETE FROM outcomes WHERE id = ?", (oid,))
        assert "append-only" in str(dp.value)

    def test_declare_is_idempotent_so_a_chain_cannot_fork(self, store):
        """The one question this table answers is "what is the current
        label", and a second initial row would make it two answers."""
        pid = _prediction()
        conn = _db()
        a = O.declare(conn, kind=O.FORECAST, subject_type="prediction",
                      subject_id=pid)
        b = O.declare(conn, kind=O.FORECAST, subject_type="prediction",
                      subject_id=pid)
        assert a == b
        assert len(O.history(conn, O.FORECAST, "prediction", pid)) == 1
        # And the index refuses one even if the idempotence check is gone.
        with pytest.raises(Exception):
            conn.execute(
                "INSERT INTO outcomes (kind, state, subject_type, subject_id) "
                "VALUES (?, ?, ?, ?)", (O.FORECAST, O.PENDING, "prediction",
                                        pid))

    def test_a_row_can_only_be_superseded_once(self, store):
        """A linear chain: two successors for one row is a fork, and a
        fork makes "the current label" ambiguous."""
        pid = _prediction()
        conn = _db()
        first = O.declare(conn, kind=O.FORECAST, subject_type="prediction",
                          subject_id=pid)
        O.resolve(conn, first, state=O.MATURED, evidence={"brier": 0.2})
        with pytest.raises(Exception):
            conn.execute(
                "INSERT INTO outcomes (kind, state, subject_type, subject_id, "
                "supersedes_id) VALUES (?, ?, ?, ?, ?)",
                (O.FORECAST, O.MATURED, "prediction", pid, first))

    def test_the_live_label_is_the_newest_of_the_chain(self, store):
        pid = _prediction()
        conn = _db()
        first = O.declare(conn, kind=O.FORECAST, subject_type="prediction",
                          subject_id=pid)
        matured = O.resolve(conn, first, state=O.MATURED)
        assert O.current(conn, O.FORECAST, "prediction",
                         pid)["id"] == matured
        assert O.current(conn, O.FORECAST, "prediction", 999_999) is None

    def test_integrity_reports_a_cross_subject_chain(self, store):
        """The shape the triggers cannot express: a successor has to be
        about the same subject as the row it succeeds. Inserted directly
        because ``resolve`` cannot produce it — which is the point of
        having a checker at all."""
        pid = _prediction()
        _prediction(code="600001")
        conn = _db()
        first = O.declare(conn, kind=O.FORECAST, subject_type="prediction",
                          subject_id=pid)
        # A successor about a *different* prediction. Built with a raw
        # INSERT rather than ``resolve`` because the transition guard is
        # the very thing that would refuse it, and because the store is
        # append-only — the corruption has to be fabricated as a new row,
        # which is also how it would arrive in the wild (a bad migration,
        # a future writer that skipped the guard).
        conn.execute(
            "INSERT INTO outcomes (kind, state, subject_type, subject_id, "
            "supersedes_id) VALUES (?, ?, ?, ?, ?)",
            (O.FORECAST, O.MATURED, "prediction", pid + 1, first))
        conn.commit()
        problems = O.integrity(conn)
        assert any("different subject" in p for p in problems), problems

    def test_integrity_is_quiet_on_a_healthy_store(self, store):
        pid = _prediction()
        conn = _db()
        first = O.declare(conn, kind=O.FORECAST, subject_type="prediction",
                          subject_id=pid)
        O.resolve(conn, first, state=O.MATURED)
        assert O.integrity(conn) == []


# ── 3. The three prohibitions of §9 ───────────────────────────────────


class TestTheThreeSubstitutionsAreProhibited:
    def test_a_close_does_not_touch_the_forecast_label(
            self, store, traders_dir):
        """§9: a stop-out after a correct call is a failed trade, not a
        failed forecast. Before T2 the close path wrote the prediction's
        ``hit``, which made the forecast score unrecoverable."""
        pid = _prediction()
        conn = _db()
        # A completed forecast label, with the call being right.
        first = O.declare(conn, kind=O.FORECAST, subject_type="prediction",
                          subject_id=pid)
        O.resolve(conn, first, state=O.MATURED, evidence=_score(outcome=True))
        conn.commit()
        before = O.history(conn, O.FORECAST, "prediction", pid)

        # Now trade it, and lose money on it.
        position_id = _open_position()
        conn.execute(
            "UPDATE virtual_portfolio SET prediction_id = ? WHERE id = ?",
            (pid, position_id))
        conn.commit()
        assert P.close_position(position_id, close_price=9.2,
                                close_reason="止损触发") is True

        after = O.history(conn, O.FORECAST, "prediction", pid)
        assert after == before, (
            "closing a position rewrote a forecast label — §9 keeps the "
            "forecast result and the trade result apart precisely so a "
            "bad exit cannot erase a good call")

    def test_a_correct_forecast_is_not_realised_profit(
            self, store, traders_dir):
        """Both facts must be visible at once: the call was right AND the
        trade lost. A single "performance" figure cannot say that, and the
        one it would say is whichever was written last."""
        pid = _prediction()
        conn = _db()
        first = O.declare(conn, kind=O.FORECAST, subject_type="prediction",
                          subject_id=pid)
        O.resolve(conn, first, state=O.MATURED, evidence=_score(outcome=True))
        position_id = _open_position()
        conn.execute(
            "UPDATE virtual_portfolio SET prediction_id = ? WHERE id = ?",
            (pid, position_id))
        conn.commit()
        P.close_position(position_id, close_price=9.2, close_reason="止损触发")
        OL.sweep_trade_labels(conn, as_of="2026-01-08")

        forecast = O.current(conn, O.FORECAST, "prediction", pid)
        trade = O.current(conn, O.TRADE, "position", position_id)
        assert forecast["state"] == O.MATURED
        assert forecast["evidence"]["outcome"] is True
        assert trade["state"] == O.MATURED
        # The trade label carries refs to the ledger, never a number it
        # computed for itself.
        assert trade["evidence"]["legs"] >= 1
        assert trade["evidence"]["exit_ids"]
        for key in ("return_pct", "net_amount", "return_amount", "brier",
                    "outcome"):
            assert key not in trade["evidence"], (
                f"{key!r} in a trade label is a second home for a number "
                "that already has one")

    def test_a_process_label_may_not_carry_a_result(self, store):
        """§9's third prohibition, as a guard: the way "it made money"
        enters a process grade is by the grader reading the P&L, so the
        field may not be there at all."""
        for field in ("return_pct", "net_amount", "pnl", "profit", "hit",
                      "win"):
            with pytest.raises(ValueError) as e:
                O.assert_no_pnl_in_process({"multi_family": True, field: 1.0})
            assert "may not carry realised results" in str(e.value)
        # A clean grade passes.
        O.assert_no_pnl_in_process({"multi_family": True, "score": 3})

    def test_a_violation_is_not_waived_because_the_trade_made_money(
            self, store, traders_dir):
        """Two theses, both badly formed, one whose trade profited. Their
        process labels must be identical apart from the identity of the
        thesis — the profitable one gets no credit it did not earn."""
        conn = _db()
        loser = _thesis(code="600000")
        winner = _thesis(code="600001")
        loser_id, winner_id = T.create(loser), T.create(winner)
        OL.sweep_process_labels(conn, as_of="2026-01-05")
        first = O.current(conn, O.PROCESS, "thesis", loser_id)["evidence"]
        second = O.current(conn, O.PROCESS, "thesis", winner_id)["evidence"]
        # ``thesis_id`` and ``code`` name the subject; everything else is
        # the grade, and the grade must not differ between the two.
        identity = {"thesis_id", "code"}
        assert {k: v for k, v in first.items() if k not in identity} == \
            {k: v for k, v in second.items() if k not in identity}
        assert not (set(first) & set(O._PNL_FIELDS)), (
            "a process grade must not contain a realised result at all")
        # And the loser/winner distinction is knowable only from the book,
        # which the process label never reads.
        positions = conn.execute(
            "SELECT COUNT(*) n FROM virtual_portfolio").fetchone()["n"]
        assert positions == 0


# ── 4. Declared horizons ───────────────────────────────────────────────


class TestTheHorizonIsDeclared:
    def test_a_three_day_forecast_matures_in_three_days(self, store):
        pid = _prediction(date="2026-01-05", horizon_days=3)
        row = _db().execute(
            "SELECT horizon_days, deadline FROM predictions WHERE id = ?",
            (pid,)).fetchone()
        assert row["horizon_days"] == 3
        assert row["deadline"] == "2026-01-08"
        assert [p["id"] for p in _due("2026-01-07")] == []
        assert [p["id"] for p in _due("2026-01-08")] == [pid]

    def test_a_five_day_forecast_matures_two_days_later(self, store):
        """The point of the declaration: two books with different horizons
        are no longer graded on one window."""
        short = _prediction(code="600000", date="2026-01-05", horizon_days=3)
        long = _prediction(code="600001", date="2026-01-05", horizon_days=5)
        assert [p["id"] for p in _due("2026-01-08")] == [short]
        assert [p["id"] for p in _due("2026-01-10")] == [short, long]

    def test_a_forecast_that_declared_nothing_says_so(self, store):
        pid = _prediction(date="2026-01-05", horizon_days=None)
        row = _db().execute(
            "SELECT horizon_days, deadline FROM predictions WHERE id = ?",
            (pid,)).fetchone()
        assert row["horizon_days"] is None
        assert row["deadline"] is None, "no declaration means no deadline"

        due = _due("2026-01-09")            # day 4: not yet, on the 5-day fallback
        assert due == []
        due = _due("2026-01-10")            # day 5: due, on the fallback
        assert [p["id"] for p in due] == [pid]
        assert due[0]["legacy_horizon"] == 1

    def test_the_label_records_that_the_horizon_was_a_fallback(self, store):
        pid = _prediction(date="2026-01-05", horizon_days=None)
        conn = _db()
        pred = _due("2026-01-10")[0]
        OL.label_forecast(conn, prediction=pred, scored=_score(), as_of="2026-01-10")
        label = O.current(conn, O.FORECAST, "prediction", pid)
        assert label["evidence"]["legacy_horizon"] is True
        assert label["evidence"]["horizon_days"] is None

    def test_a_declared_forecast_is_not_marked_legacy(self, store):
        pid = _prediction(date="2026-01-05", horizon_days=3)
        conn = _db()
        pred = _due("2026-01-08")[0]
        OL.label_forecast(conn, prediction=pred, scored=_score(), as_of="2026-01-08")
        label = O.current(conn, O.FORECAST, "prediction", pid)
        assert label["evidence"]["legacy_horizon"] is False
        assert label["evidence"]["deadline"] == "2026-01-08"

    def test_a_bad_horizon_is_stored_as_no_declaration(self, store):
        for bad in (0, -3, "soon"):
            pid = _prediction(code=f"60{bad if isinstance(bad, int) else 2}",
                              horizon_days=bad)
            assert _db().execute(
                "SELECT deadline FROM predictions WHERE id = ?",
                (pid,)).fetchone()["deadline"] is None


# ── 5. The labellers and the episode link ─────────────────────────────


class TestTheLabellers:
    def test_a_forecast_is_labelled_once_and_never_re_derived(self, store):
        """A matured label is a fact about a run, not a cache. Re-running
        the evaluator with a different score must not move it."""
        pid = _prediction(date="2026-01-05", horizon_days=3)
        conn = _db()
        pred = _due("2026-01-08")[0]
        OL.label_forecast(conn, prediction=pred, scored=_score(), as_of="2026-01-08")
        again = OL.label_forecast(conn, prediction=pred, scored=_score(),
                                  as_of="2026-01-09")
        assert again["state"] == O.MATURED
        assert again["changed"] is False, "a matured label is not re-derived"
        other = OL.label_forecast(conn, prediction=pred,
                                  scored=_score(outcome=False),
                                  as_of="2026-01-09")
        assert other["changed"] is False, "a matured label is not re-derived"
        assert len(O.history(conn, O.FORECAST, "prediction", pid)) == 2

    def test_a_missing_score_is_censored_not_dropped(self, store):
        """A system that silently skips the un-gradeable ones reports a
        hit rate over a sample it chose."""
        pid = _prediction(date="2026-01-05", horizon_days=3)
        conn = _db()
        result = OL.label_forecast(conn, prediction=_due("2026-01-08")[0],
                                   scored=None, as_of="2026-01-08")
        assert result["state"] == O.CENSORED
        label = O.current(conn, O.FORECAST, "prediction", pid)
        assert label["state"] == O.CENSORED
        assert "no score" in label["evidence"]["reason"]

    def test_a_censored_forecast_is_corrected_when_the_data_arrives(self, store):
        """The live path for ``censored → revised``: the horizon arrived
        without evidence, the evidence turned up later, and the correction
        is appended rather than overwriting the censored fact."""
        pid = _prediction(date="2026-01-05", horizon_days=3)
        conn = _db()
        pred = _due("2026-01-08")[0]
        OL.label_forecast(conn, prediction=pred, scored=None, as_of="2026-01-08")
        later = OL.label_forecast(conn, prediction=pred, scored=_score(),
                                  as_of="2026-01-12")
        assert later["state"] == O.REVISED
        chain = O.history(conn, O.FORECAST, "prediction", pid)
        assert [r["state"] for r in chain] == [O.PENDING, O.CENSORED, O.REVISED]

    def test_outstanding_forecasts_are_declared_pending(self, store):
        """So "how many calls are still awaiting their horizon" is a
        number, not an inference from an empty table."""
        a = _prediction(code="600000")
        b = _prediction(code="600001")
        conn = _db()
        assert OL.declare_outstanding_forecasts(conn, as_of="2026-01-05") == 2
        # Idempotent: a second pass adds nothing.
        assert OL.declare_outstanding_forecasts(conn, as_of="2026-01-05") == 0
        live = O.pending_labels(conn, kind=O.FORECAST)
        assert {r["subject_id"] for r in live} == {a, b}
        assert all(r["state"] == O.PENDING for r in live)

    def test_a_trade_label_waits_while_the_position_is_open(
            self, store, traders_dir):
        position_id = _open_position()
        conn = _db()
        counts = OL.sweep_trade_labels(conn, as_of="2026-01-05")
        assert counts["pending"] == 1
        label = O.current(conn, O.TRADE, "position", position_id)
        assert label["state"] == O.PENDING
        # No terminal value and no mark: this layer has no price, and a
        # number without a provenance is a fabrication.
        assert label["evidence"] == {"position_id": position_id,
                                     "status": "open", "legs": 0}

    def test_a_closed_position_is_matured_from_its_ledger_legs(
            self, store, traders_dir):
        position_id = _open_position()
        conn = _db()
        OL.sweep_trade_labels(conn, as_of="2026-01-05")
        P.close_position(position_id, close_price=11.0,
                         close_reason="止盈触发")
        counts = OL.sweep_trade_labels(conn, as_of="2026-01-06")
        assert counts["matured"] == 1
        label = O.current(conn, O.TRADE, "position", position_id)
        assert label["state"] == O.MATURED
        legs = conn.execute(
            "SELECT id FROM position_exits WHERE position_id = ? ORDER BY id",
            (position_id,)).fetchall()
        assert label["evidence"]["exit_ids"] == [l["id"] for l in legs]
        assert label["evidence"]["legs"] == len(legs)

    def test_an_order_that_never_traded_gets_no_trade_label(
            self, store, traders_dir, theme):
        """"Did not trade" and "traded and broke even" are different
        facts, and only one of them is evidence about skill."""
        oid = P.create_pending_order(
            code="600000", name="A", theme="t", order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, stop_loss=8.5, source="morning",
            reason="主线在流入", trader_id="slow")
        P.cancel_order(oid, "主线走弱")
        conn = _db()
        counts = OL.sweep_trade_labels(conn, as_of="2026-01-05")
        assert counts == {"pending": 0, "matured": 0, "unchanged": 0}
        assert O.current(conn, O.TRADE, "position", oid) is None
        # The decision itself is still recorded — as an episode, which is
        # where "we decided and nothing came of it" belongs.
        assert episodes.get_episode(conn, episodes.episode_for_ref(conn, oid))

    def test_a_process_label_matures_on_the_day_the_thesis_is_written(
            self, store, traders_dir):
        tid = T.create(_thesis())
        conn = _db()
        counts = OL.sweep_process_labels(conn, as_of="2026-01-05")
        assert counts["matured"] == 1
        label = O.current(conn, O.PROCESS, "thesis", tid)
        assert label["state"] == O.MATURED
        assert label["evidence"]["multi_family"] is True
        assert label["evidence"]["score"] >= 1
        # A thesis that just matured and has not ended is *only* matured.
        # If it were also counted as unchanged, the three numbers would
        # stop adding up to the number of theses graded.
        assert counts == {"matured": 1, "revised": 0, "unchanged": 0}

    def test_the_process_counts_partition_the_theses_graded(
            self, store, traders_dir):
        """``matured + revised + unchanged`` is the number of theses the
        sweep looked at, and no thesis is in two buckets. Without this,
        "we graded 40 and 11 ended" cannot be read off the counts."""
        open_id = T.create(_thesis(code="600000"))
        ended_id = T.create(_thesis(code="600001"))
        T.close(ended_id, T.BLIND_SPOT, close_kind="", close_note="条件没触发")
        conn = _db()
        first = OL.sweep_process_labels(conn, as_of="2026-01-05")
        assert sum(first.values()) == 2, first
        assert first["revised"] == 1 and first["matured"] == 1
        assert first["unchanged"] == 0
        # Second pass over the same two: neither needs anything now.
        again = OL.sweep_process_labels(conn, as_of="2026-01-06")
        assert again == {"matured": 0, "revised": 0, "unchanged": 2}, again
        assert sum(again.values()) == 2
        assert {O.current(conn, O.PROCESS, "thesis", i)["state"]
                for i in (open_id, ended_id)} == {O.MATURED, O.REVISED}

    def test_a_blind_spot_is_appended_as_a_revision(self, store, traders_dir):
        """``matured → revised`` in production: the grade is knowable the
        day the thesis is written, and how it ended is not."""
        tid = T.create(_thesis())
        conn = _db()
        OL.sweep_process_labels(conn, as_of="2026-01-05")
        T.close(tid, T.BLIND_SPOT, close_kind="", close_note="没有任何条件触发")
        counts = OL.sweep_process_labels(conn, as_of="2026-01-20")
        assert counts["revised"] == 1
        chain = O.history(conn, O.PROCESS, "thesis", tid)
        assert [r["state"] for r in chain] == [O.PENDING, O.MATURED, O.REVISED]
        assert chain[-1]["evidence"]["blind_spot"] is True
        assert chain[1]["evidence"].get("blind_spot") is None
        # Re-running does not add a second revision.
        again = OL.sweep_process_labels(conn, as_of="2026-01-21")
        assert again == {"matured": 0, "revised": 0, "unchanged": 1}

    def test_counts_report_the_three_kinds_apart(self, store, traders_dir):
        """§9's separation as a number: one decision can be a correct
        forecast and a losing trade at once."""
        pid = _prediction(date="2026-01-05", horizon_days=3)
        conn = _db()
        OL.label_forecast(conn, prediction=_due("2026-01-08")[0],
                          scored=_score(outcome=True), as_of="2026-01-08")
        position_id = _open_position()
        T.create(_thesis())
        OL.sweep_trade_labels(conn, as_of="2026-01-08")
        OL.sweep_process_labels(conn, as_of="2026-01-08")
        tally = O.counts(conn)
        assert tally[O.FORECAST][O.MATURED] == 1
        assert tally[O.TRADE][O.PENDING] == 1
        assert tally[O.PROCESS][O.MATURED] == 1
        assert pid and position_id
        # And the three do not merge into one performance figure.
        assert set(tally) == {O.FORECAST, O.TRADE, O.PROCESS}

    def test_a_label_is_attributable_to_the_decision_that_produced_it(
            self, store, traders_dir, theme):
        """The forecast that became an order hangs on that order's
        episode; the one that stayed a call hangs on nothing, and that
        absence is the coverage statement."""
        traded = _prediction(code="600000", date="2026-01-05",
                             horizon_days=3)
        ignored = _prediction(code="600001", date="2026-01-05",
                              horizon_days=3)
        conn = _db()
        oid = P.create_pending_order(
            code="600000", name="A", theme="t", order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, stop_loss=8.5, source="morning",
            reason="主线在流入", trader_id="slow",
            prediction_id=traded)
        assert oid is not None
        OL.declare_outstanding_forecasts(conn, as_of="2026-01-05")

        for pred_id, expected_episode in ((traded, True), (ignored, False)):
            label = O.current(conn, O.FORECAST, "prediction", pred_id)
            if expected_episode:
                assert label["episode_id"] is not None
                assert episodes.get_episode(
                    conn, label["episode_id"])["prediction_id"] == pred_id
            else:
                assert label["episode_id"] is None

    def test_for_episode_gathers_every_label_of_one_decision(
            self, store, traders_dir, theme):
        pid = _prediction(date="2026-01-05", horizon_days=3)
        conn = _db()
        oid = P.create_pending_order(
            code="600000", name="A", theme="t", order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, stop_loss=8.5, source="morning",
            reason="主线在流入", trader_id="slow", prediction_id=pid)
        order = [o for o in P.get_pending_orders("slow") if o["id"] == oid][0]
        P._fill_order(order, fill_price=10.0, fill_date="2026-01-05")
        OL.label_forecast(conn, prediction=_due("2026-01-08")[0],
                          scored=_score(), as_of="2026-01-08")
        OL.sweep_trade_labels(conn, as_of="2026-01-08")
        episode_id = episodes.episode_for_ref(conn, oid)
        kinds = {r["kind"] for r in O.for_episode(conn, episode_id)}
        assert kinds == {O.FORECAST, O.TRADE}

    def test_a_label_can_be_found_by_its_version_and_availability(
            self, store, traders_dir):
        """The two fields that make a label falsifiable after the fact:
        which evaluator produced it, and when it was knowable. Without the
        first you cannot tell a changed rule from a changed market."""
        pid = _prediction(date="2026-01-05", horizon_days=3)
        conn = _db()
        OL.label_forecast(conn, prediction=_due("2026-01-08")[0],
                          scored=_score(), as_of="2026-01-08")
        label = O.current(conn, O.FORECAST, "prediction", pid)
        assert label["evaluator_version"] == OL.FORECAST_EVALUATOR
        assert label["available_at"] == "2026-01-08"
        assert label["created_at"]


class TestTheTerminalStatusesAreStillInSync:
    def test_the_evaluator_kinds_match_the_store(self):
        """``outcome_labels`` names the kinds by constant; this is the
        guard that keeps the two modules from drifting apart."""
        assert OL.PREDICTION and OL.POSITION and OL.THESIS
        assert O.KINDS == {O.FORECAST, O.TRADE, O.PROCESS}
        assert O.TERMINAL == {O.REVISED}
