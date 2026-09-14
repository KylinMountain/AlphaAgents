"""The market snapshot and news tables.

A declaration, moved out of ``snapshot_store.py`` for the same reason
``memory_schema.py`` was moved out of ``memory_store.py``: 257 lines of DDL sat in
a file that also carries thirty readers and writers, and the two are edited for
different reasons.

``snapshot_store`` re-exports ``_SCHEMA`` so existing importers keep working, and
the name cannot drift — it is the same string object.
"""

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
