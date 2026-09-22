"""``_write_lock`` is not reentrant, and re-entering it is silent.

The thread stops on the second acquire. Nothing raises, nothing logs, the
process stays alive at 0% CPU and its own SQLite file is locked against it —
which reads exactly like a hung network call. Two 2026-09-22 replay runs died
this way; the first was misdiagnosed from the source, because by the time
anyone looked the stack was gone.

Two tests, because there are two things to keep: the path that broke, and
the sweep that finds the next one.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from alpha_agents.data import memory_store, thesis as thesis_data
from alpha_agents.data.memory_store import _write_lock
from alpha_agents.data import portfolio as P
from alpha_agents.data import trader as TR

REPO = Path(__file__).resolve().parents[1]

#: Long enough that a cold interpreter start on a slow machine does not fail
#: it, short enough that a real deadlock does not hold the suite. A deadlock
#: never finishes, so any finite bound separates the two.
CHILD_TIMEOUT = 120.0


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
    (d / "slow.yaml").write_text("id: slow\nname: slow\ncapital: 500000\n",
                                 encoding="utf-8")
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


#: The cancel runs in a child process, not a thread. A deadlocked thread
#: cannot be killed and never releases ``_write_lock``, so a thread-based
#: version of this test does not merely fail — it holds the lock for the rest
#: of the session and every later test that writes hangs behind it. The
#: failure being tested for is exactly the one that makes in-process
#: detection unsafe.
_CHILD = r"""
import json, os, sys
from pathlib import Path
from unittest.mock import patch

from alpha_agents.data import portfolio as P, thesis as thesis_data, trader as TR

TR.TRADERS_DIR = Path(os.environ["PROBE_TRADERS"])
row = {"name": "t", "status": "active", "strength": 6,
       "daily_score": 1, "core_stocks": "[]"}
with patch("alpha_agents.data.memory_store.get_active_themes", return_value=[row]), \
     patch("alpha_agents.data.memory_store.get_theme_by_name", return_value=row), \
     patch("alpha_agents.data.portfolio.get_theme_by_name", return_value=row), \
     patch("alpha_agents.data.sentiment_cycle.get_sentiment_cycle",
           return_value={"phase": "fix", "strategy": {"max_exposure_pct": 100}}):
    tid = thesis_data.create(thesis_data.Thesis(
        code="600000", name="A", theme="t", claim="flow in", trader_id="slow"))
    oid = P.create_pending_order(
        code="600000", name="A", theme="t", order_date="2026-01-05",
        entry_low=9.0, entry_high=11.0, stop_loss=8.5, source="morning",
        reason="flow in", trader_id="slow", thesis_id=tid)
    assert oid, "no order, no test"
    P.cancel_order(oid, "theme weakened")
    print(json.dumps({"status": thesis_data.get_by_id(tid).status}))
"""


def _cancel_in_a_child(tmp_path):
    """Returns (timed_out, parsed_stdout, completed_process)."""
    traders = tmp_path / "traders"
    traders.mkdir(exist_ok=True)
    (traders / "slow.yaml").write_text("id: slow\nname: slow\ncapital: 500000\n",
                                       encoding="utf-8")
    env = dict(os.environ,
               ALPHAAGENTS_DATA_DIR=str(tmp_path / "data"),
               PROBE_TRADERS=str(traders))
    (tmp_path / "data").mkdir(exist_ok=True)
    try:
        done = subprocess.run([sys.executable, "-c", _CHILD], env=env,
                              capture_output=True, text=True, cwd=REPO,
                              timeout=CHILD_TIMEOUT)
    except subprocess.TimeoutExpired:
        return True, None, None
    out = done.stdout.strip().splitlines()
    parsed = json.loads(out[-1]) if done.returncode == 0 and out else None
    return False, parsed, done


class TestTheLockIsNotReentrant:
    def test_a_second_acquire_on_one_thread_blocks(self):
        """The property everything below rests on, stated rather than assumed."""
        with _write_lock:
            assert _write_lock.acquire(timeout=0.2) is False


class TestTheFillLoopDoesNotHoldTheLock:
    """The fact the first diagnosis got wrong, as an assertion.

    ``check_pending_orders`` was read as holding ``_write_lock`` across its
    whole loop, and a fix was built on that reading: the fill-time thesis
    check was switched to ``close_unlocked``. The lock is in fact taken only
    around the reservation repair — so that "fix" wrote to the book with no
    lock held, and the test written with it asserted the same wrong thing,
    which is why the real deadlock kept its cover.

    Asserted behaviourally rather than by reading the source, because
    reading the source is what failed.
    """

    def test_the_thesis_check_runs_with_the_lock_free(
            self, store, traders_dir, theme, monkeypatch):
        seen = {}

        def probe(code, price, order):
            got = _write_lock.acquire(timeout=0.2)
            seen["free"] = got
            if got:
                _write_lock.release()
            return None

        oid = P.create_pending_order(
            code="600000", name="A", theme="t", order_date="2026-01-05",
            entry_low=9.0, entry_high=11.0, stop_loss=8.5, source="morning",
            reason="主线在流入", trader_id="slow")
        assert oid
        monkeypatch.setattr(P, "_thesis_already_broken", probe)
        P.check_pending_orders({"600000": 10.0}, "2026-01-05",
                               trader_id="slow")

        assert seen.get("free") is True, (
            "the fill loop held _write_lock — then thesis_already_broken must "
            "use close_unlocked, and this test is the one that is wrong")


class TestBothClosesStillWork:
    def test_close_takes_the_lock_for_callers_outside_it(self):
        import inspect
        assert "with _write_lock" in inspect.getsource(thesis_data.close)

    def test_close_unlocked_does_not(self):
        import inspect
        src = inspect.getsource(thesis_data.close_unlocked)
        assert "with _write_lock" not in src

    def test_both_refuse_a_status_that_is_not_a_close(self):
        with pytest.raises(ValueError):
            thesis_data.close_unlocked(1, "made_up")
        with pytest.raises(ValueError):
            thesis_data.close(1, "made_up")


class TestCancellingAnOrderThatCarriesAThesis:
    """The path that deadlocked on day 2 of the 2026-09-22 replay.

    ``_cancel_order_impl`` takes the lock, and the cancel closes the thesis
    the order was serving — ``_close_unfilled_thesis``, added days earlier so
    that a claim whose order never filled would be booked as ``unfilled``
    rather than left ``active`` forever. It called ``thesis.close``, which
    takes the same lock.

    The existing cancel tests all passed, because none of their orders
    carried a thesis: ``_close_unfilled_thesis`` returned at the NULL check
    one line before the deadlock.
    """

    def test_the_cancel_returns_and_books_the_thesis_unfilled(self, tmp_path):
        timed_out, parsed, done = _cancel_in_a_child(tmp_path)
        assert not timed_out, (
            "the cancel never returned: _write_lock was taken twice on one "
            "thread. This is the 2026-09-22 stall.")
        assert done.returncode == 0, done.stderr[-2000:]
        # The deadlock was inside the step that books the thesis, so a cancel
        # that returns without booking it would pass the timeout and still be
        # the bug.
        assert parsed == {"status": "unfilled"}, done.stdout + done.stderr[-2000:]


class TestTheSweepThatFindsTheNextOne:
    """A silent failure mode needs a mechanical check, not a careful reader.

    The first sweep for this matched direct calls to lock-taking functions,
    and the chain that broke was three hops long, so it reported nothing.
    """

    def _harness(self, *paths):
        return subprocess.run(
            [sys.executable, str(REPO / "scripts" / "lint_harness.py"), *paths],
            capture_output=True, text=True, cwd=REPO)

    def test_the_package_is_clean(self):
        out = self._harness()
        assert "lock-reentry" not in out.stdout, out.stdout

    def test_it_follows_a_chain_it_cannot_see_in_one_hop(self, tmp_path):
        """Written as three files so that a one-module check cannot pass it."""
        pkg = REPO / "alpha_agents" / "_lock_reentry_probe"
        pkg.mkdir(exist_ok=True)
        try:
            (pkg / "__init__.py").write_text("", encoding="utf-8")
            (pkg / "deep.py").write_text(
                "from alpha_agents.data.memory_store import _write_lock\n\n\n"
                "def takes_it():\n    with _write_lock:\n        pass\n",
                encoding="utf-8")
            (pkg / "middle.py").write_text(
                "from alpha_agents._lock_reentry_probe.deep import takes_it\n\n\n"
                "def wrapper():\n    takes_it()\n",
                encoding="utf-8")
            (pkg / "top.py").write_text(
                "from alpha_agents.data.memory_store import _write_lock\n"
                "from alpha_agents._lock_reentry_probe.middle import wrapper\n\n\n"
                "def holder():\n    with _write_lock:\n        wrapper()\n",
                encoding="utf-8")
            out = self._harness()
            assert "lock-reentry" in out.stdout, out.stdout
            assert "top.py" in out.stdout
            assert "wrapper → takes_it" in out.stdout, out.stdout
        finally:
            for f in pkg.glob("*.py"):
                f.unlink()
            pkg.rmdir()
