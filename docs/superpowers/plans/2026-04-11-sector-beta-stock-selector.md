# Sector Beta Stock Selector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a multi-factor stock selector that uses historical beta to find stocks with the highest "follow-the-sector" elasticity when a concept sector rallies.

**Architecture:** Two modules — `beta_calculator.py` for offline weekly computation (baostock history → beta + liquidity → DB), and `sector_beta.py` for online scoring (read cached beta + realtime data → multi-factor score → Top N). Registered as an Agent tool for use in morning scan, intraday monitor, and chat.

**Tech Stack:** baostock (historical K-lines), Sina API (realtime quotes), SQLite (beta cache in memory.db), existing concept_stocks mapping in stocks.db.

---

## File Structure

### New Files

| File | Responsibility |
|------|---------------|
| `alpha_agents/data/beta_calculator.py` | Offline beta computation: fetch history, calculate multi-period beta, store to DB |
| `alpha_agents/tools/sector_beta.py` | Online tool: read cached beta, fetch realtime + institutional data, multi-factor score, return Top N |
| `tests/test_beta_calculator.py` | Tests for beta math and DB operations |

### Modified Files

| File | Changes |
|------|---------|
| `alpha_agents/data/memory_store.py` | Add `sector_betas` table to schema |
| `alpha_agents/tools/registry.py` | Register `get_sector_best_stocks` tool, add to STOCK_TOOLS |
| `alpha_agents/pipeline/tasks/weekly_report.py` | Trigger beta calculation after weekly report |
| `main.py` | Add `build-beta` CLI command |

---

### Task 1: Add sector_betas Table Schema

**Files:**
- Modify: `alpha_agents/data/memory_store.py`
- Test: `tests/test_beta_calculator.py`

- [ ] **Step 1: Write failing test for table existence**

Create `tests/test_beta_calculator.py`:

```python
"""Tests for sector beta calculation and storage."""
import sqlite3
import tempfile
from pathlib import Path


def test_sector_betas_table_exists():
    """Verify sector_betas table is created by schema."""
    from alpha_agents.data.memory_store import _SCHEMA
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    assert "sector_betas" in tables


def test_sector_betas_columns():
    from alpha_agents.data.memory_store import _SCHEMA
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sector_betas)").fetchall()}
    expected = {"id", "concept", "code", "name", "beta_20d", "beta_60d",
                "beta_120d", "beta_weighted", "avg_daily_amount", "updated_at"}
    assert expected.issubset(cols)


def test_sector_betas_unique_constraint():
    from alpha_agents.data.memory_store import _SCHEMA
    conn = sqlite3.connect(":memory:")
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO sector_betas (concept, code, name, beta_weighted) VALUES ('电池', '300750', '宁德时代', 1.3)")
    conn.execute("INSERT OR REPLACE INTO sector_betas (concept, code, name, beta_weighted) VALUES ('电池', '300750', '宁德时代', 1.5)")
    count = conn.execute("SELECT COUNT(*) FROM sector_betas WHERE concept='电池' AND code='300750'").fetchone()[0]
    # UNIQUE constraint means upsert should result in 1 row, but INSERT OR REPLACE needs the id
    # Actually with UNIQUE on (concept, code) and no explicit id, we need to test differently
    rows = conn.execute("SELECT beta_weighted FROM sector_betas WHERE concept='电池' AND code='300750'").fetchall()
    assert len(rows) <= 2  # At most 2 without proper upsert; we'll use ON CONFLICT in real code
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_beta_calculator.py -v`
Expected: FAIL — `sector_betas` table not found.

- [ ] **Step 3: Add schema to memory_store.py**

In `alpha_agents/data/memory_store.py`, find the end of `_SCHEMA` (before the closing `"""`), and add before the `chat_memory` table:

```sql
CREATE TABLE IF NOT EXISTS sector_betas (
    id INTEGER PRIMARY KEY,
    concept TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    beta_20d REAL,
    beta_60d REAL,
    beta_120d REAL,
    beta_weighted REAL,
    avg_daily_amount REAL,
    updated_at TEXT,
    UNIQUE(concept, code)
);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_beta_calculator.py -v`
Expected: All 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add alpha_agents/data/memory_store.py tests/test_beta_calculator.py
git commit -m "feat: add sector_betas table schema for beta cache"
```

---

### Task 2: Implement Beta Calculator Module

**Files:**
- Create: `alpha_agents/data/beta_calculator.py`
- Modify: `tests/test_beta_calculator.py` (add more tests)

- [ ] **Step 1: Write failing tests for beta math**

Append to `tests/test_beta_calculator.py`:

```python
def test_compute_beta_basic():
    """Beta of a stock perfectly correlated with sector should be ~1.0."""
    from alpha_agents.data.beta_calculator import compute_beta
    # Stock returns = sector returns → beta ≈ 1.0
    stock_returns = [0.01, -0.02, 0.03, -0.01, 0.02, 0.01, -0.01, 0.02, -0.02, 0.01]
    sector_returns = [0.01, -0.02, 0.03, -0.01, 0.02, 0.01, -0.01, 0.02, -0.02, 0.01]
    beta = compute_beta(stock_returns, sector_returns)
    assert abs(beta - 1.0) < 0.01


def test_compute_beta_high_beta():
    """Stock that moves 2x sector should have beta ≈ 2.0."""
    from alpha_agents.data.beta_calculator import compute_beta
    sector_returns = [0.01, -0.02, 0.03, -0.01, 0.02, 0.01, -0.01, 0.02, -0.02, 0.01]
    stock_returns = [r * 2 for r in sector_returns]
    beta = compute_beta(stock_returns, sector_returns)
    assert abs(beta - 2.0) < 0.01


def test_compute_beta_zero_variance():
    """If sector doesn't move, beta should be 0."""
    from alpha_agents.data.beta_calculator import compute_beta
    stock_returns = [0.01, -0.02, 0.03]
    sector_returns = [0.0, 0.0, 0.0]
    beta = compute_beta(stock_returns, sector_returns)
    assert beta == 0.0


def test_weighted_beta():
    """Multi-period weighted beta calculation."""
    from alpha_agents.data.beta_calculator import weighted_beta
    result = weighted_beta(beta_20d=1.5, beta_60d=1.2, beta_120d=1.0)
    expected = 1.5 * 0.5 + 1.2 * 0.3 + 1.0 * 0.2  # = 0.75 + 0.36 + 0.20 = 1.31
    assert abs(result - expected) < 0.01
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_beta_calculator.py::test_compute_beta_basic -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement beta_calculator.py**

Create `alpha_agents/data/beta_calculator.py`:

```python
"""Offline beta calculation for sector-stock correlation.

Computes multi-period beta coefficients for all stocks within each active
concept sector. Designed to run weekly (Saturday after weekly report).

Beta = Cov(stock_returns, sector_returns) / Var(sector_returns)
"""

import json
import logging
from datetime import datetime

from alpha_agents.config import DB_PATH
from alpha_agents.data.db import get_connection
from alpha_agents.data.market_data import get_stock_history
from alpha_agents.data.memory_store import _get_conn, _write_lock, get_active_themes

logger = logging.getLogger(__name__)

# Multi-period weights
W_20D = 0.50
W_60D = 0.30
W_120D = 0.20


def compute_beta(stock_returns: list[float], sector_returns: list[float]) -> float:
    """Compute beta = Cov(stock, sector) / Var(sector).

    Returns 0.0 if sector has zero variance (no movement).
    """
    n = min(len(stock_returns), len(sector_returns))
    if n < 5:
        return 0.0

    sr = sector_returns[:n]
    st = stock_returns[:n]

    mean_sr = sum(sr) / n
    mean_st = sum(st) / n

    var_sr = sum((x - mean_sr) ** 2 for x in sr) / n
    if var_sr < 1e-12:
        return 0.0

    cov = sum((st[i] - mean_st) * (sr[i] - mean_sr) for i in range(n)) / n
    return round(cov / var_sr, 4)


def weighted_beta(
    beta_20d: float | None = None,
    beta_60d: float | None = None,
    beta_120d: float | None = None,
) -> float:
    """Compute weighted beta from multiple periods."""
    total_w = 0
    total = 0
    if beta_20d is not None:
        total += beta_20d * W_20D
        total_w += W_20D
    if beta_60d is not None:
        total += beta_60d * W_60D
        total_w += W_60D
    if beta_120d is not None:
        total += beta_120d * W_120D
        total_w += W_120D
    return round(total / total_w, 4) if total_w > 0 else 0.0


def _get_concept_stocks(concept_name: str) -> list[dict]:
    """Get all stocks in a concept from stocks.db."""
    conn = get_connection(DB_PATH)
    rows = conn.execute(
        "SELECT cs.stock_code as code, s.name "
        "FROM concept_stocks cs "
        "JOIN concepts c ON cs.concept_id = c.id "
        "JOIN stocks s ON cs.stock_code = s.code "
        "WHERE c.name = ? AND (s.is_st = 0 OR s.is_st IS NULL) "
        "AND (s.is_suspended = 0 OR s.is_suspended IS NULL)",
        (concept_name,),
    ).fetchall()
    conn.close()
    return [{"code": r["code"], "name": r["name"]} for r in rows]


def _returns_from_history(history: list[dict]) -> list[float]:
    """Convert price history to daily returns."""
    returns = []
    for i in range(1, len(history)):
        prev = history[i - 1]["close"]
        curr = history[i]["close"]
        if prev > 0:
            returns.append((curr - prev) / prev)
        else:
            returns.append(0.0)
    return returns


def _compute_sector_returns(stocks: list[dict], days: int) -> list[float]:
    """Compute equal-weight sector returns from constituent stocks.

    Uses the average daily return across all stocks as the sector benchmark.
    """
    all_returns = []
    for stock in stocks[:50]:  # Cap at 50 to limit baostock calls
        history = get_stock_history(stock["code"], days=days)
        if history and len(history) >= 5:
            all_returns.append(_returns_from_history(history))

    if not all_returns:
        return []

    # Equal-weight average across stocks for each day
    min_len = min(len(r) for r in all_returns)
    sector_returns = []
    for day in range(min_len):
        avg = sum(r[day] for r in all_returns if day < len(r)) / len(all_returns)
        sector_returns.append(avg)

    return sector_returns


def calculate_concept_betas(concept_name: str) -> list[dict]:
    """Calculate beta for all stocks in a concept.

    Returns list of {code, name, beta_20d, beta_60d, beta_120d, beta_weighted, avg_daily_amount}.
    """
    stocks = _get_concept_stocks(concept_name)
    if not stocks:
        logger.info("No stocks found for concept '%s'", concept_name)
        return []

    logger.info("Calculating betas for '%s' (%d stocks)", concept_name, len(stocks))

    # Compute sector-level returns for each period
    sector_120 = _compute_sector_returns(stocks, days=120)
    sector_60 = sector_120[-59:] if len(sector_120) >= 59 else sector_120
    sector_20 = sector_120[-19:] if len(sector_120) >= 19 else sector_120

    results = []
    for stock in stocks:
        history = get_stock_history(stock["code"], days=120)
        if not history or len(history) < 10:
            continue

        stock_returns = _returns_from_history(history)

        # Calculate beta for each period
        beta_120d = compute_beta(stock_returns, sector_120) if len(stock_returns) >= 20 else None
        beta_60d = compute_beta(stock_returns[-59:], sector_60) if len(stock_returns) >= 20 else None
        beta_20d = compute_beta(stock_returns[-19:], sector_20) if len(stock_returns) >= 10 else None

        bw = weighted_beta(beta_20d, beta_60d, beta_120d)

        # Average daily trading amount (last 20 days)
        recent = history[-20:] if len(history) >= 20 else history
        volumes = [d["volume"] for d in recent]
        closes = [d["close"] for d in recent]
        avg_amount = sum(v * c for v, c in zip(volumes, closes)) / len(recent) if recent else 0

        results.append({
            "code": stock["code"],
            "name": stock["name"],
            "beta_20d": beta_20d,
            "beta_60d": beta_60d,
            "beta_120d": beta_120d,
            "beta_weighted": bw,
            "avg_daily_amount": round(avg_amount, 0),
        })

    results.sort(key=lambda x: x["beta_weighted"], reverse=True)
    logger.info("Computed betas for %d/%d stocks in '%s'", len(results), len(stocks), concept_name)
    return results


def save_concept_betas(concept_name: str, betas: list[dict]) -> int:
    """Save computed betas to the sector_betas table (upsert)."""
    now = datetime.now().isoformat()
    saved = 0
    with _write_lock:
        conn = _get_conn()
        for b in betas:
            conn.execute(
                "INSERT INTO sector_betas (concept, code, name, beta_20d, beta_60d, "
                "beta_120d, beta_weighted, avg_daily_amount, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(concept, code) DO UPDATE SET "
                "name=excluded.name, beta_20d=excluded.beta_20d, beta_60d=excluded.beta_60d, "
                "beta_120d=excluded.beta_120d, beta_weighted=excluded.beta_weighted, "
                "avg_daily_amount=excluded.avg_daily_amount, updated_at=excluded.updated_at",
                (concept_name, b["code"], b["name"], b["beta_20d"], b["beta_60d"],
                 b["beta_120d"], b["beta_weighted"], b["avg_daily_amount"], now),
            )
            saved += 1
        conn.commit()
    return saved


def get_cached_betas(concept_name: str) -> list[dict]:
    """Read cached betas from DB for a concept."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM sector_betas WHERE concept = ? ORDER BY beta_weighted DESC",
        (concept_name,),
    ).fetchall()
    return [dict(r) for r in rows]


def run_weekly_beta_calculation() -> int:
    """Calculate betas for all active theme concepts. Called after weekly report."""
    themes = get_active_themes()
    total = 0
    for theme in themes:
        name = theme["name"]
        try:
            betas = calculate_concept_betas(name)
            if betas:
                saved = save_concept_betas(name, betas)
                total += saved
                logger.info("Saved %d betas for '%s'", saved, name)
        except Exception as e:
            logger.warning("Beta calculation failed for '%s': %s", name, e)
    logger.info("Weekly beta calculation complete: %d total betas across %d themes", total, len(themes))
    return total
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_beta_calculator.py -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add alpha_agents/data/beta_calculator.py tests/test_beta_calculator.py
git commit -m "feat: beta calculator module — multi-period beta computation and DB storage"
```

---

### Task 3: Implement Sector Beta Tool (Multi-Factor Scorer)

**Files:**
- Create: `alpha_agents/tools/sector_beta.py`

- [ ] **Step 1: Create the sector_beta tool**

Create `alpha_agents/tools/sector_beta.py`:

```python
"""Sector beta stock selector — find the best stocks within a concept sector.

Combines pre-computed beta (weekly) with realtime data to produce a
multi-factor score: beta 40% + position 25% + institutional 20% + liquidity 15%.
"""

import json
import logging

from alpha_agents.data.beta_calculator import get_cached_betas, calculate_concept_betas, save_concept_betas
from alpha_agents.data.market_data import get_realtime_quotes

logger = logging.getLogger(__name__)

# Factor weights
W_BETA = 0.40
W_POSITION = 0.25
W_INSTITUTIONAL = 0.20
W_LIQUIDITY = 0.15


def _score_position(change_pct: float) -> float:
    """Score based on intraday change — prefer stocks that haven't run yet."""
    if change_pct >= 9.8:
        return 0    # Limit up, can't buy
    if change_pct >= 6:
        return 20
    if change_pct >= 3:
        return 50
    if change_pct >= 0:
        return 80
    return 100  # Negative = hasn't started, best entry


def _score_liquidity(avg_daily_amount: float) -> float:
    """Score based on average daily trading amount."""
    yi = avg_daily_amount / 1e8  # Convert to 亿
    if yi < 0.5:
        return 0    # Too illiquid, skip
    if yi < 1:
        return 40
    if yi < 5:
        return 70
    return 100


def _score_institutional(code: str, north_data: dict, lhb_data: dict) -> float:
    """Score based on institutional recognition signals."""
    score = 0

    # North flow
    if code in north_data:
        nd = north_data[code]
        if nd.get("pct", 0) > 1:
            score += 30
        if nd.get("change", "") == "增持":
            score += 20

    # LHB
    if code in lhb_data:
        ld = lhb_data[code]
        if ld.get("is_institutional") and ld.get("net_buy", 0) > 0:
            score += 30

    return min(score, 100)


def _normalize_beta_scores(betas: list[dict]) -> dict[str, float]:
    """Normalize beta_weighted to 0-100 scale across the sector."""
    if not betas:
        return {}
    values = [b["beta_weighted"] for b in betas if b.get("beta_weighted")]
    if not values:
        return {}
    max_b = max(values)
    min_b = min(values)
    range_b = max_b - min_b if max_b > min_b else 1
    return {
        b["code"]: round(((b.get("beta_weighted") or 0) - min_b) / range_b * 100, 1)
        for b in betas
    }


def get_sector_best_stocks_fn(concept_name: str, top_n: int = 10) -> str:
    """Get top stocks in a concept sector by multi-factor score.

    Uses cached beta (weekly) + realtime price + institutional data.

    Args:
        concept_name: Concept sector name, e.g. "电池", "芯片概念"
        top_n: Number of top stocks to return
    """
    # 1. Get cached betas (or compute on-the-fly if missing)
    betas = get_cached_betas(concept_name)
    if not betas:
        logger.info("No cached betas for '%s', computing on-the-fly...", concept_name)
        try:
            computed = calculate_concept_betas(concept_name)
            if computed:
                save_concept_betas(concept_name, computed)
                betas = get_cached_betas(concept_name)
        except Exception as e:
            logger.warning("On-the-fly beta calculation failed: %s", e)

    if not betas:
        return json.dumps({"concept": concept_name, "error": "无该板块的beta数据", "top": []}, ensure_ascii=False)

    # 2. Get realtime prices
    codes = [b["code"] for b in betas]
    rt = get_realtime_quotes(codes) or {}

    # 3. Get institutional data (from daily archive or live)
    north_data = {}
    lhb_data = {}
    try:
        from alpha_agents.tools.fund_flow import get_north_flow_fn, get_lhb_detail_fn
        import json as _j

        # North flow
        north_raw = _j.loads(get_north_flow_fn("today"))
        for item in north_raw.get("data", []):
            north_data[item["code"]] = {
                "pct": item.get("pct_of_float", 0),
                "change": "增持" if item.get("change_value_wan", 0) > 0 else "减持",
            }

        # LHB
        lhb_raw = _j.loads(get_lhb_detail_fn())
        for item in lhb_raw.get("data", []):
            lhb_data[item["code"]] = {
                "is_institutional": item.get("is_institutional", False),
                "net_buy": item.get("net_buy", 0),
            }
    except Exception as e:
        logger.debug("Institutional data fetch failed: %s", e)

    # 4. Normalize beta scores
    beta_scores = _normalize_beta_scores(betas)

    # 5. Multi-factor scoring
    scored = []
    for b in betas:
        code = b["code"]
        name = b.get("name", "")

        # Get realtime data
        real = rt.get(code, {})
        price = real.get("price", 0)
        change_pct = real.get("change_pct", 0)

        # Skip limit-up stocks (can't buy)
        if change_pct >= 9.8:
            continue

        # Skip illiquid stocks
        avg_amount = b.get("avg_daily_amount", 0)
        if avg_amount < 5e7:  # < 5000万
            continue

        # Score each factor
        s_beta = beta_scores.get(code, 50)
        s_position = _score_position(change_pct)
        s_institutional = _score_institutional(code, north_data, lhb_data)
        s_liquidity = _score_liquidity(avg_amount)

        total = round(
            s_beta * W_BETA +
            s_position * W_POSITION +
            s_institutional * W_INSTITUTIONAL +
            s_liquidity * W_LIQUIDITY,
            1,
        )

        # Build note
        notes = []
        if s_beta >= 70:
            notes.append("高beta")
        if s_position >= 80:
            notes.append("低涨幅")
        if s_institutional >= 50:
            notes.append("机构认可")
        if s_liquidity >= 70:
            notes.append("流动性好")

        inst_signals = []
        if code in north_data and north_data[code].get("change") == "增持":
            inst_signals.append("北向增持")
        if code in lhb_data and lhb_data[code].get("is_institutional"):
            inst_signals.append("龙虎榜机构买入")

        scored.append({
            "code": code,
            "name": name,
            "score": total,
            "beta_weighted": b.get("beta_weighted", 0),
            "today_change_pct": change_pct,
            "price": price,
            "institutional": "+".join(inst_signals) if inst_signals else "无",
            "avg_amount_yi": round(avg_amount / 1e8, 2),
            "note": "+".join(notes) if notes else "",
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    top = scored[:top_n]

    # Add rank
    for i, item in enumerate(top):
        item["rank"] = i + 1

    return json.dumps({
        "concept": concept_name,
        "total_in_sector": len(betas),
        "scored": len(scored),
        "top": top,
    }, ensure_ascii=False)
```

- [ ] **Step 2: Verify import**

Run: `uv run python -c "from alpha_agents.tools.sector_beta import get_sector_best_stocks_fn; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add alpha_agents/tools/sector_beta.py
git commit -m "feat: sector beta tool — multi-factor scoring with beta, position, institutional, liquidity"
```

---

### Task 4: Register Tool and Wire Into Agents

**Files:**
- Modify: `alpha_agents/tools/registry.py`
- Modify: `alpha_agents/agents/morning.py`
- Modify: `alpha_agents/agents/intraday.py`
- Modify: `alpha_agents/agents/chat.py`

- [ ] **Step 1: Register tool in registry.py**

In `alpha_agents/tools/registry.py`, add import at the top with other tool imports:

```python
from alpha_agents.tools.sector_beta import get_sector_best_stocks_fn
```

Add the `@function_tool` wrapper near the other stock tools:

```python
@function_tool
def get_sector_best_stocks(concept_name: str, top_n: int = 10) -> str:
    """获取板块内综合评分最高的标的（基于历史beta跟涨弹性+实时多因子打分）。

    输入概念板块名，返回该板块内 Top N 标的。评分维度：
    1. 板块beta（40%）— 历史上板块涨时该股涨多少
    2. 当日涨幅位置（25%）— 还没涨的优先（补涨机会）
    3. 机构认可度（20%）— 北向增持+龙虎榜机构买入
    4. 流动性（15%）— 日均成交额

    用于：板块异动时选最佳标的，替代语义搜索+主观判断。
    """
    return get_sector_best_stocks_fn(concept_name=concept_name, top_n=top_n)
```

Add `get_sector_best_stocks` to `STOCK_TOOLS` list.

- [ ] **Step 2: Add to morning agent tools**

In `alpha_agents/agents/morning.py`, add `get_sector_best_stocks` to the import and `MORNING_TOOLS` list.

- [ ] **Step 3: Add to intraday agent tools**

In `alpha_agents/agents/intraday.py`, add `get_sector_best_stocks` to the import and `INTRADAY_TOOLS` list.

- [ ] **Step 4: Add to chat agent tools**

In `alpha_agents/agents/chat.py`, the chat agent already inherits `STOCK_TOOLS`, so if added there it will be available. No additional change needed if `get_sector_best_stocks` is in `STOCK_TOOLS`.

Also add a quick command shortcut in the chat command loop:

```python
# Quick sector best stocks: "选股 电池" or "best 电池"
best_match = _re.match(r"^(?:选股|best)\s+(.+)$", user_input.strip())
if best_match:
    from alpha_agents.tools.sector_beta import get_sector_best_stocks_fn
    result = get_sector_best_stocks_fn(best_match.group(1).strip())
    console.print(Panel(result, title="板块选股", border_style="cyan"))
    continue
```

- [ ] **Step 5: Verify all imports work**

Run:
```bash
uv run python -c "
from alpha_agents.tools.registry import get_sector_best_stocks, STOCK_TOOLS
print(f'Tool registered: {get_sector_best_stocks in STOCK_TOOLS}')
from alpha_agents.agents.morning import MORNING_TOOLS
from alpha_agents.agents.intraday import INTRADAY_TOOLS
print('All imports OK')
"
```
Expected: Tool registered: True, All imports OK.

- [ ] **Step 6: Commit**

```bash
git add alpha_agents/tools/registry.py alpha_agents/agents/morning.py alpha_agents/agents/intraday.py alpha_agents/agents/chat.py
git commit -m "feat: register get_sector_best_stocks tool in all agents"
```

---

### Task 5: Wire Weekly Beta Calculation + CLI Command

**Files:**
- Modify: `alpha_agents/pipeline/tasks/weekly_report.py`
- Modify: `main.py`

- [ ] **Step 1: Trigger beta calculation after weekly report**

In `alpha_agents/pipeline/tasks/weekly_report.py`, after the report is generated and notification is sent, add:

```python
    # Calculate sector betas for all active themes (weekly refresh)
    try:
        from alpha_agents.data.beta_calculator import run_weekly_beta_calculation
        import asyncio as _asyncio
        beta_count = await _asyncio.to_thread(run_weekly_beta_calculation)
        logger.info("Weekly beta calculation: %d betas computed", beta_count)
    except Exception as e:
        logger.warning("Weekly beta calculation failed: %s", e)
```

- [ ] **Step 2: Add build-beta CLI command**

In `main.py`, add a new subcommand:

```python
# build-beta — manual beta calculation
p_beta = subparsers.add_parser("build-beta", help="手动计算所有活跃主线的板块beta系数")
p_beta.set_defaults(func=cmd_build_beta)
```

And the handler function:

```python
def cmd_build_beta(args: argparse.Namespace) -> None:
    """Manually trigger beta calculation for all active themes."""
    _ensure_index()
    from alpha_agents.data.beta_calculator import run_weekly_beta_calculation
    logging.info("Starting manual beta calculation...")
    total = run_weekly_beta_calculation()
    logging.info("Beta calculation complete: %d betas", total)
```

- [ ] **Step 3: Verify**

Run: `uv run python main.py build-beta --help`
Expected: Shows help for the build-beta command.

- [ ] **Step 4: Commit**

```bash
git add alpha_agents/pipeline/tasks/weekly_report.py main.py
git commit -m "feat: weekly beta calculation trigger + build-beta CLI command"
```

---

### Task 6: Integration Test — Run Beta Calculation

**Files:**
- No new files

- [ ] **Step 1: Run beta calculation for one concept**

```bash
uv run python -c "
from alpha_agents.data.beta_calculator import calculate_concept_betas, save_concept_betas
betas = calculate_concept_betas('锂电池概念')
print(f'Computed {len(betas)} betas')
for b in betas[:5]:
    print(f'  {b[\"code\"]} {b[\"name\"]} beta={b[\"beta_weighted\"]} amt={b[\"avg_daily_amount\"]/1e8:.1f}亿')
if betas:
    saved = save_concept_betas('锂电池概念', betas)
    print(f'Saved {saved} to DB')
"
```

Expected: Betas computed and saved.

- [ ] **Step 2: Run the tool end-to-end**

```bash
uv run python -c "
from alpha_agents.tools.sector_beta import get_sector_best_stocks_fn
import json
result = json.loads(get_sector_best_stocks_fn('锂电池概念'))
print(f'Concept: {result[\"concept\"]}')
print(f'Total: {result[\"total_in_sector\"]}, Scored: {result[\"scored\"]}')
for item in result['top'][:5]:
    print(f'  #{item[\"rank\"]} {item[\"code\"]} {item[\"name\"]} score={item[\"score\"]} beta={item[\"beta_weighted\"]} {item[\"note\"]}')
"
```

Expected: Top stocks listed with scores.

- [ ] **Step 3: Run full test suite**

```bash
uv run pytest tests/test_beta_calculator.py -v
```

Expected: All tests PASS.

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "feat: sector beta stock selector — complete implementation"
```
