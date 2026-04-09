# Data Archive + Virtual Portfolio Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add daily market data archiving and a virtual portfolio system that tracks every recommendation from open to close with T+1 constraints.

**Architecture:** Two independent modules sharing the same SQLite database (`memory.db`). Daily archive runs as a post-review step. Portfolio has CRUD in `data/portfolio.py`, building logic in morning/intraday tasks, monitoring in intraday task, and reporting in review/weekly tasks.

**Tech Stack:** SQLite (existing `memory.db`), existing Sina realtime quotes API, existing tool functions for data fetching.

---

## File Structure

### New Files

| File | Responsibility |
|------|---------------|
| `alpha_agents/data/daily_archive.py` | Archive today's market data snapshots to `daily_snapshots` table |
| `alpha_agents/data/portfolio.py` | Virtual portfolio CRUD, position monitoring, statistics |
| `tests/test_daily_archive.py` | Tests for archive module |
| `tests/test_portfolio.py` | Tests for portfolio module |

### Modified Files

| File | Changes |
|------|---------|
| `alpha_agents/data/memory_store.py` | Add `daily_snapshots` + `virtual_portfolio` table schemas |
| `alpha_agents/pipeline/tasks/morning_scan.py` | After saving predictions, open virtual positions |
| `alpha_agents/pipeline/tasks/intraday_monitor.py` | After saving recs, open positions; add position monitoring step |
| `alpha_agents/pipeline/tasks/review.py` | Call daily archive; pass portfolio summary to review agent |
| `alpha_agents/pipeline/tasks/weekly_report.py` | Pass portfolio stats to weekly agent |
| `alpha_agents/prompts/review.md` | Add portfolio summary section to output format |
| `alpha_agents/prompts/weekly_report.md` | Add strategy performance section to output format |

---

### Task 1: Add Table Schemas to memory_store.py

**Files:**
- Modify: `alpha_agents/data/memory_store.py:15-60` (the `_SCHEMA` string)
- Test: `tests/test_portfolio.py`

- [ ] **Step 1: Write failing test for new tables**

Create `tests/test_portfolio.py`:

```python
"""Tests for daily_snapshots and virtual_portfolio tables."""
import sqlite3
import tempfile
from pathlib import Path


def _init_db(path: Path) -> sqlite3.Connection:
    """Initialize a test database with the memory_store schema."""
    import alpha_agents.data.memory_store as ms
    original = ms.MEMORY_DB_PATH
    ms.MEMORY_DB_PATH = path
    # Force new connection
    if hasattr(ms._local, "conn"):
        del ms._local.conn
    conn = ms._get_conn()
    ms.MEMORY_DB_PATH = original
    return conn


def test_daily_snapshots_table_exists():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "test_memory.db"
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        # Execute the schema from memory_store
        from alpha_agents.data.memory_store import _SCHEMA
        conn.executescript(_SCHEMA)
        # Verify table exists
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        assert "daily_snapshots" in tables


def test_virtual_portfolio_table_exists():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "test_memory.db"
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        from alpha_agents.data.memory_store import _SCHEMA
        conn.executescript(_SCHEMA)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        assert "virtual_portfolio" in tables


def test_virtual_portfolio_columns():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "test_memory.db"
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        from alpha_agents.data.memory_store import _SCHEMA
        conn.executescript(_SCHEMA)
        row = conn.execute("PRAGMA table_info(virtual_portfolio)").fetchall()
        col_names = {r[1] for r in row}
        expected = {
            "id", "code", "name", "theme", "open_date", "open_price",
            "stop_loss", "target_price", "status", "close_date", "close_price",
            "holding_days", "return_pct", "peak_return_pct", "max_drawdown_pct",
            "source", "reason", "close_reason", "created_at",
        }
        assert expected.issubset(col_names)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_portfolio.py -v`
Expected: FAIL — `daily_snapshots` and `virtual_portfolio` tables not found.

- [ ] **Step 3: Add table schemas to memory_store.py**

In `alpha_agents/data/memory_store.py`, append to the `_SCHEMA` string (before the closing `"""`):

```sql
CREATE TABLE IF NOT EXISTS daily_snapshots (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    data_type TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(date, data_type)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_date ON daily_snapshots(date);

CREATE TABLE IF NOT EXISTS virtual_portfolio (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL,
    name TEXT,
    theme TEXT,
    open_date TEXT NOT NULL,
    open_price REAL NOT NULL,
    stop_loss REAL,
    target_price REAL,
    status TEXT DEFAULT 'open',
    close_date TEXT,
    close_price REAL,
    holding_days INTEGER DEFAULT 0,
    return_pct REAL,
    peak_return_pct REAL DEFAULT 0,
    max_drawdown_pct REAL DEFAULT 0,
    source TEXT,
    reason TEXT,
    close_reason TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_portfolio_status ON virtual_portfolio(status);
CREATE INDEX IF NOT EXISTS idx_portfolio_date ON virtual_portfolio(open_date);
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_portfolio.py -v`
Expected: All 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add alpha_agents/data/memory_store.py tests/test_portfolio.py
git commit -m "feat: add daily_snapshots and virtual_portfolio table schemas"
```

---

### Task 2: Implement Daily Archive Module

**Files:**
- Create: `alpha_agents/data/daily_archive.py`
- Test: `tests/test_daily_archive.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_daily_archive.py`:

```python
"""Tests for daily data archiving."""
import json
from unittest.mock import patch


def test_save_and_get_snapshot():
    """Test that save_snapshot writes and get_snapshot reads correctly."""
    from alpha_agents.data.daily_archive import save_snapshot, get_snapshot

    with patch("alpha_agents.data.daily_archive._get_conn") as mock_conn:
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        from alpha_agents.data.memory_store import _SCHEMA
        conn.executescript(_SCHEMA)
        mock_conn.return_value = conn

        # Save
        data = {"gainers": [{"concept": "芯片", "change_pct": 3.5}]}
        save_snapshot("2026-04-09", "concept_fund_flow", data)

        # Read back
        result = get_snapshot("2026-04-09", "concept_fund_flow")
        assert result is not None
        assert result["gainers"][0]["concept"] == "芯片"


def test_save_snapshot_upserts():
    """Test that saving the same date+type overwrites."""
    from alpha_agents.data.daily_archive import save_snapshot, get_snapshot

    with patch("alpha_agents.data.daily_archive._get_conn") as mock_conn:
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        from alpha_agents.data.memory_store import _SCHEMA
        conn.executescript(_SCHEMA)
        mock_conn.return_value = conn

        save_snapshot("2026-04-09", "lhb", {"count": 10})
        save_snapshot("2026-04-09", "lhb", {"count": 20})

        result = get_snapshot("2026-04-09", "lhb")
        assert result["count"] == 20


def test_run_daily_archive_calls_all_sources(monkeypatch):
    """Test that run_daily_archive attempts to archive all data types."""
    from alpha_agents.data import daily_archive
    archived = []

    def fake_save(date, data_type, data):
        archived.append(data_type)

    monkeypatch.setattr(daily_archive, "save_snapshot", fake_save)

    # Mock all data source functions to return valid JSON
    for fn_name in [
        "get_concept_ranking_fn", "get_sector_ranking_fn",
        "get_anomaly_stocks_fn", "get_market_breadth_fn",
        "get_lhb_detail_fn", "get_block_trade_fn",
        "get_north_flow_fn", "get_margin_data_fn",
    ]:
        monkeypatch.setattr(
            daily_archive, fn_name,
            lambda *a, **kw: json.dumps({"data": []}),
        )

    # Mock get_active_themes to return empty (skip theme_fund_flow)
    monkeypatch.setattr(daily_archive, "get_active_themes", lambda: [])

    daily_archive.run_daily_archive()

    expected_types = {
        "concept_fund_flow", "industry_fund_flow", "limit_up_pool",
        "market_breadth", "lhb", "block_trade", "north_flow", "margin",
    }
    assert expected_types.issubset(set(archived))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_daily_archive.py -v`
Expected: FAIL — module `daily_archive` does not exist.

- [ ] **Step 3: Implement daily_archive.py**

Create `alpha_agents/data/daily_archive.py`:

```python
"""Daily market data archiving — snapshot all key data sources for future backtesting.

Called after the review task (15:30). Pure data collection, no LLM involved.
"""

import json
import logging
import time

from alpha_agents.data.memory_store import _get_conn, _write_lock, get_active_themes
from alpha_agents.tools.sector_ranking import get_sector_ranking_fn, get_concept_ranking_fn
from alpha_agents.tools.anomaly_detect import get_anomaly_stocks_fn
from alpha_agents.tools.market_breadth import get_market_breadth_fn
from alpha_agents.tools.fund_flow import (
    get_lhb_detail_fn, get_block_trade_fn,
    get_north_flow_fn, get_margin_data_fn,
    get_stock_fund_flow_fn,
)

logger = logging.getLogger(__name__)


def save_snapshot(date: str, data_type: str, data: dict) -> None:
    """Save a data snapshot. Upserts on (date, data_type)."""
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO daily_snapshots (date, data_type, data) VALUES (?, ?, ?) "
            "ON CONFLICT(date, data_type) DO UPDATE SET data = excluded.data, "
            "created_at = datetime('now')",
            (date, data_type, json.dumps(data, ensure_ascii=False)),
        )
        conn.commit()


def get_snapshot(date: str, data_type: str) -> dict | None:
    """Read a snapshot back. Returns parsed JSON or None."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT data FROM daily_snapshots WHERE date = ? AND data_type = ?",
        (date, data_type),
    ).fetchone()
    if row:
        return json.loads(row["data"])
    return None


def run_daily_archive() -> int:
    """Archive all data sources for today. Returns count of successful archives."""
    today = time.strftime("%Y-%m-%d")
    archived = 0

    sources = [
        ("concept_fund_flow", lambda: get_concept_ranking_fn(top_n=50)),
        ("industry_fund_flow", lambda: get_sector_ranking_fn(top_n=30)),
        ("limit_up_pool", lambda: get_anomaly_stocks_fn()),
        ("market_breadth", lambda: get_market_breadth_fn()),
        ("lhb", lambda: get_lhb_detail_fn()),
        ("block_trade", lambda: get_block_trade_fn()),
        ("north_flow", lambda: get_north_flow_fn("today")),
        ("margin", lambda: get_margin_data_fn()),
    ]

    for data_type, fetch_fn in sources:
        try:
            raw = fetch_fn()
            data = json.loads(raw)
            save_snapshot(today, data_type, data)
            archived += 1
            logger.info("Archived %s for %s", data_type, today)
        except Exception as e:
            logger.warning("Failed to archive %s: %s", data_type, e)

    # Archive fund flow for active theme stocks
    try:
        themes = get_active_themes()
        theme_flows = {}
        for theme in themes:
            stocks = json.loads(theme["core_stocks"]) if theme.get("core_stocks") else []
            for stock in stocks[:5]:
                code = stock.get("code", "")
                if code and code not in theme_flows:
                    try:
                        raw = get_stock_fund_flow_fn(code)
                        theme_flows[code] = json.loads(raw)
                    except Exception:
                        pass
        if theme_flows:
            save_snapshot(today, "theme_fund_flow", theme_flows)
            archived += 1
            logger.info("Archived theme_fund_flow (%d stocks) for %s", len(theme_flows), today)
    except Exception as e:
        logger.warning("Failed to archive theme_fund_flow: %s", e)

    logger.info("Daily archive complete: %d/%d sources archived", archived, len(sources) + 1)
    return archived
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_daily_archive.py -v`
Expected: All 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add alpha_agents/data/daily_archive.py tests/test_daily_archive.py
git commit -m "feat: daily data archive module for future backtesting"
```

---

### Task 3: Implement Portfolio CRUD and Monitoring

**Files:**
- Create: `alpha_agents/data/portfolio.py`
- Modify: `tests/test_portfolio.py` (add more tests)

- [ ] **Step 1: Write failing tests for portfolio CRUD**

Append to `tests/test_portfolio.py`:

```python
import sqlite3
from unittest.mock import patch


def _make_test_conn():
    """Create an in-memory DB with schema for testing."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    from alpha_agents.data.memory_store import _SCHEMA
    conn.executescript(_SCHEMA)
    return conn


def test_open_position():
    from alpha_agents.data.portfolio import open_position, get_open_positions

    with patch("alpha_agents.data.portfolio._get_conn") as mock:
        mock.return_value = _make_test_conn()
        pos_id = open_position(
            code="300475", name="香农芯创", theme="芯片概念",
            open_date="2026-04-09", open_price=147.11,
            stop_loss=135.0, target_price=165.0,
            source="morning", reason="机构看多",
        )
        assert pos_id > 0
        positions = get_open_positions()
        assert len(positions) == 1
        assert positions[0]["code"] == "300475"
        assert positions[0]["status"] == "open"


def test_no_duplicate_open_position():
    from alpha_agents.data.portfolio import open_position, get_open_positions

    with patch("alpha_agents.data.portfolio._get_conn") as mock:
        mock.return_value = _make_test_conn()
        open_position(code="300475", name="香农芯创", theme="芯片",
                      open_date="2026-04-09", open_price=147.0,
                      source="morning", reason="test")
        # Second open for same code should be skipped
        pos_id = open_position(code="300475", name="香农芯创", theme="芯片",
                               open_date="2026-04-09", open_price=150.0,
                               source="intraday", reason="test2")
        assert pos_id is None
        assert len(get_open_positions()) == 1


def test_close_position():
    from alpha_agents.data.portfolio import open_position, close_position, get_open_positions

    with patch("alpha_agents.data.portfolio._get_conn") as mock:
        mock.return_value = _make_test_conn()
        pos_id = open_position(code="300475", name="香农芯创", theme="芯片",
                               open_date="2026-04-08", open_price=140.0,
                               source="morning", reason="test")
        close_position(pos_id, close_price=130.0, close_reason="止损触发")
        positions = get_open_positions()
        assert len(positions) == 0


def test_check_positions_stop_loss():
    from alpha_agents.data.portfolio import open_position, check_positions

    with patch("alpha_agents.data.portfolio._get_conn") as mock:
        conn = _make_test_conn()
        mock.return_value = conn
        open_position(code="300475", name="香农芯创", theme="芯片",
                      open_date="2026-04-08", open_price=147.0,
                      stop_loss=135.0, source="morning", reason="test")
        # Simulate price below stop loss
        alerts = check_positions(
            realtime_prices={"300475": 130.0},
            today="2026-04-09",
        )
        assert len(alerts) == 1
        assert alerts[0]["type"] == "stopped"
        assert alerts[0]["code"] == "300475"


def test_check_positions_t1_skip():
    """T+1: positions opened today should NOT be checked."""
    from alpha_agents.data.portfolio import open_position, check_positions

    with patch("alpha_agents.data.portfolio._get_conn") as mock:
        mock.return_value = _make_test_conn()
        open_position(code="300475", name="香农芯创", theme="芯片",
                      open_date="2026-04-09", open_price=147.0,
                      stop_loss=135.0, source="morning", reason="test")
        # Same day — should skip even if price is below stop
        alerts = check_positions(
            realtime_prices={"300475": 100.0},
            today="2026-04-09",
        )
        assert len(alerts) == 0


def test_get_portfolio_stats():
    from alpha_agents.data.portfolio import open_position, close_position, get_portfolio_stats

    with patch("alpha_agents.data.portfolio._get_conn") as mock:
        mock.return_value = _make_test_conn()
        id1 = open_position(code="300475", name="A", theme="芯片",
                            open_date="2026-04-07", open_price=100.0,
                            source="morning", reason="t")
        close_position(id1, close_price=110.0, close_reason="止盈触发")
        id2 = open_position(code="000586", name="B", theme="通信",
                            open_date="2026-04-07", open_price=100.0,
                            source="morning", reason="t")
        close_position(id2, close_price=93.0, close_reason="止损触发")

        stats = get_portfolio_stats(days=7)
        assert stats["total_closed"] == 2
        assert stats["wins"] == 1
        assert stats["losses"] == 1
        assert stats["win_rate"] == 50.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_portfolio.py -v -k "test_open or test_no_dup or test_close or test_check or test_get_portfolio"`
Expected: FAIL — module `portfolio` does not exist.

- [ ] **Step 3: Implement portfolio.py**

Create `alpha_agents/data/portfolio.py`:

```python
"""Virtual portfolio — track recommendations from open to close.

Simulates holding positions with stop-loss/take-profit monitoring.
T+1 constraint: positions opened today are not checked until tomorrow.
"""

import json
import logging
from datetime import datetime

from alpha_agents.data.memory_store import _get_conn, _write_lock, get_theme_by_name

logger = logging.getLogger(__name__)

MAX_HOLDING_DAYS = 5


def open_position(
    *,
    code: str,
    name: str,
    theme: str,
    open_date: str,
    open_price: float,
    stop_loss: float | None = None,
    target_price: float | None = None,
    source: str = "morning",
    reason: str = "",
) -> int | None:
    """Open a virtual position. Returns position id, or None if duplicate."""
    with _write_lock:
        conn = _get_conn()
        # Check for existing open position on same stock
        existing = conn.execute(
            "SELECT id FROM virtual_portfolio WHERE code = ? AND status = 'open'",
            (code,),
        ).fetchone()
        if existing:
            logger.info("Position already open for %s %s, skipping", code, name)
            return None

        cursor = conn.execute(
            "INSERT INTO virtual_portfolio "
            "(code, name, theme, open_date, open_price, stop_loss, target_price, "
            " status, source, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)",
            (code, name, theme, open_date, open_price, stop_loss, target_price,
             source, reason),
        )
        conn.commit()
        logger.info("Opened position: %s %s @ %.2f (stop=%.2f, target=%s)",
                     code, name, open_price,
                     stop_loss or 0, target_price or "none")
        return cursor.lastrowid


def close_position(
    position_id: int,
    *,
    close_price: float,
    close_reason: str,
) -> None:
    """Close a virtual position."""
    with _write_lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT open_price, open_date, peak_return_pct FROM virtual_portfolio WHERE id = ?",
            (position_id,),
        ).fetchone()
        if not row:
            return

        open_price = row["open_price"]
        return_pct = round((close_price - open_price) / open_price * 100, 2) if open_price else 0
        today = datetime.now().strftime("%Y-%m-%d")

        conn.execute(
            "UPDATE virtual_portfolio SET "
            "status = ?, close_date = ?, close_price = ?, return_pct = ?, "
            "close_reason = ? WHERE id = ?",
            (_status_from_reason(close_reason), today, close_price, return_pct,
             close_reason, position_id),
        )
        conn.commit()
        logger.info("Closed position #%d @ %.2f (return=%.2f%%, reason=%s)",
                     position_id, close_price, return_pct, close_reason)


def _status_from_reason(reason: str) -> str:
    if "止损" in reason:
        return "stopped"
    if "止盈" in reason:
        return "target_hit"
    return "expired"


def get_open_positions() -> list[dict]:
    """Get all currently open positions."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM virtual_portfolio WHERE status = 'open' ORDER BY open_date"
    ).fetchall()
    return [dict(r) for r in rows]


def check_positions(
    realtime_prices: dict[str, float],
    today: str,
) -> list[dict]:
    """Check open positions against realtime prices. Returns list of alerts.

    Respects T+1: skips positions where open_date == today.
    """
    conn = _get_conn()
    positions = conn.execute(
        "SELECT * FROM virtual_portfolio WHERE status = 'open' AND open_date < ?",
        (today,),
    ).fetchall()

    alerts = []
    for pos in positions:
        pos = dict(pos)
        code = pos["code"]
        price = realtime_prices.get(code)
        if price is None or price <= 0:
            continue

        open_price = pos["open_price"]
        current_return = round((price - open_price) / open_price * 100, 2) if open_price else 0

        # Update peak and drawdown
        peak = max(pos.get("peak_return_pct", 0) or 0, current_return)
        drawdown = round(peak - current_return, 2) if peak > 0 else 0

        # Count holding days (simple: business days between open_date and today)
        try:
            open_dt = datetime.strptime(pos["open_date"], "%Y-%m-%d")
            today_dt = datetime.strptime(today, "%Y-%m-%d")
            holding_days = max(0, (today_dt - open_dt).days)
        except ValueError:
            holding_days = pos.get("holding_days", 0)

        # Update tracking fields
        with _write_lock:
            conn.execute(
                "UPDATE virtual_portfolio SET peak_return_pct = ?, "
                "max_drawdown_pct = ?, holding_days = ? WHERE id = ?",
                (peak, drawdown, holding_days, pos["id"]),
            )
            conn.commit()

        # Check triggers (priority order)
        alert = None
        stop_loss = pos.get("stop_loss")
        target_price = pos.get("target_price")

        if stop_loss and price <= stop_loss:
            alert = {"type": "stopped", "reason": "止损触发"}
        elif target_price and price >= target_price:
            alert = {"type": "target_hit", "reason": "止盈触发"}
        elif pos.get("theme"):
            theme = get_theme_by_name(pos["theme"])
            if theme and theme.get("status") in ("declining", "archived"):
                alert = {"type": "expired", "reason": f"主线衰退({pos['theme']}已{theme['status']})"}
        if not alert and holding_days >= MAX_HOLDING_DAYS:
            alert = {"type": "expired", "reason": f"持仓到期({holding_days}天)"}

        if alert:
            close_position(pos["id"], close_price=price, close_reason=alert["reason"])
            alert.update({
                "code": code,
                "name": pos.get("name", ""),
                "open_price": open_price,
                "close_price": price,
                "return_pct": current_return,
                "holding_days": holding_days,
            })
            alerts.append(alert)

    return alerts


def get_open_positions_summary() -> str:
    """Format open positions as text for agent context."""
    positions = get_open_positions()
    if not positions:
        return "当前无持仓"
    lines = []
    for p in positions:
        lines.append(
            f"  {p['code']} {p.get('name','')} | 建仓{p['open_date']} @ {p['open_price']:.2f} | "
            f"止损{p.get('stop_loss', '无')} | 来源{p.get('source', '?')} | "
            f"持仓{p.get('holding_days', 0)}天"
        )
    return f"当前持仓 {len(positions)} 笔:\n" + "\n".join(lines)


def get_today_changes_summary(today: str) -> str:
    """Format today's portfolio changes for agent context."""
    conn = _get_conn()
    opened = conn.execute(
        "SELECT code, name, open_price, source FROM virtual_portfolio WHERE open_date = ?",
        (today,),
    ).fetchall()
    closed = conn.execute(
        "SELECT code, name, close_price, return_pct, close_reason "
        "FROM virtual_portfolio WHERE close_date = ?",
        (today,),
    ).fetchall()

    lines = []
    if opened:
        lines.append(f"今日建仓 {len(opened)} 笔:")
        for r in opened:
            lines.append(f"  {r['code']} {r['name']} @ {r['open_price']:.2f} ({r['source']})")
    if closed:
        lines.append(f"今日平仓 {len(closed)} 笔:")
        for r in closed:
            ret = r['return_pct'] or 0
            lines.append(
                f"  {r['code']} {r['name']} @ {r['close_price']:.2f} "
                f"({'盈' if ret >= 0 else '亏'}{abs(ret):.1f}%) — {r['close_reason']}"
            )
    return "\n".join(lines) if lines else "今日无持仓变动"


def get_portfolio_stats(days: int = 7) -> dict:
    """Get portfolio performance statistics for recent N days."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM virtual_portfolio WHERE status != 'open' "
        "ORDER BY close_date DESC LIMIT ?",
        (days * 10,),
    ).fetchall()

    if not rows:
        return {
            "total_closed": 0, "wins": 0, "losses": 0, "win_rate": 0,
            "avg_return": 0, "max_win": 0, "max_loss": 0,
            "avg_holding_days": 0, "by_theme": {}, "by_source": {},
        }

    total = len(rows)
    returns = [r["return_pct"] or 0 for r in rows]
    wins = sum(1 for ret in returns if ret > 0)
    losses = sum(1 for ret in returns if ret <= 0)

    # By theme
    by_theme: dict[str, list[float]] = {}
    for r in rows:
        theme = r["theme"] or "未知"
        by_theme.setdefault(theme, []).append(r["return_pct"] or 0)

    theme_stats = {}
    for theme, rets in by_theme.items():
        theme_stats[theme] = {
            "count": len(rets),
            "win_rate": round(sum(1 for r in rets if r > 0) / len(rets) * 100, 1),
            "avg_return": round(sum(rets) / len(rets), 2),
        }

    # By source
    by_source: dict[str, list[float]] = {}
    for r in rows:
        src = r["source"] or "未知"
        by_source.setdefault(src, []).append(r["return_pct"] or 0)

    source_stats = {}
    for src, rets in by_source.items():
        source_stats[src] = {
            "count": len(rets),
            "win_rate": round(sum(1 for r in rets if r > 0) / len(rets) * 100, 1),
            "avg_return": round(sum(rets) / len(rets), 2),
        }

    return {
        "total_closed": total,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / total * 100, 1) if total else 0,
        "avg_return": round(sum(returns) / total, 2) if total else 0,
        "max_win": round(max(returns), 2) if returns else 0,
        "max_loss": round(min(returns), 2) if returns else 0,
        "avg_holding_days": round(sum(r["holding_days"] or 0 for r in rows) / total, 1) if total else 0,
        "by_theme": theme_stats,
        "by_source": source_stats,
    }


def format_portfolio_stats(stats: dict) -> str:
    """Format portfolio stats as text for agent/report context."""
    if stats["total_closed"] == 0:
        return "暂无已平仓记录"

    lines = [
        f"已平仓: {stats['total_closed']}笔 | 胜率: {stats['win_rate']:.1f}% "
        f"({stats['wins']}胜{stats['losses']}负)",
        f"平均收益: {stats['avg_return']:+.2f}% | 最大盈利: {stats['max_win']:+.2f}% | "
        f"最大亏损: {stats['max_loss']:+.2f}%",
        f"平均持仓: {stats['avg_holding_days']:.1f}天",
    ]

    if stats["by_theme"]:
        lines.append("按主线归因:")
        for theme, ts in stats["by_theme"].items():
            lines.append(f"  {theme}: {ts['count']}笔, 胜率{ts['win_rate']:.0f}%, 均收{ts['avg_return']:+.2f}%")

    if stats["by_source"]:
        lines.append("按来源归因:")
        for src, ss in stats["by_source"].items():
            lines.append(f"  {src}: {ss['count']}笔, 胜率{ss['win_rate']:.0f}%, 均收{ss['avg_return']:+.2f}%")

    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_portfolio.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add alpha_agents/data/portfolio.py tests/test_portfolio.py
git commit -m "feat: virtual portfolio CRUD, monitoring, and statistics"
```

---

### Task 4: Wire Portfolio into Morning Scan

**Files:**
- Modify: `alpha_agents/pipeline/tasks/morning_scan.py`

- [ ] **Step 1: Add import and portfolio opening logic**

At the top of `morning_scan.py`, add:

```python
from alpha_agents.data.portfolio import open_position
```

Find the `_save_recommendations_list` function. After each successful `save_prediction(...)` call (around line 376-390), add portfolio opening:

```python
            # Open virtual position
            try:
                stop_loss_val = _parse_stop_loss(r.get("action", ""))
                open_position(
                    code=code,
                    name=r.get("name", ""),
                    theme=r.get("theme", ""),
                    open_date=today,
                    open_price=entry_prices.get(code) or 0,
                    stop_loss=stop_loss_val,
                    source="morning",
                    reason=r.get("reason", "")[:100],
                )
            except Exception as e:
                logger.debug("Failed to open position for %s: %s", code, e)
```

Add the helper function above `_save_recommendations_list`:

```python
def _parse_stop_loss(action_text: str) -> float | None:
    """Extract stop loss price from action text like '止损50元' or '止损50.5'."""
    import re
    match = re.search(r"止损[：:\s]*(\d+\.?\d*)\s*元?", action_text)
    if match:
        return float(match.group(1))
    return None
```

- [ ] **Step 2: Test manually**

Run: `uv run python -c "from alpha_agents.pipeline.tasks.morning_scan import _parse_stop_loss; print(_parse_stop_loss('回调至52元可介入，止损50元')); print(_parse_stop_loss('止损21.57元')); print(_parse_stop_loss('无止损'))"`

Expected:
```
50.0
21.57
None
```

- [ ] **Step 3: Commit**

```bash
git add alpha_agents/pipeline/tasks/morning_scan.py
git commit -m "feat: open virtual positions from morning scan recommendations"
```

---

### Task 5: Wire Portfolio into Intraday Monitor

**Files:**
- Modify: `alpha_agents/pipeline/tasks/intraday_monitor.py`

- [ ] **Step 1: Add imports**

At the top of `intraday_monitor.py`, add:

```python
from alpha_agents.data.portfolio import open_position, check_positions, get_open_positions
from alpha_agents.data.market_data import get_realtime_quotes
```

- [ ] **Step 2: Add position opening in _save_intraday_recommendations**

In `_save_intraday_recommendations`, after the `save_prediction(...)` call for `actionable` type (not signal), add:

```python
        if rec_type != "signal" and entry_price:
            try:
                stop_loss_val = _parse_stop_loss(r.get("action", ""))
                open_position(
                    code=code,
                    name=r.get("name", ""),
                    theme=r.get("theme", ""),
                    open_date=today,
                    open_price=entry_price,
                    stop_loss=stop_loss_val,
                    source="intraday",
                    reason=r.get("reason", "")[:100],
                )
            except Exception as e:
                logger.debug("Failed to open intraday position for %s: %s", code, e)
```

Add `_parse_stop_loss` (same as morning_scan version):

```python
def _parse_stop_loss(action_text: str) -> float | None:
    """Extract stop loss price from action text."""
    import re
    match = re.search(r"止损[：:\s]*(\d+\.?\d*)\s*元?", action_text)
    if match:
        return float(match.group(1))
    return None
```

- [ ] **Step 3: Add position monitoring in run_intraday_monitor**

In `run_intraday_monitor()`, right after the lunch break check and before the theme/anomaly detection section, add a position monitoring block:

```python
    # ── Check existing positions (T+1 constraint handled by check_positions) ──
    today_str = now.strftime("%Y-%m-%d")
    open_pos = get_open_positions()
    if open_pos:
        codes = [p["code"] for p in open_pos]
        rt_prices = await asyncio.to_thread(get_realtime_quotes, codes)
        if rt_prices:
            price_map = {code: data["price"] for code, data in rt_prices.items()}
            alerts = check_positions(realtime_prices=price_map, today=today_str)
            for alert in alerts:
                msg = _format_portfolio_alert(alert)
                logger.info("Portfolio alert: %s", msg)
                try:
                    await asyncio.to_thread(notify_all, "AlphaAgents 持仓提醒", msg)
                except Exception:
                    pass
```

Add the alert formatter:

```python
def _format_portfolio_alert(alert: dict) -> str:
    """Format a portfolio alert for notification."""
    code = alert["code"]
    name = alert.get("name", "")
    ret = alert.get("return_pct", 0)
    reason = alert.get("reason", "")
    close_price = alert.get("close_price", 0)
    sign = "盈" if ret >= 0 else "亏"
    return f"{reason} | {code} {name} 平仓价{close_price:.2f}元（{sign}{abs(ret):.1f}%）"
```

- [ ] **Step 4: Verify import works**

Run: `uv run python -c "from alpha_agents.pipeline.tasks.intraday_monitor import run_intraday_monitor; print('OK')"`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add alpha_agents/pipeline/tasks/intraday_monitor.py
git commit -m "feat: intraday position opening and T+1 monitoring with alerts"
```

---

### Task 6: Wire Archive and Portfolio into Review Task

**Files:**
- Modify: `alpha_agents/pipeline/tasks/review.py`
- Modify: `alpha_agents/prompts/review.md`

- [ ] **Step 1: Add imports to review.py**

Add at the top of `review.py`:

```python
from alpha_agents.data.daily_archive import run_daily_archive
from alpha_agents.data.portfolio import (
    get_open_positions_summary, get_today_changes_summary, get_portfolio_stats,
    format_portfolio_stats,
)
```

- [ ] **Step 2: Add portfolio context to the review agent's input**

In `run_review()`, find where the user message is built for the review agent. Add portfolio context to it. Locate the call to `run_review_analysis(...)` and add portfolio summary to the context being passed. Before that call, add:

```python
    # Portfolio context
    today_str = datetime.now().strftime("%Y-%m-%d")
    portfolio_ctx = ""
    try:
        pos_summary = get_open_positions_summary()
        changes_summary = get_today_changes_summary(today_str)
        perf_stats = format_portfolio_stats(get_portfolio_stats(days=7))
        portfolio_ctx = (
            f"【虚拟持仓状态】\n{pos_summary}\n\n"
            f"【今日持仓变动】\n{changes_summary}\n\n"
            f"【近7天策略表现】\n{perf_stats}"
        )
    except Exception as e:
        logger.debug("Failed to build portfolio context: %s", e)
```

Then pass `portfolio_ctx` as part of the agent's input context.

- [ ] **Step 3: Call daily archive at the end of run_review()**

At the very end of `run_review()`, before the return, add:

```python
    # Archive today's market data for future backtesting
    try:
        await asyncio.to_thread(run_daily_archive)
    except Exception as e:
        logger.warning("Daily archive failed: %s", e)
```

- [ ] **Step 4: Update review.md prompt**

In `alpha_agents/prompts/review.md`, after the `【经验总结】` section in the output format, add:

```markdown
【持仓状态】
• 当前持仓: X笔, 浮盈/浮亏情况
• 今日建仓: X笔 | 今日平仓: X笔（止损X笔、止盈X笔、到期X笔）
• 近7天策略表现: 胜率X%, 平均收益X%
```

- [ ] **Step 5: Commit**

```bash
git add alpha_agents/pipeline/tasks/review.py alpha_agents/prompts/review.md
git commit -m "feat: wire daily archive and portfolio context into review task"
```

---

### Task 7: Wire Portfolio Stats into Weekly Report

**Files:**
- Modify: `alpha_agents/pipeline/tasks/weekly_report.py`
- Modify: `alpha_agents/prompts/weekly_report.md`

- [ ] **Step 1: Add portfolio stats to weekly report context**

In `weekly_report.py`, add import:

```python
from alpha_agents.data.portfolio import get_portfolio_stats, format_portfolio_stats
```

In `run_weekly_report()`, after gathering `stats_text`, add:

```python
    # Portfolio performance
    portfolio_stats = get_portfolio_stats(days=7)
    portfolio_text = format_portfolio_stats(portfolio_stats)
```

Add `portfolio_text` to the `user_message` passed to the agent:

```python
    user_message = (
        f"[当前时间: {now.strftime('%Y-%m-%d %H:%M')}]\n\n"
        f"【本周预测统计】\n{stats_text}\n"
        f"【本周策略表现（虚拟持仓）】\n{portfolio_text}\n"
        f"【活跃主线】\n{themes_text}\n"
        f"请生成本周周报。"
    )
```

- [ ] **Step 2: Update weekly_report.md prompt**

In `alpha_agents/prompts/weekly_report.md`, after `【本周战绩】` section, add:

```markdown
【本周策略表现（虚拟持仓）】
建仓: X笔 | 平仓: Y笔 | 当前持仓: Z笔
胜率: XX%（止盈+正收益过期 / 总平仓）
平均收益: X% | 最大单笔盈利: +X% | 最大单笔亏损: -X%
平均持仓天数: X天

按主线归因:
• {主线名}: X笔, 胜率 X%, 平均收益 X%

按来源归因:
• 晨扫推荐: X笔, 胜率 X%
• 盘中推荐: X笔, 胜率 X%

累计（自系统启动）:
总交易: XX笔 | 累计胜率: XX% | 累计收益: XX%
```

- [ ] **Step 3: Verify import works**

Run: `uv run python -c "from alpha_agents.pipeline.tasks.weekly_report import run_weekly_report; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add alpha_agents/pipeline/tasks/weekly_report.py alpha_agents/prompts/weekly_report.md
git commit -m "feat: add portfolio strategy performance to weekly report"
```

---

### Task 8: Integration Smoke Test

**Files:**
- No new files

- [ ] **Step 1: Verify all imports work**

```bash
uv run python -c "
from alpha_agents.data.daily_archive import run_daily_archive, save_snapshot, get_snapshot
from alpha_agents.data.portfolio import (
    open_position, close_position, get_open_positions, check_positions,
    get_portfolio_stats, format_portfolio_stats,
    get_open_positions_summary, get_today_changes_summary,
)
from alpha_agents.pipeline.tasks.morning_scan import _parse_stop_loss
from alpha_agents.pipeline.tasks.intraday_monitor import _parse_stop_loss as _ps2
from alpha_agents.pipeline.tasks.review import run_review
from alpha_agents.pipeline.tasks.weekly_report import run_weekly_report
print('All imports OK')
"
```

Expected: `All imports OK`

- [ ] **Step 2: Run full test suite**

```bash
uv run pytest tests/test_portfolio.py tests/test_daily_archive.py -v
```

Expected: All tests PASS.

- [ ] **Step 3: Verify tables are created in existing DB**

```bash
uv run python -c "
from alpha_agents.data.memory_store import _get_conn
conn = _get_conn()
tables = [r[0] for r in conn.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()]
print('Tables:', tables)
assert 'daily_snapshots' in tables, 'daily_snapshots missing'
assert 'virtual_portfolio' in tables, 'virtual_portfolio missing'
print('Schema OK')
"
```

Expected: Tables listed including `daily_snapshots` and `virtual_portfolio`.

- [ ] **Step 4: Commit all remaining changes**

```bash
git add -A
git status
git commit -m "feat: complete data archive + virtual portfolio system"
```
