"""The horizon a decision declares has to reach the *forecast*, not only the thesis.

A forecast that declares no deadline is not "graded on five days". It is graded
on the global five-day fallback **and** labelled ``legacy_horizon``, because
``_deadline_for`` refuses to write a declaration the caller never made. That
refusal is right and it was working.

What was missing is the other half: the callers *had* declared a horizon — the
morning prompt asks the model for one per pick, and the intraday path hands a
literal ``3`` to the thesis built from the very same decision — and neither
passed it to ``save_prediction``. So all 232 rows in production carried NULL in
``horizon_days`` and ``deadline`` while 37 of the 41 theses said three days, and
every forecast was being judged against a deadline its own thesis disagreed
with. The evidence the loop spends twenty trading days accumulating was the
wrong evidence.

These tests drive the two real save paths rather than ``save_prediction``:
``save_prediction`` was never the broken part.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from alpha_agents.data import clock, memory_store
from alpha_agents.data.trader import get_trader
from alpha_agents.pipeline.tasks import intraday_monitor, morning_scan


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_store, "MEMORY_DB_PATH",
                        tmp_path / "memory.db", raising=False)
    monkeypatch.setattr(memory_store._local, "conn", None, raising=False)
    yield
    conn = getattr(memory_store._local, "conn", None)
    if conn is not None:
        conn.close()
    memory_store._local.conn = None


@pytest.fixture()
def no_quotes(monkeypatch):
    """The morning path batch-fetches a close price per pick. Off the network."""
    monkeypatch.setattr(morning_scan, "get_stock_quotes_fn",
                        lambda **kwargs: json.dumps({"quotes": []}))


def _prediction(code: str) -> dict:
    conn = memory_store._get_conn()
    row = conn.execute(
        "SELECT code, horizon_days, deadline FROM predictions WHERE code = ?",
        (code,)).fetchone()
    assert row is not None, f"{code} was not saved at all"
    return dict(row)


def _thesis(code: str) -> dict:
    conn = memory_store._get_conn()
    row = conn.execute(
        "SELECT id, horizon_days, status FROM theses WHERE code = ?",
        (code,)).fetchone()
    assert row is not None, f"no thesis for {code}"
    return dict(row)


PICK = {"name": "浦发银行", "theme": "测试主线", "reason": "测试",
        "entry_low": 10.0, "entry_high": 10.5, "stop_loss": 9.5}


class TestAnActionableIntradayPickDeclaresItsHorizon:
    """One constant, two readers: the forecast and the thesis it belongs to.

    The value is patched to a number neither path would produce by accident, so
    a reader that still used a literal ``3`` fails instead of agreeing.
    """

    def test_the_forecast_and_the_thesis_mature_together(self, store,
                                                         monkeypatch):
        monkeypatch.setattr(intraday_monitor, "INTRADAY_HORIZON_DAYS", 4)
        today = clock.today()
        assert intraday_monitor._record_intraday_pick(
            get_trader("default"), dict(PICK), "600000", today, "intraday",
            "high", 10.2, {}, {}, "actionable") is True

        forecast = _prediction("600000")
        thesis = _thesis("600000")
        assert forecast["horizon_days"] == 4
        assert thesis["horizon_days"] == 4, "the thesis and its own forecast disagree"
        assert forecast["deadline"] == (
            datetime.strptime(today, "%Y-%m-%d") + timedelta(days=4)
        ).strftime("%Y-%m-%d")

    def test_a_signal_row_declares_nothing(self, store, monkeypatch):
        """A limit-up ``signal`` is an observation: no probability, no window to
        mature, so no horizon. Filling one in would be a declaration the row has
        no forecast to keep."""
        monkeypatch.setattr(intraday_monitor, "INTRADAY_HORIZON_DAYS", 4)
        today = clock.today()
        intraday_monitor._record_intraday_pick(
            None, dict(PICK), "600001", today, "intraday_signal", "high",
            10.2, {}, {}, "signal")

        forecast = _prediction("600001")
        assert forecast["horizon_days"] is None
        assert forecast["deadline"] is None


class TestAMorningPickDeclaresItsOwnHorizon:
    def test_the_declared_horizon_reaches_both_records(self, store, no_quotes):
        morning_scan._save_recommendations_list(
            [{**PICK, "code": "600000", "confidence": "high",
              "horizon_days": 3}])

        assert _prediction("600000")["horizon_days"] == 3
        assert _thesis("600000")["horizon_days"] == 3

    def test_an_undeclared_horizon_is_not_substituted(self, store, no_quotes):
        """The one substitution ``_deadline_for`` exists to refuse.

        The thesis takes the documented default — that is
        ``from_recommendation``'s own decision and predates this — but the
        forecast stays undeclared, which is what makes the label
        ``legacy_horizon`` mean something. Writing 5 here would convert "the
        model said nothing" into "the model said five days", and the row would
        then be indistinguishable from one that really did.
        """
        morning_scan._save_recommendations_list(
            [{**PICK, "code": "600002", "confidence": "high"}])

        forecast = _prediction("600002")
        assert forecast["horizon_days"] is None
        assert forecast["deadline"] is None
        assert _thesis("600002")["horizon_days"] == 5

    def test_the_deadline_comes_from_the_declaration(self, store, no_quotes):
        morning_scan._save_recommendations_list(
            [{**PICK, "code": "600003", "confidence": "high",
              "horizon_days": 8}])
        today = datetime.strptime(clock.today(), "%Y-%m-%d")
        assert _prediction("600003")["deadline"] == (
            today + timedelta(days=8)).strftime("%Y-%m-%d")
