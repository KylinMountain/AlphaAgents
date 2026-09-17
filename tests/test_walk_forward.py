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
import json
import os
import sqlite3
import sys
import threading
import time
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))

import walk_bootstrap  # noqa: E402
import walk_forward  # noqa: E402

from alpha_agents import config as app_config  # noqa: E402
from alpha_agents import llm_journal  # noqa: E402
from alpha_agents.data import learning_candidates as LC  # noqa: E402
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
                run_id="acceptance", out=None, keep_going=False,
                decider="placeholder", panel_size=40, news_limit=60,
                model_timeout=120.0, pace_seconds=0.0, agent_exits=False)
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


def wf_exit_attribution(result: dict) -> list[str]:
    return walk_forward._exit_attribution(result)


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


def _ordered_codes(replay: Path) -> set[str]:
    """Every code this run put an order on, whatever became of the order.

    Read from the book rather than from ``result``: a code the run *considered*
    and then cancelled leaves no fill, and its cancel is counted by reason
    without a code. The question here is what the decider chose, so the book is
    the only place that still knows.
    """
    con = sqlite3.connect(f"file:{replay / 'memory.db'}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT DISTINCT code FROM virtual_portfolio").fetchall()
    finally:
        con.close()
    return {row[0] for row in rows}


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
        if not _PRODUCTION_MEMORY.exists():
            # CI checks out the repo without ``data/`` (it is gitignored), so
            # there is no live book to protect there. The guard below is kept
            # for the machine that *does* have one: it turns "we hashed
            # nothing" into a failure rather than a green tick. Skipping here
            # is the same policy ``harness.yml`` already states for the 1.1GB
            # history file — the data-dependent assertions run where the data
            # is, and are visible as skips where it is not.
            pytest.skip("no production book on this machine (data/ not checked out)")

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

        # The verdict is about the replay's own handles, not about a file
        # another process owns (D39). The guard that the check saw a real file
        # stays: an access report over an empty set would pass vacuously.
        access = result["corpus"]["access"]
        assert access["files"]["market_history.db"]["present"] is True, (
            "the access check saw no history file, so 'read_only' below would "
            "be a statement about nothing")
        assert access["files"]["market_history.db"]["shared"] is True
        assert access["read_only"] is True

        # Kept, and it means less than it looks like: this fixture's corpus has
        # no second writer, so on a live box the same comparison is red because
        # production moved the file. That is why it is no longer the verdict.
        assert result["corpus"]["before"] == result["corpus"]["after"]
        assert result["corpus"]["changes"] == []

    def test_a_corpus_file_that_was_not_shared_is_reported_writable(self, tmp_path):
        """The negative half — without it ``read_only`` could be a constant.

        A check that can only come out true is not a check. The state it exists
        to catch is a corpus file the bootstrap did not link: ``corpus_access``
        then opens it read-write and the replay can append to history. That
        state is planted here by replacing one link with a real copy, which is
        also the only way to reach it — the bootstrap always links.
        """
        replay, corpus = _prepare(tmp_path, *_normal())
        link = replay / "stocks.db"
        assert link.is_symlink(), "fixture did not share stocks.db to begin with"
        link.unlink()
        link.write_bytes((corpus / "stocks.db").read_bytes())

        result = _run(replay, days=2)

        assert result["corpus"]["access"]["read_only"] is False
        assert result["corpus"]["access"]["files"]["stocks.db"] == {
            "present": True, "shared": False}
        # And the summary names the file, rather than printing a bare 否.
        assert "stocks.db" in walk_forward._corpus_access_line(
            result["corpus"]["access"])

    def test_a_file_production_moves_does_not_fail_the_run(self, tmp_path):
        """D39's harm, pinned: the exit status must not depend on a file another
        process owns.

        Production touches ``market_snapshots.db`` while a window runs. That is
        how the old check came out 否 on a live box on **every** long run, and
        because it gated the exit status, every long run returned 1. The move is
        simulated here by re-stamping a shared file's mtime from a thread while
        the window runs — production's write reduced to the effect the
        fingerprint reads, and one that cannot corrupt the corpus it is about.
        """
        replay, corpus = _prepare(tmp_path, *_normal())
        target = corpus / "market_history.db"
        assert (replay / "market_history.db").is_symlink(), (
            "the fixture shared no history, so nothing is being simulated")

        stop = threading.Event()

        def _production_writes():
            while not stop.is_set():
                os.utime(target, None)
                time.sleep(0.01)

        walk_forward._REPLAY_DIR = replay
        writer = threading.Thread(target=_production_writes, daemon=True)
        writer.start()
        try:
            rc = walk_forward.main(["--start", _START, "--days", "2",
                                    "--trader", "pullback", "--run-id", "d39"])
        finally:
            stop.set()
            writer.join(timeout=5)

        meta = json.loads((replay / "walk-reports" / "d39" / "run.json")
                          .read_text(encoding="utf-8"))
        assert meta["corpus_changes"], (
            "the diagnostic saw nothing move, so this test proved nothing about "
            "the case it exists for")
        assert meta["corpus_read_only"] is True
        assert rc == 0, "a file another process owns moved, and the run failed"

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
        assert report["meta"]["corpus_read_only"] is True


# ── 5. the decision cannot see the day it trades ────────────────────────────


class TestTheDecisionCannotSeeTheDayItTrades:
    """The strict time envelope, pinned instead of merely structural.

    The acceptance list asks that "the D 09:00 context contains no D
    close/high/low/volume". In the runner that holds *by construction*:
    ``_decide(ctx, day, prev_day)`` ranks on ``prev_day``'s bars, so the window
    day's bar is not in scope at the point the order is chosen. But nothing went
    red if that changed — and **a structural guarantee is not a pinned one**.
    The cheapest way to tell the two apart is to make today's bars disagree
    loudly with yesterday's and see which one the pick follows.

    The check is decisive rather than suggestive: on the window day ``600003``
    rises 9%, which clears ``LIMIT_UP_SKIP_PCT`` (9.5) and would win the ranking
    outright, while on the previous session it is the *weakest* of the three.
    If the decider ever ranked on the day it trades, the order would move to
    ``600003`` and this test would say so.
    """

    def test_a_name_that_only_leads_today_is_not_picked(self, tmp_path):
        series, instruments = _normal()
        series["600003"][_START] = _bar(open_=10.90, high=10.90, low=10.90,
                                        close=10.90, change_pct=9.0)
        replay, _ = _prepare(tmp_path, series, instruments)
        result = _run(replay)

        ordered = _ordered_codes(replay)
        assert ordered, "no order was placed, so this would prove nothing"
        assert ordered == {_PICKED}, (
            f"the decider ranked on the day it trades: it ordered {ordered}, "
            f"and 600003 is the name that leads on {_START}")
        assert _fills(result, "600003") == []


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


# ── 6. the universe as-of, on the side that chooses ─────────────────────────


def _listing_on(day: str) -> tuple[dict, dict]:
    """The three normal names plus a fourth whose corpus history starts ``day``.

    A code with no bar before ``day`` is what an IPO looks like in this
    corpus, because the first bar is the *only* evidence of listing that
    exists here: ``stocks.db`` holds one current row per code and this
    repository has no dated name table anywhere.
    """
    series, instruments = _normal()
    series["600009"] = _series([d for d in _SESSIONS if d >= day], close=10.0)
    instruments["600009"] = {"name": "丁"}
    return series, instruments


class TestASecurityThatHadNotListedCannotBeSelected:
    """M0's "universe 也要 as-of", on the side that chooses.

    Suspension was covered on the settlement side and ST by
    ``market_rules.listing_day_exemption``, but the *selection* side had no
    as-of at all — ``stocks.db`` holds the current name — so "a security that
    was not in the pool at the time must not be selected" had no test.

    The rule is ``first_bar < day``: strictly before, anchored on the decision
    day. It is one gate that both deciders call, and what makes the gate
    *reachable* is that both iterate the whole instrument table rather than the
    previous session's bars. A candidate set drawn from T-1 bars can only ever
    contain listed names, so a gate applied to it returns ``None`` for every
    row — measured, on the real corpus, as 0 of 1,986 not-yet-listed codes in a
    2020 window ever reaching it.

    The assertion is on the counter rather than on the order, deliberately.
    With a T-1 ranking rule a not-yet-listed name has no previous close, so it
    could not have been ranked anyway: "not selected" is guaranteed by
    construction and asserting it would be vacuous. The counter is what tells
    "excluded by the as-of rule" apart from "excluded by accident", and it is
    what goes to zero if the branch is deleted.
    """

    def test_a_name_listed_on_the_window_day_is_counted_not_listed(self, tmp_path):
        series, instruments = _listing_on(_START)
        replay, _ = _prepare(tmp_path, series, instruments)
        result = _run(replay)

        counters = result["ctx"].counters
        assert counters["eligibility:not_listed"] >= 1, (
            "a code whose first corpus session is the decision day was not "
            f"excluded by the as-of rule: {dict(counters)}")
        assert _ordered_codes(replay) == {_PICKED}, (
            "the ordinary names stopped being selectable, so this run is not "
            "measuring the rule at all")

    def test_the_boundary_is_the_decision_day_not_short_history(self, tmp_path):
        """Listed *today* is out; the next session it is in.

        The pair is the whole claim, and it is why the comparison is strict
        and anchored on ``day``: at 09:00 on D a name that listed on D has no
        previous close to rank or price against, while one that listed on T-1
        has both and is trading. An implementation that asked "does it have a
        long history?" would pass the first half and fail the second.
        """
        series, instruments = _listing_on(_START)
        replay, _ = _prepare(tmp_path, series, instruments)
        ctx = _run(replay)["ctx"]

        assert ctx.corpus.is_listed("600009", _START) is False
        assert walk_forward._eligibility(
            ctx, "600009", _START, _PREV) == "not_listed"

        after = _SESSIONS[22]
        assert ctx.corpus.is_listed("600009", after) is True, (
            "the name never becomes listed, so 'listed' is not being measured")
        assert walk_forward._eligibility(ctx, "600009", after, _START) is None, (
            "the gate kept refusing a name that has been trading since "
            "yesterday, so the boundary is not where the docstring says")


class TestTheDeclarationAndTheMeasurementMustAgree:
    """Both directions of one claim: what a run *says* it called a model.

    A placeholder that silently calls a model and a model decider that
    silently calls nothing are the same lie pointing opposite ways, and the
    second is the easier one to ship — an empty journal still produces a
    complete-looking report. The first version of this check compared
    ``journal_after > 0``, which a **correct** replay fails: a replay reads a
    journal that is already populated and adds nothing. It was found by
    running one; the record pass passed.
    """

    @staticmethod
    def _ctx(decider: str) -> argparse.Namespace:
        return argparse.Namespace(decider=decider)

    def test_a_placeholder_that_calls_a_model_is_refused(self):
        ok, detail = walk_forward._model_usage(
            self._ctx("placeholder"),
            {"mode": llm_journal.RECORD, "journal_before": 0, "journal_after": 3})
        assert ok is False and "3" in detail

    def test_a_record_pass_that_called_nothing_is_refused(self):
        ok, _ = walk_forward._model_usage(
            self._ctx("llm"),
            {"mode": llm_journal.RECORD, "journal_before": 0, "journal_after": 0})
        assert ok is False, "a record pass with an empty journal recorded nothing"

    def test_a_replay_adds_nothing_and_must_already_hold_something(self):
        ok, _ = walk_forward._model_usage(
            self._ctx("llm"),
            {"mode": llm_journal.REPLAY, "journal_before": 40, "journal_after": 40})
        assert ok is True, "a correct replay was reported as inconsistent"

        ok, _ = walk_forward._model_usage(
            self._ctx("llm"),
            {"mode": llm_journal.REPLAY, "journal_before": 0, "journal_after": 0})
        assert ok is False, "a replay of an empty journal answered from nothing"

    def test_a_live_model_run_cannot_be_checked_at_all(self):
        ok, detail = walk_forward._model_usage(
            self._ctx("llm"),
            {"mode": llm_journal.LIVE, "journal_before": 0, "journal_after": 0})
        assert ok is False and llm_journal.LIVE in detail

    def test_a_model_decider_under_live_is_refused_before_it_runs(self, monkeypatch):
        """Refused, not warned: under ``live`` two arms cannot be compared."""
        monkeypatch.setenv(llm_journal.MODE_ENV, llm_journal.LIVE)
        with pytest.raises(SystemExit):
            walk_forward._assert_model_mode("llm")
        walk_forward._assert_model_mode("placeholder")

        monkeypatch.setenv(llm_journal.MODE_ENV, llm_journal.RECORD)
        walk_forward._assert_model_mode("llm")
        monkeypatch.setenv(llm_journal.MODE_ENV, llm_journal.REPLAY)
        walk_forward._assert_model_mode("llm")


# ── 7. the learning step ────────────────────────────────────────────────────


class TestTheObservationIsAttributable:
    """M3's claim at both ends: the note cites episodes, and the citation reads back.

    The distillation is deliberately not a model's opinion of itself — the
    claim is a median of realised returns and the feature is re-read from the
    corpus — so what is testable is the **attribution**: that the episodes it
    cites are real ids, that both buckets are stated even when one is empty,
    and that ``candidates_citing`` can enumerate the note from an episode. A
    candidate whose evidence cannot be walked back to the trades it came from
    is prose with a foreign key in it.
    """

    @staticmethod
    def _ctx() -> argparse.Namespace:
        return argparse.Namespace(trader="pullback", entry_zone=(0.970, 1.005),
                                  window_start=_START)

    @staticmethod
    def _trades(pairs, *, first_episode: int = 101) -> list[dict]:
        """``(t1_change, realised_return)`` pairs, as ``_closed_trades`` shapes them."""
        return [{"position_id": i + 1, "code": f"6000{i + 1:02d}",
                 "episode_id": first_episode + i, "close_date": _START,
                 "return_pct": ret, "close_reason": "stop", "t1_change": chg}
                for i, (chg, ret) in enumerate(pairs)]

    def test_too_few_trades_writes_nothing(self):
        """Three is under the floor, and the floor is why a window can be silent."""
        before = len(LC.candidates_by_status())
        assert walk_forward._distil(self._ctx(), _START, self._trades(
            [(3.0, -1.0), (1.0, 2.0), (2.0, 1.0)])) is None
        assert len(LC.candidates_by_status()) == before

    def test_a_note_cites_the_episodes_it_came_from(self):
        ctx = self._ctx()
        # Bought high and lost, or bought low and won — all four support the
        # proposition, so the *opposing* list is empty and must say so.
        out = walk_forward._distil(ctx, _START, self._trades(
            [(5.0, -3.0), (4.0, -2.0), (0.5, 2.0), (0.2, 1.0)]))
        assert out is not None
        assert out["n"] == 4
        assert (out["supporting"], out["opposing"]) == (4, 0)
        assert out["high_median"] == -2.5 and out["low_median"] == 1.5

        stored = LC.get_candidate(out["candidate_id"])
        assert stored["status"] == LC.OBSERVATION, (
            "the pipeline must not promote: advance_candidate is the only writer")
        assert "n=4" in stored["claim"], stored["claim"]
        assert "n≥50" in stored["claim"], (
            "the claim has to name its own thinness, or a reader takes a "
            "four-trade observation for a finding")
        assert json.loads(stored["evidence_episode_ids"]) == {
            "supporting": [101, 102, 103, 104], "opposing": []}

        for episode_id in (101, 102, 103, 104):
            citing = LC.candidates_citing(episode_id)
            assert [c["id"] for c in citing] == [out["candidate_id"]]
            assert citing[0]["cited_as"] == ["supporting"]

    def test_opposing_evidence_is_stated_not_omitted(self):
        """§10: evidence that is only the cases that agree is an advertisement."""
        out = walk_forward._distil(self._ctx(), _START, self._trades(
            [(5.0, -3.0), (4.0, 2.0), (0.5, 2.0), (0.2, -1.0)]))
        assert out is not None
        assert out["supporting"] >= 1 and out["opposing"] >= 1

        stored = LC.get_candidate(out["candidate_id"])
        cited = json.loads(stored["evidence_episode_ids"])
        assert cited["supporting"] and cited["opposing"]
        assert LC.candidates_citing(102)[0]["cited_as"] == ["opposing"]

    def test_the_note_reaches_the_next_day_and_not_the_day_it_was_written(self):
        """The loop's last arrow, and its one lookahead guard.

        An observation written at the close of D has to be visible to the
        decision on D+1 and invisible on D. Without the second half, a re-run
        of a day could decide using knowledge distilled from that day's own
        result — which is the leak the whole as-of apparatus exists to stop.
        """
        ctx = self._ctx()
        out = walk_forward._distil(ctx, _START, self._trades(
            [(5.0, -3.0), (4.0, -2.0), (0.5, 2.0), (0.2, 1.0)]))
        assert out is not None

        assert walk_forward._knowledge_block(ctx, _START) == "", (
            "the day it was written can see it")

        block = walk_forward._knowledge_block(ctx, _SESSIONS[22])
        assert "【你自己的交易记录" in block
        assert "n=4" in block
        assert "#101" in block and "#104" in block, (
            f"the block did not name the episodes it rests on:\n{block}")


class TestASupportCountMustBeAboutReturnsNotGroupMembership:
    """The counts beside the medians have to be about returns, not the split.

    Measured on the 2025-07-01 window, where **all four** closed trades lost
    money. The first version tested ``(t1 > cut) != (return_pct > 0)``;
    ``return_pct > 0`` was ``False`` for every one of them, so the expression
    collapsed to ``t1 > cut`` and "2 笔支持、2 笔反对" was nothing but the two
    group sizes. It could not have disagreed with the medians printed beside
    it — which is precisely why it read as confirmation.

    It also filed half the citations the wrong way, including the window's
    best trade: the lowest T-1 change and the smallest loss (-4.96%), which
    it listed as *opposing* the proposition that trade supports.
    """

    #: The four closes of the 2025-07-01 window, as the corpus gave them up.
    _LOSSES = [(6.7633, -4.96), (8.4783, -10.01), (9.4047, -8.91), (9.1102, -6.84)]

    @staticmethod
    def _ctx() -> argparse.Namespace:
        return argparse.Namespace(trader="pullback", entry_zone=(0.970, 1.005),
                                  window_start=_START)

    @staticmethod
    def _trades(pairs, *, first_episode: int = 101) -> list[dict]:
        return [{"position_id": i + 1, "code": f"6000{i + 1:02d}",
                 "episode_id": first_episode + i, "close_date": _START,
                 "return_pct": ret, "close_reason": "stop", "t1_change": chg}
                for i, (chg, ret) in enumerate(pairs)]

    def test_a_window_where_every_trade_lost_still_separates_the_two_sides(self):
        out = walk_forward._distil(self._ctx(), _START, self._trades(self._LOSSES))
        assert out is not None
        assert out["n"] == 4
        # Also 2/2, so the counts alone cannot tell the implementations
        # apart — the membership is what differs.
        assert (out["supporting"], out["opposing"]) == (2, 2)

        cited = json.loads(
            LC.get_candidate(out["candidate_id"])["evidence_episode_ids"])
        # #101: up 6.76% on T-1 and the smallest loss. The proposition
        # holding, so it supports.
        assert 101 in cited["supporting"], (
            "the smallest loss on the lowest T-1 change supports "
            f"'up more, did worse'; got {cited}")
        # #104: the mirror image — up 9.11% on T-1, lost less than typical.
        assert 104 in cited["opposing"], cited

    def test_the_counts_are_not_simply_the_group_sizes(self):
        """A verdict that only mirrors the split is not evidence about returns.

        Both groups hold two trades, so a test that reduces to group
        membership reports 2/2 no matter what the returns did. Moving one
        return across the window median, leaving every T-1 change alone, has
        to move that trade's citation with it.
        """
        before = json.loads(LC.get_candidate(
            walk_forward._distil(self._ctx(), _START,
                                 self._trades(self._LOSSES))["candidate_id"]
        )["evidence_episode_ids"])

        moved = [(6.7633, -9.5), (8.4783, -10.01),
                 (9.4047, -8.91), (9.1102, -6.84)]
        after = json.loads(LC.get_candidate(
            walk_forward._distil(self._ctx(), _START,
                                 self._trades(moved))["candidate_id"]
        )["evidence_episode_ids"])

        assert 101 in before["supporting"], before
        assert 101 in after["opposing"], (
            f"a trade that fell from the best return to the worst kept its "
            f"side: {before} → {after}")

    def test_the_record_carries_each_trades_verdict(self):
        """The counts are re-derivable from the record, not taken on trust."""
        out = walk_forward._distil(self._ctx(), _START, self._trades(self._LOSSES))
        payload = json.loads(
            LC.get_candidate(out["candidate_id"])["payload_json"])
        assert len(payload["trades"]) == out["n"]
        for trade in payload["trades"]:
            assert isinstance(trade["supports"], bool), trade
        assert sum(t["supports"] for t in payload["trades"]) == out["supporting"]
        assert payload["median_return_pct"] == -7.875


class TestADecisionFailureCostsOnlyTheDecision:
    """Found by running a 180-day window against a rate-limited provider.

    Every ``429`` took the whole day down with it: the pending orders were
    never settled, the exits were never processed and no equity row was
    written, so the curve had a hole exactly where the market had a session.
    The two failures are different facts — "the model did not answer" and "the
    book did not settle" — and they shared one ``try``.
    """

    def test_a_decider_that_raises_still_settles_the_book(self, tmp_path, monkeypatch):
        series, instruments = _normal()
        replay, _ = _prepare(tmp_path, series, instruments)

        def _throttled(ctx, day, prev_day):
            raise RuntimeError("provider throttled")

        monkeypatch.setattr(walk_forward, "_run_decider", _throttled)
        result = _run(replay, days=2, keep_going=True)

        assert [e["stage"] for e in result["errors"]] == ["decide", "decide"]
        assert len(result["equity"]) == 2, (
            "the equity curve has a hole where the market had a session")
        assert len(result["settlement"]) == 2
        assert result["equity"][-1]["equity"] > 0
        # And the failure is named by stage, so "14 errors" cannot be read as
        # a broken settlement when every one of them was a throttled model.
        assert "decide:RuntimeError×2" in walk_forward._error_kinds(result["errors"])


class TestTheObservationIsWrittenOnlyWhenSomethingWasLearned:
    """The defect this pins was **measured, not imagined**.

    The first version distilled on every day, gated only on ``n >= 4``. On the
    first 180-day window that produced **11 candidates for 2 distinct facts**:
    once the fourth close landed, every following day wrote another copy of the
    same sentence, and every copy was *true* — which is why nothing else would
    have caught it. ``save_candidate`` cannot dedupe them either: ``source_date``
    is part of the fingerprint, so "the same observation on a later day" is by
    construction a different row. A candidate list that is mostly duplicates
    makes ``counts()`` meaningless and makes the loop look busier than it is.
    """

    @staticmethod
    def _ctx() -> argparse.Namespace:
        return argparse.Namespace(trader="pullback", entry_zone=(0.970, 1.005),
                                  window_start=_START, counters=Counter())

    @staticmethod
    def _one_close(close_date: str) -> list[dict]:
        return [{"position_id": 1, "code": "600001", "episode_id": 11,
                 "close_date": close_date, "return_pct": -1.0,
                 "close_reason": "stop", "t1_change": 3.0}]

    def _learn_with(self, monkeypatch, close_date: str) -> list[str]:
        calls: list[str] = []
        monkeypatch.setattr(walk_forward, "_closed_trades",
                            lambda ctx, day, conn: self._one_close(close_date))
        monkeypatch.setattr(
            walk_forward, "_distil",
            lambda ctx, day, trades: calls.append(day) or None)
        walk_forward._learn(self._ctx(), _START)
        return calls

    def test_a_day_with_no_close_does_not_distil_again(self, monkeypatch):
        assert self._learn_with(monkeypatch, _PREV) == [], (
            "a day on which nothing closed distilled again, which is how one "
            "observation becomes eleven")

    def test_a_day_that_closed_something_does(self, monkeypatch):
        """The companion, or the test above would pass against a dead loop."""
        assert self._learn_with(monkeypatch, _START) == [_START]


class TestTheLearningStepIsAFunctionOfTheDaysBook:
    """The new risk the loop introduced: it *writes* as it goes.

    A replay has to re-derive the same observations, not merely the same
    fills — an observation written from wall-clock state or from a stale
    handle would make the second pass differ while every CSV matched.

    Pinned with the placeholder decider, which calls no model, so this is
    about the runner's determinism and not the journal's. The journal's own
    record/replay is pinned call by call in ``test_llm_journal``, and the
    end-to-end model pass is demonstrated by running one over a real window
    rather than by this test.
    """

    def test_two_passes_label_and_distil_identically(self, tmp_path, monkeypatch):
        monkeypatch.setenv(llm_journal.MODE_ENV, llm_journal.REPLAY)
        series, instruments = _normal()

        replay, corpus = _prepare(tmp_path, series, instruments)
        first = _run(replay, days=5)
        assert first["learning"], "the loop did not run, so this proves nothing"

        _forget_open_handles()
        walk_bootstrap.bootstrap(replay, corpus, force=True)
        second = _run(replay, days=5)

        assert second["learning"] == first["learning"]
        assert (walk_forward._learning_meta(second["learning"])
                == walk_forward._learning_meta(first["learning"]))
        # What the run *says* it wrote and what the table holds are one fact.
        assert len(LC.candidates_by_status(LC.OBSERVATION)) == len(
            [row for row in second["learning"] if row["distilled"]]), (
            "the candidate table and the run's own record of what it wrote "
            "disagree, so one of the two is not the loop's output")


# ── the agent's sell side: ordering is the contract ─────────────────────────


class TestTheHardStopIsOutsideTheAgentsReach:
    """The one ordering in this runner that protects money.

    `_settle_exits` runs first and closes anything that gapped through its
    stop; `_agent_exits` is asked about survivors only. Swap them and a model
    that answers "hold" keeps a position the risk line exists to cut. So the
    position the agent is *shown* must never include one the stop took, and
    this is asserted on the day loop rather than on `_agent_exits` alone —
    the ordering lives in the loop, not in the function.
    """

    def test_the_exit_step_runs_after_the_mechanical_settlement(
            self, tmp_path, monkeypatch):
        calls: list[str] = []
        real_settle = walk_forward._settle_exits
        real_agent = walk_forward._agent_exits

        def _settle(ctx, day):
            calls.append(f"settle:{day}")
            return real_settle(ctx, day)

        def _agent(ctx, day):
            calls.append(f"agent:{day}")
            return real_agent(ctx, day)

        monkeypatch.setattr(walk_forward, "_settle_exits", _settle)
        monkeypatch.setattr(walk_forward, "_agent_exits", _agent)
        # A model object, so the placeholder-decider guard does not skip the
        # step: this test is about order, not about the model.
        monkeypatch.setattr(walk_forward.Context, "model",
                            object(), raising=False)

        series, instruments = _normal()
        replay, _ = _prepare(tmp_path, series, instruments)
        _run(replay, days=2, agent_exits=True)

        assert calls, "neither settlement nor the exit step ran"
        for day in {c.split(":")[1] for c in calls}:
            assert calls.index(f"settle:{day}") < calls.index(f"agent:{day}"), (
                f"on {day} the agent was asked about positions before the "
                "mechanical stop had closed them")

    def test_a_placeholder_decider_skips_the_exit_step(self, tmp_path):
        """`ctx.model` is None without `--decider llm`, so the exit step must
        refuse rather than let `decide()` build an unjournaled live client."""
        series, instruments = _normal()
        replay, _ = _prepare(tmp_path, series, instruments)
        result = _run(replay, days=2, agent_exits=True, decider="placeholder")
        assert result["ctx"].counters.get("agent_exit_calls", 0) == 0

    def test_the_flag_defaults_to_off(self):
        """It doubles the run's model calls, so it is a choice the operator
        made rather than a surprise on the bill."""
        assert _args().agent_exits is False


class TestTheAgentsOwnExitsReachTheReport:
    """They were dropped, and the one number a reader checks was the one
    wrong.

    `_agent_exits` built its fill rows correctly and the book was updated, so
    the trades really happened — the database carried
    `close_reason: agent卖出: ...` and the realised P&L included them. But
    the day loop only merged the `entries` and `exits` buckets into
    `fill_rows`, so the agent's own sells never reached the report. A run
    whose agent sold twice printed **卖出 0 笔**.

    A reader answering "does it sell when nothing else will" looks at exactly
    that line, so this is the worst place for a silent omission.
    """

    def _run_with_fake_agent_sell(self, tmp_path, monkeypatch):
        """One window where the agent sells everything it is shown.

        The decider is stubbed rather than sampled, so the test measures the
        plumbing — does an agent sell reach the report — and not the model.
        """
        import alpha_agents.pipeline.tasks.exit_decision as ED

        async def _fake_decide(positions, price_map, **kw):
            return [{"code": p["code"], "action": "sell", "reason": "实验"}
                    for p in positions]

        monkeypatch.setenv("ALPHAAGENTS_LLM_MODE", "replay-recorded")
        monkeypatch.setattr(ED, "decide_for_replay", _fake_decide)
        monkeypatch.setattr(ED, "decide", _fake_decide)
        # `--decider llm` builds a real client from the environment, which a
        # test has no credentials for. The buy side is stubbed out too, so
        # the only thing under test is whether an agent sell reaches the
        # report.
        series, instruments = _normal()
        replay, _ = _prepare(tmp_path, series, instruments)

        # The buy side is stubbed to nothing, so the position the agent will
        # sell is planted directly. It is opened on the session *before* the
        # window so T+1 does not hide it.
        original = walk_forward._run_decider

        def _plant_then_decide(ctx, day, prev_day):
            from alpha_agents.data import portfolio as P
            if not P.get_open_positions(ctx.trader):
                P.create_pending_order(
                    code="600001", name="甲", theme=ctx.theme,
                    order_date=prev_day, entry_low=9.0, entry_high=11.0,
                    stop_loss=8.0, source="test", reason="planted",
                    trader_id=ctx.trader)
                settled = walk_forward._settle_entries(
                    ctx, prev_day, P.get_pending_orders(ctx.trader))
                assert settled["fills"], "the planted order did not fill"
            return []

        monkeypatch.setattr(walk_forward, "_run_decider", _plant_then_decide)
        monkeypatch.setattr(walk_forward, "_build_model",
                            lambda timeout=None: object())
        return _run(replay, days=3, agent_exits=True, decider="llm",
                    panel_size=5)

    def test_an_agent_sell_appears_in_fills(self, tmp_path, monkeypatch):
        result = self._run_with_fake_agent_sell(tmp_path, monkeypatch)
        agent_sells = [f for f in result["fills"]
                       if str(f.get("reason", "")).startswith("agent")]
        assert agent_sells, (
            "the agent sold but no fill carries an agent reason: the third "
            "bucket was dropped before the report")

    def test_the_report_attributes_it_to_the_agent(self, tmp_path, monkeypatch):
        result = self._run_with_fake_agent_sell(tmp_path, monkeypatch)
        line = " ".join(wf_exit_attribution(result))
        assert "agent" in line
        assert "agent 0 笔" not in line, (
            "the attribution says the agent sold nothing while its fills are "
            "present")
