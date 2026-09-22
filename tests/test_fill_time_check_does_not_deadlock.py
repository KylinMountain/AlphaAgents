"""The fill-time thesis check runs inside the write lock, so it must not take it.

``portfolio.check_pending_orders`` holds ``memory_store._write_lock`` across
its whole fill loop and asks ``portfolio_entry.thesis_already_broken``
whether the claim survived the wait. That check closes the thesis when it
did not — and ``thesis.close`` took the same lock. ``_write_lock`` is a
plain ``threading.Lock``: a second acquire on the same thread blocks
forever.

It was latent for as long as the check could not fire. The view carried
price and the theme table only, so every flow-dependent condition was
skipped and the close was never reached. Supplying the flow made
``theme_flow_negative`` fire at the door for the first time, and the next
replay stalled on day one — process alive, 0% CPU, no network, four write
handles open on its own database, 103 minutes of silence. No crash, no log
line, nothing to grep for. The same shape as every other defect this day
turned up: it looked like nothing was happening because nothing was.

``portfolio_book._cancel_order_unlocked`` already existed for exactly this,
which is why the fix is the same shape rather than a new idea.
"""

import threading
from unittest.mock import patch

import pytest

from alpha_agents.data import portfolio_entry as PE
from alpha_agents.data import thesis as T
from alpha_agents.data.memory_store import _write_lock


def _order():
    return {"theme": "存储芯片", "entry_low": 40.0, "entry_high": 41.0,
            "trader_id": "pullback"}


def _thesis():
    th = T.Thesis(code="300475", name="香农芯创", theme="存储芯片",
                  conditions=[T.Condition("theme_flow_negative", 30.0, "资金破裂")])
    th.id = 5
    th.position_id = None
    return th


class TestTheLockIsNotReentrant:
    def test_a_second_acquire_on_one_thread_blocks(self):
        """The property the whole fix rests on, stated rather than assumed."""
        with _write_lock:
            assert _write_lock.acquire(timeout=0.2) is False


class TestTheCheckDoesNotTakeTheLock:
    def test_it_closes_without_deadlocking_inside_the_lock(self):
        done = threading.Event()
        result = {}

        def run():
            with _write_lock:                     # what the fill loop holds
                with patch.object(T, "get_active", return_value=[_thesis()]), \
                     patch.object(T, "close_unlocked") as closed, \
                     patch.object(PE, "get_theme_by_name", return_value=None), \
                     patch("alpha_agents.data.theme_state.concept_state",
                           return_value=({"存储芯片": 348},
                                         {"存储芯片": -72.7})):
                    result["fired"] = PE.thesis_already_broken(
                        "300475", 40.5, _order())
                    result["closed"] = closed.called
            done.set()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        assert done.wait(5), "the fill-time check deadlocked inside the lock"
        assert result["fired"] is not None
        assert result["closed"], "a broken claim must still be resolved"

    def test_it_uses_the_unlocked_close(self):
        import inspect
        src = inspect.getsource(PE.thesis_already_broken)
        assert "close_unlocked" in src
        assert "thesis_data.close(" not in src


class TestBothClosesStillWork:
    def test_close_takes_the_lock_for_callers_outside_it(self):
        import inspect
        assert "with _write_lock" in inspect.getsource(T.close)

    def test_close_unlocked_does_not(self):
        import inspect
        assert "with _write_lock" not in inspect.getsource(T.close_unlocked)

    def test_both_refuse_a_status_that_is_not_a_close(self):
        with pytest.raises(ValueError):
            T.close_unlocked(1, "made_up")
        with pytest.raises(ValueError):
            T.close(1, "made_up")
