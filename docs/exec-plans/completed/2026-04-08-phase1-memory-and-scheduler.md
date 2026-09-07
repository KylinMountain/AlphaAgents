# Phase 1: Memory System + Trading Day Scheduler

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the simple polling `NewsMonitor` with a trading-day-aware scheduler that runs different analysis tasks at different times, backed by a persistent memory system (theme lines, predictions, market cognition) that gives the analyst agent accumulated context.

**Architecture:** New `memory_store.py` for three SQLite tables (theme_lines, predictions_v2, market_cognition) alongside existing `report_store.py`. New `scheduler.py` replaces `NewsMonitor` as the main loop, dispatching named tasks at configured times. Each task is an async function that reads/writes the memory store and calls existing tools/agents. Existing news fetching, digest, and tool infrastructure is fully reused.

**Tech Stack:** Python 3.12, asyncio, SQLite (existing pattern from `data/db.py`), akshare (existing), OpenAI Agents SDK (existing).

**Note:** This plan covers ONLY Phase 1 (infrastructure). Phases 2-5 (data tools, agents, output, macro) are separate plans that build on this foundation.

---

## File Structure

```
alpha_agents/
├── data/
│   ├── db.py                    # Existing — reuse get_connection()
│   ├── report_store.py          # Existing — keep as-is
│   └── memory_store.py          # NEW — theme_lines, predictions_v2, market_cognition tables
├── pipeline/
│   ├── monitor.py               # Existing — kept for backward compat, but scheduler replaces it
│   ├── scheduler.py             # NEW — trading-day-aware task scheduler
│   ├── tasks/                   # NEW — one file per scheduled task
│   │   ├── __init__.py
│   │   ├── morning_scan.py      # 06:30 晨扫
│   │   ├── intraday_monitor.py  # 09:30-15:00 盘中监控
│   │   └── review.py            # 15:30 复盘
│   └── theme_manager.py         # NEW — theme line CRUD + auto-discover/retire logic
├── config.py                    # Modify — add MEMORY_DB_PATH
└── ...
main.py                          # Modify — add cmd_run_v2 using scheduler
```

---

### Task 1: Create memory store schema

The three-table memory system. Follows the existing `report_store.py` pattern: module-level schema string, thread-local connections, plain functions.

**Files:**
- Create: `alpha_agents/data/memory_store.py`
- Modify: `alpha_agents/config.py` (add MEMORY_DB_PATH)

- [ ] **Step 1: Add MEMORY_DB_PATH to config.py**

In `alpha_agents/config.py`, after the line `CHROMA_PATH = DATA_DIR / "chroma"` (line 10), add:

```python
MEMORY_DB_PATH = DATA_DIR / "memory.db"
```

- [ ] **Step 2: Create memory_store.py with schema and init**

Create `alpha_agents/data/memory_store.py`:

```python
"""Persistent memory for the analyst — theme lines, predictions, market cognition.

Follows the same pattern as report_store.py: thread-local SQLite connections,
plain functions, JSON for flexible fields.
"""

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from alpha_agents.config import MEMORY_DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS theme_lines (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    status TEXT DEFAULT 'watching',
    strength INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT,
    catalyst TEXT,
    core_stocks TEXT,
    leader_code TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS predictions_v2 (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    report_type TEXT,
    code TEXT NOT NULL,
    name TEXT,
    direction TEXT,
    confidence TEXT,
    theme_line TEXT,
    entry_price REAL,
    reason TEXT,
    next_day_return REAL,
    week_return REAL,
    hit INTEGER,
    review_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_pred_date ON predictions_v2(date);
CREATE INDEX IF NOT EXISTS idx_pred_code ON predictions_v2(code);

CREATE TABLE IF NOT EXISTS market_cognition (
    id INTEGER PRIMARY KEY,
    sector TEXT NOT NULL,
    date TEXT NOT NULL,
    position TEXT,
    fund_trend TEXT,
    pe_percentile REAL,
    recent_events TEXT,
    assessment TEXT,
    UNIQUE(sector, date)
);
CREATE INDEX IF NOT EXISTS idx_cognition_sector ON market_cognition(sector);
"""

_local = threading.local()
_write_lock = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    """Get or create a thread-local connection to memory.db."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        MEMORY_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(MEMORY_DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        _local.conn = conn
    return conn


# ── Theme Lines ──────────────────────────────────────────────

def get_active_themes(min_status: str = "watching") -> list[dict]:
    """Get all non-archived theme lines, ordered by strength desc."""
    status_order = ["watching", "active", "peak", "declining"]
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM theme_lines WHERE status != 'archived' ORDER BY strength DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_theme_by_name(name: str) -> dict | None:
    """Get a single theme line by name."""
    conn = _get_conn()
    row = conn.execute("SELECT * FROM theme_lines WHERE name = ?", (name,)).fetchone()
    return dict(row) if row else None


def upsert_theme(
    name: str,
    *,
    status: str | None = None,
    strength: int | None = None,
    catalyst: str | None = None,
    core_stocks: list[dict] | None = None,
    leader_code: str | None = None,
    notes: str | None = None,
) -> int:
    """Create or update a theme line. Returns the theme id."""
    now = datetime.now().isoformat()
    with _write_lock:
        conn = _get_conn()
        existing = conn.execute(
            "SELECT id FROM theme_lines WHERE name = ?", (name,)
        ).fetchone()

        if existing:
            sets, vals = [], []
            if status is not None:
                sets.append("status = ?"); vals.append(status)
            if strength is not None:
                sets.append("strength = ?"); vals.append(strength)
            if catalyst is not None:
                sets.append("catalyst = ?"); vals.append(catalyst)
            if core_stocks is not None:
                sets.append("core_stocks = ?"); vals.append(json.dumps(core_stocks, ensure_ascii=False))
            if leader_code is not None:
                sets.append("leader_code = ?"); vals.append(leader_code)
            if notes is not None:
                sets.append("notes = ?"); vals.append(notes)
            sets.append("updated_at = ?"); vals.append(now)
            vals.append(existing["id"])
            conn.execute(f"UPDATE theme_lines SET {', '.join(sets)} WHERE id = ?", vals)
            conn.commit()
            return existing["id"]
        else:
            cur = conn.execute(
                "INSERT INTO theme_lines (name, status, strength, catalyst, core_stocks, leader_code, notes, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name, status or "watching", strength or 0, catalyst,
                 json.dumps(core_stocks or [], ensure_ascii=False), leader_code, notes, now, now),
            )
            conn.commit()
            return cur.lastrowid


def archive_theme(name: str) -> None:
    """Archive a theme line (soft delete)."""
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE theme_lines SET status = 'archived', updated_at = ? WHERE name = ?",
            (datetime.now().isoformat(), name),
        )
        conn.commit()


# ── Predictions ──────────────────────────────────────────────

def save_prediction(
    date: str,
    report_type: str,
    code: str,
    name: str,
    direction: str,
    confidence: str,
    theme_line: str,
    entry_price: float | None,
    reason: str,
) -> int:
    """Record a stock recommendation."""
    with _write_lock:
        conn = _get_conn()
        cur = conn.execute(
            "INSERT INTO predictions_v2 (date, report_type, code, name, direction, confidence, theme_line, entry_price, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (date, report_type, code, name, direction, confidence, theme_line, entry_price, reason),
        )
        conn.commit()
        return cur.lastrowid


def update_prediction_result(pred_id: int, *, next_day_return: float | None = None,
                              week_return: float | None = None, hit: int | None = None,
                              review_note: str | None = None) -> None:
    """Fill in backtesting results for a prediction."""
    with _write_lock:
        conn = _get_conn()
        sets, vals = [], []
        if next_day_return is not None:
            sets.append("next_day_return = ?"); vals.append(next_day_return)
        if week_return is not None:
            sets.append("week_return = ?"); vals.append(week_return)
        if hit is not None:
            sets.append("hit = ?"); vals.append(hit)
        if review_note is not None:
            sets.append("review_note = ?"); vals.append(review_note)
        if sets:
            vals.append(pred_id)
            conn.execute(f"UPDATE predictions_v2 SET {', '.join(sets)} WHERE id = ?", vals)
            conn.commit()


def get_pending_predictions(date: str) -> list[dict]:
    """Get predictions that haven't been reviewed yet for a given date."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM predictions_v2 WHERE date = ? AND hit IS NULL", (date,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_prediction_stats(days: int = 7) -> dict:
    """Get hit rate statistics for recent predictions."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT direction, confidence, hit FROM predictions_v2 "
        "WHERE hit IS NOT NULL ORDER BY date DESC LIMIT ?",
        (days * 20,),
    ).fetchall()
    if not rows:
        return {"total": 0, "hits": 0, "hit_rate": 0.0, "by_confidence": {}}
    total = len(rows)
    hits = sum(1 for r in rows if r["hit"] == 1)
    by_conf = {}
    for r in rows:
        c = r["confidence"] or "unknown"
        by_conf.setdefault(c, {"total": 0, "hits": 0})
        by_conf[c]["total"] += 1
        if r["hit"] == 1:
            by_conf[c]["hits"] += 1
    for v in by_conf.values():
        v["hit_rate"] = round(v["hits"] / v["total"] * 100, 1) if v["total"] else 0
    return {"total": total, "hits": hits, "hit_rate": round(hits / total * 100, 1), "by_confidence": by_conf}


# ── Market Cognition ─────────────────────────────────────────

def upsert_cognition(sector: str, date: str, *, position: str | None = None,
                      fund_trend: str | None = None, pe_percentile: float | None = None,
                      recent_events: list[str] | None = None, assessment: str | None = None) -> None:
    """Update the analyst's understanding of a sector."""
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO market_cognition (sector, date, position, fund_trend, pe_percentile, recent_events, assessment) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(sector, date) DO UPDATE SET "
            "position=COALESCE(excluded.position, position), "
            "fund_trend=COALESCE(excluded.fund_trend, fund_trend), "
            "pe_percentile=COALESCE(excluded.pe_percentile, pe_percentile), "
            "recent_events=COALESCE(excluded.recent_events, recent_events), "
            "assessment=COALESCE(excluded.assessment, assessment)",
            (sector, date, position, fund_trend, pe_percentile,
             json.dumps(recent_events or [], ensure_ascii=False) if recent_events else None, assessment),
        )
        conn.commit()


def get_cognition(sector: str) -> dict | None:
    """Get the latest cognition for a sector."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM market_cognition WHERE sector = ? ORDER BY date DESC LIMIT 1", (sector,)
    ).fetchone()
    return dict(row) if row else None


def get_all_cognition_latest() -> list[dict]:
    """Get latest cognition for all tracked sectors."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT m1.* FROM market_cognition m1 "
        "INNER JOIN (SELECT sector, MAX(date) as max_date FROM market_cognition GROUP BY sector) m2 "
        "ON m1.sector = m2.sector AND m1.date = m2.max_date "
        "ORDER BY m1.sector"
    ).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 3: Test memory store**

Run:
```bash
uv run python -c "
from alpha_agents.data.memory_store import *

# Test theme CRUD
tid = upsert_theme('AI算力', status='active', strength=8, catalyst='博通TPU合作',
                    core_stocks=[{'code': '300308', 'name': '中际旭创', 'role': '龙头'}],
                    leader_code='300308')
print(f'Created theme id={tid}')

# Test get
t = get_theme_by_name('AI算力')
print(f'Theme: {t[\"name\"]}, status={t[\"status\"]}, strength={t[\"strength\"]}')

# Test prediction
pid = save_prediction('2026-04-08', 'morning', '300308', '中际旭创', 'bullish', 'high', 'AI算力', 152.3, 'TPU概念龙头')
print(f'Prediction id={pid}')

# Test cognition
upsert_cognition('半导体', '2026-04-08', position='high', fund_trend='inflow', assessment='趋势向上但高位')
c = get_cognition('半导体')
print(f'Cognition: {c[\"sector\"]}, position={c[\"position\"]}')

# Test stats
stats = get_prediction_stats()
print(f'Stats: {stats}')

print('ALL TESTS PASSED')
"
```
Expected: All operations succeed, prints "ALL TESTS PASSED".

- [ ] **Step 4: Commit**

```bash
git add alpha_agents/config.py alpha_agents/data/memory_store.py
git commit -m "feat: add memory store — theme lines, predictions, market cognition tables"
```

---

### Task 2: Create trading day scheduler

Replaces the simple `while True: sleep(interval)` loop in NewsMonitor with a time-aware scheduler that runs named tasks at specific times on trading days.

**Files:**
- Create: `alpha_agents/pipeline/scheduler.py`

- [ ] **Step 1: Create the scheduler**

```python
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
        date_str = date.strftime("%Y%m%d")
        return date_str in df["trade_date"].astype(str).values
    except Exception as e:
        # Fallback: weekdays are trading days
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
            # Repeating task (e.g., intraday monitor 09:30-15:00 every 15min)
            if not (self.run_at <= current_time <= self.end_at):
                return False
            if self._last_run:
                elapsed = (now - self._last_run).total_seconds() / 60
                return elapsed >= self.interval_minutes
            return True
        else:
            # One-shot task (e.g., morning scan at 06:30)
            if self._last_run and self._last_run.date() == now.date():
                return False  # Already ran today
            # Run if we're within 5 minutes of the scheduled time
            scheduled = now.replace(hour=self.run_at.hour, minute=self.run_at.minute, second=0)
            return 0 <= (now - scheduled).total_seconds() < 300  # 5-minute window


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
        logger.info("Trading day scheduler started")

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
                        task._last_run = datetime.now()  # Don't retry immediately

            await asyncio.sleep(30)  # Check every 30 seconds

    def stop(self) -> None:
        """Stop the scheduler."""
        self._running = False
```

- [ ] **Step 2: Test the scheduler**

Run:
```bash
uv run python -c "
import asyncio
from datetime import time as dtime, datetime
from alpha_agents.pipeline.scheduler import Task, TradingDayScheduler

ran = []

async def mock_task():
    ran.append(datetime.now())

# Create a task that should run now
now = datetime.now()
task = Task('test', mock_task, now.time(), trading_day_only=False)

sched = TradingDayScheduler()
sched.add_task(task)

assert task.should_run(now, True) == True, 'should_run failed'
print(f'should_run: OK')

# Test already-ran check
task._last_run = now
assert task.should_run(now, True) == False, 'already-ran check failed'
print(f'already-ran check: OK')

# Test trading day filter
task2 = Task('trading_only', mock_task, now.time(), trading_day_only=True)
assert task2.should_run(now, False) == False, 'trading_day filter failed'
print(f'trading_day filter: OK')

# Test interval task
task3 = Task('interval', mock_task, dtime(9, 30), end_at=dtime(15, 0), interval_minutes=15, trading_day_only=False)
test_time = datetime.now().replace(hour=10, minute=0)
assert task3.should_run(test_time, True) == True, 'interval initial run failed'
task3._last_run = test_time
test_time2 = test_time.replace(minute=10)
assert task3.should_run(test_time2, True) == False, 'interval too soon failed'
test_time3 = test_time.replace(minute=16)
assert task3.should_run(test_time3, True) == True, 'interval ready failed'
print(f'interval checks: OK')

print('ALL TESTS PASSED')
"
```
Expected: "ALL TESTS PASSED"

- [ ] **Step 3: Commit**

```bash
git add alpha_agents/pipeline/scheduler.py
git commit -m "feat: add trading-day-aware task scheduler"
```

---

### Task 3: Create theme manager

Encapsulates theme line lifecycle logic: auto-discovery from sector data, strength updates, auto-retirement. Uses memory_store for persistence.

**Files:**
- Create: `alpha_agents/pipeline/theme_manager.py`

- [ ] **Step 1: Create the theme manager**

```python
"""Theme line lifecycle manager.

Handles auto-discovery of new market themes from sector data,
strength tracking, and automatic retirement of fading themes.

Theme lifecycle: watching → active → peak → declining → archived
"""

import json
import logging
from datetime import datetime

from alpha_agents.data.memory_store import (
    get_active_themes, get_theme_by_name, upsert_theme, archive_theme,
)

logger = logging.getLogger(__name__)

MAX_ACTIVE_THEMES = 8

# Status progression
STATUS_ORDER = ["watching", "active", "peak", "declining", "archived"]


def evaluate_theme_signals(
    sector_name: str,
    sector_change_pct: float,
    sector_fund_flow: float,
    market_change_pct: float,
    has_news_catalyst: bool = False,
    leader_hit_limit: bool = False,
    consecutive_inflow_days: int = 0,
    consecutive_outflow_days: int = 0,
) -> dict:
    """Evaluate bullish/bearish signals for a potential or existing theme.

    Returns:
        {"bullish_signals": int, "bearish_signals": int, "details": [...]}
    """
    bullish, bearish, details = 0, 0, []

    # Bullish signals
    relative_strength = sector_change_pct - market_change_pct
    if relative_strength > 1.0:
        bullish += 1
        details.append(f"跑赢大盘{relative_strength:.1f}%")
    if sector_fund_flow > 0:
        bullish += 1
        details.append(f"资金净流入{sector_fund_flow/1e8:.1f}亿")
    if has_news_catalyst:
        bullish += 1
        details.append("有新闻催化")
    if leader_hit_limit:
        bullish += 1
        details.append("龙头涨停")
    if consecutive_inflow_days >= 2:
        bullish += 1
        details.append(f"连续{consecutive_inflow_days}天资金流入")

    # Bearish signals
    if relative_strength < -1.0:
        bearish += 1
        details.append(f"跑输大盘{abs(relative_strength):.1f}%")
    if sector_fund_flow < 0:
        bearish += 1
        details.append(f"资金净流出{abs(sector_fund_flow)/1e8:.1f}亿")
    if consecutive_outflow_days >= 3:
        bearish += 2  # Double weight
        details.append(f"连续{consecutive_outflow_days}天资金流出")

    return {"bullish_signals": bullish, "bearish_signals": bearish, "details": details}


def update_theme_strength(name: str, signals: dict) -> None:
    """Update a theme's strength based on today's signals.

    Strength 0-10. Bullish signals increase, bearish decrease.
    Status transitions based on strength thresholds.
    """
    theme = get_theme_by_name(name)
    if not theme or theme["status"] == "archived":
        return

    current = theme["strength"] or 0
    bull = signals["bullish_signals"]
    bear = signals["bearish_signals"]

    # Adjust strength
    delta = bull - bear
    new_strength = max(0, min(10, current + delta))

    # Determine status based on strength trajectory
    status = theme["status"]
    if new_strength >= 7 and status in ("watching", "active"):
        status = "peak" if new_strength >= 9 else "active"
    elif new_strength >= 4 and status == "watching":
        status = "active"
    elif new_strength < 4 and status in ("active", "peak"):
        status = "declining"
    elif new_strength <= 1 and status == "declining":
        status = "archived"

    upsert_theme(name, status=status, strength=new_strength)
    logger.info("Theme '%s': strength %d→%d, status=%s (%s)",
                name, current, new_strength, status, "; ".join(signals["details"]))


def maybe_discover_theme(
    sector_name: str,
    signals: dict,
    catalyst: str = "",
) -> bool:
    """Check if signals warrant creating a new theme line.

    Requirements: 2+ bullish signals and no existing theme with this name.
    Returns True if a new theme was created.
    """
    if signals["bullish_signals"] < 2:
        return False

    existing = get_theme_by_name(sector_name)
    if existing and existing["status"] != "archived":
        return False  # Already tracking

    # Check capacity
    active = get_active_themes()
    if len(active) >= MAX_ACTIVE_THEMES:
        # Archive the weakest
        weakest = min(active, key=lambda t: t["strength"])
        if weakest["strength"] < signals["bullish_signals"]:
            archive_theme(weakest["name"])
            logger.info("Archived weakest theme '%s' (strength=%d) to make room",
                        weakest["name"], weakest["strength"])
        else:
            return False  # All existing themes are stronger

    upsert_theme(
        sector_name,
        status="watching",
        strength=signals["bullish_signals"],
        catalyst=catalyst or "; ".join(signals["details"]),
    )
    logger.info("Discovered new theme: '%s' (strength=%d, catalyst=%s)",
                sector_name, signals["bullish_signals"], catalyst)
    return True


def retire_stale_themes(max_age_days: int = 14) -> list[str]:
    """Archive themes that have been declining for too long.

    Returns list of archived theme names.
    """
    archived = []
    for theme in get_active_themes():
        if theme["status"] == "declining":
            updated = datetime.fromisoformat(theme["updated_at"]) if theme["updated_at"] else datetime.now()
            age = (datetime.now() - updated).days
            if age > max_age_days:
                archive_theme(theme["name"])
                archived.append(theme["name"])
                logger.info("Retired stale theme '%s' (declining for %d days)", theme["name"], age)
    return archived
```

- [ ] **Step 2: Test theme manager**

Run:
```bash
uv run python -c "
from alpha_agents.pipeline.theme_manager import *
from alpha_agents.data.memory_store import get_active_themes, get_theme_by_name

# Test signal evaluation
signals = evaluate_theme_signals(
    sector_name='AI算力',
    sector_change_pct=3.5,
    sector_fund_flow=5e8,
    market_change_pct=0.5,
    has_news_catalyst=True,
    leader_hit_limit=True,
)
print(f'Signals: bull={signals[\"bullish_signals\"]}, bear={signals[\"bearish_signals\"]}')
assert signals['bullish_signals'] >= 3, 'Expected 3+ bullish signals'

# Test theme discovery
created = maybe_discover_theme('AI算力测试', signals, catalyst='博通TPU合作')
assert created, 'Should have created theme'
t = get_theme_by_name('AI算力测试')
assert t is not None, 'Theme should exist'
assert t['status'] == 'watching', f'Expected watching, got {t[\"status\"]}'
print(f'Discovery: OK (status={t[\"status\"]}, strength={t[\"strength\"]})')

# Test strength update
update_theme_strength('AI算力测试', signals)
t2 = get_theme_by_name('AI算力测试')
assert t2['strength'] > t['strength'], 'Strength should increase'
print(f'Strength update: OK ({t[\"strength\"]}→{t2[\"strength\"]})')

# Test retirement
from alpha_agents.data.memory_store import upsert_theme
upsert_theme('衰退测试', status='declining', strength=1)
retired = retire_stale_themes(max_age_days=0)  # Immediate retirement for test
print(f'Retirement: {retired}')

print('ALL TESTS PASSED')
"
```
Expected: "ALL TESTS PASSED"

- [ ] **Step 3: Commit**

```bash
git add alpha_agents/pipeline/theme_manager.py
git commit -m "feat: add theme manager — auto-discover, strength tracking, retirement"
```

---

### Task 4: Create stub task files for scheduled tasks

Create the task directory structure with stub implementations. Each task reads memory, calls existing tools/agents, and writes back to memory. Stubs do minimal work now (just logging + memory reads) — full agent logic comes in Phase 3.

**Files:**
- Create: `alpha_agents/pipeline/tasks/__init__.py`
- Create: `alpha_agents/pipeline/tasks/morning_scan.py`
- Create: `alpha_agents/pipeline/tasks/intraday_monitor.py`
- Create: `alpha_agents/pipeline/tasks/review.py`

- [ ] **Step 1: Create __init__.py**

```python
"""Scheduled analysis tasks for the trading day."""
```

- [ ] **Step 2: Create morning_scan.py**

```python
"""Morning scan task — runs at 06:30 before market open.

Reads: overnight news, foreign markets, active theme lines
Outputs: morning briefing report
"""

import json
import logging
import time

from alpha_agents.data.memory_store import (
    get_active_themes, get_prediction_stats, get_all_cognition_latest,
)
from alpha_agents.pipeline.digest import digest_news
from alpha_agents.pipeline.monitor import NEWS_SOURCES
from alpha_agents.notify import notify_all

logger = logging.getLogger(__name__)


async def run_morning_scan() -> str | None:
    """Execute the morning scan task.

    1. Fetch overnight news from all sources
    2. Read active theme lines and market cognition
    3. Digest news with theme context
    4. Generate morning briefing

    Returns the morning report text, or None if nothing significant.
    """
    import asyncio

    logger.info("Morning scan starting...")

    # 1. Fetch news from all sources (reuse existing infrastructure)
    news_items = []
    for source_id, name, fetch_fn_factory in NEWS_SOURCES:
        try:
            raw = await asyncio.to_thread(fetch_fn_factory)
            data = json.loads(raw)
            items = data.get("news", [])
            news_items.extend(items)
            logger.debug("Morning scan: %d items from %s", len(items), name)
        except Exception as e:
            logger.debug("Morning scan: %s unavailable: %s", name, e)

    if not news_items:
        logger.info("Morning scan: no news items")
        return None

    # 2. Read memory for context
    themes = get_active_themes()
    stats = get_prediction_stats(days=7)
    cognition = get_all_cognition_latest()

    logger.info("Morning scan: %d news items, %d active themes, hit rate=%.1f%%",
                len(news_items), len(themes), stats.get("hit_rate", 0))

    # 3. Digest news (reuse existing cheap LLM filtering)
    events = await digest_news(news_items)

    if not events:
        logger.info("Morning scan: no significant events after digest")
        return None

    # 4. Generate morning report
    # TODO Phase 3: Replace with morning_scan Agent that uses memory context
    # For now, build a simple structured report
    report_lines = [
        f"=== AlphaAgents 晨报 | {time.strftime('%Y-%m-%d')} ===",
        "",
        "【活跃主线】",
    ]
    for t in themes[:5]:
        stocks = json.loads(t["core_stocks"]) if t["core_stocks"] else []
        leader = next((s["name"] for s in stocks if s.get("role") == "龙头"), "无")
        report_lines.append(f"  {t['name']}（强度 {t['strength']}/10, {t['status']}）— 龙头: {leader}")

    report_lines.append("")
    report_lines.append(f"【预测命中率（近7天）】{stats.get('hit_rate', 0):.1f}% ({stats.get('hits', 0)}/{stats.get('total', 0)})")

    report_lines.append("")
    report_lines.append("【今日事件】")
    for e in events[:5]:
        report_lines.append(f"  [{e.get('category', '?')}] {e.get('event', '?')} — 重要性 {e.get('importance', 0)}/5")

    report = "\n".join(report_lines)
    logger.info("Morning scan complete: %d events, report length=%d", len(events), len(report))

    return report
```

- [ ] **Step 3: Create intraday_monitor.py**

```python
"""Intraday monitoring task — runs every 15 minutes during trading hours (09:30-15:00).

Watches for anomalies in active theme line stocks and sector fund flows.
Triggers alerts only when significant changes detected.
"""

import json
import logging

from alpha_agents.data.memory_store import get_active_themes

logger = logging.getLogger(__name__)


async def run_intraday_monitor() -> str | None:
    """Execute one intraday monitoring cycle.

    1. Get active theme lines and their core stocks
    2. Check sector fund flows for anomalies
    3. Check theme leader stocks for significant moves
    4. If anomaly detected, trace the cause and generate alert

    Returns alert text if anomaly found, None otherwise.
    """
    themes = get_active_themes()
    if not themes:
        logger.debug("Intraday monitor: no active themes to watch")
        return None

    logger.info("Intraday monitor: watching %d themes", len(themes))

    # TODO Phase 2+3: Add sector fund flow checking, anomaly detection,
    # cause-tracing with news, and alert generation.
    # For now, just log that we're watching.

    for theme in themes:
        stocks = json.loads(theme["core_stocks"]) if theme["core_stocks"] else []
        leader = theme.get("leader_code", "?")
        logger.debug("  Watching '%s' (strength=%d, leader=%s, %d stocks)",
                     theme["name"], theme["strength"], leader, len(stocks))

    return None
```

- [ ] **Step 4: Create review.py**

```python
"""Post-market review task — runs at 15:30 after market close.

Reviews today's predictions, updates theme line strengths,
updates market cognition, generates review report.
"""

import json
import logging
import time
from datetime import datetime

from alpha_agents.data.memory_store import (
    get_active_themes, get_pending_predictions, update_prediction_result,
    get_prediction_stats, upsert_cognition,
)
from alpha_agents.pipeline.theme_manager import (
    evaluate_theme_signals, update_theme_strength, retire_stale_themes,
)

logger = logging.getLogger(__name__)


async def run_review() -> str | None:
    """Execute the post-market review task.

    1. Check today's prediction results (were we right?)
    2. Update theme line strengths based on today's market data
    3. Update market cognition for tracked sectors
    4. Generate review report

    Returns the review report text.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    logger.info("Review starting for %s...", today)

    # 1. Check pending predictions
    pending = get_pending_predictions(today)
    logger.info("Review: %d pending predictions to verify", len(pending))

    # TODO Phase 3: Use get_stock_quotes to check actual returns
    # and call update_prediction_result for each

    # 2. Update theme strengths
    themes = get_active_themes()
    logger.info("Review: evaluating %d active themes", len(themes))

    # TODO Phase 2+3: Fetch actual sector data for each theme
    # and call update_theme_strength with real signals

    # 3. Retire stale themes
    retired = retire_stale_themes()
    if retired:
        logger.info("Review: retired themes: %s", retired)

    # 4. Generate review report
    stats = get_prediction_stats(days=7)
    report_lines = [
        f"=== AlphaAgents 复盘 | {today} ===",
        "",
        f"【预测验证】待验证: {len(pending)}条 | 近7天命中率: {stats.get('hit_rate', 0):.1f}%",
        "",
        "【主线状态】",
    ]
    for t in themes:
        report_lines.append(f"  {t['name']}（强度 {t['strength']}/10, {t['status']}）")
    if retired:
        report_lines.append(f"  已退出: {', '.join(retired)}")

    report = "\n".join(report_lines)
    logger.info("Review complete")

    return report
```

- [ ] **Step 5: Commit**

```bash
git add alpha_agents/pipeline/tasks/
git commit -m "feat: add scheduled task stubs — morning scan, intraday monitor, review"
```

---

### Task 5: Wire scheduler into main.py

Add a new `cmd_run_v2` command that uses the scheduler instead of NewsMonitor. Keep the old `cmd_run` for backward compatibility.

**Files:**
- Modify: `main.py`

- [ ] **Step 1: Add the v2 run command**

In `main.py`, after the existing `cmd_run` function, add:

```python
def cmd_run_v2(args: argparse.Namespace) -> None:
    """Run AlphaAgents 2.0 with trading-day scheduler."""
    _ensure_index()
    _ensure_embeddings()

    from datetime import time as dtime
    from alpha_agents.pipeline.scheduler import Task, TradingDayScheduler
    from alpha_agents.pipeline.tasks.morning_scan import run_morning_scan
    from alpha_agents.pipeline.tasks.intraday_monitor import run_intraday_monitor
    from alpha_agents.pipeline.tasks.review import run_review

    scheduler = TradingDayScheduler()

    # Morning scan: 06:30, trading days only
    scheduler.add_task(Task("morning_scan", run_morning_scan, dtime(6, 30)))

    # Intraday monitor: 09:30-15:00 every 15 min, trading days only
    scheduler.add_task(Task(
        "intraday_monitor", run_intraday_monitor,
        dtime(9, 30), end_at=dtime(15, 0), interval_minutes=15,
    ))

    # Post-market review: 15:30, trading days only
    scheduler.add_task(Task("review", run_review, dtime(15, 30)))

    # Night scan: 20:00, every day (monitors foreign markets)
    scheduler.add_task(Task(
        "night_scan", run_morning_scan,  # Reuse morning scan logic for now
        dtime(20, 0), trading_day_only=False,
    ))

    logging.info("Starting AlphaAgents 2.0 scheduler...")
    try:
        asyncio.run(scheduler.run())
    except KeyboardInterrupt:
        logging.info("Scheduler stopped by user.")
```

- [ ] **Step 2: Add the subcommand to argparse**

In the `main()` function, after the existing `run` subparser, add:

```python
p_run_v2 = sub.add_parser("run-v2", help="Run with trading-day scheduler (AlphaAgents 2.0)")
p_run_v2.set_defaults(func=cmd_run_v2)
```

- [ ] **Step 3: Test the v2 command starts**

Run:
```bash
uv run python main.py run-v2 &
PID=$!
sleep 5
kill $PID 2>/dev/null
echo "Scheduler started and stopped OK"
```
Expected: Logs show "Starting AlphaAgents 2.0 scheduler...", registered tasks, then clean shutdown.

- [ ] **Step 4: Commit**

```bash
git add main.py
git commit -m "feat: add run-v2 command with trading-day scheduler"
```

---

### Task 6: End-to-end test — verify all Phase 1 components

Run the full scheduler for a brief period and verify memory operations work.

**Files:** (none created, test only)

- [ ] **Step 1: Run integrated test**

```bash
set -a && source .env && set +a && uv run python -c "
import asyncio, logging, time
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')

from alpha_agents.data.memory_store import upsert_theme, get_active_themes, save_prediction, get_prediction_stats
from alpha_agents.pipeline.theme_manager import evaluate_theme_signals, maybe_discover_theme
from alpha_agents.pipeline.tasks.morning_scan import run_morning_scan
from alpha_agents.pipeline.tasks.review import run_review

async def test():
    # 1. Seed a theme
    upsert_theme('集成测试主线', status='active', strength=7,
                  core_stocks=[{'code': '000001', 'name': '平安银行', 'role': '龙头'}],
                  leader_code='000001', catalyst='测试催化')
    themes = get_active_themes()
    print(f'Active themes: {len(themes)}')
    assert len(themes) >= 1

    # 2. Record a prediction
    save_prediction('2026-04-08', 'test', '000001', '平安银行', 'bullish', 'high', '集成测试主线', 10.5, '测试推荐')
    stats = get_prediction_stats()
    print(f'Prediction stats: {stats}')

    # 3. Run morning scan (should fetch news and produce report)
    print('Running morning scan...')
    report = await run_morning_scan()
    if report:
        print(f'Morning report ({len(report)} chars):')
        print(report[:500])
    else:
        print('Morning scan: no output (OK if no significant news)')

    # 4. Run review
    print('Running review...')
    review = await run_review()
    if review:
        print(f'Review report ({len(review)} chars):')
        print(review[:500])

    print('INTEGRATION TEST PASSED')

asyncio.run(test())
"
```

Expected: Themes created, predictions recorded, morning scan runs (may produce report depending on news availability), review runs. Prints "INTEGRATION TEST PASSED".

- [ ] **Step 2: Commit if fixes needed**

```bash
git add -A && git commit -m "fix: phase 1 integration test fixes" || echo "Nothing to fix"
```
