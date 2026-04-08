"""Trading-day-aware task scheduler.

Replaces NewsMonitor's simple polling loop with a scheduler that dispatches
different analysis tasks at different times throughout the trading day.

Non-trading days (weekends, holidays) only run overnight scans.
"""

import asyncio
import logging
from datetime import datetime, time as dtime, timedelta
from typing import Callable, Awaitable

import akshare as ak

from alpha_agents.config import no_proxy

logger = logging.getLogger(__name__)


def _is_trading_day(date: datetime | None = None) -> bool:
    """Check if a date is a trading day using akshare calendar."""
    try:
        with no_proxy():
            df = ak.tool_trade_date_hist_sina()
        date = date or datetime.now()
        trade_dates = set(df["trade_date"].astype(str).values)
        # Try both formats: YYYY-MM-DD and YYYYMMDD
        date_a = date.strftime("%Y-%m-%d")
        date_b = date.strftime("%Y%m%d")
        return date_a in trade_dates or date_b in trade_dates
    except Exception as e:
        logger.warning("Failed to check trading calendar, using weekday fallback: %s", e)
        return (date or datetime.now()).weekday() < 5


class Task:
    """A scheduled task with a time window and run conditions."""

    def __init__(
        self,
        name: str,
        run_fn: Callable[..., Awaitable[None]],
        run_at: dtime,
        *,
        end_at: dtime | None = None,
        interval_minutes: int | None = None,
        trading_day_only: bool = True,
    ):
        self.name = name
        self.run_fn = run_fn
        self.run_at = run_at
        self.end_at = end_at
        self.interval_minutes = interval_minutes
        self.trading_day_only = trading_day_only
        self._last_run: datetime | None = None

    def should_run(self, now: datetime, is_trading: bool) -> bool:
        """Check if this task should run at the given time."""
        if self.trading_day_only and not is_trading:
            return False

        current_time = now.time()

        if self.end_at and self.interval_minutes:
            if not (self.run_at <= current_time <= self.end_at):
                return False
            if self._last_run:
                elapsed = (now - self._last_run).total_seconds() / 60
                return elapsed >= self.interval_minutes
            return True
        else:
            if self._last_run and self._last_run.date() == now.date():
                return False
            scheduled = now.replace(hour=self.run_at.hour, minute=self.run_at.minute, second=0)
            return 0 <= (now - scheduled).total_seconds() < 300


class TradingDayScheduler:
    """Main scheduler that dispatches tasks based on trading day schedule."""

    def __init__(self, event_bus=None):
        self._tasks: list[Task] = []
        self._bus = event_bus
        self._running = False
        self._trading_day_cache: dict[str, bool] = {}

    def add_task(self, task: Task) -> None:
        """Register a task with the scheduler."""
        self._tasks.append(task)
        logger.info("Registered task: %s at %s", task.name, task.run_at)

    def is_trading_day(self, date: datetime | None = None) -> bool:
        """Check trading day with daily cache."""
        date = date or datetime.now()
        key = date.strftime("%Y%m%d")
        if key not in self._trading_day_cache:
            self._trading_day_cache[key] = _is_trading_day(date)
        return self._trading_day_cache[key]

    async def run(self) -> None:
        """Main scheduler loop. Checks every 30 seconds which tasks should run."""
        self._running = True
        is_trading = self.is_trading_day()
        logger.info("Trading day scheduler started (today is %s)",
                     "trading day" if is_trading else "non-trading day")

        # Show active themes on startup
        try:
            from alpha_agents.data.memory_store import get_active_themes
            import json as _json
            themes = get_active_themes()
            if themes:
                logger.info("Active themes (%d):", len(themes))
                for t in themes:
                    stocks = _json.loads(t["core_stocks"]) if t["core_stocks"] else []
                    leader = next((s["name"] for s in stocks if s.get("role") == "龙头"), "无")
                    logger.info("  %s (strength=%d, %s) leader=%s",
                                t["name"], t["strength"], t["status"], leader)
            else:
                logger.info("No active themes yet")
        except Exception:
            pass

        while self._running:
            now = datetime.now()
            is_trading = self.is_trading_day(now)

            for task in self._tasks:
                if task.should_run(now, is_trading):
                    logger.info("Running task: %s", task.name)
                    try:
                        await task.run_fn()
                        task._last_run = datetime.now()
                        logger.info("Task %s completed", task.name)
                    except Exception:
                        logger.exception("Task %s failed", task.name)
                        task._last_run = datetime.now()

            await asyncio.sleep(30)

    def stop(self) -> None:
        """Stop the scheduler."""
        self._running = False
