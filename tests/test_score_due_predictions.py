"""Grading the due set: which rows get a score, which get a label, which wait.

``review._score_due_predictions`` is where D10 lived. It had **no test at all**,
which is why the bug was found by reasoning about the calendar instead of by a
red line: it was the only place that joined three separate questions —

1. *is this row past its declared deadline* (a calendar count, the SQL filter),
2. *has the market traded the window shut* (trading days, the real question),
3. *did a score come back* (market data for this stock).

Conflating 2 and 3 turned "we have not waited yet" into "we waited and got
nothing", and wrote a censored label on every forecast in the book the moment
its calendar deadline passed.

Nothing here is stubbed. The market is a synthetic ``daily_kline`` seeded with
a deterministic price path, and the scores come from the real
``scoring.score_prediction`` over it — the stub would have hidden exactly the
disagreement this file exists to pin.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from alpha_agents.data import memory_store, scoring
from alpha_agents.data import outcomes as O
from alpha_agents.pipeline.tasks.review import _score_due_predictions

_CODES = tuple(f"6000{i:02d}" for i in range(60))

_DAILY_KLINE = """
CREATE TABLE daily_kline (
    code TEXT NOT NULL, date TEXT NOT NULL, open REAL, high REAL, low REAL,
    close REAL, volume INTEGER, turnover_rate REAL, change_pct REAL,
    PRIMARY KEY (code, date));
CREATE INDEX idx_kline_date ON daily_kline(date);
"""


def _day(offset: int) -> str:
    """A date relative to the kernel's today, so the test does not rot."""
    return (datetime.now().date() + timedelta(days=offset)).strftime("%Y-%m-%d")


#: The market's last bar. Two days before "today", so the newest forecast
#: window a test can ask about is still open — which is the case D10 missed.
_LAST_BAR = _day(-2)
#: Old enough that its 5 calendar days have elapsed.
_CLOSED_ENTRY = _day(-20)
#: Exactly five calendar days back: the deadline has fired today, and the
#: window genuinely has not closed.
_DUE_TODAY_ENTRY = _day(-5)


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
    """A synthetic market: every calendar day, 60 codes, one price path.

    Daily rather than weekday-only on purpose — the test then measures the
    trading-day logic without depending on which weekday it runs.
    """
    path = tmp_path / "market_history.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(_DAILY_KLINE)
    days = [_day(-i) for i in range(43, 1, -1)]          # _day(-43) .. _day(-2)
    for i, code in enumerate(_CODES):
        for j, date in enumerate(days):
            conn.execute(
                "INSERT INTO daily_kline (code, date, close, volume, "
                "turnover_rate) VALUES (?, ?, ?, 1000, 1.0)",
                (code, date, round(10 + 0.1 * i + 0.02 * j, 4)))
    conn.commit()
    monkeypatch.setattr(scoring, "DB_PATH", path)
    yield conn
    conn.close()


def _predict(*, date: str, code: str, prob: float = 0.6,
             horizon_days: int = 5, report_type: str = "morning") -> int:
    return memory_store.save_prediction(
        date, report_type, code, "A", "看多", "medium", "t", 10.0,
        "资金持续流入，主力净买入 1.2 亿", {}, prob, "default",
        horizon_days=horizon_days)


def _row(conn, table: str, **where) -> dict | None:
    sql = (f"SELECT * FROM {table} WHERE "
           + " AND ".join(f"{k} = ?" for k in where) + " ORDER BY id LIMIT 1")
    row = conn.execute(sql, tuple(where.values())).fetchone()
    return dict(row) if row else None


def _labels(conn) -> list[dict]:
    """Each forecast's **current** label, one row per prediction.

    Not every row in ``outcomes``: a label chain appends its successor, so a
    prediction that has been graded has both a ``pending`` row and a
    ``matured`` one. Reading the table raw would count the history as if each
    step were a separate forecast.
    """
    rows = conn.execute(
        "SELECT state, evidence_json FROM outcomes o WHERE o.kind = ? AND "
        "o.id = (SELECT MAX(id) FROM outcomes WHERE kind = ? "
        "        AND subject_id = o.subject_id) ORDER BY o.subject_id",
        (O.FORECAST, O.FORECAST)).fetchall()
    return [dict(r) for r in rows]


def _states(conn) -> list[str]:
    return sorted(row["state"] for row in _labels(conn))


class TestTheDueSetIsThreeQuestionsNotOne:
    def test_a_row_whose_window_is_open_is_left_pending(self, store, market):
        """The row the calendar hands over and the market has not finished.

        Its ``deadline`` has passed — that is asserted, not assumed — and the
        label still must not be terminal. This is the whole of D10.
        """
        pred_id = _predict(date=_DUE_TODAY_ENTRY, code="600001")
        row = _row(store, "predictions", id=pred_id)
        assert row["deadline"] <= datetime.now().strftime("%Y-%m-%d"), \
            "the calendar deadline really has fired"

        _score_due_predictions()

        after = _row(store, "predictions", id=pred_id)
        assert after["scored_at"] is None, "nothing was gradeable yet"
        assert after["brier"] is None
        assert _states(store) == [O.PENDING]
        assert scoring.evidence_window_closed(_DUE_TODAY_ENTRY, 5) is False

    def test_a_row_whose_window_closed_is_graded(self, store, market):
        pred_id = _predict(date=_CLOSED_ENTRY, code="600000")

        _score_due_predictions()

        after = _row(store, "predictions", id=pred_id)
        assert after["scored_at"], "a closed window with data must be graded"
        assert after["brier"] is not None
        assert _states(store) == [O.MATURED]

    def test_a_closed_window_with_no_price_is_censored(self, store, market):
        """The case the censored label is actually for: we waited, and this
        stock has nothing to grade — suspended, delisted, renamed."""
        pred_id = _predict(date=_CLOSED_ENTRY, code="NO_SUCH_CODE")

        _score_due_predictions()

        after = _row(store, "predictions", id=pred_id)
        assert after["scored_at"] is None
        assert _states(store) == [O.CENSORED]
        assert "no score" in _labels(store)[0]["evidence_json"]

    def test_the_three_outcomes_coexist_in_one_sweep(self, store, market):
        """One run, three answers, told apart. A single "0/3 scored" line
        would report the same thing for all of them."""
        graded = _predict(date=_CLOSED_ENTRY, code="600000")
        waiting = _predict(date=_DUE_TODAY_ENTRY, code="600001")
        missing = _predict(date=_CLOSED_ENTRY, code="NO_SUCH_CODE")

        _score_due_predictions()

        assert _row(store, "predictions", id=graded)["scored_at"]
        assert _row(store, "predictions", id=waiting)["scored_at"] is None
        assert _row(store, "predictions", id=missing)["scored_at"] is None
        assert sorted(_states(store)) == sorted(
            [O.MATURED, O.PENDING, O.CENSORED])

    def test_a_deferred_row_is_not_forgotten_by_later_sweeps(
            self, store, market):
        """Waiting is not dropping. Once the window closes the same row is
        graded without anyone re-declaring it."""
        pred_id = _predict(date=_DUE_TODAY_ENTRY, code="600002")
        _score_due_predictions()
        assert _states(store) == [O.PENDING]

        # The market trades two more days, which closes a 5-day window that
        # had four bars of it.
        for offset in (-1, 0):
            for i, code in enumerate(_CODES):
                market.execute(
                    "INSERT INTO daily_kline (code, date, close, volume, "
                    "turnover_rate) VALUES (?, ?, ?, 1000, 1.0)",
                    (code, _day(offset), round(11 + 0.1 * i, 4)))
        market.commit()
        assert scoring.evidence_window_closed(_DUE_TODAY_ENTRY, 5) is True

        _score_due_predictions()

        assert _row(store, "predictions", id=pred_id)["scored_at"]
        assert _states(store) == [O.MATURED]

    def test_the_summary_only_reports_graded_rows(self, store, market):
        """The report block is built from scores, so a sweep that graded
        nothing returns nothing rather than a page of zeroes."""
        assert _score_due_predictions() == ""
        _predict(date=_CLOSED_ENTRY, code="600000")
        assert "预测质量" in _score_due_predictions()
