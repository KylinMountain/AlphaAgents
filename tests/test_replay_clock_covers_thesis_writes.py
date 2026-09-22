"""Everything a replayed day writes must be dated inside that day.

``_check_theses`` ran outside ``replay_as_of``. Every write it reached
therefore stamped the wall clock: ``close_position`` calls
``clock.today()``, which is replay-aware and so returned the real date;
``thesis.close`` and ``add_checkpoint`` use ``datetime.now()`` directly.

The damage is legible in a real run. Eight theses closed in a 2026-01
window all carry ``close_date = 2026-09-22``, the day the replay was
executed, while ``holding_days`` is correctly 0, 2 or 3. Same row, two
timelines.

Cosmetic is exactly what it is not:

* ``portfolio_report`` selects a session's exits with ``WHERE close_date
  = ?`` and would find none of them on their own day;
* ``scoring.excess_over_market`` takes the window from ``open_date`` to
  ``close_date``, so a same-day round trip would be graded against eight
  months of market return;
* a reviewer reading checkpoints sees today's date on a January event.
"""

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import walk_forward as wf  # noqa: E402


class TestTheThesisCheckIsDatedByTheReplay:
    def test_check_all_runs_inside_replay_as_of(self):
        lines = inspect.getsource(wf._check_theses).splitlines()
        # The call, not the docstring's mention of it.
        call = next(i for i, ln in enumerate(lines)
                    if "result = thesis_monitor.check_all" in ln)
        opened = next((i for i in range(call - 1, max(-1, call - 4), -1)
                       if "replay_as_of" in lines[i]), None)
        assert opened is not None, (
            "check_all writes close_date, checkpoints and thesis closes; "
            "outside the block they all take the wall-clock date")
        indent = len(lines[call]) - len(lines[call].lstrip())
        assert indent > len(lines[opened]) - len(lines[opened].lstrip()), (
            "the call must sit inside the block, not merely after it")

    def test_the_stamp_is_the_replayed_day(self):
        src = inspect.getsource(wf._check_theses)
        assert 'replay_as_of(f"{day}' in src, (
            "the block must be opened on the replayed day, not on a constant")


class TestTheAgentExitPathWasAlreadyCovered:
    """Stated so a future reader does not 'fix' the one that was right."""

    def test_agent_exits_are_wrapped_by_their_caller(self):
        src = inspect.getsource(wf)
        block = src.split("agent_exits = []")[1][:900]
        assert "replay_as_of(stamp)" in block
        assert "_agent_exits(ctx, day, phase)" in block


class TestTheFieldsThatDependOnIt:
    def test_the_grader_takes_its_window_from_the_two_dates(self):
        from alpha_agents.data import portfolio_exit
        src = inspect.getsource(portfolio_exit)
        assert 'pos["close_date"]' in src, (
            "excess_over_market reads close_date; a wall-clock value here "
            "measures the trade against months it was never in")

    def test_the_daily_report_selects_exits_by_close_date(self):
        from alpha_agents.data import portfolio_report
        src = inspect.getsource(portfolio_report)
        assert "WHERE close_date = ?" in src
