"""The market grades the Trader's decisions; the reviewing model does not."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

import pytest

from alpha_agents.data import memory_store
from alpha_agents.data import trader_learning as D
from alpha_agents.evolution import decision_outcomes as O


def _sessions(n: int) -> list[str]:
    out, day = [], date(2026, 1, 5)
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


DAYS = _sessions(12)


@pytest.fixture()
def book(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_store, "MEMORY_DB_PATH",
                        tmp_path / "memory.db", raising=False)
    monkeypatch.setattr(memory_store._local, "conn", None, raising=False)
    db = memory_store._get_conn()
    yield db
    db.close()
    memory_store._local.conn = None


def _hist(prices: dict[str, list[float]], *, market: int = 60,
          changes: dict[str, list[float]] | None = None) -> sqlite3.Connection:
    """``market`` flat filler names make the median 0 unless moved."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE daily_kline (code TEXT, date TEXT, open REAL, "
               "high REAL, low REAL, close REAL, change_pct REAL)")
    for i in range(market):
        for d in DAYS:
            db.execute("INSERT INTO daily_kline VALUES (?,?,10,10,10,10,0)",
                       (f"9{i:05d}", d))
    for code, closes in prices.items():
        for j, (d, c) in enumerate(zip(DAYS, closes)):
            chg = (changes or {}).get(code, [0.0] * len(DAYS))[j]
            db.execute("INSERT INTO daily_kline VALUES (?,?,?,?,?,?,?)",
                       (code, d, c, c, c, c, chg))
    return db


def _seal(book, decisions: list[dict]) -> None:
    book.execute(
        "INSERT INTO trader_state_snapshots (state_hash,run_id,trader_id,"
        "version,as_of,payload_json) VALUES ('h','run','default',1,'x',?)",
        (json.dumps({"recent_decisions": decisions}),))
    book.commit()


def _decision(action, code="600001", day=DAYS[1], hhmm="09:05", i=0):
    return {"decision_id": f"{action}-{i}", "action": action, "code": code,
            "made_at": f"{day}T{hhmm}:00+08:00", "timeframe": "1d",
            "decision_horizon": "3-5d", "evidence_scope": "replay_daily"}


@pytest.mark.parametrize("action,excess,expected", [
    ("buy", 5, "right"), ("buy", -5, "wrong"),
    ("add", 5, "right"), ("hold", -5, "wrong"),
    ("sell", -5, "right"), ("sell", 5, "wrong"),
    ("reduce", -5, "right"), ("reject", -5, "right"),
    ("wait", 5, "wrong"), ("buy", 0.2, "flat"), ("wait", -0.3, "flat"),
])
def test_verdicts(action, excess, expected):
    assert O.verdict(action, excess) == expected


def test_a_rising_name_makes_a_buy_right_and_a_reject_wrong(book):
    rising = [10, 10, 10.2, 10.4, 10.6, 10.8, 11.0, 11.2, 11.4, 11.6, 11.8, 12]
    hist = _hist({"600001": rising})
    _seal(book, [_decision("buy", i=1), _decision("reject", i=2)])
    got = O.grade(book, hist, run_id="run", trader_id="default",
                  as_of=DAYS[-1])
    assert got["graded"] == 2
    rows = {r["action"]: r for r in D.decision_outcomes(
        run_id="run", trader_id="default", conn=book)}
    # Open-phase decision on DAYS[1]: base is DAYS[0]'s close (10.0), end is
    # five sessions later (DAYS[5] = 10.8) — +8%, against a flat median.
    assert rows["buy"]["base_date"] == DAYS[0]
    assert rows["buy"]["end_date"] == DAYS[5]
    assert rows["buy"]["forward_pct"] == pytest.approx(8.0)
    assert rows["buy"]["market_median_pct"] == pytest.approx(0.0)
    assert rows["buy"]["verdict"] == "right"
    assert rows["reject"]["verdict"] == "wrong"


def test_a_close_phase_decision_is_based_on_its_own_close(book):
    hist = _hist({"600001": [10] * 12})
    _seal(book, [_decision("hold", hhmm="14:55")])
    O.grade(book, hist, run_id="run", trader_id="default", as_of=DAYS[-1])
    row = D.decision_outcomes(run_id="run", trader_id="default", conn=book)[0]
    assert row["base_date"] == DAYS[1]


def test_an_open_window_is_not_graded_and_a_future_bar_changes_nothing(book):
    """Graded at DAYS[4] the 5-session window from DAYS[0] is not closed; the
    bars after DAYS[4] exist in the table and must not be read."""
    hist = _hist({"600001": [10, 10, 10, 10, 10, 50, 50, 50, 50, 50, 50, 50]})
    _seal(book, [_decision("buy")])
    got = O.grade(book, hist, run_id="run", trader_id="default",
                  as_of=DAYS[4])
    assert got == {"graded": 0, "pending": 1, "ungradeable": 0}
    assert D.decision_outcomes(run_id="run", trader_id="default",
                               conn=book) == []


def test_the_benchmark_is_the_market_median_not_its_mean(book):
    """Three filler names quintuple: the mean jumps, the median does not."""
    movers = {f"8{i:05d}": [10] + [50] * 11 for i in range(3)}
    hist = _hist({"600001": [10] + [10.3] * 11, **movers})
    _seal(book, [_decision("buy")])
    O.grade(book, hist, run_id="run", trader_id="default", as_of=DAYS[-1])
    row = D.decision_outcomes(run_id="run", trader_id="default", conn=book)[0]
    # +3% against a median of 0 is right; against the mean (~+19%) it would
    # have been graded wrong.
    assert row["market_median_pct"] == pytest.approx(0.0)
    assert row["verdict"] == "right"


def test_grading_is_idempotent(book):
    hist = _hist({"600001": [10 + i for i in range(12)]})
    _seal(book, [_decision("buy")])
    O.grade(book, hist, run_id="run", trader_id="default", as_of=DAYS[-1])
    again = O.grade(book, hist, run_id="run", trader_id="default",
                    as_of=DAYS[-1])
    assert again["graded"] == 0
    assert len(D.decision_outcomes(run_id="run", trader_id="default",
                                   conn=book)) == 1


def test_grading_works_with_a_bare_connection(book):
    """The callers open market history with plain sqlite3.connect and no
    row factory — the live close-review task and the replay runner both
    do. The helpers read rows by name, so grade must set the Row factory
    itself. Before that, a bare connection turned every decision into
    "pending" behind a caught TypeError: thirty replay days, 505 decisions
    gradeable, zero graded, and the report's only symptom was "窗口内还没
    有闭合的决策窗口" (found 2026-09-26 on run horizon30-20260105)."""
    rising = [10, 10, 10.2, 10.4, 10.6, 10.8, 11.0, 11.2, 11.4, 11.6, 11.8, 12]
    bare = _hist({"600001": rising})
    bare.row_factory = None                       # what the callers do
    _seal(book, [_decision("buy", i=1)])
    got = O.grade(book, bare, run_id="run", trader_id="default",
                  as_of=DAYS[-1])
    assert got["graded"] == 1, "a tuple-factory history must still grade"
    row = D.decision_outcomes(run_id="run", trader_id="default",
                              conn=book)[0]
    assert row["verdict"] == "right"


def test_situation_tags_come_from_the_base_session():
    closes = [10, 10, 10, 10, 10, 12, 11]
    hist = _hist({"600001": closes},
                 changes={"600001": [0, 0, 0, 0, 0, 20.0, -8.3] + [0] * 5})
    tags = O.situation_tags(hist, "600001", DAYS[5])
    assert "t1_up_big" in tags and "run_up_5d" in tags
    later = O.situation_tags(hist, "600001", DAYS[6])
    assert "t1_down" in later and "t1_up_big" not in later
