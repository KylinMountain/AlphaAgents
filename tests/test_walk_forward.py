"""M1's acceptance list, run rather than asserted in prose.

``docs/exec-plans/active/2026-09-16-the-agent-decides-each-morning.md`` §M1
lists four machine-checkable claims about the walk-forward runner:

1. the production book's content hash is the same before and after a run,
2. the same window under ``replay-recorded`` twice is row-for-row identical,
3. a name opening at its up limit does not fill, and a suspended name does not,
4. the ``intraday_ambiguous`` count is emitted rather than swallowed.

All four were unchecked, and the runner had no test at all: nothing under
``tests/`` imported it, so its first real execution was against live history —
which is how D27 (a self-deadlock on the fill path) and D28 (a negative budget
that aborted a whole pending-order cycle) were found. This file runs it end to
end against a synthetic corpus so the four claims are checked by a machine.

Why the shape is what it is
---------------------------
``tests/conftest.py`` refuses SQLite connections into ``data/`` and pins every
store's path at a per-test sandbox directory *before* the test body runs. So the
runner cannot be handed a corpus directory of its own: its replay directory is
the sandbox's own data directory, and the corpus arrives as symlinks planted by
``scripts/walk_bootstrap.py`` — the same way production delivers it. That is not
a workaround around the isolation; it is the isolation, under test.

The corpus is synthetic and small on purpose. Twenty sessions of history must sit
behind the window before the runner will size an order at all, because ``adv20``
asks for twenty bars ending at the previous session and a name it cannot measure
is skipped rather than ordered-and-hoped.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import sqlite3
import sys
import threading
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))

import walk_bootstrap  # noqa: E402
import walk_forward  # noqa: E402

from alpha_agents import config as app_config  # noqa: E402
from alpha_agents.data import market_history as mh  # noqa: E402
from alpha_agents.data import t1_settlement as S  # noqa: E402

#: The live book. Read by plain file open, never through SQLite: the conftest
#: forbids a connection into ``data/``, and hashing needs no connection.
_PRODUCTION_MEMORY = _PROJECT_ROOT / "data" / "memory.db"


def _weekdays(first: date, count: int) -> list[str]:
    out: list[str] = []
    day = first
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


#: Twenty-six weekdays: twenty-one behind the window (enough for ``adv20`` to
#: answer) plus the window itself.
_SESSIONS = _weekdays(date(2025, 6, 2), 26)

#: The day the decider ranks on, and the day the window opens.
_PREV = _SESSIONS[20]      # 2025-06-30
_START = _SESSIONS[21]     # 2025-07-01

#: The only code the decider ever picks in these fixtures, and the three names
#: the corpus holds. ``600001`` leads on the previous session's change, so a
#: scenario is changed by editing its window-day bar and nothing else.
_PICKED = "600001"


# ── fixtures ────────────────────────────────────────────────────────────────


def _bar(*, open_, high, low, close, volume=1_000_000, change_pct=0.0) -> dict:
    return {"open": open_, "high": high, "low": low, "close": close,
            "volume": volume, "turnover_rate": 1.0, "change_pct": change_pct}


def _series(days, *, close=10.0, volume=1_000_000, change_pct=0.0) -> dict:
    """A flat history: every session the same quiet bar."""
    return {d: _bar(open_=close, high=close, low=close, close=close,
                    volume=volume, change_pct=change_pct) for d in days}


def _normal() -> tuple[dict, dict]:
    """Three main-board names, one clearly the leader on the previous session.

    The two followers exist so the decider has a ranking to do rather than a
    single candidate it cannot get wrong.
    """
    instruments = {
        "600001": {"name": "甲"},
        "600002": {"name": "乙"},
        "600003": {"name": "丙"},
    }
    series = {}
    for code, chg in (("600001", 3.0), ("600002", 1.0), ("600003", 0.5)):
        days = _series(_SESSIONS, close=10.0)
        days[_PREV] = _bar(open_=10.0, high=10.0, low=10.0, close=10.0,
                           change_pct=chg)
        series[code] = days
    return series, instruments


def _write_corpus(root: Path, *, series: dict, instruments: dict) -> Path:
    """A synthetic ``market_history.db`` and ``stocks.db``.

    The DDL comes from the modules' own schema constants rather than a copy
    written here: a fixture that restates a schema is a second definition of it,
    and this repository has already paid for that pattern once.
    """
    root.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(root / "market_history.db")
    try:
        con.executescript(mh._SCHEMA)
        con.executemany(
            "INSERT INTO daily_kline (code, date, open, high, low, close, volume,"
            " turnover_rate, change_pct) VALUES (?,?,?,?,?,?,?,?,?)",
            [(code, day, b["open"], b["high"], b["low"], b["close"],
              b["volume"], b["turnover_rate"], b["change_pct"])
             for code, days in series.items() for day, b in sorted(days.items())])
        con.commit()
    finally:
        con.close()

    con = sqlite3.connect(root / "stocks.db")
    try:
        con.execute(
            "CREATE TABLE stocks (code TEXT PRIMARY KEY, name TEXT NOT NULL,"
            " market_cap REAL, industry TEXT, is_st INTEGER NOT NULL DEFAULT 0,"
            " is_suspended INTEGER NOT NULL DEFAULT 0)")
        con.executemany(
            "INSERT INTO stocks (code, name, is_st, is_suspended) VALUES (?,?,?,?)",
            [(code, meta["name"], int(meta.get("is_st", False)),
              int(meta.get("is_suspended", False)))
             for code, meta in sorted(instruments.items())])
        con.commit()
    finally:
        con.close()
    return root


def _prepare(tmp_path: Path, series: dict, instruments: dict) -> tuple[Path, Path]:
    """Plant the corpus, then a fresh trader book beside it.

    The replay directory must *be* the sandbox data directory — ``conftest``
    pins every store there before the test body runs, and ``run`` refuses to
    proceed unless the imported ``config.DATA_DIR`` is the directory it was
    handed.
    """
    corpus = _write_corpus(tmp_path / "corpus", series=series,
                           instruments=instruments)
    replay = Path(app_config.DATA_DIR)
    walk_bootstrap.bootstrap(replay, corpus, force=True)
    return replay, corpus


def _forget_open_handles() -> None:
    """Drop the stores' cached connections so a rebuilt file is re-read.

    ``bootstrap`` replaces ``memory.db`` with a new file, and a connection held
    from before still points at the old inode — a run after that would read the
    book the previous run left behind and the comparison would be measuring the
    wrong thing. ``conftest`` performs this reset between tests; a test that
    rebuilds a book inside itself has to perform it itself.
    """
    for name in ("memory_store", "market_history", "activity_log",
                 "report_store", "snapshot_store"):
        module = sys.modules.get(f"alpha_agents.data.{name}")
        if module is not None and hasattr(module, "_local"):
            module._local = threading.local()


def _args(**over) -> argparse.Namespace:
    base = dict(target=None, start=_START, days=1, trader="pullback", picks=1,
                theme="WALK-TEST", stop_pct=8.0, participation=0.10,
                run_id="acceptance", out=None, keep_going=False)
    base.update(over)
    return argparse.Namespace(**base)


def _run(replay: Path, **over) -> dict:
    """One window, in this process.

    ``run`` refuses when the module was imported rather than executed, which is
    this case. Binding the global is not a bypass of the guard that matters:
    ``_assert_actually_bound`` still runs on the next line and still refuses
    unless the process's own ``DATA_DIR`` is the directory given here.
    """
    walk_forward._REPLAY_DIR = replay
    return walk_forward.run(_args(**over))


# ── reading a result ────────────────────────────────────────────────────────


def _fills(result: dict, code: str) -> list[dict]:
    return [f for f in result["fills"] if f["code"] == code]


def _statuses(result: dict, code: str) -> list[str]:
    return [e["status"] for e in result["events"] if e["code"] == code]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _csv_rows(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# ── 1. the production book is untouched ─────────────────────────────────────


class TestTheProductionBookIsUntouched:
    def test_a_run_leaves_the_live_book_and_the_corpus_alone(self, tmp_path):
        """M1 #1, measured outside the runner as well as by it.

        The runner computes this itself and reports it; the assertion here is
        made from the file, so a runner that reported the wrong thing would
        still be caught. The shared corpus is checked the same way and in the
        same test, because the two claims are one claim: a run reads history
        and writes only its own book.
        """
        assert _PRODUCTION_MEMORY.exists(), (
            "no production book to protect; this test would prove nothing")

        before = _sha256(_PRODUCTION_MEMORY)
        assert len(before) == 64, (
            "the live book hashed to something that is not a digest, so "
            "'unchanged' below would be comparing nothing to nothing")
        replay, _ = _prepare(tmp_path, *_normal())
        result = _run(replay, days=2)
        after = _sha256(_PRODUCTION_MEMORY)

        assert result["fills"], "a run with no fills proves little about a run"
        assert after == before
        assert result["production"]["unchanged"] is True
        assert result["production"]["hash_before"] == result["production"]["hash_after"]

        fingerprint = result["corpus"]["before"]
        assert fingerprint["market_history.db"] is not None, (
            "the corpus fingerprint saw no history file, so 'untouched' below "
            "would be comparing two absences")
        assert result["corpus"]["untouched"] is True
        assert result["corpus"]["before"] == result["corpus"]["after"]

    def test_the_runner_refuses_the_production_directory(self):
        """The guard behind #1, stated where it can fail loudly."""
        production = walk_forward._PRODUCTION_DIR.resolve()
        with pytest.raises(SystemExit):
            walk_forward._refuse_production(production)
        with pytest.raises(SystemExit):
            walk_forward._refuse_production(None)


# ── 2. the same window twice is the same run ────────────────────────────────


class TestTheSameWindowTwiceIsTheSameRun:
    def test_two_runs_agree_row_for_row(self, tmp_path, monkeypatch):
        """M1 #2.

        ``replay-recorded`` is declared rather than assumed: M1's placeholder
        decider calls no model at all, so what this pins is the clock, the
        settlement and the book. The model half of the claim is D24's
        record/replay mechanism, which is tested by call site in
        ``test_llm_journal``.
        """
        monkeypatch.setenv("ALPHAAGENTS_LLM_MODE", "replay-recorded")
        series, instruments = _normal()

        replay, corpus = _prepare(tmp_path, series, instruments)
        first = _run(replay, days=2)
        assert first["fills"], (
            "no fill to compare: two identical empty lists would make this "
            "claim vacuously true")
        assert first["model_calls"]["journal_after"] == 0, (
            "the placeholder decider called a model; the window is not "
            "reproducible and the report is not evidence")

        _forget_open_handles()
        walk_bootstrap.bootstrap(replay, corpus, force=True)
        second = _run(replay, days=2)

        assert [dict(f) for f in second["fills"]] == [dict(f) for f in first["fills"]]
        assert second["settlement"] == first["settlement"]
        assert second["equity"] == first["equity"]


# ── 3. one-way limits and suspensions ───────────────────────────────────────


class TestANameThatCouldNotTradeDoesNotFill:
    def test_an_open_at_the_up_limit_does_not_fill(self, tmp_path):
        """M1 #3, first half — 一字板 has no counterparty, so it is not a fill."""
        series, instruments = _normal()
        locked = round(10.00 * 1.10, 2)
        series[_PICKED][_START] = _bar(open_=locked, high=locked, low=locked,
                                       close=locked, change_pct=10.0)

        replay, _ = _prepare(tmp_path, series, instruments)
        result = _run(replay)

        assert _fills(result, _PICKED) == []
        assert _statuses(result, _PICKED) == [S.LIMIT_BLOCKED]
        assert S.LIMIT_BLOCKED in S.STILL_WAITING

    def test_one_cent_lower_it_is_no_longer_the_limit_that_refused(self, tmp_path):
        """The companion, and it is the whole point: 一字板 is exact, not fuzzy.

        The same order, one cent lower, and the *reason* changes — from "no
        counterparty" to "the session never reached the order". Without this,
        the test above would also pass for a runner that refused every open
        outside the entry zone, which is a different rule: the zone declines
        *this* order, the limit says there was no order anyone could have
        filled.

        It does not fill either way, and that is not the point being made. The
        pullback zone tops out at ``1.005 × 10.00``, so a cent under the limit
        is far above it by arithmetic, not by policy.

        Read from the settlement table rather than from the events: ``no_fill``
        is deliberately not an event, because "nothing happened" is not worth a
        row per order per day. The counter is where it lives.
        """
        series, instruments = _normal()
        near = round(round(10.00 * 1.10, 2) - 0.01, 2)
        series[_PICKED][_START] = _bar(open_=near, high=near, low=10.50,
                                       close=10.80, change_pct=10.0)

        replay, _ = _prepare(tmp_path, series, instruments)
        result = _run(replay)
        day = result["settlement"][0]

        assert _fills(result, _PICKED) == []
        assert day[S.NO_FILL] == 1
        assert day[S.LIMIT_BLOCKED] == 0
        assert _statuses(result, _PICKED) == []

    def test_a_name_with_no_bar_is_suspended_not_cancelled(self, tmp_path):
        """M1 #3, second half — and the order stays alive rather than expiring."""
        series, instruments = _normal()
        del series[_PICKED][_START]

        replay, _ = _prepare(tmp_path, series, instruments)
        result = _run(replay)

        assert _fills(result, _PICKED) == []
        assert _statuses(result, _PICKED) == [S.SUSPENDED]
        assert S.SUSPENDED in S.STILL_WAITING
        # "not cancelled" has to be asked of the counter's *values*, not of its
        # keys: the keys are reasons, so ``_PICKED not in result["cancels"]``
        # would hold whatever happened. Nothing was cancelled here at all.
        assert not result["cancels"], result["cancels"]


# ── 4. the ambiguous count is emitted ───────────────────────────────────────


class TestTheAmbiguousCountIsEmitted:
    def test_a_touch_the_open_missed_is_counted_and_written_out(self, tmp_path):
        """M1 #4 — counted in the result *and* present in the report.

        The order's zone is (9.70, 10.05) off a 10.00 previous close. The
        session opens at 10.20, above it, and trades down to 9.90 — through the
        zone. A daily bar does not say whether the order was resting when it
        got there, so this is an ambiguity to report, not a fill to claim.
        """
        series, instruments = _normal()
        series[_PICKED][_START] = _bar(open_=10.20, high=10.30, low=9.90,
                                       close=10.10, change_pct=2.0)

        replay, _ = _prepare(tmp_path, series, instruments)
        result = _run(replay)
        report = walk_forward.write_report(result, tmp_path / "out")

        assert _fills(result, _PICKED) == []
        assert _statuses(result, _PICKED) == [S.INTRADAY_AMBIGUOUS]
        assert result["settlement"][0]["intraday_ambiguous"] == 1

        rows = _csv_rows(tmp_path / "out" / "settlement.csv")
        assert rows, "the settlement table is the one this count has to reach"
        assert "intraday_ambiguous" in rows[0]
        assert rows[0]["intraday_ambiguous"] == "1"

        assert report["meta"]["placeholder_decider_called_no_model"] is True
        assert report["meta"]["production_db_unchanged"] is True
        assert report["meta"]["corpus_untouched"] is True


# ── the summary closes its own arithmetic ───────────────────────────────────


class TestTheSummaryNamesWhatItLeftOut:
    """Found by running the runner, not by reading it.

    The 30-session window on real history (2025-07-01 → 2025-08-11) printed
    ``挂单撤销 47（资金不足×36；到期未到价（5天）×3；到期未到价（7天）×2）``
    — a total, and parts summing to 41. Neither half was right, and the two
    halves are one defect seen twice: the six missing cancels *were* the
    run-aways, and they were missing because each was a class of one (the class
    below). With the key fixed they outrank ``到期未到价（5天）``, and with the
    remainder named the line closes:

    ``挂单撤销 47（资金不足×36；价格涨走×6；到期未到价（5天）×3；另有 2 条未列出（1 个原因））``
    """

    def test_a_truncated_breakdown_names_the_remainder(self):
        # That window's measured distribution, not an illustration of one.
        counter = Counter({"资金不足": 36, "价格涨走": 6,
                           "到期未到价（5天）": 3, "到期未到价（7天）": 2})
        text = walk_forward._top_reasons(counter)

        assert "资金不足×36" in text, "the reasons it does show must still show"
        assert "价格涨走×6" in text, "a class of six must outrank a class of three"
        assert "另有 2 条未列出" in text
        assert "1 个原因" in text
        # The line and its remainder now account for the whole counter. The
        # second assertion is the defect itself: the top three used to be
        # printed alone, and 36 + 3 + 2 is not 47.
        assert 36 + 6 + 3 + 2 == sum(counter.values())
        assert 36 + 3 + 2 != sum(counter.values())

    def test_a_breakdown_that_fits_says_nothing_about_a_remainder(self):
        text = walk_forward._top_reasons(Counter({"资金不足": 4, "价格涨走": 1}))
        assert text == "资金不足×4；价格涨走×1"
        assert "未列出" not in text

    def test_no_cancels_at_all(self):
        assert walk_forward._top_reasons(Counter()) == "无"


class TestTheInstanceIsCountedAsItsClass:
    """The second half of the same defect.

    ``价格涨走(7.51)`` is a sentence about one order, not a category. Counted
    verbatim it made every run-away a class of one, so a category of three never
    surfaced in a summary that ranks by count.
    """

    def test_a_price_in_the_reason_does_not_make_a_class_of_one(self):
        assert walk_forward._cancel_class("价格涨走(7.51)") == "价格涨走"
        assert walk_forward._cancel_class("价格涨走(30.32)") == "价格涨走"

    def test_a_full_width_parenthetical_is_part_of_the_reason(self):
        """Five days and seven days are different reasons, not one with a number."""
        assert walk_forward._cancel_class("到期未到价（5天）") == "到期未到价（5天）"
        assert walk_forward._cancel_class("到期未到价（7天）") == "到期未到价（7天）"

    def test_a_reason_with_no_parenthetical_is_untouched(self):
        for reason in ("资金不足", "无关联主线", "主线不存在", "组合回撤触及上限"):
            assert walk_forward._cancel_class(reason) == reason

    def test_the_thesis_date_is_the_instance(self):
        assert walk_forward._cancel_class("论点已失效(2026-09-01)") == "论点已失效"


def _cancelled_reasons(replay: Path) -> list[str]:
    """What the kernel actually wrote on the cancelled orders.

    Read-only, and through the sandbox connection, because the question is what
    reached the book rather than what the caller passed.
    """
    con = sqlite3.connect(f"file:{replay / 'memory.db'}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT close_reason FROM virtual_portfolio WHERE status = 'cancelled'"
        ).fetchall()
    finally:
        con.close()
    return [row[0] for row in rows]


class TestACancelIsCountedByItsKindAndRecordedByItsInstance:
    """The wiring, driven through the runner rather than called directly.

    ``TestTheInstanceIsCountedAsItsClass`` tests ``_cancel_class`` as a function,
    and a function test is exactly the shape that lets an **unwired** function
    pass: delete the call in ``_settle_entries`` and every one of those four
    assertions still holds while the summary goes back to counting
    ``价格涨走(7.51)`` as a class of one. That is the same "declared but never
    called" defect this repository has already paid for three times, so the
    wiring gets its own test on a real cancel.

    The scenario is a run-away: the order's zone tops out at ``10.05``, and the
    session opens at ``10.80`` — above the ``1.05 ×`` run-away bar, below the
    ``11.00`` one-way limit. That is the cheapest deterministic cancel to build,
    because it needs no theme row, no thesis and no expiry clock.
    """

    def test_a_runaway_is_summarised_as_its_kind(self, tmp_path):
        """The whole chain: alert reason → counter key → ``summary.txt``.

        Asserting the helper's return value would still leave ``_summary`` free
        to print the raw counter, so the last assertion reads the file the run
        actually wrote.
        """
        series, instruments = _normal()
        series[_PICKED][_START] = _bar(open_=10.80, high=10.90, low=10.70,
                                       close=10.85, change_pct=8.5)
        replay, _ = _prepare(tmp_path, series, instruments)
        result = _run(replay)
        walk_forward.write_report(result, tmp_path / "out")

        assert result["cancels"], (
            "no cancel was produced, so this test would pass against a runner "
            "that normalised nothing")
        assert set(result["cancels"]) == {"价格涨走"}, (
            f"the counter kept the instance: {dict(result['cancels'])}")
        assert "(" not in "".join(result["cancels"]), (
            "an ASCII parenthetical survived into a grouping key, which is what "
            "made each run-away a class of one")
        assert walk_forward._top_reasons(result["cancels"]) == "价格涨走×1"

        summary = (tmp_path / "out" / "summary.txt").read_text(encoding="utf-8")
        assert "价格涨走×1" in summary, (
            f"the summary line did not reach the report:\n{summary}")
        assert "价格涨走(10.80)" not in summary, (
            "the report still prints the instance, so the fix never reached "
            "the page a reader sees")

    def test_the_instance_still_survives_on_the_order_row(self, tmp_path):
        """Aggregating the key must not aggregate the record.

        The summary is allowed to add up; the evidence is not allowed to. If
        normalisation reached the write path too, the line would read tidily and
        "which price ran away?" would have no answer anywhere.
        """
        series, instruments = _normal()
        series[_PICKED][_START] = _bar(open_=10.80, high=10.90, low=10.70,
                                       close=10.85, change_pct=8.5)
        replay, _ = _prepare(tmp_path, series, instruments)
        _run(replay)

        reasons = _cancelled_reasons(replay)
        assert len(reasons) == 1, reasons
        assert reasons[0].startswith("价格已涨走("), (
            f"the row lost its detail: {reasons[0]!r}")
        assert "10.80" in reasons[0], (
            f"the row no longer names the price that ran away: {reasons[0]!r}")
        assert "10.05" in reasons[0], (
            f"the row no longer names the limit it ran away from: {reasons[0]!r}")
