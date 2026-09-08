"""Trading-day-aware task scheduler.

Replaces NewsMonitor's simple polling loop with a scheduler that dispatches
different analysis tasks at different times throughout the trading day.

Non-trading days (weekends, holidays) only run overnight scans.
"""

import asyncio
import json as _json_mod
import logging
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Callable, Awaitable

import akshare as ak

from alpha_agents.config import no_proxy, DATA_DIR
from alpha_agents.data.activity_log import log_activity

logger = logging.getLogger(__name__)


# A task repeating this often is plumbing, not news: its start/finish
# rows are noise in a feed a person reads. 15 min keeps news_ingest (5)
# and intraday_monitor (5) quiet while leaving every scheduled report —
# all of which run once or twice a day — fully logged.
HIGH_FREQUENCY_MINUTES = 15


def _is_high_frequency(task) -> bool:
    interval = getattr(task, "interval_minutes", None)
    return bool(interval and interval <= HIGH_FREQUENCY_MINUTES)


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
        logger.warning("Trading calendar API failed — falling back to weekday check. "
                       "This may incorrectly treat Chinese holidays as trading days: %s", e)
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
        weekday: int | None = None,
        timeout_seconds: int = 600,
        catch_up_grace_minutes: int | None = 15,
    ):
        self.name = name
        self.run_fn = run_fn
        self.run_at = run_at
        self.end_at = end_at
        self.interval_minutes = interval_minutes
        self.trading_day_only = trading_day_only
        self._last_run: datetime | None = None
        # Weekday constraint: 0=Monday … 6=Sunday. None means any day.
        self.weekday = weekday
        # Hard ceiling on a single run. Prevents one wedged external call
        # (DNS, baostock fetch, hung LLM tool) from freezing the scheduler.
        self.timeout_seconds = timeout_seconds
        # One-shot tasks may be caught up after restart only inside this
        # window. Stale catch-up would run "morning" logic with afternoon data.
        self.catch_up_grace_minutes = catch_up_grace_minutes
        # Dynamic interval boost — used to shorten polling on anomaly detection
        self._boost_until: datetime | None = None
        self._boost_interval: int = 2  # minutes to use while boosted

    def boost(self, minutes: int = 15) -> None:
        """Temporarily shorten the polling interval for *minutes* minutes."""
        self._boost_until = datetime.now() + timedelta(minutes=minutes)
        logger.info("Task %s boosted for %d min (interval → %d min)",
                     self.name, minutes, self._boost_interval)

    def should_run(self, now: datetime, is_trading: bool) -> bool:
        """Check if this task should run at the given time."""
        if self.trading_day_only and not is_trading:
            return False

        # Weekday gate (e.g. weekly report only on Saturday)
        if self.weekday is not None and now.weekday() != self.weekday:
            return False

        current_time = now.time()

        if self.end_at and self.interval_minutes:
            if not (self.run_at <= current_time <= self.end_at):
                return False
            if self._last_run:
                # Use boosted interval if active
                interval = self.interval_minutes
                if self._boost_until and now < self._boost_until:
                    interval = self._boost_interval
                elapsed = (now - self._last_run).total_seconds() / 60
                return elapsed >= interval
            return True
        else:
            # One-shot task: run once per day, at or after scheduled time
            if self._last_run and self._last_run.date() == now.date():
                return False
            scheduled = now.replace(hour=self.run_at.hour, minute=self.run_at.minute, second=0)
            # Trigger if we're past the scheduled time (handles sleep/resume)
            return (now - scheduled).total_seconds() >= 0


class TradingDayScheduler:
    """Main scheduler that dispatches tasks based on trading day schedule."""

    _STATE_FILE = DATA_DIR / "scheduler_state.json"

    def __init__(self, event_bus=None, on_task_output=None):
        self._tasks: list[Task] = []
        self._bus = event_bus
        self._running = False
        self._trading_day_cache: dict[str, bool] = {}
        self._on_task_output = on_task_output  # Callback: (task_name, output_text) -> None

    def add_task(self, task: Task) -> None:
        """Register a task with the scheduler."""
        self._tasks.append(task)
        logger.info("Registered task: %s at %s", task.name, task.run_at)

    def _load_state(self) -> None:
        """Restore task _last_run from disk so restarts don't re-run today's tasks."""
        try:
            if self._STATE_FILE.exists():
                state = _json_mod.loads(self._STATE_FILE.read_text(encoding="utf-8"))
                today = datetime.now().strftime("%Y-%m-%d")
                for task in self._tasks:
                    ts = state.get(task.name)
                    if ts:
                        last = datetime.fromisoformat(ts)
                        # Only restore if from today (stale state from yesterday is useless)
                        if last.strftime("%Y-%m-%d") == today:
                            task._last_run = last
                            logger.info("Restored task '%s' last_run=%s", task.name, ts)
        except Exception as e:
            logger.warning("Failed to load scheduler state: %s", e)

    def _save_state(self) -> None:
        """Persist task _last_run to disk."""
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            state = {}
            for task in self._tasks:
                if task._last_run:
                    state[task.name] = task._last_run.isoformat()
            self._STATE_FILE.write_text(
                _json_mod.dumps(state, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as e:
            logger.warning("Failed to save scheduler state: %s", e)

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

        # Restore state from previous run (prevents re-running today's tasks on restart)
        self._load_state()

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
            logger.exception("Failed to load active themes on startup")

        # On startup, check for missed tasks today and run them
        await self._catch_up_missed(is_trading)

        while self._running:
            now = datetime.now()
            is_trading = self.is_trading_day(now)

            for task in self._tasks:
                if task.should_run(now, is_trading):
                    logger.info("Running task: %s (timeout=%ds)", task.name, task.timeout_seconds)
                    chatty = _is_high_frequency(task)
                    if not chatty:
                        log_activity("task_start", task=task.name, status="running",
                                     message=f"开始 {task.name}")
                    started = datetime.now()
                    try:
                        result = await asyncio.wait_for(task.run_fn(), timeout=task.timeout_seconds)
                        task._last_run = datetime.now()
                        logger.info("Task %s completed", task.name)
                        took = (task._last_run - started).total_seconds()
                        preview = result if isinstance(result, str) else ""
                        # A silent high-frequency task logs nothing: 288
                        # "完成" rows a day from news_ingest alone told the
                        # reader nothing they could act on. It still logs
                        # when it produced output, and failures always log.
                        if preview or not chatty:
                            log_activity(
                                "task_done", task=task.name, status="ok",
                                message=preview[:2000] or f"{task.name} 完成",
                                detail={"seconds": round(took, 1),
                                        "has_output": bool(preview)},
                            )
                        # Persist the report itself. Without this the
                        # morning scan, review, night scan and weekly
                        # report existed only as a push notification and
                        # a 2000-char activity row — the dashboard's
                        # report page could never show any of them.
                        if preview.strip():
                            try:
                                from alpha_agents.data.report_store import (
                                    save_task_report,
                                )
                                save_task_report(task.name, preview)
                            except Exception as e:
                                logger.warning(
                                    "Failed to persist %s report: %s",
                                    task.name, e,
                                )

                        # Notify chat terminal if callback is set
                        if self._on_task_output and result and isinstance(result, str):
                            self._on_task_output(task.name, result)
                    except asyncio.TimeoutError:
                        logger.error("Task %s exceeded %ds timeout — cancelled "
                                     "(any to_thread workers will keep running until "
                                     "their blocking call returns)",
                                     task.name, task.timeout_seconds)
                        task._last_run = datetime.now()
                        log_activity("task_failed", task=task.name, status="timeout",
                                     message=f"{task.name} 超时 ({task.timeout_seconds}s)")
                    except Exception as e:
                        logger.exception("Task %s failed", task.name)
                        task._last_run = datetime.now()
                        log_activity("task_failed", task=task.name, status="failed",
                                     message=f"{task.name} 失败: {e}"[:500])
                    self._save_state()

            # Check custom tasks from DB
            await self._check_custom_tasks(now, is_trading)

            await asyncio.sleep(30)

    async def _run_custom_task(self, task: dict) -> None:
        """Execute a custom task by running its prompt through an Agent."""
        from alpha_agents.agents.chat import _create_chat_agent
        from agents import Runner

        agent = _create_chat_agent()
        prompt = task["prompt"]
        logger.info("Custom task #%d: %s", task["id"], prompt[:80])

        try:
            result = await asyncio.wait_for(
                Runner.run(agent, prompt, max_turns=15),
                timeout=120,
            )
            output = result.final_output
            logger.info("Custom task #%d result: %s", task["id"], output[:200])

            # Push result to notification
            from alpha_agents.notify import notify_all
            notify_all(
                f"AlphaAgents 自定义任务 #{task['id']}",
                f"{prompt}\n\n{output[:500]}",
            )
        except asyncio.TimeoutError:
            logger.warning("Custom task #%d timed out", task["id"])
        except Exception as e:
            logger.warning("Custom task #%d failed: %s", task["id"], e)

    async def _check_custom_tasks(self, now: datetime, is_trading: bool) -> None:
        """Check and run any due custom tasks."""
        try:
            from alpha_agents.data.memory_store import get_active_custom_tasks, update_custom_task_last_run
            tasks = get_active_custom_tasks()
        except Exception:
            return

        today = now.strftime("%Y-%m-%d")
        current_hm = now.strftime("%H:%M")

        for task in tasks:
            schedule_time = task.get("schedule_time", "")
            interval = task.get("interval", "once")
            last_run = task.get("last_run", "")

            # Skip if not the right time (check within 1-minute window)
            if not schedule_time or abs(self._time_diff_minutes(current_hm, schedule_time)) > 1:
                continue

            # Skip weekday-only tasks on non-trading days
            if interval == "weekday" and not is_trading:
                continue

            # Skip if already ran today
            if last_run and last_run[:10] == today:
                continue

            # Run it
            await self._run_custom_task(task)
            update_custom_task_last_run(task["id"])

    @staticmethod
    def _time_diff_minutes(t1: str, t2: str) -> float:
        """Difference in minutes between two HH:MM strings."""
        try:
            h1, m1 = map(int, t1.split(":"))
            h2, m2 = map(int, t2.split(":"))
            return (h1 * 60 + m1) - (h2 * 60 + m2)
        except Exception:
            return 999

    async def _catch_up_missed(self, is_trading: bool) -> None:
        """Run any non-interval tasks that should have run today but were missed."""
        now = datetime.now()
        for task in self._tasks:
            # Skip interval tasks (intraday monitor) — only catch up one-shot tasks
            if task.interval_minutes:
                continue
            if task.trading_day_only and not is_trading:
                continue
            if task.weekday is not None and now.weekday() != task.weekday:
                continue
            # If the task's scheduled time has passed today and it hasn't run yet
            scheduled = now.replace(hour=task.run_at.hour, minute=task.run_at.minute, second=0)
            elapsed_seconds = (now - scheduled).total_seconds()
            if elapsed_seconds <= 0 or task._last_run is not None:
                continue

            grace = task.catch_up_grace_minutes
            if grace is not None and elapsed_seconds > grace * 60:
                logger.info(
                    "Catch-up: skipping stale task '%s' (scheduled at %s, %.0f min late)",
                    task.name, task.run_at, elapsed_seconds / 60,
                )
                # Mark as handled for today so the main loop does not run it
                # immediately after this catch-up pass.
                task._last_run = now
                self._save_state()
                continue

            if elapsed_seconds > 0:
                logger.info("Catch-up: running missed task '%s' (was scheduled at %s)",
                            task.name, task.run_at)
                try:
                    result = await asyncio.wait_for(task.run_fn(), timeout=task.timeout_seconds)
                    task._last_run = datetime.now()
                    logger.info("Catch-up: task %s completed", task.name)
                    if self._on_task_output and result and isinstance(result, str):
                        self._on_task_output(task.name, result)
                except asyncio.TimeoutError:
                    logger.error("Catch-up: task %s exceeded %ds timeout — cancelled",
                                 task.name, task.timeout_seconds)
                    task._last_run = datetime.now()
                except Exception:
                    logger.exception("Catch-up: task %s failed", task.name)
                    task._last_run = datetime.now()
                self._save_state()

    def boost_task(self, name: str, minutes: int = 15) -> None:
        """Temporarily shorten a task's polling interval after anomaly detection."""
        for task in self._tasks:
            if task.name == name:
                task.boost(minutes)
                return
        logger.warning("boost_task: no task named '%s'", name)

    def stop(self) -> None:
        """Stop the scheduler."""
        self._running = False
