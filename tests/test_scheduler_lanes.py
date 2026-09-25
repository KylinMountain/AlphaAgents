"""Scheduler safety-lane contracts for M2-C.

A long research/model call must not own the scheduler's control loop.  Work in
one lane stays serialized, while account checks can make progress independently.
"""

import asyncio
from datetime import time as dtime

import pytest

from alpha_agents.pipeline import scheduler as S


def _quiet(scheduler, tmp_path, monkeypatch):
    scheduler._STATE_FILE = tmp_path / "scheduler-state.json"
    monkeypatch.setattr(S, "log_activity", lambda *a, **k: None)


def test_task_rejects_unknown_lane():
    async def noop():
        return None

    with pytest.raises(ValueError, match="Unknown task lane"):
        S.Task("bad", noop, dtime(0, 0), lane="anything")


def test_research_does_not_block_account_lane(tmp_path, monkeypatch):
    async def scenario():
        scheduler = S.TradingDayScheduler()
        _quiet(scheduler, tmp_path, monkeypatch)
        research_started = asyncio.Event()
        release_research = asyncio.Event()
        account_done = asyncio.Event()

        async def research():
            research_started.set()
            await release_research.wait()

        async def account():
            account_done.set()

        slow = S.Task("slow-research", research, dtime(0, 0),
                      lane="research", timeout_seconds=2)
        critical = S.Task("book-check", account, dtime(0, 0),
                          lane="account", timeout_seconds=2)

        assert scheduler._dispatch(slow)
        await asyncio.wait_for(research_started.wait(), timeout=0.5)
        assert scheduler._dispatch(critical)

        # This is the M2-C contract: the account check completes while the
        # research task is deliberately still blocked.
        await asyncio.wait_for(account_done.wait(), timeout=0.5)
        assert not scheduler._inflight["slow-research"].done()

        release_research.set()
        await scheduler._drain_inflight()

    asyncio.run(scenario())


def test_same_lane_is_serialized(tmp_path, monkeypatch):
    async def scenario():
        scheduler = S.TradingDayScheduler()
        _quiet(scheduler, tmp_path, monkeypatch)
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        second_started = asyncio.Event()

        async def first():
            first_started.set()
            await release_first.wait()

        async def second():
            second_started.set()

        one = S.Task("research-one", first, dtime(0, 0),
                     lane="research", timeout_seconds=2)
        two = S.Task("research-two", second, dtime(0, 0),
                     lane="research", timeout_seconds=2)

        scheduler._dispatch(one)
        await asyncio.wait_for(first_started.wait(), timeout=0.5)
        scheduler._dispatch(two)
        await asyncio.sleep(0)
        assert not second_started.is_set(), "research jobs must not overlap"

        release_first.set()
        await asyncio.wait_for(second_started.wait(), timeout=0.5)
        await scheduler._drain_inflight()

    asyncio.run(scenario())


def test_duplicate_tick_does_not_queue_second_copy(tmp_path, monkeypatch):
    async def scenario():
        scheduler = S.TradingDayScheduler()
        _quiet(scheduler, tmp_path, monkeypatch)
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def work():
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()

        task = S.Task("intraday", work, dtime(0, 0),
                      lane="account", timeout_seconds=2)
        assert scheduler._dispatch(task)
        await asyncio.wait_for(started.wait(), timeout=0.5)
        assert not scheduler._dispatch(task)
        assert calls == 1

        release.set()
        await scheduler._drain_inflight()
        assert calls == 1

    asyncio.run(scenario())


def test_catch_up_admits_without_waiting_for_research(tmp_path, monkeypatch):
    async def scenario():
        scheduler = S.TradingDayScheduler()
        _quiet(scheduler, tmp_path, monkeypatch)
        started = asyncio.Event()
        release = asyncio.Event()

        async def work():
            started.set()
            await release.wait()

        scheduler.add_task(S.Task(
            "missed-research", work, dtime(0, 0),
            trading_day_only=False, catch_up_grace_minutes=None,
            lane="research", timeout_seconds=2,
        ))

        # _catch_up_missed used to await the whole task. It must now only
        # admit it, so startup can continue to account-critical scheduling.
        await asyncio.wait_for(scheduler._catch_up_missed(False), timeout=0.5)
        await asyncio.wait_for(started.wait(), timeout=0.5)
        assert not scheduler._inflight["missed-research"].done()

        release.set()
        await scheduler._drain_inflight()

    asyncio.run(scenario())
