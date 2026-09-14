"""The Learn page's evaluation currency, counted the way the grader counts.

The forecasts section used to print one wrong sentence in two ways. It
called ``rows - brier_scored`` the gap — but most rows carry no ``prob`` and
can never carry a ``brier``, so 184 of 232 "missing" evaluations were never
going to exist. And it called the whole difference "the most pressing gap" —
when the actual state was D10's own calendar: a window that has not traded
shut yet is maturity, not a defect.

Nothing here is stubbed. The market is a synthetic ``daily_kline`` on real
dates, and the window arithmetic comes from the real
``scoring.window_progress`` — the same code the grader runs, which is the
point: the page and the grader must not be able to disagree about when a
window shuts.
"""

from __future__ import annotations

import sqlite3

import pytest

from alpha_agents.data import memory_store, scoring
from alpha_agents.server.readmodels import learn

_WEEK = ("2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11")
_MONDAY_AFTER = "2026-09-14"

_DAILY_KLINE = """
CREATE TABLE daily_kline (
    code TEXT NOT NULL, date TEXT NOT NULL, open REAL, high REAL, low REAL,
    close REAL, volume INTEGER, turnover_rate REAL, change_pct REAL,
    PRIMARY KEY (code, date));
CREATE INDEX idx_kline_date ON daily_kline(date);
"""


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_store, "MEMORY_DB_PATH",
                        tmp_path / "memory.db", raising=False)
    monkeypatch.setattr(memory_store._local, "conn", None, raising=False)
    yield memory_store._get_conn()
    conn = getattr(memory_store._local, "conn", None)
    if conn is not None:
        conn.close()
    memory_store._local.conn = None


@pytest.fixture()
def market(tmp_path, monkeypatch):
    path = tmp_path / "market_history.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(_DAILY_KLINE)
    for date in (*_WEEK, _MONDAY_AFTER):
        conn.execute(
            "INSERT INTO daily_kline (code, date, close, volume, "
            "turnover_rate) VALUES ('600000', ?, 10.0, 1000, 1.0)", (date,))
    conn.commit()
    monkeypatch.setattr(scoring, "DB_PATH", path)
    yield conn
    conn.close()


def _predict(*, date: str, code: str = "600000", prob: float | None = 0.6,
             horizon_days: int | None = None, report_type: str = "morning") -> int:
    return memory_store.save_prediction(
        date, report_type, code, "A", "看多", "medium", "t", 10.0,
        "资金持续流入，主力净买入 1.2 亿", {}, prob, "default",
        horizon_days=horizon_days)


class TestThePricingBlockCountsWindowsNotWishes:
    def test_the_denominator_is_the_probable_population(self, store, market):
        """A row without a prob is not a pending Brier evaluation.

        The old page subtracted ``brier_scored`` from ``rows`` and reported
        every probability-less row as an evaluation somebody owed. It was
        not owed: there is nothing to price, and the honest denominator for
        the evaluation currency is the population that carries a prob.
        """
        _predict(date="2026-09-07", prob=0.6)
        _predict(date="2026-09-07", code="600001", prob=None)  # a signal, no pricing
        totals, _ = learn._read_forecasts()

        assert totals["rows"] == 2
        assert totals["prob_rows"] == 1
        pricing = totals["pricing"]
        assert pricing["unscored"] == 1
        assert [b["rows"] for b in pricing["batches"]] == [1]

    def test_an_open_window_is_maturity_with_a_remaining_count(self, store,
                                                               market):
        """The 09-08 batch under a 5-day horizon is one trading day short.

        Six bars from 2026-09-08 would close the window; the market has
        traded five (09-08..09-11, 09-14). The page must say that — a number
        anyone can check against the calendar — instead of "gap".
        """
        _predict(date="2026-09-08", horizon_days=5)
        pricing = learn._read_forecasts()[0]["pricing"]

        assert pricing["unripe"] == 1
        assert pricing["ripe_unscored"] == 0
        assert pricing["min_remaining_days"] == 1
        batch = pricing["batches"][0]
        assert batch["have"] == 5
        assert batch["need"] == 6
        assert batch["remaining"] == 1

    def test_a_closed_window_without_a_score_is_named_as_the_defect(self,
                                                                    store,
                                                                    market):
        """Six bars have traded and no score landed: that IS a defect.

        The whole point of splitting the states — this is the only branch
        the page renders as a warning, because it is the only one somebody
        can fix by running the review step.
        """
        _predict(date="2026-09-07", horizon_days=5)
        pricing = learn._read_forecasts()[0]["pricing"]

        assert pricing["ripe_unscored"] == 1
        assert pricing["unripe"] == 0

    def test_legacy_rows_measure_the_same_window_the_grader_will(self, store,
                                                                 market):
        """A row written before horizons were declared falls back to 5 days.

        The fallback must be the grader's own default. If the page picked a
        different number, it would declare a batch ripe while the grader
        still measures it unripe — two definitions of one finish line.
        """
        _predict(date="2026-09-08", horizon_days=None)
        pricing = learn._read_forecasts()[0]["pricing"]

        assert pricing["batches"][0]["horizon"] == \
            scoring.DEFAULT_HORIZON_DAYS
        assert pricing["unripe"] == 1

    def test_an_absent_archive_is_a_refusal_not_a_countdown(self, store,
                                                            tmp_path,
                                                            monkeypatch):
        """With no market archive, not even "open" can be asserted.

        ``remaining`` would read as a patient countdown over a market nobody
        can see. The batch keeps its declared ``need``, drops its progress,
        and the block says the archive was unreadable — the page then refuses
        to display progress instead of inventing it.
        """
        _predict(date="2026-09-08", horizon_days=5)
        monkeypatch.setattr(scoring, "DB_PATH",
                            tmp_path / "nothing.db", raising=False)
        pricing = learn._read_forecasts()[0]["pricing"]

        assert pricing["archive_readable"] is False
        assert pricing["unripe"] == 0
        assert pricing["ripe_unscored"] == 0
        assert pricing["min_remaining_days"] is None
        batch = pricing["batches"][0]
        assert batch["need"] == 6
        assert batch["have"] is None
        assert batch["remaining"] is None
