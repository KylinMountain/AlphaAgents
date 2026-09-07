"""Local market history database — full-market daily K-lines in SQLite.

Stores ~5000 stocks × 120 trading days in data/market_history.db.
Provides fast local queries, batch initialization, and daily updates.
"""

import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from functools import lru_cache
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

def get_local_history(code: str, days: int = 5,
                      as_of: str | None = None) -> list[dict] | None:
    """Get recent K-lines from local DB. Returns None if not enough data.

    ``as_of`` (YYYY-MM-DD): if set, only return bars with ``date <= as_of``
    — used by backtest/replay to get "historical view" of the stock.
    """
    conn = _get_conn()
    if as_of:
        rows = conn.execute(
            "SELECT * FROM daily_kline WHERE code = ? AND date <= ? "
            "ORDER BY date DESC LIMIT ?",
            (code, as_of, days),
        ).fetchall()
    else:
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


@lru_cache(maxsize=512)
def get_latest_trading_day_at_or_before(cut: str) -> str | None:
    """Most recent market-wide trading day in daily_kline with date <= cut.

    Used by VPA loaders to detect suspended stocks: if a stock's most
    recent bar is older than the market-wide latest trading day, the
    stock has missed sessions and analysis would use stale data.

    Memoized — backtests call per-stock per-day but ``cut`` only takes
    a few hundred distinct values across a multi-month run.
    """
    conn = _get_conn()
    row = conn.execute(
        "SELECT MAX(date) FROM daily_kline WHERE date <= ?", (cut,)
    ).fetchone()
    return row[0] if row and row[0] else None


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


def get_all_codes(*, include_inactive: bool = True,
                  start_date: str | None = None) -> list[str]:
    """Get A-share stock codes from baostock.

    For historical initialization, include stocks that were delisted after the
    requested start date. This is still limited by baostock's basic table, but
    avoids the obvious survivorship bias from using only today's active names.
    """
    _bs_login()
    try:
        rs = bs.query_stock_basic()
        codes = []
        while rs.error_code == "0" and rs.next():
            row = rs.get_row_data()
            # row: [code, code_name, ipoDate, outDate, type, status]
            if row[4] != "1":  # type=stock
                continue
            out_date = row[3] if len(row) > 3 else ""
            is_active = row[5] == "1"
            overlaps_window = bool(start_date and out_date and out_date >= start_date)
            if not is_active and not (include_inactive and overlaps_window):
                continue
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
        all_codes = get_all_codes(include_inactive=True, start_date=start_date)
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


def _fetch_batch(
    codes: list[str],
    start_date: str,
    end_date: str,
    *,
    progress_every: int = 500,
) -> list[tuple]:
    """Fetch history for a batch of stock codes from baostock.

    Logs a progress line every ``progress_every`` codes (INFO), tracking
    got/empty/error counts so silent baostock failures (session eviction,
    delayed post-close publishing) are visible. Set progress_every=0 to
    silence progress (e.g. inside init_history's own batch loop).
    """
    _bs_login()
    rows = []
    n = len(codes)
    got = empty = errors = 0
    try:
        for i, code in enumerate(codes, 1):
            bs_code = _to_bs_code(code)
            code_rows_before = len(rows)
            try:
                rs = bs.query_history_k_data_plus(
                    bs_code,
                    "date,open,high,low,close,volume,turn,pctChg",
                    start_date=start_date, end_date=end_date,
                    frequency="d", adjustflag="2",
                )
                if rs.error_code != "0":
                    errors += 1
                else:
                    while rs.next():
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
                    if len(rows) > code_rows_before:
                        got += 1
                    else:
                        empty += 1
            except Exception:
                errors += 1
            if progress_every > 0 and (i % progress_every == 0 or i == n):
                logger.info(
                    "Fetch progress %d/%d (got=%d empty=%d err=%d rows=%d)",
                    i, n, got, empty, errors, len(rows),
                )
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

def _ensure_progress_visible() -> None:
    """Attach a stderr handler to the root logger if nothing is consuming INFO.

    Lets ``python -c "...; update_daily()"`` show progress lines without
    boilerplate logging config. No-op inside the scheduler, which already
    configures root logging at startup.
    """
    import sys
    root = logging.getLogger()
    if root.handlers:
        return
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"))
    root.addHandler(h)
    root.setLevel(logging.INFO)


def update_daily() -> int:
    """Fetch today's data for all stocks. Called after market close."""
    _ensure_progress_visible()
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
    - Normal (60xxxx, 00xxxx): +-10% -> limit at >= 9.8%
    - ChiNext/STAR (300xxx, 688xxx): +-20% -> limit at >= 19.8%
    - ST stocks: +-5% -> limit at >= 4.8% (we skip ST for simplicity)
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
            # high_change ~ change_pct * (high / close) -- rough approximation
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
