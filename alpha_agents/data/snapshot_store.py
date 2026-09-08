"""Time-indexed market data snapshots for replay fidelity.

Purpose: every call to a volatile data source in live mode writes a row here
keyed by (data_type, captured_at). Replay mode reads "latest row <= as_of".

Each data type gets its own structured table (not a generic JSON blob), so
snapshots are directly queryable (trend charts, backtests without re-parsing).

Lives in a dedicated DB ``data/market_snapshots.db`` to keep memory.db (which
holds portfolio/predictions/themes) small and transactional.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

import pandas as pd

from alpha_agents.config import DATA_DIR

logger = logging.getLogger(__name__)

SNAPSHOTS_DB_PATH = DATA_DIR / "market_snapshots.db"

_SCHEMA = """
-- 1. Industry/concept fund flow — board-level rankings captured at time T
CREATE TABLE IF NOT EXISTS sector_flow_snapshots (
    captured_at TEXT NOT NULL,
    scope TEXT NOT NULL,
    sector_name TEXT NOT NULL,
    change_pct REAL,
    net_flow_yi REAL,
    leader TEXT,
    leader_change_pct REAL,
    company_count INTEGER,
    PRIMARY KEY(captured_at, scope, sector_name)
);
CREATE INDEX IF NOT EXISTS idx_sf_lookup
    ON sector_flow_snapshots(scope, captured_at);

-- 2. Market breadth (advance/decline, limit-up counts, sentiment)
CREATE TABLE IF NOT EXISTS market_breadth_snapshots (
    captured_at TEXT PRIMARY KEY,
    advances INTEGER, declines INTEGER, flat INTEGER,
    limit_up INTEGER, limit_down INTEGER,
    real_limit_up INTEGER, real_limit_down INTEGER,
    ad_ratio REAL, sentiment TEXT,
    activity_pct TEXT
);

-- 3. Limit-up/down/broken pool (changes intraday as stocks hit/break limit)
CREATE TABLE IF NOT EXISTS limit_pool_snapshots (
    captured_at TEXT NOT NULL,
    code TEXT NOT NULL,
    pool_type TEXT NOT NULL,   -- 'up' | 'down' | 'broken'
    name TEXT,
    change_pct REAL,
    turnover_rate REAL,
    seal_amount_yi REAL,
    first_seal_time TEXT,
    break_count INTEGER,
    consecutive_limits INTEGER,
    sector TEXT,
    PRIMARY KEY(captured_at, code, pool_type)
);
CREATE INDEX IF NOT EXISTS idx_lp_type
    ON limit_pool_snapshots(pool_type, captured_at);

-- 4. Realtime quotes per stock
CREATE TABLE IF NOT EXISTS realtime_quote_snapshots (
    captured_at TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    price REAL, change_pct REAL,
    volume INTEGER, amount_yi REAL,
    high REAL, low REAL, open REAL, prev_close REAL,
    PRIMARY KEY(captured_at, code)
);

-- 5. Per-stock fund flow (consecutive inflow days etc.)
CREATE TABLE IF NOT EXISTS stock_fund_flow_snapshots (
    captured_at TEXT NOT NULL,
    code TEXT NOT NULL,
    consecutive_inflow_days INTEGER,
    consecutive_outflow_days INTEGER,
    trend TEXT,
    main_net_latest REAL,
    main_pct_latest REAL,
    data_full TEXT,
    PRIMARY KEY(captured_at, code)
);

-- 6. Global market (US indices / bonds / commodities / futures)
CREATE TABLE IF NOT EXISTS global_market_snapshots (
    captured_at TEXT NOT NULL,
    category TEXT NOT NULL,
    name TEXT NOT NULL,
    value REAL, change_pct REAL,
    PRIMARY KEY(captured_at, category, name)
);

-- 8. Full-market realtime spot snapshot (all ~5200 A-shares per capture)
CREATE TABLE IF NOT EXISTS all_quote_snapshots (
    captured_at TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    price REAL, change_pct REAL,
    volume INTEGER, amount_yi REAL,
    high REAL, low REAL, open REAL, prev_close REAL,
    turnover_rate REAL, volume_ratio REAL,
    amplitude REAL, pe REAL, pb REAL,
    market_cap_yi REAL, float_cap_yi REAL,
    PRIMARY KEY(captured_at, code)
);
CREATE INDEX IF NOT EXISTS idx_aq_code ON all_quote_snapshots(code, captured_at);

-- ============================================================
-- Tushare-sourced daily tables (EOD, one row per (trade_date, ts_code) or
-- (trade_date,) for market-wide). Schemas mirror Tushare's output so backfill
-- can INSERT OR REPLACE without transformation.
-- ============================================================

-- Full-market EOD stock fund flow (Tushare moneyflow_dc, ~5900 rows/day)
CREATE TABLE IF NOT EXISTS stock_fund_flow_daily (
    trade_date TEXT NOT NULL,
    ts_code TEXT NOT NULL,
    code TEXT NOT NULL,                   -- bare 6-digit for joins
    name TEXT,
    pct_change REAL,
    close REAL,
    net_amount REAL,                      -- 主力净流入(万元)
    net_amount_rate REAL,
    buy_elg_amount REAL,                  -- 超大单净流入(万元)
    buy_elg_amount_rate REAL,
    buy_lg_amount REAL,                   -- 大单
    buy_lg_amount_rate REAL,
    buy_md_amount REAL,                   -- 中单
    buy_md_amount_rate REAL,
    buy_sm_amount REAL,                   -- 小单
    buy_sm_amount_rate REAL,
    PRIMARY KEY(trade_date, ts_code)
);
CREATE INDEX IF NOT EXISTS idx_sfd_code ON stock_fund_flow_daily(code, trade_date);

-- Dragon-Tiger list main (top_list)
CREATE TABLE IF NOT EXISTS lhb_daily (
    trade_date TEXT NOT NULL,
    ts_code TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    close REAL,
    pct_change REAL,
    turnover_rate REAL,
    amount REAL,
    l_sell REAL,
    l_buy REAL,
    l_amount REAL,
    net_amount REAL,
    net_rate REAL,
    amount_rate REAL,
    float_values REAL,
    reason TEXT,
    PRIMARY KEY(trade_date, ts_code, reason)
);

-- Dragon-Tiger institutional seats (top_inst)
CREATE TABLE IF NOT EXISTS lhb_inst_daily (
    trade_date TEXT NOT NULL,
    ts_code TEXT NOT NULL,
    exalter TEXT NOT NULL,
    side INTEGER,                         -- 0 买方, 1 卖方
    buy REAL,
    buy_rate REAL,
    sell REAL,
    sell_rate REAL,
    net_buy REAL,
    reason TEXT,
    PRIMARY KEY(trade_date, ts_code, exalter, side, reason)
);

-- 游资 (hm_detail)
CREATE TABLE IF NOT EXISTS hm_daily (
    trade_date TEXT NOT NULL,
    ts_code TEXT NOT NULL,
    code TEXT NOT NULL,
    ts_name TEXT,
    buy_amount REAL,
    sell_amount REAL,
    net_amount REAL,
    hm_name TEXT,
    hm_orgs TEXT,
    tag TEXT,
    PRIMARY KEY(trade_date, ts_code, hm_name)
);

-- 北向资金 EOD (moneyflow_hsgt, 1 row/day)
CREATE TABLE IF NOT EXISTS north_flow_daily (
    trade_date TEXT PRIMARY KEY,
    ggt_ss REAL,                          -- 港股通(上) 成交净买额
    ggt_sz REAL,                          -- 港股通(深)
    hgt REAL,                             -- 沪股通
    sgt REAL,                             -- 深股通
    north_money REAL,                     -- 北向资金净流入 (亿)
    south_money REAL                      -- 南向
);

-- 两融 EOD (margin, 1 row/day/exchange)
CREATE TABLE IF NOT EXISTS margin_daily (
    trade_date TEXT NOT NULL,
    exchange_id TEXT NOT NULL,            -- SSE / SZSE
    rzye REAL,                            -- 融资余额
    rzmre REAL,                           -- 融资买入额
    rzche REAL,                           -- 融资偿还额
    rqye REAL,                            -- 融券余额
    rqmcl REAL,
    rzrqye REAL,                          -- 两融余额合计
    rqyl REAL,
    PRIMARY KEY(trade_date, exchange_id)
);

-- Web search results (query → results), for replay fidelity when cause_analyst
-- or similar agents trigger DuckDuckGo. Exact-query lookup only — new queries
-- during replay return empty + warning (no future leakage).
CREATE TABLE IF NOT EXISTS web_search_snapshots (
    captured_at TEXT NOT NULL,
    query TEXT NOT NULL,
    result_json TEXT NOT NULL,
    PRIMARY KEY(captured_at, query)
);
CREATE INDEX IF NOT EXISTS idx_ws_query ON web_search_snapshots(query, captured_at);

-- KPL 涨停原因 (Tushare kpl_list) — 开盘啦每日涨停股附带"涨停原因"
-- (lu_desc) 和"题材标签"(theme)，这是同花顺网页展示的那个字段。
CREATE TABLE IF NOT EXISTS kpl_limit_list_daily (
    trade_date TEXT NOT NULL,
    ts_code TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    lu_time TEXT,               -- 首次涨停时间
    lu_desc TEXT,               -- 涨停原因（如 "算力", "通信", "锂电池"）
    theme TEXT,                 -- 题材标签（逗号分隔，如 "光模块、通信"）
    status TEXT,                -- 首板 / 2连板 / 3连板 / 炸板
    tag TEXT,                   -- 涨停 / 跌停 / 炸板
    amount REAL,
    turnover_rate REAL,
    net_change REAL,
    limit_order REAL,
    lu_limit_order REAL,
    PRIMARY KEY(trade_date, ts_code)
);
CREATE INDEX IF NOT EXISTS idx_kpl_code ON kpl_limit_list_daily(code, trade_date);
CREATE INDEX IF NOT EXISTS idx_kpl_desc ON kpl_limit_list_daily(trade_date, lu_desc);

-- KPL 当日涨停最集中的题材榜单 (Tushare limit_cpt_list) —
-- 每日 ~20 个题材 + 该题材几连板、涨停数、涨幅 → 真实主线总览
CREATE TABLE IF NOT EXISTS kpl_concept_daily (
    trade_date TEXT NOT NULL,
    ts_code TEXT NOT NULL,      -- 题材指数代码 (如 885756.TI)
    name TEXT NOT NULL,         -- 题材名 (如 "芯片概念")
    days INTEGER,
    up_stat TEXT,               -- "7天4板"
    cons_nums TEXT,             -- 连板数
    up_nums INTEGER,            -- 当日涨停家数
    pct_chg REAL,               -- 题材涨幅
    rank INTEGER,
    PRIMARY KEY(trade_date, ts_code)
);
CREATE INDEX IF NOT EXISTS idx_kpl_cpt_rank ON kpl_concept_daily(trade_date, rank);

-- 7. News items (cross-source, deduplicated by hash)
CREATE TABLE IF NOT EXISTS news_items (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    published_at TEXT NOT NULL,
    title TEXT, summary TEXT, url TEXT,
    hash TEXT NOT NULL UNIQUE,
    captured_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_news_time ON news_items(published_at);
CREATE INDEX IF NOT EXISTS idx_news_src ON news_items(source, published_at);
"""

_local = threading.local()
_write_lock = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    """Thread-local connection to snapshots DB."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        SNAPSHOTS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(SNAPSHOTS_DB_PATH, check_same_thread=False,
                               isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        _local.conn = conn
    return conn


def now_captured_at() -> str:
    """Current timestamp string (minute-precise)."""
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# ── Sector flow (industry + concept) ──

_SECTOR_FLOW_COLS = {
    "industry": {
        "name_col": "行业",
        "change_col": "行业-涨跌幅",
        "flow_col": "净额",
        "leader_col": "领涨股",
        "leader_change_col": "领涨股-涨跌幅",
        "count_col": "公司家数",
    },
    "concept": {
        "name_col_candidates": ["行业", "名称"],
        "change_col": "行业-涨跌幅",
        "flow_col": "净额",
        "leader_col": "领涨股",
        "leader_change_col": "领涨股-涨跌幅",
        "count_col": "公司家数",
    },
}


def save_sector_flow(df: pd.DataFrame, scope: str, captured_at: str | None = None) -> int:
    """Persist a sector/concept fund-flow snapshot. Returns rows written."""
    if df is None or df.empty:
        return 0
    captured_at = captured_at or now_captured_at()
    cols = _SECTOR_FLOW_COLS[scope]
    if scope == "concept":
        name_col = next((c for c in cols["name_col_candidates"] if c in df.columns), None)
    else:
        name_col = cols["name_col"] if cols["name_col"] in df.columns else None
    if not name_col:
        logger.warning("save_sector_flow: no name column in df (scope=%s)", scope)
        return 0

    rows = []
    for _, r in df.iterrows():
        name = str(r.get(name_col, "")).strip()
        if not name:
            continue
        rows.append((
            captured_at, scope, name,
            float(r.get(cols["change_col"], 0) or 0),
            float(r.get(cols["flow_col"], 0) or 0),
            str(r.get(cols["leader_col"], "") or ""),
            float(r.get(cols["leader_change_col"], 0) or 0),
            int(r.get(cols["count_col"], 0) or 0),
        ))
    if not rows:
        return 0

    with _write_lock:
        conn = _get_conn()
        conn.executemany(
            "INSERT OR REPLACE INTO sector_flow_snapshots "
            "(captured_at, scope, sector_name, change_pct, net_flow_yi, "
            " leader, leader_change_pct, company_count) "
            "VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


def read_sector_flow(scope: str, as_of: str) -> pd.DataFrame | None:
    """Read latest sector flow snapshot at or before ``as_of``.

    ``as_of`` is 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM'. Returns a DataFrame shaped
    like the live akshare output (so the existing consumer in
    ``sector_ranking`` works unchanged).
    """
    # Bare date means "any time on that day"; without this, a query for
    # "2026-04-18" would not see a row captured at "2026-04-18 09:25"
    # because of lexical ordering (space > empty).
    if len(as_of) == 10:
        as_of = as_of + " 23:59"
    conn = _get_conn()
    row = conn.execute(
        "SELECT captured_at FROM sector_flow_snapshots "
        "WHERE scope = ? AND captured_at <= ? "
        "ORDER BY captured_at DESC LIMIT 1",
        (scope, as_of),
    ).fetchone()
    if not row:
        return None
    latest = row["captured_at"]

    rows = conn.execute(
        "SELECT sector_name, change_pct, net_flow_yi, leader, "
        "       leader_change_pct, company_count "
        "FROM sector_flow_snapshots "
        "WHERE scope = ? AND captured_at = ?",
        (scope, latest),
    ).fetchall()
    if not rows:
        return None

    # Reshape to match akshare columns the consumer code expects
    name_col = "行业"  # sector_ranking code checks both '行业' and '名称'
    return pd.DataFrame([{
        name_col: r["sector_name"],
        "行业-涨跌幅": r["change_pct"],
        "净额": r["net_flow_yi"],
        "领涨股": r["leader"] or "",
        "领涨股-涨跌幅": r["leader_change_pct"] or 0,
        "公司家数": r["company_count"] or 0,
    } for r in rows])


# ── Market breadth ──

def save_market_breadth(activity_df: pd.DataFrame, captured_at: str | None = None) -> bool:
    """Persist a market breadth snapshot from akshare's stock_market_activity_legu
    (a 2-column DataFrame of item/value pairs). Returns True if saved."""
    if activity_df is None or activity_df.empty:
        return False
    # Flip the 2-col frame into a dict
    data = {str(r["item"]): r["value"] for _, r in activity_df.iterrows()}
    captured_at = captured_at or now_captured_at()

    def _int(k):
        try:
            return int(float(data.get(k, 0) or 0))
        except Exception:
            return 0

    advances = _int("上涨")
    declines = _int("下跌")
    ad_ratio = round(advances / declines, 2) if declines > 0 else None

    with _write_lock:
        _get_conn().execute(
            "INSERT OR REPLACE INTO market_breadth_snapshots "
            "(captured_at, advances, declines, flat, limit_up, limit_down, "
            " real_limit_up, real_limit_down, ad_ratio, sentiment, activity_pct) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (captured_at, advances, declines, _int("平盘"),
             _int("涨停"), _int("跌停"),
             _int("真实涨停"), _int("真实跌停"),
             ad_ratio, None, str(data.get("活跃度", "") or "")),
        )
    return True


def read_market_breadth(as_of: str) -> pd.DataFrame | None:
    """Reshape back to akshare's 2-col item/value form so callers don't change."""
    if len(as_of) == 10:
        as_of = as_of + " 23:59"
    row = _get_conn().execute(
        "SELECT * FROM market_breadth_snapshots "
        "WHERE captured_at <= ? ORDER BY captured_at DESC LIMIT 1",
        (as_of,),
    ).fetchone()
    if not row:
        return None
    items = [
        ("上涨", row["advances"]),
        ("下跌", row["declines"]),
        ("平盘", row["flat"]),
        ("涨停", row["limit_up"]),
        ("跌停", row["limit_down"]),
        ("真实涨停", row["real_limit_up"]),
        ("真实跌停", row["real_limit_down"]),
        ("活跃度", row["activity_pct"] or ""),
        ("统计日期", row["captured_at"]),
    ]
    return pd.DataFrame(items, columns=["item", "value"])


# ── Limit-up / broken / limit-down pools ──

_POOL_COL_MAP = {
    "code": "代码",
    "name": "名称",
    "change_pct": "涨跌幅",
    "turnover_rate": "换手率",
    "seal_amount_yi": "封板资金",   # note: stored in original yuan; /1e8 on read
    "first_seal_time": "首次封板时间",
    "break_count": "炸板次数",
    "consecutive_limits": "连板数",
    "sector": "所属行业",
}


def save_limit_pool(df: pd.DataFrame, pool_type: str,
                    captured_at: str | None = None) -> int:
    """Persist a limit-up/limit-down/broken pool snapshot.
    pool_type: 'up' | 'down' | 'broken'."""
    if df is None or df.empty:
        return 0
    captured_at = captured_at or now_captured_at()

    def _f(row, key, default=0.0):
        try:
            return float(row.get(_POOL_COL_MAP[key], default) or default)
        except Exception:
            return default

    def _i(row, key, default=0):
        try:
            return int(row.get(_POOL_COL_MAP[key], default) or default)
        except Exception:
            return default

    def _s(row, key):
        return str(row.get(_POOL_COL_MAP[key], "") or "")

    rows = []
    for _, r in df.iterrows():
        code = _s(r, "code").strip()
        if not code:
            continue
        rows.append((
            captured_at, code, pool_type,
            _s(r, "name"), _f(r, "change_pct"), _f(r, "turnover_rate"),
            round(_f(r, "seal_amount_yi") / 1e8, 2),
            _s(r, "first_seal_time"), _i(r, "break_count"),
            _i(r, "consecutive_limits"), _s(r, "sector"),
        ))
    if not rows:
        return 0
    with _write_lock:
        _get_conn().executemany(
            "INSERT OR REPLACE INTO limit_pool_snapshots "
            "(captured_at, code, pool_type, name, change_pct, turnover_rate, "
            " seal_amount_yi, first_seal_time, break_count, consecutive_limits, "
            " sector) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


def read_limit_pool(pool_type: str, as_of: str) -> pd.DataFrame | None:
    """Reshape to akshare column names so callers don't change."""
    if len(as_of) == 10:
        as_of = as_of + " 23:59"
    conn = _get_conn()
    row = conn.execute(
        "SELECT captured_at FROM limit_pool_snapshots "
        "WHERE pool_type = ? AND captured_at <= ? "
        "ORDER BY captured_at DESC LIMIT 1",
        (pool_type, as_of),
    ).fetchone()
    if not row:
        return None
    latest = row["captured_at"]

    rows = conn.execute(
        "SELECT code, name, change_pct, turnover_rate, seal_amount_yi, "
        "       first_seal_time, break_count, consecutive_limits, sector "
        "FROM limit_pool_snapshots "
        "WHERE pool_type = ? AND captured_at = ?",
        (pool_type, latest),
    ).fetchall()
    if not rows:
        return None
    return pd.DataFrame([{
        "代码": r["code"],
        "名称": r["name"],
        "涨跌幅": r["change_pct"],
        "换手率": r["turnover_rate"],
        # Writers converted 封板资金 → 亿; restore to yuan to match live shape
        "封板资金": (r["seal_amount_yi"] or 0) * 1e8,
        "首次封板时间": r["first_seal_time"] or "",
        "炸板次数": r["break_count"] or 0,
        "连板数": r["consecutive_limits"] or 0,
        "所属行业": r["sector"] or "",
    } for r in rows])


# ── Realtime quotes (per-stock) ──

def save_realtime_quotes(quotes: dict, captured_at: str | None = None) -> int:
    """Persist a batch of realtime quotes (dict keyed by code)."""
    if not quotes:
        return 0
    captured_at = captured_at or now_captured_at()
    rows = []
    for code, q in quotes.items():
        if not q.get("price"):
            continue
        rows.append((
            captured_at, code, q.get("name", ""),
            float(q.get("price", 0) or 0),
            float(q.get("change_pct", 0) or 0),
            int(q.get("volume", 0) or 0),
            float(q.get("amount_yi", 0) or 0),
            float(q.get("high", 0) or 0),
            float(q.get("low", 0) or 0),
            float(q.get("open", 0) or 0),
            float(q.get("prev_close", 0) or 0),
        ))
    if not rows:
        return 0
    with _write_lock:
        _get_conn().executemany(
            "INSERT OR REPLACE INTO realtime_quote_snapshots "
            "(captured_at, code, name, price, change_pct, volume, amount_yi, "
            " high, low, open, prev_close) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


def read_realtime_quotes(codes: list[str], as_of: str,
                          require_complete: bool = False) -> dict | None:
    """Read latest quote per code <= as_of.

    Returns dict of found codes (may be partial), or None if:
      - no codes found, OR
      - ``require_complete=True`` and not all ``codes`` were found.

    Callers wanting strict completeness (replay fidelity) should pass
    ``require_complete=True`` so they fall through to lower-tier fallbacks
    (all_quote_snapshots, daily K-line) instead of acting on partial data.
    """
    if not codes:
        return None
    if len(as_of) == 10:
        as_of = as_of + " 23:59"
    conn = _get_conn()
    result = {}
    for code in codes:
        row = conn.execute(
            "SELECT * FROM realtime_quote_snapshots "
            "WHERE code = ? AND captured_at <= ? "
            "ORDER BY captured_at DESC LIMIT 1",
            (code, as_of),
        ).fetchone()
        if not row:
            continue
        result[code] = {
            "code": code, "name": row["name"],
            "price": row["price"], "change_pct": row["change_pct"],
            "volume": row["volume"], "amount_yi": row["amount_yi"],
            "high": row["high"], "low": row["low"],
            "open": row["open"], "prev_close": row["prev_close"],
            "volume_ratio": 0, "turnover_rate": 0,
            "date": row["captured_at"][:10],
        }
    if not result:
        return None
    if require_complete and len(result) < len(codes):
        return None
    return result


# ── Per-stock fund flow ──

def save_stock_fund_flow(code: str, payload: dict,
                         captured_at: str | None = None) -> bool:
    """Persist the output of get_stock_fund_flow_fn (already JSON-parsed) for one code."""
    if not payload or payload.get("error"):
        return False
    captured_at = captured_at or now_captured_at()
    data_records = payload.get("data", [])
    latest = data_records[-1] if data_records else {}

    with _write_lock:
        _get_conn().execute(
            "INSERT OR REPLACE INTO stock_fund_flow_snapshots "
            "(captured_at, code, consecutive_inflow_days, consecutive_outflow_days, "
            " trend, main_net_latest, main_pct_latest, data_full) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (captured_at, code,
             int(payload.get("consecutive_inflow_days", 0) or 0),
             int(payload.get("consecutive_outflow_days", 0) or 0),
             str(payload.get("trend", "") or ""),
             float(latest.get("main_net_flow_yi", 0) or 0),
             float(latest.get("main_net_pct", 0) or 0),
             json.dumps(payload, ensure_ascii=False)),
        )
    return True


def read_stock_fund_flow(code: str, as_of: str) -> dict | None:
    if len(as_of) == 10:
        as_of = as_of + " 23:59"
    row = _get_conn().execute(
        "SELECT data_full FROM stock_fund_flow_snapshots "
        "WHERE code = ? AND captured_at <= ? "
        "ORDER BY captured_at DESC LIMIT 1",
        (code, as_of),
    ).fetchone()
    if not row:
        return None
    return json.loads(row["data_full"])


# ── Global market ──

def save_global_market(payload: dict, captured_at: str | None = None) -> int:
    """Persist global overview JSON (us_indices + bond_yields).

    Flattens to per-indicator rows so queries like 'give me 美债10Y at T' work.
    """
    if not payload:
        return 0
    captured_at = captured_at or now_captured_at()
    rows = []
    for idx in payload.get("us_indices", []):
        rows.append((captured_at, "us_index", idx.get("name", ""),
                     float(idx.get("close", 0) or 0),
                     float(idx.get("change_pct", 0) or 0)))
    bonds = payload.get("bond_yields", {}) or {}
    if bonds.get("us_10y") is not None:
        rows.append((captured_at, "bond", "美债10Y",
                     float(bonds["us_10y"] or 0), 0.0))
    if bonds.get("cn_10y") is not None:
        rows.append((captured_at, "bond", "国债10Y",
                     float(bonds["cn_10y"] or 0), 0.0))
    if bonds.get("us_10y_2y_spread") is not None:
        rows.append((captured_at, "spread", "美债10-2Y",
                     float(bonds["us_10y_2y_spread"] or 0), 0.0))
    if bonds.get("cn_us_spread") is not None:
        rows.append((captured_at, "spread", "中美利差",
                     float(bonds["cn_us_spread"] or 0), 0.0))
    if not rows:
        return 0
    with _write_lock:
        _get_conn().executemany(
            "INSERT OR REPLACE INTO global_market_snapshots "
            "(captured_at, category, name, value, change_pct) "
            "VALUES (?,?,?,?,?)",
            rows,
        )
    return len(rows)


def read_global_market(as_of: str) -> dict | None:
    """Reassemble the shape of get_global_overview_fn's output."""
    if len(as_of) == 10:
        as_of = as_of + " 23:59"
    conn = _get_conn()
    row = conn.execute(
        "SELECT MAX(captured_at) AS t FROM global_market_snapshots "
        "WHERE captured_at <= ?", (as_of,),
    ).fetchone()
    if not row or not row["t"]:
        return None
    latest = row["t"]
    rows = conn.execute(
        "SELECT category, name, value, change_pct FROM global_market_snapshots "
        "WHERE captured_at = ?", (latest,),
    ).fetchall()
    us_indices = []
    bond_yields = {"us_10y": None, "cn_10y": None,
                   "us_10y_2y_spread": None, "cn_us_spread": None}
    for r in rows:
        if r["category"] == "us_index":
            us_indices.append({"name": r["name"], "close": r["value"],
                               "change_pct": r["change_pct"]})
        elif r["category"] == "bond":
            if r["name"] == "美债10Y":
                bond_yields["us_10y"] = r["value"]
            elif r["name"] == "国债10Y":
                bond_yields["cn_10y"] = r["value"]
        elif r["category"] == "spread":
            if r["name"] == "美债10-2Y":
                bond_yields["us_10y_2y_spread"] = r["value"]
            elif r["name"] == "中美利差":
                bond_yields["cn_us_spread"] = r["value"]
    return {"us_indices": us_indices, "bond_yields": bond_yields,
            "signals": [], "signal_count": 0}


# ── Full-market spot quotes ──

# akshare stock_zh_a_spot_em() column map
_AQ_COL_MAP = {
    "code": "代码",
    "name": "名称",
    "price": "最新价",
    "change_pct": "涨跌幅",
    "volume": "成交量",         # 手
    "amount": "成交额",         # 元
    "high": "最高",
    "low": "最低",
    "open": "今开",
    "prev_close": "昨收",
    "turnover_rate": "换手率",
    "volume_ratio": "量比",
    "amplitude": "振幅",
    "pe": "市盈率-动态",
    "pb": "市净率",
    "market_cap": "总市值",       # 元
    "float_cap": "流通市值",      # 元
}


def _tencent_symbol(code: str) -> str:
    """Convert bare 6-digit code to Tencent qt.gtimg.cn symbol."""
    if code.startswith("6"):
        return f"sh{code}"
    if code.startswith(("0", "3")):
        return f"sz{code}"
    if code.startswith(("8", "4")):
        return f"bj{code}"
    return f"sz{code}"


def _parse_tencent_line(line: str) -> dict | None:
    """Parse one Tencent qt.gtimg.cn response line: v_sh600519="1~name~code~...";

    Returns dict matching the akshare stock_zh_a_spot_em column schema so
    downstream save_all_quotes() accepts it unchanged.
    """
    if '="' not in line:
        return None
    try:
        _var, payload = line.split("=", 1)
        payload = payload.strip().strip('"').strip(";").strip('"')
        if not payload:
            return None
        f = payload.split("~")
        if len(f) < 47 or not f[2]:
            return None
        return {
            "代码": f[2],
            "名称": f[1],
            "最新价": float(f[3]) if f[3] else None,
            "涨跌幅": float(f[32]) if len(f) > 32 and f[32] else None,
            "成交量": int(float(f[36]) * 100) if len(f) > 36 and f[36] else None,  # 手 → 股
            "成交额": float(f[37]) * 10000 if len(f) > 37 and f[37] else None,      # 万元 → 元
            "最高": float(f[33]) if len(f) > 33 and f[33] else None,
            "最低": float(f[34]) if len(f) > 34 and f[34] else None,
            "今开": float(f[5]) if f[5] else None,
            "昨收": float(f[4]) if f[4] else None,
            "换手率": float(f[38]) if len(f) > 38 and f[38] else None,
            "量比": None,   # Tencent doesn't expose 量比 directly
            "振幅": float(f[43]) if len(f) > 43 and f[43] else None,
            "市盈率-动态": float(f[39]) if len(f) > 39 and f[39] else None,
            "市净率": float(f[46]) if len(f) > 46 and f[46] else None,
            "总市值": float(f[45]) * 1e8 if len(f) > 45 and f[45] else None,   # 亿 → 元
            "流通市值": float(f[44]) * 1e8 if len(f) > 44 and f[44] else None,
        }
    except (ValueError, IndexError):
        return None


def capture_market_snapshot(codes: list[str] | None = None) -> int:
    """One-shot full-market capture. Returns rows written (0 on failure).

    Intended for intraday_monitor to call at the top of each cycle — fast
    (~10s), resilient (Tencent source), and captures to ``all_quote_snapshots``
    for per-minute replay fidelity across ALL A-shares.

    Never raises — failure returns 0 and is logged at debug level. Safe to
    wire into the hot path of intraday_monitor.
    """
    try:
        if codes is None:
            import sqlite3
            from alpha_agents.config import DATA_DIR
            conn = sqlite3.connect(DATA_DIR / "stocks.db")
            codes = [r[0] for r in conn.execute("SELECT code FROM stocks").fetchall()]
            conn.close()
        if not codes:
            return 0
        df = fetch_all_quotes_tencent(codes)
        if df is None or df.empty:
            return 0
        return save_all_quotes(df)
    except Exception as e:
        logger.debug("capture_market_snapshot failed: %s", e)
        return 0


def fetch_all_quotes_tencent(codes: list[str], batch_size: int = 200,
                              timeout: int = 10) -> pd.DataFrame:
    """Fetch whole-market spot via Tencent qt.gtimg.cn in batches.

    Robust against push2.eastmoney.com blocks. Returns a DataFrame shaped like
    akshare's stock_zh_a_spot_em output (so save_all_quotes accepts it).
    """
    import requests
    if not codes:
        return pd.DataFrame()

    symbols = [_tencent_symbol(c) for c in codes]
    batches = [symbols[i:i + batch_size] for i in range(0, len(symbols), batch_size)]

    rows = []
    from alpha_agents.config import no_proxy
    with no_proxy():
        for b in batches:
            url = "https://qt.gtimg.cn/q=" + ",".join(b)
            try:
                r = requests.get(url, timeout=timeout)
                if r.status_code != 200:
                    continue
                for line in r.text.splitlines():
                    rec = _parse_tencent_line(line)
                    if rec:
                        rows.append(rec)
            except Exception as e:
                logger.debug("Tencent batch failed: %s", e)
                continue
    return pd.DataFrame(rows)


def save_all_quotes(df: pd.DataFrame, captured_at: str | None = None) -> int:
    """Persist a whole-market spot snapshot. Returns rows written."""
    if df is None or df.empty:
        return 0
    captured_at = captured_at or now_captured_at()

    def _f(row, key):
        try:
            v = row.get(_AQ_COL_MAP[key])
            return float(v) if v is not None and v == v else None
        except Exception:
            return None

    def _i(row, key):
        try:
            v = row.get(_AQ_COL_MAP[key])
            return int(v) if v is not None and v == v else None
        except Exception:
            return None

    def _s(row, key):
        v = row.get(_AQ_COL_MAP[key])
        return str(v) if v is not None else ""

    rows = []
    for _, r in df.iterrows():
        code = _s(r, "code").strip()
        if not code:
            continue
        amt = _f(r, "amount")
        mc = _f(r, "market_cap")
        fc = _f(r, "float_cap")
        rows.append((
            captured_at, code, _s(r, "name"),
            _f(r, "price"), _f(r, "change_pct"),
            _i(r, "volume"),
            round(amt / 1e8, 4) if amt else None,
            _f(r, "high"), _f(r, "low"), _f(r, "open"), _f(r, "prev_close"),
            _f(r, "turnover_rate"), _f(r, "volume_ratio"),
            _f(r, "amplitude"), _f(r, "pe"), _f(r, "pb"),
            round(mc / 1e8, 2) if mc else None,
            round(fc / 1e8, 2) if fc else None,
        ))
    if not rows:
        return 0
    with _write_lock:
        _get_conn().executemany(
            "INSERT OR REPLACE INTO all_quote_snapshots "
            "(captured_at, code, name, price, change_pct, volume, amount_yi, "
            " high, low, open, prev_close, turnover_rate, volume_ratio, "
            " amplitude, pe, pb, market_cap_yi, float_cap_yi) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


def read_all_quotes(codes: list[str] | None, as_of: str,
                     require_complete: bool = False) -> dict | None:
    """Read whole-market quote at as_of. If codes=None, returns full snapshot.

    ``require_complete`` mirrors read_realtime_quotes: when True, returns None
    unless every requested code was found in the same captured_at snapshot.
    """
    if len(as_of) == 10:
        as_of = as_of + " 23:59"
    conn = _get_conn()
    # Find latest captured_at <= as_of
    row = conn.execute(
        "SELECT MAX(captured_at) AS t FROM all_quote_snapshots "
        "WHERE captured_at <= ?",
        (as_of,),
    ).fetchone()
    if not row or not row["t"]:
        return None
    latest = row["t"]

    if codes:
        placeholders = ",".join("?" * len(codes))
        params = [latest] + list(codes)
        rows = conn.execute(
            f"SELECT * FROM all_quote_snapshots "
            f"WHERE captured_at = ? AND code IN ({placeholders})",
            params,
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM all_quote_snapshots WHERE captured_at = ?",
            (latest,),
        ).fetchall()
    if not rows:
        return None
    result = {r["code"]: {
        "code": r["code"], "name": r["name"],
        "price": r["price"], "change_pct": r["change_pct"],
        "volume": r["volume"], "amount_yi": r["amount_yi"],
        "high": r["high"], "low": r["low"],
        "open": r["open"], "prev_close": r["prev_close"],
        "turnover_rate": r["turnover_rate"] or 0,
        "volume_ratio": r["volume_ratio"] or 0,
        "date": latest[:10],
    } for r in rows}
    if require_complete and codes and len(result) < len(codes):
        return None
    return result


# ── News items (cross-source, deduplicated) ──

import hashlib


def _news_hash(source: str, title: str, published_at: str) -> str:
    key = f"{source}|{title}|{published_at}".encode("utf-8")
    return hashlib.md5(key).hexdigest()


_TS_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
    "%a, %d %b %Y %H:%M:%S %z",   # RFC 2822 — RSS feeds
    "%a, %d %b %Y %H:%M:%S",
    "%a, %d %b %Y",
    "%a %b %d %H:%M:%S %z %Y",    # Twitter/X
    "%a %b %d %H:%M",             # X syndication — no year
)


def normalise_published_at(raw: str) -> str:
    """Coerce a source's timestamp into 'YYYY-MM-DD HH:MM:SS'.

    Sources disagree wildly — RSS sends RFC 2822 ('Mon, 07 Sep 2026 …'),
    PBOC sends a bare date, X syndication omits the year entirely. Stored
    verbatim these do not order, and since/as_of window queries compare
    ``published_at`` as a *string*, so a mixed table silently returns the
    wrong rows. Normalising at write time is what makes the window read
    meaningful.

    Returns '' when nothing parses, and the caller drops the row rather
    than poisoning the ordering with an unparseable value.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""

    from datetime import datetime as _dt
    for fmt in _TS_FORMATS:
        try:
            parsed = _dt.strptime(raw, fmt)
        except ValueError:
            continue
        # Formats without a year default to 1900; assume the current one.
        if parsed.year == 1900:
            parsed = parsed.replace(year=_dt.now().year)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    return ""


def save_news(source: str, items: list[dict],
              captured_at: str | None = None) -> int:
    """Persist news items. Input format: [{title, summary, time, url?}, ...].

    Dedup by md5(source|title|published_at). ``time`` is normalised to
    'YYYY-MM-DD HH:MM:SS' first — see normalise_published_at.
    """
    if not items:
        return 0
    captured_at = captured_at or now_captured_at()
    rows = []
    for item in items:
        title = (item.get("title") or "").strip()
        published = normalise_published_at(item.get("time") or "")
        if not title or not published:
            continue
        h = _news_hash(source, title, published)
        rows.append((source, published, title,
                     (item.get("summary") or "")[:500],
                     item.get("url", "") or "", h, captured_at))
    if not rows:
        return 0
    with _write_lock:
        n_before = _get_conn().execute(
            "SELECT COUNT(*) FROM news_items WHERE source = ?", (source,),
        ).fetchone()[0]
        _get_conn().executemany(
            "INSERT OR IGNORE INTO news_items "
            "(source, published_at, title, summary, url, hash, captured_at) "
            "VALUES (?,?,?,?,?,?,?)",
            rows,
        )
        n_after = _get_conn().execute(
            "SELECT COUNT(*) FROM news_items WHERE source = ?", (source,),
        ).fetchone()[0]
    return n_after - n_before


def read_news(sources: list[str] | None, as_of: str,
              keyword: str | None = None, limit: int = 50,
              source_prefix: str | None = None,
              since: str | None = None) -> list[dict]:
    """Read news items published <= as_of, optionally >= since.

    ``since`` turns this into a window read, which is what the flash
    sources need: they are continuous streams, so a task wants
    "everything published between the last run and now", not "the latest
    N items" — the latter silently drops whatever arrived beyond N and
    re-reads what it already saw.

    Source filter is either an exact list (``sources``) or a prefix
    pattern (``source_prefix``, e.g. 'X/@') for dynamic source labels
    like truthsocial handles.
    """
    if len(as_of) == 10:
        as_of = as_of + " 23:59:59"
    q = ["SELECT source, published_at, title, summary, url FROM news_items "
         "WHERE published_at <= ?"]
    params: list = [as_of]
    if since:
        if len(since) == 10:
            since = since + " 00:00:00"
        q.append("AND published_at >= ?")
        params.append(since)
    if sources:
        placeholders = ",".join("?" * len(sources))
        q.append(f"AND source IN ({placeholders})")
        params.extend(sources)
    elif source_prefix:
        q.append("AND source LIKE ?")
        params.append(f"{source_prefix}%")
    if keyword:
        q.append("AND (title LIKE ? OR summary LIKE ?)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    q.append("ORDER BY published_at DESC LIMIT ?")
    params.append(limit)
    rows = _get_conn().execute(" ".join(q), params).fetchall()
    return [{
        "source": r["source"], "time": r["published_at"],
        "title": r["title"], "summary": r["summary"] or "",
        "url": r["url"] or "",
    } for r in rows]


def read_latest_news(limit: int = 100,
                     sources: list[str] | None = None) -> list[dict]:
    """Newest flashes across all sources — the live 7x24 feed.

    Deliberately not ``read_news(as_of=now)``: an as_of cut compares the
    source's own published_at against this box's clock, so a flash stamped
    a minute ahead of us would vanish from a feed whose whole job is to
    show what just arrived. The dashboard is live, never replayed, so
    there is nothing to cut against.
    """
    q = ["SELECT source, published_at, title, summary, url FROM news_items"]
    params: list = []
    if sources:
        q.append("WHERE source IN (%s)" % ",".join("?" * len(sources)))
        params.extend(sources)
    q.append("ORDER BY published_at DESC LIMIT ?")
    params.append(limit)
    rows = _get_conn().execute(" ".join(q), params).fetchall()
    return [{
        "source": r["source"], "time": r["published_at"],
        "title": r["title"], "summary": r["summary"] or "",
        "url": r["url"] or "",
    } for r in rows]


def replay_news_response(sources: list[str] | None, limit: int,
                          keyword: str | None = None,
                          source_prefix: str | None = None) -> str | None:
    """Helper for news source ``get_*_fn`` functions: if replay mode is
    active, return a JSON string of snapshot matches; otherwise None so the
    caller can fetch live.
    """
    try:
        from alpha_agents.evolution.replay_mode import get_replay_as_of
        as_of = get_replay_as_of()
    except Exception:
        return None
    if not as_of:
        return None
    items = read_news(sources=sources, as_of=as_of, keyword=keyword,
                      limit=limit, source_prefix=source_prefix)
    return json.dumps({"news": items, "count": len(items)},
                      ensure_ascii=False)


# ── Web search (query → results, per-capture) ──

def save_web_search(query: str, result_json: str,
                    captured_at: str | None = None) -> bool:
    """Persist a web_search result keyed by exact query."""
    if not query or not result_json:
        return False
    captured_at = captured_at or now_captured_at()
    with _write_lock:
        _get_conn().execute(
            "INSERT OR REPLACE INTO web_search_snapshots "
            "(captured_at, query, result_json) VALUES (?,?,?)",
            (captured_at, query, result_json),
        )
    return True


def read_web_search(query: str, as_of: str) -> str | None:
    """Return the latest captured result_json for ``query`` at or before
    ``as_of``. None if nothing captured — caller should return empty + warn."""
    if len(as_of) == 10:
        as_of = as_of + " 23:59"
    row = _get_conn().execute(
        "SELECT result_json FROM web_search_snapshots "
        "WHERE query = ? AND captured_at <= ? "
        "ORDER BY captured_at DESC LIMIT 1",
        (query, as_of),
    ).fetchone()
    return row["result_json"] if row else None


# ── Convenience ──

def latest_snapshot_time(data_type: str, scope: str | None = None) -> str | None:
    conn = _get_conn()
    if data_type == "sector_flow":
        row = conn.execute(
            "SELECT MAX(captured_at) AS t FROM sector_flow_snapshots "
            "WHERE scope = ?", (scope,),
        ).fetchone()
        return row["t"] if row else None
    if data_type == "market_breadth":
        row = conn.execute(
            "SELECT MAX(captured_at) AS t FROM market_breadth_snapshots"
        ).fetchone()
        return row["t"] if row else None
    return None
