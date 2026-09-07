# AlphaAgents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a news-driven autonomous stock screening system that monitors financial news, analyzes geopolitical impact on A-share sectors, and outputs recommended stocks.

**Architecture:** Three decoupled modules — (1) stock data source with local SQLite index, (2) news data source via akshare, (3) autonomous agent layer using Claude Agent SDK with MCP tools. The agent layer consumes data modules through `@tool` decorated functions packaged as an MCP Server.

**Tech Stack:** Python 3.11+, claude-agent-sdk, akshare, baostock, SQLite3 (stdlib), anyio

---

## File Structure

```
AlphaAgents/
├── pyproject.toml                  Project metadata & dependencies
├── main.py                         CLI entry point (analyze / monitor)
├── alpha_agents/
│   ├── __init__.py
│   ├── config.py                   Settings & constants
│   ├── data/
│   │   ├── __init__.py
│   │   ├── db.py                   SQLite connection & schema management
│   │   └── index_builder.py        Offline: pull akshare data → SQLite
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── server.py               MCP Server: register all @tool functions
│   │   ├── news.py                 @tool get_news
│   │   ├── stock_search.py         @tool search_stocks (query local SQLite)
│   │   ├── sector.py               @tool get_sector_data
│   │   ├── stock_filter.py         @tool filter_stocks
│   │   └── watchlist.py            @tool get_watchlist
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── strategist.py           Main agent setup (ClaudeSDKClient + options)
│   │   └── geopolitical.py         Sub-agent definition (AgentDefinition)
│   ├── prompts/
│   │   ├── strategist.md           Main agent system prompt
│   │   └── geopolitical.md         Geopolitical analyst system prompt
│   └── monitor.py                  News monitoring loop (event-driven trigger)
├── config/
│   └── watchlist.json              User's watchlist
└── tests/
    ├── __init__.py
    ├── test_db.py                  SQLite schema & query tests
    ├── test_index_builder.py       Index building tests (mocked akshare)
    ├── test_news.py                News tool tests
    ├── test_stock_search.py        Stock search tool tests
    ├── test_sector.py              Sector data tool tests
    ├── test_stock_filter.py        Stock filter tool tests
    ├── test_watchlist.py           Watchlist tool tests
    ├── test_server.py              MCP Server registration tests
    └── test_monitor.py             Monitor loop tests
```

---

### Task 1: Project Scaffolding

**Files:**
- Create: `pyproject.toml`
- Create: `alpha_agents/__init__.py`
- Create: `alpha_agents/config.py`
- Create: `config/watchlist.json`
- Create: `tests/__init__.py`

- [ ] **Step 1: Create pyproject.toml**

```toml
[project]
name = "alpha-agents"
version = "0.1.0"
description = "News-driven autonomous stock screening system"
requires-python = ">=3.11"
dependencies = [
    "claude-agent-sdk",
    "akshare",
    "baostock",
    "anyio",
]

[project.optional-dependencies]
dev = [
    "pytest",
    "pytest-asyncio",
]

[project.scripts]
alpha-agents = "main:main"
```

- [ ] **Step 2: Create alpha_agents/__init__.py**

```python
"""AlphaAgents — News-driven autonomous stock screening system."""
```

- [ ] **Step 3: Create alpha_agents/config.py**

```python
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "alpha_agents" / "data"
PROMPTS_DIR = PROJECT_ROOT / "alpha_agents" / "prompts"

DB_PATH = DATA_DIR / "stocks.db"
WATCHLIST_PATH = CONFIG_DIR / "watchlist.json"

# News monitor settings
MONITOR_INTERVAL_SECONDS = 300
NEWS_FETCH_LIMIT = 50
```

- [ ] **Step 4: Create config/watchlist.json**

```json
{
  "stocks": [
    {"code": "600519", "name": "贵州茅台", "concepts": ["白酒", "消费"]},
    {"code": "000858", "name": "五粮液", "concepts": ["白酒", "消费"]}
  ]
}
```

- [ ] **Step 5: Create tests/__init__.py**

Empty file.

- [ ] **Step 6: Install dependencies and verify**

Run: `cd /Users/evilkylin/Projects/AlphaAgents && pip install -e ".[dev]"`
Expected: Successful installation with all dependencies resolved.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml alpha_agents/__init__.py alpha_agents/config.py config/watchlist.json tests/__init__.py
git commit -m "feat: project scaffolding with dependencies and config"
```

---

### Task 2: SQLite Schema & Database Module

**Files:**
- Create: `alpha_agents/data/__init__.py`
- Create: `alpha_agents/data/db.py`
- Create: `tests/test_db.py`

- [ ] **Step 1: Write failing tests for database module**

Create `tests/test_db.py`:

```python
import sqlite3
from pathlib import Path

import pytest

from alpha_agents.data.db import init_db, get_connection


@pytest.fixture
def tmp_db(tmp_path):
    db_path = tmp_path / "test_stocks.db"
    init_db(db_path)
    return db_path


def test_init_db_creates_tables(tmp_db):
    conn = get_connection(tmp_db)
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = [row[0] for row in cursor.fetchall()]
    conn.close()
    assert "concepts" in tables
    assert "stocks" in tables
    assert "concept_stocks" in tables


def test_init_db_concepts_schema(tmp_db):
    conn = get_connection(tmp_db)
    cursor = conn.execute("PRAGMA table_info(concepts)")
    columns = {row[1] for row in cursor.fetchall()}
    conn.close()
    assert columns == {"id", "name", "source"}


def test_init_db_stocks_schema(tmp_db):
    conn = get_connection(tmp_db)
    cursor = conn.execute("PRAGMA table_info(stocks)")
    columns = {row[1] for row in cursor.fetchall()}
    conn.close()
    assert columns == {"code", "name", "market_cap", "industry", "is_st", "is_suspended"}


def test_init_db_concept_stocks_schema(tmp_db):
    conn = get_connection(tmp_db)
    cursor = conn.execute("PRAGMA table_info(concept_stocks)")
    columns = {row[1] for row in cursor.fetchall()}
    conn.close()
    assert columns == {"concept_id", "stock_code"}


def test_init_db_idempotent(tmp_db):
    """Calling init_db twice should not error."""
    init_db(tmp_db)
    conn = get_connection(tmp_db)
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = [row[0] for row in cursor.fetchall()]
    conn.close()
    assert "concepts" in tables
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/evilkylin/Projects/AlphaAgents && python -m pytest tests/test_db.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'alpha_agents.data'`

- [ ] **Step 3: Implement database module**

Create `alpha_agents/data/__init__.py`:

```python
"""Stock data storage and indexing."""
```

Create `alpha_agents/data/db.py`:

```python
import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS concepts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL DEFAULT 'ths'
);

CREATE TABLE IF NOT EXISTS stocks (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    market_cap REAL,
    industry TEXT,
    is_st INTEGER NOT NULL DEFAULT 0,
    is_suspended INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS concept_stocks (
    concept_id INTEGER NOT NULL,
    stock_code TEXT NOT NULL,
    PRIMARY KEY (concept_id, stock_code),
    FOREIGN KEY (concept_id) REFERENCES concepts(id),
    FOREIGN KEY (stock_code) REFERENCES stocks(code)
);

CREATE INDEX IF NOT EXISTS idx_concept_name ON concepts(name);
CREATE INDEX IF NOT EXISTS idx_stock_name ON stocks(name);
"""


def get_connection(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection(db_path)
    conn.executescript(_SCHEMA)
    conn.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_db.py -v`
Expected: All 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add alpha_agents/data/__init__.py alpha_agents/data/db.py tests/test_db.py
git commit -m "feat: SQLite schema with concepts, stocks, concept_stocks tables"
```

---

### Task 3: Index Builder (akshare → SQLite)

**Files:**
- Create: `alpha_agents/data/index_builder.py`
- Create: `tests/test_index_builder.py`

- [ ] **Step 1: Write failing tests with mocked akshare**

Create `tests/test_index_builder.py`:

```python
import sqlite3
from pathlib import Path
from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

from alpha_agents.data.db import init_db, get_connection
from alpha_agents.data.index_builder import build_index


@pytest.fixture
def tmp_db(tmp_path):
    db_path = tmp_path / "test_stocks.db"
    init_db(db_path)
    return db_path


def _mock_concept_names():
    return pd.DataFrame({
        "概念名称": ["国产替代", "光刻胶"],
    })


def _mock_concept_cons(symbol):
    data = {
        "国产替代": pd.DataFrame({
            "代码": ["688001", "688002"],
            "名称": ["华兴源创", "睿创微纳"],
        }),
        "光刻胶": pd.DataFrame({
            "代码": ["300236", "688001"],
            "名称": ["上海新阳", "华兴源创"],
        }),
    }
    return data.get(symbol, pd.DataFrame({"代码": [], "名称": []}))


def _mock_stock_info():
    return pd.DataFrame({
        "代码": ["688001", "688002", "300236"],
        "名称": ["华兴源创", "睿创微纳", "上海新阳"],
        "总市值": [10000000000.0, 5000000000.0, 8000000000.0],
        "行业": ["半导体", "半导体", "化工"],
    })


@patch("alpha_agents.data.index_builder._fetch_stock_info", side_effect=lambda: _mock_stock_info())
@patch("alpha_agents.data.index_builder._fetch_concept_constituents", side_effect=_mock_concept_cons)
@patch("alpha_agents.data.index_builder._fetch_concept_names", side_effect=lambda: _mock_concept_names())
def test_build_index_creates_concepts(mock_names, mock_cons, mock_info, tmp_db):
    build_index(tmp_db)
    conn = get_connection(tmp_db)
    concepts = conn.execute("SELECT name FROM concepts ORDER BY name").fetchall()
    conn.close()
    assert [r["name"] for r in concepts] == ["光刻胶", "国产替代"]


@patch("alpha_agents.data.index_builder._fetch_stock_info", side_effect=lambda: _mock_stock_info())
@patch("alpha_agents.data.index_builder._fetch_concept_constituents", side_effect=_mock_concept_cons)
@patch("alpha_agents.data.index_builder._fetch_concept_names", side_effect=lambda: _mock_concept_names())
def test_build_index_creates_stocks(mock_names, mock_cons, mock_info, tmp_db):
    build_index(tmp_db)
    conn = get_connection(tmp_db)
    stocks = conn.execute("SELECT code, name FROM stocks ORDER BY code").fetchall()
    conn.close()
    assert len(stocks) == 3
    assert stocks[0]["code"] == "300236"


@patch("alpha_agents.data.index_builder._fetch_stock_info", side_effect=lambda: _mock_stock_info())
@patch("alpha_agents.data.index_builder._fetch_concept_constituents", side_effect=_mock_concept_cons)
@patch("alpha_agents.data.index_builder._fetch_concept_names", side_effect=lambda: _mock_concept_names())
def test_build_index_creates_mappings(mock_names, mock_cons, mock_info, tmp_db):
    build_index(tmp_db)
    conn = get_connection(tmp_db)
    # 688001 (华兴源创) should be in both 国产替代 and 光刻胶
    mappings = conn.execute(
        """
        SELECT c.name FROM concept_stocks cs
        JOIN concepts c ON c.id = cs.concept_id
        WHERE cs.stock_code = '688001'
        ORDER BY c.name
        """
    ).fetchall()
    conn.close()
    assert [r["name"] for r in mappings] == ["光刻胶", "国产替代"]


@patch("alpha_agents.data.index_builder._fetch_stock_info", side_effect=lambda: _mock_stock_info())
@patch("alpha_agents.data.index_builder._fetch_concept_constituents", side_effect=_mock_concept_cons)
@patch("alpha_agents.data.index_builder._fetch_concept_names", side_effect=lambda: _mock_concept_names())
def test_build_index_is_idempotent(mock_names, mock_cons, mock_info, tmp_db):
    build_index(tmp_db)
    build_index(tmp_db)
    conn = get_connection(tmp_db)
    concepts = conn.execute("SELECT COUNT(*) as cnt FROM concepts").fetchone()
    conn.close()
    assert concepts["cnt"] == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_index_builder.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'alpha_agents.data.index_builder'`

- [ ] **Step 3: Implement index builder**

Create `alpha_agents/data/index_builder.py`:

```python
import logging
from pathlib import Path

import akshare as ak
import pandas as pd

from alpha_agents.data.db import get_connection, init_db

logger = logging.getLogger(__name__)


def _fetch_concept_names() -> pd.DataFrame:
    return ak.stock_board_concept_name_ths()


def _fetch_concept_constituents(symbol: str) -> pd.DataFrame:
    try:
        return ak.stock_board_concept_cons_ths(symbol=symbol)
    except Exception:
        logger.warning("Failed to fetch constituents for concept: %s", symbol)
        return pd.DataFrame({"代码": [], "名称": []})


def _fetch_stock_info() -> pd.DataFrame:
    return ak.stock_zh_a_spot_em()


def build_index(db_path: Path) -> None:
    init_db(db_path)
    conn = get_connection(db_path)

    try:
        # Clear existing data for idempotent rebuild
        conn.execute("DELETE FROM concept_stocks")
        conn.execute("DELETE FROM concepts")
        conn.execute("DELETE FROM stocks")

        # 1. Fetch and insert stock info
        logger.info("Fetching stock info...")
        stock_info = _fetch_stock_info()
        for _, row in stock_info.iterrows():
            code = str(row.get("代码", ""))
            name = str(row.get("名称", ""))
            market_cap = float(row["总市值"]) if pd.notna(row.get("总市值")) else None
            industry = str(row.get("行业", "")) if pd.notna(row.get("行业")) else None
            is_st = 1 if "ST" in name or "st" in name else 0
            conn.execute(
                "INSERT OR REPLACE INTO stocks (code, name, market_cap, industry, is_st, is_suspended) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (code, name, market_cap, industry, is_st),
            )

        # 2. Fetch concept names
        logger.info("Fetching concept names...")
        concept_names_df = _fetch_concept_names()

        for _, row in concept_names_df.iterrows():
            concept_name = str(row["概念名称"])
            conn.execute(
                "INSERT OR REPLACE INTO concepts (name, source) VALUES (?, 'ths')",
                (concept_name,),
            )
            concept_id = conn.execute(
                "SELECT id FROM concepts WHERE name = ?", (concept_name,)
            ).fetchone()["id"]

            # 3. Fetch constituents for each concept
            logger.info("Fetching constituents for: %s", concept_name)
            cons_df = _fetch_concept_constituents(concept_name)
            for _, stock_row in cons_df.iterrows():
                stock_code = str(stock_row["代码"])
                conn.execute(
                    "INSERT OR IGNORE INTO concept_stocks (concept_id, stock_code) VALUES (?, ?)",
                    (concept_id, stock_code),
                )

        conn.commit()
        logger.info("Index build complete.")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_index_builder.py -v`
Expected: All 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add alpha_agents/data/index_builder.py tests/test_index_builder.py
git commit -m "feat: index builder pulls akshare concept data into SQLite"
```

---

### Task 4: Stock Search Tool (search_stocks)

**Files:**
- Create: `alpha_agents/tools/__init__.py`
- Create: `alpha_agents/tools/stock_search.py`
- Create: `tests/test_stock_search.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_stock_search.py`:

```python
import json
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from alpha_agents.data.db import init_db, get_connection
from alpha_agents.data.index_builder import build_index
from alpha_agents.tools.stock_search import search_stocks_fn


@pytest.fixture
def populated_db(tmp_path):
    db_path = tmp_path / "test.db"
    init_db(db_path)
    conn = get_connection(db_path)
    conn.execute("INSERT INTO concepts (name, source) VALUES ('国产替代', 'ths')")
    conn.execute("INSERT INTO concepts (name, source) VALUES ('光刻胶', 'ths')")
    conn.execute("INSERT INTO concepts (name, source) VALUES ('白酒', 'ths')")
    conn.execute(
        "INSERT INTO stocks (code, name, market_cap, industry, is_st, is_suspended) "
        "VALUES ('688001', '华兴源创', 10000000000, '半导体', 0, 0)"
    )
    conn.execute(
        "INSERT INTO stocks (code, name, market_cap, industry, is_st, is_suspended) "
        "VALUES ('300236', '上海新阳', 8000000000, '化工', 0, 0)"
    )
    conn.execute("INSERT INTO concept_stocks (concept_id, stock_code) VALUES (1, '688001')")
    conn.execute("INSERT INTO concept_stocks (concept_id, stock_code) VALUES (2, '688001')")
    conn.execute("INSERT INTO concept_stocks (concept_id, stock_code) VALUES (2, '300236')")
    conn.commit()
    conn.close()
    return db_path


def test_search_exact_match(populated_db):
    result = search_stocks_fn("国产替代", populated_db)
    parsed = json.loads(result)
    assert len(parsed["matches"]) == 1
    assert parsed["matches"][0]["concept"] == "国产替代"
    assert len(parsed["matches"][0]["stocks"]) == 1
    assert parsed["matches"][0]["stocks"][0]["code"] == "688001"


def test_search_fuzzy_match(populated_db):
    result = search_stocks_fn("光刻", populated_db)
    parsed = json.loads(result)
    assert len(parsed["matches"]) == 1
    assert parsed["matches"][0]["concept"] == "光刻胶"


def test_search_no_match(populated_db):
    result = search_stocks_fn("火星探索", populated_db)
    parsed = json.loads(result)
    assert len(parsed["matches"]) == 0


def test_search_multiple_matches(populated_db):
    """'国产' should match '国产替代', keyword in concept name."""
    result = search_stocks_fn("国产", populated_db)
    parsed = json.loads(result)
    assert len(parsed["matches"]) >= 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_stock_search.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement stock search tool**

Create `alpha_agents/tools/__init__.py`:

```python
"""MCP tools for stock data access."""
```

Create `alpha_agents/tools/stock_search.py`:

```python
import json
from pathlib import Path

from alpha_agents.config import DB_PATH
from alpha_agents.data.db import get_connection


def search_stocks_fn(keyword: str, db_path: Path = DB_PATH) -> str:
    conn = get_connection(db_path)
    try:
        concepts = conn.execute(
            "SELECT id, name FROM concepts WHERE name LIKE ?",
            (f"%{keyword}%",),
        ).fetchall()

        matches = []
        for concept in concepts:
            stocks = conn.execute(
                """
                SELECT s.code, s.name, s.market_cap, s.industry
                FROM concept_stocks cs
                JOIN stocks s ON s.code = cs.stock_code
                WHERE cs.concept_id = ?
                ORDER BY s.market_cap DESC NULLS LAST
                """,
                (concept["id"],),
            ).fetchall()

            matches.append({
                "concept": concept["name"],
                "stock_count": len(stocks),
                "stocks": [
                    {
                        "code": s["code"],
                        "name": s["name"],
                        "market_cap": s["market_cap"],
                        "industry": s["industry"],
                    }
                    for s in stocks
                ],
            })

        return json.dumps({"keyword": keyword, "matches": matches}, ensure_ascii=False)
    finally:
        conn.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_stock_search.py -v`
Expected: All 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add alpha_agents/tools/__init__.py alpha_agents/tools/stock_search.py tests/test_stock_search.py
git commit -m "feat: search_stocks tool with fuzzy concept matching from SQLite"
```

---

### Task 5: Remaining Stock Data Tools (sector, filter, watchlist)

**Files:**
- Create: `alpha_agents/tools/sector.py`
- Create: `alpha_agents/tools/stock_filter.py`
- Create: `alpha_agents/tools/watchlist.py`
- Create: `tests/test_sector.py`
- Create: `tests/test_stock_filter.py`
- Create: `tests/test_watchlist.py`

- [ ] **Step 1: Write failing tests for sector tool**

Create `tests/test_sector.py`:

```python
import json
from unittest.mock import patch

import pandas as pd
import pytest

from alpha_agents.tools.sector import get_sector_data_fn


def _mock_sector_fund_flow():
    return pd.DataFrame({
        "名称": ["半导体", "白酒"],
        "今日涨跌幅": [2.5, -1.3],
        "今日主力净流入-净额": [500000000.0, -200000000.0],
        "今日主力净流入-净占比": [5.2, -2.1],
    })


@patch("alpha_agents.tools.sector._fetch_sector_fund_flow", side_effect=lambda: _mock_sector_fund_flow())
def test_get_sector_data(mock_flow):
    result = get_sector_data_fn("半导体")
    parsed = json.loads(result)
    assert parsed["sector_name"] == "半导体"
    assert parsed["change_pct"] == 2.5


@patch("alpha_agents.tools.sector._fetch_sector_fund_flow", side_effect=lambda: _mock_sector_fund_flow())
def test_get_sector_data_not_found(mock_flow):
    result = get_sector_data_fn("火星板块")
    parsed = json.loads(result)
    assert parsed["error"] is not None
```

- [ ] **Step 2: Write failing tests for filter tool**

Create `tests/test_stock_filter.py`:

```python
import json
from pathlib import Path

import pytest

from alpha_agents.data.db import init_db, get_connection
from alpha_agents.tools.stock_filter import filter_stocks_fn


@pytest.fixture
def populated_db(tmp_path):
    db_path = tmp_path / "test.db"
    init_db(db_path)
    conn = get_connection(db_path)
    conn.execute(
        "INSERT INTO stocks VALUES ('000001', '平安银行', 200000000000, '银行', 0, 0)"
    )
    conn.execute(
        "INSERT INTO stocks VALUES ('000002', 'ST世纪', 500000000, '房地产', 1, 0)"
    )
    conn.execute(
        "INSERT INTO stocks VALUES ('000003', '微型公司', 100000000, '其他', 0, 0)"
    )
    conn.execute(
        "INSERT INTO stocks VALUES ('000004', '停牌股', 5000000000, '科技', 0, 1)"
    )
    conn.commit()
    conn.close()
    return db_path


def test_filter_removes_st(populated_db):
    result = filter_stocks_fn(["000001", "000002"], populated_db)
    parsed = json.loads(result)
    codes = [s["code"] for s in parsed["stocks"]]
    assert "000001" in codes
    assert "000002" not in codes


def test_filter_removes_suspended(populated_db):
    result = filter_stocks_fn(["000001", "000004"], populated_db)
    parsed = json.loads(result)
    codes = [s["code"] for s in parsed["stocks"]]
    assert "000004" not in codes


def test_filter_removes_small_cap(populated_db):
    result = filter_stocks_fn(["000001", "000003"], populated_db, min_market_cap=1e9)
    parsed = json.loads(result)
    codes = [s["code"] for s in parsed["stocks"]]
    assert "000003" not in codes


def test_filter_reports_removed(populated_db):
    result = filter_stocks_fn(["000001", "000002", "000003", "000004"], populated_db)
    parsed = json.loads(result)
    assert len(parsed["removed"]) == 3
```

- [ ] **Step 3: Write failing tests for watchlist tool**

Create `tests/test_watchlist.py`:

```python
import json
from pathlib import Path

import pytest

from alpha_agents.tools.watchlist import get_watchlist_fn


@pytest.fixture
def watchlist_file(tmp_path):
    wl_path = tmp_path / "watchlist.json"
    wl_path.write_text(json.dumps({
        "stocks": [
            {"code": "600519", "name": "贵州茅台", "concepts": ["白酒", "消费"]},
        ]
    }), encoding="utf-8")
    return wl_path


def test_get_watchlist(watchlist_file):
    result = get_watchlist_fn(watchlist_file)
    parsed = json.loads(result)
    assert len(parsed["stocks"]) == 1
    assert parsed["stocks"][0]["code"] == "600519"
    assert "白酒" in parsed["stocks"][0]["concepts"]


def test_get_watchlist_empty(tmp_path):
    wl_path = tmp_path / "watchlist.json"
    wl_path.write_text(json.dumps({"stocks": []}), encoding="utf-8")
    result = get_watchlist_fn(wl_path)
    parsed = json.loads(result)
    assert len(parsed["stocks"]) == 0
```

- [ ] **Step 4: Run all tests to verify they fail**

Run: `python -m pytest tests/test_sector.py tests/test_stock_filter.py tests/test_watchlist.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 5: Implement sector tool**

Create `alpha_agents/tools/sector.py`:

```python
import json
import logging

import akshare as ak
import pandas as pd

logger = logging.getLogger(__name__)


def _fetch_sector_fund_flow() -> pd.DataFrame:
    return ak.stock_board_concept_fund_flow_ths()


def get_sector_data_fn(sector_name: str) -> str:
    try:
        df = _fetch_sector_fund_flow()
        row = df[df["名称"] == sector_name]
        if row.empty:
            return json.dumps({"sector_name": sector_name, "error": f"板块 '{sector_name}' 未找到"}, ensure_ascii=False)

        r = row.iloc[0]
        return json.dumps({
            "sector_name": sector_name,
            "change_pct": float(r.get("今日涨跌幅", 0)),
            "main_net_inflow": float(r.get("今日主力净流入-净额", 0)),
            "main_net_inflow_pct": float(r.get("今日主力净流入-净占比", 0)),
            "error": None,
        }, ensure_ascii=False)
    except Exception as e:
        logger.error("Failed to fetch sector data: %s", e)
        return json.dumps({"sector_name": sector_name, "error": str(e)}, ensure_ascii=False)
```

- [ ] **Step 6: Implement filter tool**

Create `alpha_agents/tools/stock_filter.py`:

```python
import json
from pathlib import Path

from alpha_agents.config import DB_PATH
from alpha_agents.data.db import get_connection

DEFAULT_MIN_MARKET_CAP = 1_000_000_000  # 10亿


def filter_stocks_fn(
    stock_codes: list[str],
    db_path: Path = DB_PATH,
    min_market_cap: float = DEFAULT_MIN_MARKET_CAP,
) -> str:
    conn = get_connection(db_path)
    try:
        placeholders = ",".join("?" for _ in stock_codes)
        rows = conn.execute(
            f"SELECT code, name, market_cap, industry, is_st, is_suspended "
            f"FROM stocks WHERE code IN ({placeholders})",
            stock_codes,
        ).fetchall()

        kept = []
        removed = []
        for r in rows:
            reasons = []
            if r["is_st"]:
                reasons.append("ST")
            if r["is_suspended"]:
                reasons.append("停牌")
            if r["market_cap"] is not None and r["market_cap"] < min_market_cap:
                reasons.append(f"市值不足{min_market_cap/1e8:.0f}亿")

            stock = {"code": r["code"], "name": r["name"], "market_cap": r["market_cap"], "industry": r["industry"]}
            if reasons:
                removed.append({**stock, "reasons": reasons})
            else:
                kept.append(stock)

        return json.dumps({"stocks": kept, "removed": removed}, ensure_ascii=False)
    finally:
        conn.close()
```

- [ ] **Step 7: Implement watchlist tool**

Create `alpha_agents/tools/watchlist.py`:

```python
import json
from pathlib import Path

from alpha_agents.config import WATCHLIST_PATH


def get_watchlist_fn(watchlist_path: Path = WATCHLIST_PATH) -> str:
    try:
        data = json.loads(watchlist_path.read_text(encoding="utf-8"))
        return json.dumps(data, ensure_ascii=False)
    except FileNotFoundError:
        return json.dumps({"stocks": [], "error": "自选股文件不存在"}, ensure_ascii=False)
```

- [ ] **Step 8: Run all tests to verify they pass**

Run: `python -m pytest tests/test_sector.py tests/test_stock_filter.py tests/test_watchlist.py -v`
Expected: All 8 tests PASS.

- [ ] **Step 9: Commit**

```bash
git add alpha_agents/tools/sector.py alpha_agents/tools/stock_filter.py alpha_agents/tools/watchlist.py tests/test_sector.py tests/test_stock_filter.py tests/test_watchlist.py
git commit -m "feat: sector data, stock filter, and watchlist tools"
```

---

### Task 6: News Tool (get_news)

**Files:**
- Create: `alpha_agents/tools/news.py`
- Create: `tests/test_news.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_news.py`:

```python
import json
from unittest.mock import patch

import pandas as pd
import pytest

from alpha_agents.tools.news import get_news_fn


def _mock_stock_news():
    return pd.DataFrame({
        "标题": ["特朗普宣布对华加征关税", "央行降准0.5个百分点", "某公司发布年报"],
        "内容": ["美国总统特朗普宣布...", "中国人民银行决定...", "某公司2025年营收..."],
        "发布时间": ["2026-04-02 08:00", "2026-04-02 07:30", "2026-04-02 07:00"],
        "文章来源": ["新浪财经", "东方财富", "同花顺"],
    })


@patch("alpha_agents.tools.news._fetch_news", side_effect=lambda **kw: _mock_stock_news())
def test_get_news_returns_list(mock_fetch):
    result = get_news_fn(limit=10)
    parsed = json.loads(result)
    assert len(parsed["news"]) == 3
    assert parsed["news"][0]["title"] == "特朗普宣布对华加征关税"


@patch("alpha_agents.tools.news._fetch_news", side_effect=lambda **kw: _mock_stock_news())
def test_get_news_with_keyword(mock_fetch):
    result = get_news_fn(limit=10, keyword="特朗普")
    parsed = json.loads(result)
    assert len(parsed["news"]) == 1
    assert "特朗普" in parsed["news"][0]["title"]


@patch("alpha_agents.tools.news._fetch_news", side_effect=lambda **kw: _mock_stock_news())
def test_get_news_respects_limit(mock_fetch):
    result = get_news_fn(limit=2)
    parsed = json.loads(result)
    assert len(parsed["news"]) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_news.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement news tool**

Create `alpha_agents/tools/news.py`:

```python
import json
import logging

import akshare as ak
import pandas as pd

logger = logging.getLogger(__name__)


def _fetch_news(**kwargs) -> pd.DataFrame:
    return ak.stock_news_em()


def get_news_fn(limit: int = 50, keyword: str | None = None) -> str:
    try:
        df = _fetch_news()

        if keyword:
            mask = df["标题"].str.contains(keyword, na=False) | df["内容"].str.contains(keyword, na=False)
            df = df[mask]

        df = df.head(limit)

        news = []
        for _, row in df.iterrows():
            news.append({
                "title": str(row.get("标题", "")),
                "summary": str(row.get("内容", ""))[:200],
                "time": str(row.get("发布时间", "")),
                "source": str(row.get("文章来源", "")),
            })

        return json.dumps({"news": news, "count": len(news)}, ensure_ascii=False)
    except Exception as e:
        logger.error("Failed to fetch news: %s", e)
        return json.dumps({"news": [], "count": 0, "error": str(e)}, ensure_ascii=False)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_news.py -v`
Expected: All 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add alpha_agents/tools/news.py tests/test_news.py
git commit -m "feat: get_news tool with keyword filtering via akshare"
```

---

### Task 7: MCP Server Registration

**Files:**
- Create: `alpha_agents/tools/server.py`
- Create: `tests/test_server.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_server.py`:

```python
from alpha_agents.tools.server import create_tools_server


def test_create_server_returns_server_config():
    server = create_tools_server()
    # create_sdk_mcp_server returns a dict-like config with "tools" key
    # The exact type depends on SDK internals, just verify it's not None
    assert server is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_server.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement MCP server**

Create `alpha_agents/tools/server.py`:

```python
from claude_agent_sdk import tool, create_sdk_mcp_server

from alpha_agents.tools.news import get_news_fn
from alpha_agents.tools.stock_search import search_stocks_fn
from alpha_agents.tools.sector import get_sector_data_fn
from alpha_agents.tools.stock_filter import filter_stocks_fn
from alpha_agents.tools.watchlist import get_watchlist_fn


@tool("get_news", "获取最新财经新闻。可按关键词过滤。", {"limit": int, "keyword": str})
async def get_news(args):
    result = get_news_fn(limit=args.get("limit", 50), keyword=args.get("keyword"))
    return {"content": [{"type": "text", "text": result}]}


@tool("search_stocks", "根据概念/板块关键词检索相关个股。从本地同花顺概念板块索引中模糊匹配。", {"keyword": str})
async def search_stocks(args):
    result = search_stocks_fn(keyword=args["keyword"])
    return {"content": [{"type": "text", "text": result}]}


@tool("get_sector_data", "获取板块行情数据，包括涨跌幅和资金流向。", {"sector_name": str})
async def get_sector_data(args):
    result = get_sector_data_fn(sector_name=args["sector_name"])
    return {"content": [{"type": "text", "text": result}]}


@tool("filter_stocks", "过滤不适合的个股（剔除ST、停牌、市值过小）。", {"stock_codes": list})
async def filter_stocks(args):
    result = filter_stocks_fn(stock_codes=args["stock_codes"])
    return {"content": [{"type": "text", "text": result}]}


@tool("get_watchlist", "读取用户自选股列表。", {})
async def get_watchlist(args):
    result = get_watchlist_fn()
    return {"content": [{"type": "text", "text": result}]}


def create_tools_server():
    return create_sdk_mcp_server(
        "alpha-agents-data",
        tools=[get_news, search_stocks, get_sector_data, filter_stocks, get_watchlist],
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_server.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add alpha_agents/tools/server.py tests/test_server.py
git commit -m "feat: MCP server registering all data tools"
```

---

### Task 8: Agent Prompts

**Files:**
- Create: `alpha_agents/prompts/strategist.md`
- Create: `alpha_agents/prompts/geopolitical.md`

- [ ] **Step 1: Create main agent (strategist) system prompt**

Create `alpha_agents/prompts/strategist.md`:

```markdown
# 你是AlphaAgents首席策略师

你是一个自主选股分析系统的核心决策者。你的任务是监控财经新闻，分析事件对A股市场的影响，输出推荐关注的个股列表。

## 工作流程

你不需要遵循固定流程。根据新闻内容自主决定分析路径。以下是你的能力：

1. **获取新闻** — 使用 get_news 获取最新财经新闻
2. **判断重要性** — 自主判断哪些新闻值得分析：
   - 5级（必须分析）：重大政策变动、贸易战升级/缓和、制裁、战争、央行重大动作
   - 4级（应该分析）：行业政策、重要经济数据、大型企业重大变动
   - 3级（可以分析）：行业动态、市场情绪变化
   - 1-2级（跳过）：个股公告、常规新闻
3. **深度分析** — 遇到复杂地缘政治事件（贸易战、制裁、国际冲突），调度局势深度分析师（geopolitical子Agent）
4. **检索个股** — 使用 search_stocks 从概念/板块关键词检索相关个股
5. **核查数据** — 使用 get_sector_data 确认板块资金流向是否支持你的判断
6. **过滤筛选** — 使用 filter_stocks 剔除ST、停牌、小市值
7. **检查自选股** — 使用 get_watchlist 检查用户自选股是否受影响

## 自主决策原则

- 信息不够就继续搜索，不要一轮就结束
- 一个事件可能影响多个方向，逐个追查
- 可以多次调用工具验证判断（先搜概念，再查资金流向）
- 小事件轻分析，大事件深挖
- 使用 WebSearch/WebFetch 搜索海外新闻获取更多上下文

## 板块影响分析方法论

- **美林时钟**：当前经济周期决定了哪些板块处于有利位置
  - 衰退期：债券、防御性板块（医药、公用事业）
  - 复苏期：股票、周期性板块（消费、科技）
  - 过热期：大宗商品、资源类
  - 滞涨期：现金、防御性板块
- **事件→板块映射**：
  - 贸易战/制裁 → 国产替代、自主可控利好；出口依赖型利空
  - 降准降息 → 银行利空、地产/基建利好
  - 战争/冲突 → 军工、能源利好；航空、旅游利空
  - 科技政策 → 对应细分领域

## 输出规范

每次分析完成后，严格按以下格式输出：

═══════════════════════════════════════════
AlphaAgents 分析报告 | {日期时间}
═══════════════════════════════════════════

【触发事件】
{事件描述}

【局势解读】
{分析内容}

【推荐关注】
| 代码 | 名称 | 关联概念 | 推荐理由 |
|------|------|---------|---------|
| ... | ... | ... | ... |

【自选股影响】
• {代码} {名称} — {影响描述}（利好/利空程度X/5）

【风险提示】
此为事件驱动初筛，需结合单股深度分析做最终决策。
```

- [ ] **Step 2: Create geopolitical analyst sub-agent prompt**

Create `alpha_agents/prompts/geopolitical.md`:

```markdown
# 你是局势深度分析师

你专注于解读地缘政治事件对A股市场的影响。你的分析要深入、具体、可操作。

## 专长领域

- 贸易战（关税、出口管制、实体清单）
- 国际制裁（金融制裁、技术封锁）
- 军事冲突（地区战争、军事对峙）
- 外交变动（建交/断交、联盟变化）
- 大国政策（特朗普政策、欧盟政策、日韩政策）

## 特朗普政策分析框架（TACO — 交易的艺术）

特朗普的政策行为通常遵循以下模式：
1. **极限施压**：先提出极端要求（如高额关税），制造恐慌
2. **谈判空间**：留出让步余地，等待对方回应
3. **交易达成**：以"让步"姿态达成比现状更有利的协议
4. **宣传胜利**：不管结果如何都宣布"伟大的交易"

市场影响规律：
- 施压阶段：市场恐慌，相关板块大跌（此时可能是买入机会）
- 缓和信号：市场反弹，之前跌的最多的反弹最大
- 达成协议：利好出尽，注意获利了结
- 识别是真制裁还是谈判筹码很关键

## 输出格式

分析完成后，输出板块影响映射：

**利好板块：**
- {板块名称}（影响程度：X/5）— {影响理由}

**利空板块：**
- {板块名称}（影响程度：X/5）— {影响理由}

**判断依据：**
- {关键论点1}
- {关键论点2}

**不确定因素：**
- {可能改变判断的变量}
```

- [ ] **Step 3: Commit**

```bash
mkdir -p alpha_agents/prompts
git add alpha_agents/prompts/strategist.md alpha_agents/prompts/geopolitical.md
git commit -m "feat: system prompts for strategist and geopolitical analyst"
```

---

### Task 9: Agent Definitions (strategist + geopolitical)

**Files:**
- Create: `alpha_agents/agents/__init__.py`
- Create: `alpha_agents/agents/strategist.py`
- Create: `alpha_agents/agents/geopolitical.py`

- [ ] **Step 1: Create agents package**

Create `alpha_agents/agents/__init__.py`:

```python
"""Agent definitions for AlphaAgents."""
```

- [ ] **Step 2: Implement geopolitical sub-agent definition**

Create `alpha_agents/agents/geopolitical.py`:

```python
from claude_agent_sdk import AgentDefinition

from alpha_agents.config import PROMPTS_DIR


def get_geopolitical_agent() -> tuple[str, AgentDefinition]:
    prompt = (PROMPTS_DIR / "geopolitical.md").read_text(encoding="utf-8")
    return "geopolitical", AgentDefinition(
        description="局势深度分析师。专注地缘政治事件（贸易战、制裁、冲突）对A股板块的影响分析。",
        prompt=prompt,
        tools=["WebSearch", "WebFetch"],
    )
```

- [ ] **Step 3: Implement main agent (strategist) setup**

Create `alpha_agents/agents/strategist.py`:

```python
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, ResultMessage, AssistantMessage, TextBlock

from alpha_agents.config import PROMPTS_DIR
from alpha_agents.tools.server import create_tools_server
from alpha_agents.agents.geopolitical import get_geopolitical_agent


def _build_options(system_prompt: str) -> ClaudeAgentOptions:
    tools_server = create_tools_server()
    agent_name, agent_def = get_geopolitical_agent()

    return ClaudeAgentOptions(
        system_prompt=system_prompt,
        mcp_servers={"alpha-data": tools_server},
        allowed_tools=["Agent", "WebSearch", "WebFetch"],
        agents={agent_name: agent_def},
    )


async def run_analysis(prompt: str) -> str:
    system_prompt = (PROMPTS_DIR / "strategist.md").read_text(encoding="utf-8")
    options = _build_options(system_prompt)

    results = []
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        results.append(block.text)
            elif isinstance(message, ResultMessage):
                results.append(message.result)

    return "\n".join(results)
```

- [ ] **Step 4: Commit**

```bash
git add alpha_agents/agents/__init__.py alpha_agents/agents/strategist.py alpha_agents/agents/geopolitical.py
git commit -m "feat: strategist and geopolitical agent definitions with MCP tools"
```

---

### Task 10: News Monitor Loop

**Files:**
- Create: `alpha_agents/monitor.py`
- Create: `tests/test_monitor.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_monitor.py`:

```python
import json
from unittest.mock import patch, AsyncMock

import pytest

from alpha_agents.monitor import NewsMonitor


@pytest.fixture
def monitor():
    return NewsMonitor(interval=10)


def test_monitor_dedup(monitor):
    """Same news title should not trigger twice."""
    news_batch_1 = [
        {"title": "特朗普加关税", "summary": "...", "time": "2026-04-02 08:00", "source": "新浪"},
        {"title": "央行降准", "summary": "...", "time": "2026-04-02 07:00", "source": "东方财富"},
    ]
    new_items = monitor.deduplicate(news_batch_1)
    assert len(new_items) == 2

    # Second call with same news + one new
    news_batch_2 = [
        {"title": "特朗普加关税", "summary": "...", "time": "2026-04-02 08:00", "source": "新浪"},
        {"title": "新的重大消息", "summary": "...", "time": "2026-04-02 09:00", "source": "财联社"},
    ]
    new_items = monitor.deduplicate(news_batch_2)
    assert len(new_items) == 1
    assert new_items[0]["title"] == "新的重大消息"


def test_monitor_seen_limit(monitor):
    """Seen set should not grow unbounded."""
    for i in range(2000):
        monitor.deduplicate([{"title": f"news_{i}", "summary": "", "time": "", "source": ""}])
    assert len(monitor._seen_titles) <= 1500
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_monitor.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement monitor**

Create `alpha_agents/monitor.py`:

```python
import asyncio
import json
import logging
from collections import deque

from alpha_agents.config import MONITOR_INTERVAL_SECONDS, NEWS_FETCH_LIMIT
from alpha_agents.tools.news import get_news_fn
from alpha_agents.agents.strategist import run_analysis

logger = logging.getLogger(__name__)

MAX_SEEN = 1000


class NewsMonitor:
    def __init__(self, interval: int = MONITOR_INTERVAL_SECONDS):
        self.interval = interval
        self._seen_titles: set[str] = set()
        self._seen_order: deque[str] = deque()

    def deduplicate(self, news_items: list[dict]) -> list[dict]:
        new_items = []
        for item in news_items:
            title = item["title"]
            if title not in self._seen_titles:
                self._seen_titles.add(title)
                self._seen_order.append(title)
                new_items.append(item)

        # Evict oldest to keep bounded
        while len(self._seen_titles) > MAX_SEEN:
            old = self._seen_order.popleft()
            self._seen_titles.discard(old)

        return new_items

    async def run(self) -> None:
        logger.info("News monitor started. Interval: %ds", self.interval)
        while True:
            try:
                raw = get_news_fn(limit=NEWS_FETCH_LIMIT)
                news_data = json.loads(raw)
                news_items = news_data.get("news", [])

                new_items = self.deduplicate(news_items)
                if new_items:
                    logger.info("Found %d new news items", len(new_items))
                    news_summary = "\n".join(
                        f"- [{item['time']}] {item['title']}: {item['summary']}"
                        for item in new_items
                    )
                    prompt = (
                        f"以下是最新获取的{len(new_items)}条财经新闻，请分析是否有值得关注的事件，"
                        f"如有，自主完成完整的分析流程并输出推荐报告：\n\n{news_summary}"
                    )
                    result = await run_analysis(prompt)
                    print(result)
                else:
                    logger.debug("No new news items")

            except Exception:
                logger.exception("Error in monitor loop")

            await asyncio.sleep(self.interval)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_monitor.py -v`
Expected: All 2 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add alpha_agents/monitor.py tests/test_monitor.py
git commit -m "feat: news monitor with deduplication and auto-trigger"
```

---

### Task 11: CLI Entry Point (main.py)

**Files:**
- Create: `main.py`

- [ ] **Step 1: Implement CLI entry point**

Create `main.py`:

```python
import argparse
import asyncio
import logging
import sys

from alpha_agents.agents.strategist import run_analysis
from alpha_agents.monitor import NewsMonitor
from alpha_agents.data.index_builder import build_index
from alpha_agents.config import DB_PATH, MONITOR_INTERVAL_SECONDS


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def cmd_analyze(args: argparse.Namespace) -> None:
    if args.event:
        prompt = f"请分析以下事件对A股市场的影响，完成完整分析并输出推荐报告：\n\n{args.event}"
    else:
        prompt = "请获取最新财经新闻，分析当前市场局势，如有值得关注的事件，完成完整分析并输出推荐报告。"

    result = asyncio.run(run_analysis(prompt))
    print(result)


def cmd_monitor(args: argparse.Namespace) -> None:
    interval = args.interval or MONITOR_INTERVAL_SECONDS
    monitor = NewsMonitor(interval=interval)
    asyncio.run(monitor.run())


def cmd_build_index(args: argparse.Namespace) -> None:
    logging.info("Building stock concept index...")
    build_index(DB_PATH)
    logging.info("Index built successfully at %s", DB_PATH)


def main() -> None:
    parser = argparse.ArgumentParser(description="AlphaAgents — 新闻驱动的自主选股系统")
    parser.add_argument("-v", "--verbose", action="store_true", help="详细日志输出")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # analyze
    p_analyze = subparsers.add_parser("analyze", help="手动触发分析")
    p_analyze.add_argument("--event", type=str, help="指定分析的事件")
    p_analyze.set_defaults(func=cmd_analyze)

    # monitor
    p_monitor = subparsers.add_parser("monitor", help="启动新闻监控（持续运行）")
    p_monitor.add_argument("--interval", type=int, help="监控间隔（秒）")
    p_monitor.set_defaults(func=cmd_monitor)

    # build-index
    p_index = subparsers.add_parser("build-index", help="构建股票概念索引")
    p_index.set_defaults(func=cmd_build_index)

    args = parser.parse_args()
    setup_logging(args.verbose)
    args.func(args)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify CLI help works**

Run: `cd /Users/evilkylin/Projects/AlphaAgents && python main.py --help`
Expected: Shows help with `analyze`, `monitor`, `build-index` subcommands.

Run: `python main.py analyze --help`
Expected: Shows `--event` option.

- [ ] **Step 3: Commit**

```bash
git add main.py
git commit -m "feat: CLI entry point with analyze, monitor, and build-index commands"
```

---

### Task 12: End-to-End Verification

- [ ] **Step 1: Run full test suite**

Run: `cd /Users/evilkylin/Projects/AlphaAgents && python -m pytest tests/ -v`
Expected: All tests PASS.

- [ ] **Step 2: Build stock index (requires network)**

Run: `python main.py build-index -v`
Expected: Successfully pulls akshare data and builds `alpha_agents/data/stocks.db`. This may take a few minutes due to the volume of concept boards.

- [ ] **Step 3: Test manual analysis (requires ANTHROPIC_API_KEY)**

Run: `python main.py analyze --event "特朗普宣布对中国芯片出口管制升级" -v`
Expected: Agent runs autonomously — fetches related concepts, searches stocks, filters, outputs a formatted report.

- [ ] **Step 4: Test monitor mode briefly**

Run: `python main.py monitor --interval 30 -v`
Expected: Monitor starts, fetches news, deduplicates. If significant news found, triggers analysis. Ctrl+C to stop.

- [ ] **Step 5: Final commit and push**

```bash
git add -A
git commit -m "feat: end-to-end verification complete"
git push origin main
```
