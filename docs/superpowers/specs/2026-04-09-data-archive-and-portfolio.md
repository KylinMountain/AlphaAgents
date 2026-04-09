# AlphaAgents: 每日数据归档 + 虚拟持仓系统

## 背景

当前系统能发现异动、推荐标的、给出操作建议，但缺少两个关键能力：
1. **数据归档** — 资金行为数据（龙虎榜、北向、主力资金流等）是核心信号源，但只有当天快照，无法回测。需要从今天开始每日归档，为未来因子回测积累数据。
2. **执行闭环** — 推荐完就结束了，不跟踪后续表现。需要虚拟持仓系统，实现"推荐 → 建仓 → 盘中跟踪 → 止损/止盈 → 归因"的完整闭环。

## 模块一：每日数据归档

### 目的

每个交易日收盘后，自动存一份当日市场数据快照到本地数据库，为未来的因子回测和信号有效性验证积累原始数据。

### 数据表

```sql
CREATE TABLE IF NOT EXISTS daily_snapshots (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,           -- 交易日 YYYY-MM-DD
    data_type TEXT NOT NULL,      -- 数据类型标识
    data TEXT NOT NULL,           -- JSON blob
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(date, data_type)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_date ON daily_snapshots(date);
```

### 归档的数据类型

| data_type | 数据来源函数 | 说明 |
|-----------|-------------|------|
| `concept_fund_flow` | `get_concept_ranking_fn(50)` | 概念板块资金流排名（Top 50） |
| `industry_fund_flow` | `get_sector_ranking_fn(30)` | 行业板块资金流排名（Top 30） |
| `limit_up_pool` | `get_anomaly_stocks_fn()` | 涨停池 / 炸板池 / 连板股 |
| `market_breadth` | `get_market_breadth_fn()` | 市场涨跌比 / 情绪 |
| `lhb` | `get_lhb_detail_fn()` | 龙虎榜明细 |
| `block_trade` | `get_block_trade_fn()` | 大宗交易 |
| `north_flow` | `get_north_flow_fn("today")` | 北向资金持仓 Top 30 |
| `margin` | `get_margin_data_fn()` | 融资融券余额 Top 20 |
| `theme_fund_flow` | 自定义 | 活跃主线标的的个股资金流（遍历主线核心股调用 `get_stock_fund_flow_fn`） |

### 触发时机

在复盘任务（15:30）中，Agent 分析完成后，额外执行一步数据归档。归档逻辑是纯函数调用，不需要 LLM 参与。

### 实现位置

- 新文件：`alpha_agents/data/daily_archive.py` — 归档逻辑
- 修改：`alpha_agents/pipeline/tasks/review.py` — 复盘结束后调用归档
- 修改：`alpha_agents/data/db.py` 或 `memory_store.py` — 新增 `daily_snapshots` 表和读写函数

---

## 模块二：虚拟持仓系统

### 目的

模拟真实持仓跟踪，让每条推荐从产生到平仓有完整的生命周期记录。纯模拟，不涉及实际交易。为后续策略评估和因子回测提供闭环数据。

### 数据表

```sql
CREATE TABLE IF NOT EXISTS virtual_portfolio (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL,              -- 股票代码
    name TEXT,                       -- 股票名称
    theme TEXT,                      -- 关联主线
    open_date TEXT NOT NULL,         -- 建仓日（推荐日）
    open_price REAL NOT NULL,        -- 建仓价（推荐时实时价格）
    stop_loss REAL,                  -- 止损价
    target_price REAL,               -- 止盈目标价（可选）
    status TEXT DEFAULT 'open',      -- open / stopped / target_hit / expired / manual_close
    close_date TEXT,                 -- 平仓日
    close_price REAL,                -- 平仓价
    holding_days INTEGER DEFAULT 0,  -- 持仓交易日天数
    return_pct REAL,                 -- 最终收益率 %
    peak_return_pct REAL DEFAULT 0,  -- 持仓期内最高收益率 %
    max_drawdown_pct REAL DEFAULT 0, -- 持仓期内最大回撤 %（从峰值回落）
    source TEXT,                     -- morning / intraday
    reason TEXT,                     -- 建仓理由
    close_reason TEXT,               -- 平仓原因
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_portfolio_status ON virtual_portfolio(status);
CREATE INDEX IF NOT EXISTS idx_portfolio_date ON virtual_portfolio(open_date);
```

### 持仓状态流转

```
open → stopped       (触发止损)
open → target_hit    (触发止盈)
open → expired       (主线衰退 / 超过最大持仓天数)
open → manual_close  (预留：未来用户手动平仓)
```

### 生命周期

#### 1. 建仓（推荐产生时）

触发时机：晨扫或盘中监控产生 `actionable` 类型推荐时。

逻辑：
- 从推荐的 `entry_price` 作为 `open_price`
- 从推荐的 `action` 字段解析 `stop_loss`（如"止损 50 元"→ 50.0）
- 如果 `institutional_position` 工具返回了 `stop_loss`，优先用工具计算的值
- `target_price` 可选，如果 Agent 给了就存
- `source` = "morning" 或 "intraday"
- 推送通知："已建仓 XXX @ XX 元，止损 XX 元"
- 同一只股票 status=open 时不重复建仓

#### 2. 盘中跟踪（每次盘中监控时）

触发时机：盘中监控的每个周期（默认 5 分钟），在异动检测之外，独立执行持仓检查。

逻辑：
- 查询所有 `status='open'` 且 `open_date < 今天`（T+1 约束：建仓日不检查止损止盈）
- 批量获取这些股票的实时价格（新浪 API）
- 对每只持仓股：
  - 计算当前收益率：`(实时价 - open_price) / open_price * 100`
  - 更新 `peak_return_pct` = max(当前值, 当前收益率)
  - 更新 `max_drawdown_pct` = 从 peak 到当前的回撤
  - 更新 `holding_days`（交易日计算）
  - 检查触发条件（按优先级）：
    1. `实时价 <= stop_loss` → 止损触发
    2. `target_price` 存在且 `实时价 >= target_price` → 止盈触发
    3. 关联主线 status == "declining" 或 "archived" → 主线衰退
    4. `holding_days >= 5`（默认最大持仓 5 个交易日）→ 超时过期
- 触发平仓时：
  - 更新 `status`、`close_date`、`close_price`、`return_pct`、`close_reason`
  - 推送通知，内容区分：
    - 止损："止损触发 | XXX 跌至 XX 元（亏损 X%），已模拟平仓"
    - 止盈："止盈触发 | XXX 涨至 XX 元（盈利 X%），已模拟平仓"
    - 主线衰退："主线衰退 | XXX 关联主线已降级，模拟平仓（收益 X%）"
    - 超时："持仓到期 | XXX 持仓 5 天，模拟平仓（收益 X%）"

#### 3. 复盘汇总（15:30）

在复盘 Agent 的上下文中加入当日持仓变动：
- 今日新建仓 N 笔
- 今日平仓 N 笔（止损 X 笔、止盈 X 笔、过期 X 笔）
- 当前持仓 N 笔，整体浮盈/浮亏

复盘 Agent 可以据此分析"为什么这笔止损了"、"这个主线的推荐整体表现如何"。

#### 4. 周报归因

周报新增"策略复盘"板块，统计：

```
=== 本周策略表现 ===
建仓: X 笔 | 平仓: Y 笔 | 当前持仓: Z 笔
胜率: XX%（止盈+正收益过期 / 总平仓）
平均收益: X% | 最大单笔盈利: +X% | 最大单笔亏损: -X%
平均持仓天数: X 天

按主线归因:
• 芯片概念: 3 笔, 胜率 67%, 平均收益 +2.3%
• 华为概念: 2 笔, 胜率 50%, 平均收益 -0.5%

按信号类型归因:
• 资金流入+北向增持: 4 笔, 胜率 75%
• 纯板块联动: 3 笔, 胜率 33%

累计（自系统启动）:
总交易: XX 笔 | 累计胜率: XX% | 累计收益: XX%
```

### T+1 约束总结

| 时间 | 行为 |
|------|------|
| T 日（建仓日） | 只推送"已建仓"通知，不检查止损/止盈 |
| T+1 日起 | 每个盘中周期检查止损/止盈/主线衰退/超时 |
| 平仓时 | close_price = 触发时的实时价格（模拟盘中卖出） |

### 最大持仓天数

默认 5 个交易日。超过后自动平仓。理由：
- 系统定位是短线趋势跟踪，不是长线持有
- 避免僵尸持仓占用跟踪资源
- 可以按主线调整（强主线可以延长到 10 天）

---

## 实现文件清单

### 新文件

| 文件 | 说明 |
|------|------|
| `alpha_agents/data/daily_archive.py` | 每日数据归档逻辑 |
| `alpha_agents/data/portfolio.py` | 虚拟持仓 CRUD + 状态检查 + 统计 |

### 修改文件

| 文件 | 改动 |
|------|------|
| `alpha_agents/data/memory_store.py` | 新增 `daily_snapshots` 和 `virtual_portfolio` 表的 schema |
| `alpha_agents/pipeline/tasks/review.py` | 复盘结束后调用数据归档 + 持仓汇总 |
| `alpha_agents/pipeline/tasks/intraday_monitor.py` | 盘中监控增加持仓检查步骤 |
| `alpha_agents/pipeline/tasks/morning_scan.py` | 推荐保存后自动建仓 |
| `alpha_agents/pipeline/tasks/weekly_report.py` | 周报增加策略复盘板块 |
| `alpha_agents/prompts/review.md` | 复盘 prompt 增加持仓变动上下文 |
| `alpha_agents/prompts/weekly_report.md` | 周报 prompt 增加策略复盘格式 |
| `alpha_agents/notify.py` | 可能需要新增止损/止盈通知模板 |

---

## 不做的事

- 不做实际交易对接（未来扩展）
- 不做仓位管理（单票仓位比例、总仓位控制 — 未来 B 方案）
- 不做做 T（T+0 日内交易 — 需要用户实际底仓信息）
- 不做历史回测引擎（等数据积累 2-3 个月后再做）
- 回测相关的因子统计模块（依赖归档数据积累）
