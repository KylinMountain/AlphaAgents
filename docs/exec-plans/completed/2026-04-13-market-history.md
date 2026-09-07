# Market History Database Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local SQLite database storing full-market daily K-lines (6 months, ~5000 stocks), with batch init, incremental update, limit-up stats computation, and sentiment snapshot backfill.

**Architecture:** One new module `market_history.py` owns the `market_history.db` file. It provides `get_local_history()` which `market_data.get_stock_history()` checks first (local cache hit → skip baostock). Init is batch + resumable. Daily update runs after review. Limit-up computation replaces akshare dependency for sentiment cycle.

**Tech Stack:** baostock (batch history), SQLite (local storage), existing daily_archive for snapshot backfill.

---

## File Structure

### New Files

| File | Responsibility |
|------|---------------|
| `alpha_agents/data/market_history.py` | Local history DB: init, update, query, limit-up stats, backfill |

### Modified Files

| File | Changes |
|------|---------|
| `alpha_agents/data/market_data.py` | `get_stock_history` checks local DB first |
| `alpha_agents/data/sentiment_cycle.py` | `backfill_snapshots` uses local data instead of akshare |
| `alpha_agents/pipeline/tasks/review.py` | Call `update_daily()` after archive |
| `main.py` | Add `init-history` CLI command |

---

### Task 1: Create market_history.py Core Module

**Files:**
- Create: `alpha_agents/data/market_history.py`

- [ ] **Step 1: Create the module with DB setup, get/save functions**

Create `alpha_agents/data/market_history.py`:

```python
"""Local market history database — full-market daily K-lines in SQLite.

Stores ~5000 stocks × 120 trading days in data/market_history.db.
Provides fast local queries, batch initialization, and daily updates.
"""

import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

import baostock as bs

from alpha_agents.config import DATA_DIR, no_proxy

logger = logging.getLogger(__name__)

DB_PATH = DATA_DIR / "market_history.db"
PROGRESS_PATH = DATA_DIR / "init_progress.json"
BATCH_SIZE = 500

_local = threading.local()
_write_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_kline (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume INTEGER,
    turnover_rate REAL,
    change_pct REAL,
    PRIMARY KEY (code, date)
);
CREATE INDEX IF NOT EXISTS idx_kline_date ON daily_kline(date);
"""


def _get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "hist_conn", None)
    if conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        _local.hist_conn = conn
    return conn


# ── Query ────────────────────────────────────────────────────

def get_local_history(code: str, days: int = 5) -> list[dict] | None:
    """Get recent K-lines from local DB. Returns None if not enough data."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM daily_kline WHERE code = ? ORDER BY date DESC LIMIT ?",
        (code, days),
    ).fetchall()
    if not rows or len(rows) < days:
        return None
    result = [dict(r) for r in reversed(rows)]  # Oldest first
    return result


def get_klines_for_date(date: str) -> list[dict]:
    """Get all stocks' K-line data for a specific date."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM daily_kline WHERE date = ?", (date,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_available_dates() -> list[str]:
    """Get all dates in the database."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT DISTINCT date FROM daily_kline ORDER BY date"
    ).fetchall()
    return [r["date"] for r in rows]


# ── Baostock Helpers ─────────────────────────────────────────

def _bs_login():
    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"baostock login failed: {lg.error_msg}")


def _bs_logout():
    try:
        bs.logout()
    except Exception:
        pass


def _to_bs_code(code: str) -> str:
    if code.startswith("6"):
        return f"sh.{code}"
    return f"sz.{code}"


def get_all_codes() -> list[str]:
    """Get all active A-share stock codes from baostock."""
    _bs_login()
    try:
        rs = bs.query_stock_basic()
        codes = []
        while rs.error_code == "0" and rs.next():
            row = rs.get_row_data()
            # row: [code, code_name, ipoDate, outDate, type, status]
            if row[4] == "1" and row[5] == "1":  # type=stock, status=active
                # Convert sh.600000 → 600000
                raw_code = row[0]
                code = raw_code.split(".")[-1] if "." in raw_code else raw_code
                codes.append(code)
        return codes
    finally:
        _bs_logout()


# ── Batch Init ───────────────────────────────────────────────

def _load_progress() -> dict:
    if PROGRESS_PATH.exists():
        return json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
    return {}


def _save_progress(progress: dict):
    PROGRESS_PATH.write_text(json.dumps(progress, ensure_ascii=False), encoding="utf-8")


def init_history(months: int = 6, batch_size: int = BATCH_SIZE) -> int:
    """Initialize full-market history. Supports resume on interruption.

    Args:
        months: How many months of history to fetch
        batch_size: Stocks per batch (saves progress between batches)

    Returns:
        Total rows inserted.
    """
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=months * 30)).strftime("%Y-%m-%d")

    # Get all codes
    progress = _load_progress()
    if progress.get("codes"):
        all_codes = progress["codes"]
        logger.info("Resuming init: %d codes from previous run", len(all_codes))
    else:
        logger.info("Fetching all stock codes...")
        all_codes = get_all_codes()
        progress["codes"] = all_codes
        progress["start_date"] = start_date
        progress["end_date"] = end_date
        progress["completed"] = 0
        _save_progress(progress)
        logger.info("Got %d stock codes", len(all_codes))

    completed = progress.get("completed", 0)
    total_inserted = 0

    # Process in batches
    for batch_start in range(completed, len(all_codes), batch_size):
        batch_end = min(batch_start + batch_size, len(all_codes))
        batch_codes = all_codes[batch_start:batch_end]
        batch_num = batch_start // batch_size + 1
        total_batches = (len(all_codes) + batch_size - 1) // batch_size

        logger.info("Batch %d/%d: fetching %d stocks (%d-%d)...",
                     batch_num, total_batches, len(batch_codes), batch_start, batch_end)

        rows = _fetch_batch(batch_codes, start_date, end_date)
        if rows:
            inserted = _save_batch(rows)
            total_inserted += inserted
            logger.info("Batch %d: inserted %d rows", batch_num, inserted)

        # Save progress
        progress["completed"] = batch_end
        _save_progress(progress)

    # Clean up progress file
    if PROGRESS_PATH.exists():
        PROGRESS_PATH.unlink()
    logger.info("Init complete: %d total rows for %d stocks", total_inserted, len(all_codes))
    return total_inserted


def _fetch_batch(codes: list[str], start_date: str, end_date: str) -> list[tuple]:
    """Fetch history for a batch of stock codes from baostock."""
    _bs_login()
    rows = []
    try:
        for code in codes:
            bs_code = _to_bs_code(code)
            rs = bs.query_history_k_data_plus(
                bs_code,
                "date,open,high,low,close,volume,turn,pctChg",
                start_date=start_date, end_date=end_date,
                frequency="d", adjustflag="2",
            )
            while rs.error_code == "0" and rs.next():
                r = rs.get_row_data()
                try:
                    rows.append((
                        code,           # code (6-digit)
                        r[0],           # date
                        float(r[1]) if r[1] else 0,  # open
                        float(r[2]) if r[2] else 0,  # high
                        float(r[3]) if r[3] else 0,  # low
                        float(r[4]) if r[4] else 0,  # close
                        int(r[5]) if r[5] else 0,     # volume
                        float(r[6]) if r[6] else 0,  # turnover_rate
                        float(r[7]) if r[7] else 0,  # change_pct
                    ))
                except (ValueError, IndexError):
                    continue
    finally:
        _bs_logout()
    return rows


def _save_batch(rows: list[tuple]) -> int:
    """Bulk insert rows into the database."""
    with _write_lock:
        conn = _get_conn()
        conn.executemany(
            "INSERT OR REPLACE INTO daily_kline "
            "(code, date, open, high, low, close, volume, turnover_rate, change_pct) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    return len(rows)


# ── Daily Update ─────────────────────────────────────────────

def update_daily() -> int:
    """Fetch today's data for all stocks. Called after market close."""
    today = datetime.now().strftime("%Y-%m-%d")

    # Check if already updated today
    conn = _get_conn()
    count = conn.execute(
        "SELECT COUNT(*) FROM daily_kline WHERE date = ?", (today,)
    ).fetchone()[0]
    if count > 100:
        logger.info("Daily update: already have %d rows for %s, skipping", count, today)
        return 0

    # Get all codes from existing data
    codes = [r[0] for r in conn.execute(
        "SELECT DISTINCT code FROM daily_kline"
    ).fetchall()]

    if not codes:
        logger.warning("No codes in history DB, run init-history first")
        return 0

    logger.info("Daily update: fetching %s for %d stocks...", today, len(codes))
    rows = _fetch_batch(codes, today, today)
    if rows:
        inserted = _save_batch(rows)
        logger.info("Daily update: inserted %d rows for %s", inserted, today)
        return inserted
    return 0


# ── Limit-Up Stats Computation ───────────────────────────────

def compute_limit_up_stats(date: str) -> dict:
    """Compute limit-up/broken-limit/consecutive stats from local K-line data.

    A-share rules:
    - Normal (60xxxx, 00xxxx): ±10% → limit at >= 9.8%
    - ChiNext/STAR (300xxx, 688xxx): ±20% → limit at >= 19.8%
    - ST stocks: ±5% → limit at >= 4.8% (we skip ST for simplicity)
    """
    klines = get_klines_for_date(date)
    if not klines:
        return {"limit_up_count": 0, "broken_limit_count": 0,
                "advance_decline_ratio": 1.0, "advances": 0, "declines": 0,
                "consecutive_limit_stocks": []}

    limit_up = []
    broken_limit = []
    advances = 0
    declines = 0

    for k in klines:
        code = k["code"]
        close = k["close"] or 0
        high = k["high"] or 0
        change_pct = k["change_pct"] or 0
        volume = k["volume"] or 0

        if close <= 0 or volume <= 0:
            continue

        # Determine limit threshold
        if code.startswith("3") or code.startswith("688"):
            limit_pct = 19.8
        else:
            limit_pct = 9.8

        # Count advances/declines
        if change_pct > 0:
            advances += 1
        elif change_pct < 0:
            declines += 1

        # Check limit-up
        if change_pct >= limit_pct:
            limit_up.append({"code": code, "name": "", "change_pct": change_pct})

        # Check broken limit (high hit limit but close didn't)
        # Need previous close to compute limit price
        # Approximate: if high's implied change >= limit but close change < limit
        elif high > 0 and close > 0:
            # high_change ≈ change_pct * (high / close) — rough approximation
            # Better: use the actual previous close
            prev_close = close / (1 + change_pct / 100) if change_pct != 0 else close
            if prev_close > 0:
                high_change = (high - prev_close) / prev_close * 100
                if high_change >= limit_pct and change_pct < limit_pct:
                    broken_limit.append({"code": code, "change_pct": change_pct})

    # Compute consecutive limit-ups (check previous days)
    consecutive = _compute_consecutive_limits(date, {k["code"] for k in limit_up})

    ad_ratio = round(advances / declines, 2) if declines > 0 else (10.0 if advances > 0 else 1.0)

    return {
        "limit_up_count": len(limit_up),
        "broken_limit_count": len(broken_limit),
        "advance_decline_ratio": ad_ratio,
        "advances": advances,
        "declines": declines,
        "consecutive_limit_stocks": consecutive,
    }


def _compute_consecutive_limits(date: str, today_limit_codes: set[str]) -> list[dict]:
    """Find stocks with consecutive limit-up days ending on `date`."""
    if not today_limit_codes:
        return []

    conn = _get_conn()
    # Get the last 10 trading dates before this date
    dates = [r["date"] for r in conn.execute(
        "SELECT DISTINCT date FROM daily_kline WHERE date <= ? ORDER BY date DESC LIMIT 10",
        (date,),
    ).fetchall()]

    if len(dates) < 2:
        return [{"code": c, "name": "", "consecutive_limits": 1} for c in list(today_limit_codes)[:10]]

    consecutive = []
    for code in today_limit_codes:
        is_300_or_688 = code.startswith("3") or code.startswith("688")
        limit_pct = 19.8 if is_300_or_688 else 9.8

        count = 1  # Today is already limit-up
        for prev_date in dates[1:]:  # Skip today, go backwards
            row = conn.execute(
                "SELECT change_pct FROM daily_kline WHERE code = ? AND date = ?",
                (code, prev_date),
            ).fetchone()
            if row and (row["change_pct"] or 0) >= limit_pct:
                count += 1
            else:
                break

        if count >= 2:
            consecutive.append({
                "code": code,
                "name": "",  # We don't have names in kline DB
                "consecutive_limits": count,
            })

    consecutive.sort(key=lambda x: x["consecutive_limits"], reverse=True)
    return consecutive[:10]


# ── Sentiment Backfill ───────────────────────────────────────

def backfill_sentiment_snapshots(days: int = 120) -> int:
    """Compute and save limit-up stats for historical dates to daily_snapshots.

    Uses local K-line data instead of akshare API.
    """
    from alpha_agents.data.daily_archive import save_snapshot

    dates = get_available_dates()
    if not dates:
        logger.warning("No data in market_history.db, run init-history first")
        return 0

    # Only process last N days
    dates = dates[-days:]
    filled = 0

    for date_str in dates:
        stats = compute_limit_up_stats(date_str)
        if stats["limit_up_count"] == 0 and stats["advances"] == 0:
            continue

        # Save as limit_up_pool snapshot
        save_snapshot(date_str, "limit_up_pool", {
            "summary": {
                "limit_up_count": stats["limit_up_count"],
                "broken_limit_count": stats["broken_limit_count"],
                "consecutive_limit_stocks": stats["consecutive_limit_stocks"],
            }
        })

        # Save as market_breadth snapshot
        save_snapshot(date_str, "market_breadth", {
            "advance_decline_ratio": stats["advance_decline_ratio"],
            "advances": stats["advances"],
            "declines": stats["declines"],
        })

        filled += 1

    logger.info("Backfilled %d days of sentiment snapshots from local data", filled)
    return filled
```

- [ ] **Step 2: Verify import**

Run: `uv run python -c "from alpha_agents.data.market_history import get_local_history, init_history, update_daily, compute_limit_up_stats, backfill_sentiment_snapshots; print('OK')"`

- [ ] **Step 3: Commit**

```bash
git add alpha_agents/data/market_history.py
git commit -m "feat: market history module — local SQLite for full-market daily K-lines"
```

---

### Task 2: Add CLI Command + Wire Into Review

**Files:**
- Modify: `main.py`
- Modify: `alpha_agents/pipeline/tasks/review.py`

- [ ] **Step 1: Add init-history CLI command to main.py**

Add handler:
```python
def cmd_init_history(args: argparse.Namespace) -> None:
    """Initialize full-market history database."""
    from alpha_agents.data.market_history import init_history
    months = getattr(args, 'months', 6)
    logging.info("Initializing market history (%d months)...", months)
    total = init_history(months=months)
    logging.info("Init complete: %d rows", total)
```

Add subparser:
```python
p_init_hist = subparsers.add_parser("init-history", help="初始化全市场历史日线数据（支持断点续传）")
p_init_hist.add_argument("--months", type=int, default=6, help="拉取几个月历史（默认6）")
p_init_hist.set_defaults(func=cmd_init_history)
```

- [ ] **Step 2: Wire update_daily into review.py**

After the daily_archive call in `run_review()`, add:
```python
    # Update local market history DB with today's data
    try:
        from alpha_agents.data.market_history import update_daily
        updated = await asyncio.to_thread(update_daily)
        logger.info("Market history daily update: %d rows", updated)
    except Exception as e:
        logger.warning("Market history update failed: %s", e)
```

- [ ] **Step 3: Verify**

Run: `uv run python main.py init-history --help`

- [ ] **Step 4: Commit**

```bash
git add main.py alpha_agents/pipeline/tasks/review.py
git commit -m "feat: init-history CLI + daily update in review task"
```

---

### Task 3: Wire get_stock_history to Check Local First

**Files:**
- Modify: `alpha_agents/data/market_data.py`

- [ ] **Step 1: Modify get_stock_history to check local DB first**

In `alpha_agents/data/market_data.py`, at the top of `get_stock_history()`, add a local check:

```python
def get_stock_history(code: str, days: int = 5) -> Optional[list[dict]]:
    """Get recent daily OHLCV for a stock. Checks local DB first, falls back to baostock."""
    # Try local market history DB first (fast, no network)
    try:
        from alpha_agents.data.market_history import get_local_history
        local = get_local_history(code, days)
        if local:
            return local
    except Exception:
        pass

    # Fallback to baostock (slow, network)
    with _bs_lock:
        ...existing baostock code...
```

- [ ] **Step 2: Verify**

Run: `uv run python -c "from alpha_agents.data.market_data import get_stock_history; print('OK')"`

- [ ] **Step 3: Commit**

```bash
git add alpha_agents/data/market_data.py
git commit -m "feat: get_stock_history checks local market_history.db first"
```

---

### Task 4: Update sentiment_cycle to Use Local Backfill

**Files:**
- Modify: `alpha_agents/data/sentiment_cycle.py`

- [ ] **Step 1: Replace akshare-based backfill with local data**

In `alpha_agents/data/sentiment_cycle.py`, replace the `backfill_snapshots()` function to try local data first:

```python
def backfill_snapshots(days: int = 10) -> int:
    """Backfill daily_snapshots with historical limit-up data.

    Tries local market_history.db first (fast, covers months).
    Falls back to akshare API (slow, covers ~1 month).
    """
    # Try local market history first
    try:
        from alpha_agents.data.market_history import backfill_sentiment_snapshots
        filled = backfill_sentiment_snapshots(days=days)
        if filled > 0:
            logger.info("Backfilled %d days from local market history", filled)
            return filled
    except Exception as e:
        logger.debug("Local backfill failed, trying akshare: %s", e)

    # Fallback to akshare API (existing code)
    ...keep existing akshare backfill code as fallback...
```

- [ ] **Step 2: Verify**

Run: `uv run python -c "from alpha_agents.data.sentiment_cycle import backfill_snapshots; print('OK')"`

- [ ] **Step 3: Commit and push**

```bash
git add alpha_agents/data/sentiment_cycle.py
git commit -m "feat: sentiment backfill uses local market history, falls back to akshare"
git push
```
