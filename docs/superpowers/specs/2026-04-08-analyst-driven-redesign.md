# AlphaAgents 2.0: Analyst-Driven Redesign Spec

## Vision

从"新闻驱动的推荐系统"重设计为"模拟顶级量化分析师工作日的自主分析系统"。核心转变：不再是 `新闻 → 推测影响 → 推荐`，而是 `观察市场 → 发现异动 → 追溯原因 → 交叉验证 → 建立信心 → 推荐`。

## 设计原则

1. **模拟人类分析师**，不是构建数据管道。思维链路比数据完整性更重要。
2. **资金行为优先于新闻叙事**。看"谁在买卖"比看"发生了什么"更可靠。
3. **持续记忆积累经验**。系统应该越用越准，不是每次从零开始。
4. **不做散户技术分析**（MACD/KDJ/画线），做机构思维（资金流、相对强弱、因子分析）。

---

## 模块一：工作日调度器（Scheduler）

系统按 A 股交易日节奏运行不同任务，非交易日只运行外盘监控。

### 任务时间表

| 时间 | 任务 | 模式 | 输出 |
|------|------|------|------|
| 06:30 | 晨扫 | 预判 | 晨报 |
| 09:15 | 开盘前 | 预判验证 | 开盘提醒 |
| 09:30-15:00 | 盘中监控 | 异动追因 | 盘中提醒（仅异动时） |
| 15:30 | 复盘 | 主线评估 | 复盘报告 |
| 20:00 | 夜扫 | 预判 | 夜报（有重大变化时） |
| 周末 | 周报 | 学习总结 | 周报 |

### 调度实现

- 基于 `asyncio` 的定时调度器，替代现有的单一 `NewsMonitor` 循环
- 每个任务是一个独立的 async 函数，调度器按时间触发
- 盘中监控以 15 分钟为最小周期轮询，异动时缩短到 5 分钟
- 非交易日（周末、节假日）跳过盘中任务，只在 06:30 和 20:00 运行外盘扫描
- 交易日判断：使用 akshare 的交易日历 `ak.tool_trade_date_hist_sina()`

### 与现有系统的关系

- 现有 `NewsMonitor` 的新闻抓取能力保留，作为晨扫/夜扫的数据输入之一
- 现有 `digest_news()` 保留用于新闻聚合，但不再是唯一的分析触发器
- 现有 `route_and_analyze()` 重写为多种分析模式

---

## 模块二：分析师思维引擎（多模式 Agent）

替代现有的单一策略师 Agent，设计四种思维模式，不同场景自动切换。

### 模式 1：异动追因（盘中主要模式）

触发条件：板块或主线标的出现异动（涨跌幅/资金流/量比超阈值）

思维链路：
1. **观察**：检测到什么异动？（板块突然放量、龙头涨停、资金异常流入）
2. **追因**：为什么？查新闻催化、查资金来源（北向/机构/游资）、查关联板块联动
3. **判断**：一日游还是持续行情？根据资金类型（机构 vs 游资）、催化力度、板块联动度
4. **决策**：是否纳入/升级主线？推荐什么标的？
5. **交叉验证**：通过验证清单确认信心水平（见下方）

异动阈值：
- 板块：15分钟涨幅 > 1.5%，或资金净流入 > 板块均值 2 倍
- 个股（主线标的）：涨停、跌停、量比 > 3

### 模式 2：预判验证（晨扫 → 开盘）

思维链路：
1. **预判**：根据隔夜外盘 + 新闻 + 主线状态，预判今日哪些方向可能有动作
2. **验证**：09:15 集合竞价结果是否与预判一致
3. **调整**：预期差分析——市场不买账说明什么？调整今日关注方向

### 模式 3：主线评估（复盘模式）

思维链路：
1. **回顾**：今日推荐表现、主线标的涨跌、龙虎榜/大宗交易/融资融券数据
2. **归因**：推对了为什么对，推错了为什么错
3. **学习**：更新市场认知表、调整主线强度
4. **决策**：主线升级/降级/新增/退出

### 模式 4：交叉验证（推荐前必过关卡）

所有推荐在输出前必须通过 4 维验证：

| 维度 | 验证内容 | 工具 |
|------|---------|------|
| 资金面 | 板块资金流向 + 龙虎榜机构动向 + 融资余额 | get_sector_data, get_lhb, get_margin |
| 基本面 | ROE/负债率/业绩预告 | get_financial_data, get_earnings_calendar |
| 位置面 | 当前价格相对位置、是否追高 | get_stock_quotes, 市场认知表 |
| 情绪面 | 大盘涨跌比、市场风险偏好 | get_market_breadth |

信心评级：
- 4 项中 3+ 通过 → 高信心推荐
- 2 项通过 → 中信心推荐（标注风险）
- 1 项或更少 → 仅列入观察，不推荐

### Agent 架构

不再是单一策略师 + 地缘子 Agent，而是：

```
调度器
├─ 晨扫 Agent（模式2：预判）
├─ 盘中 Agent（模式1：异动追因）
├─ 复盘 Agent（模式3：主线评估）
└─ 验证 Agent（模式4：交叉验证，被其他 Agent 调用）
```

每个 Agent 有自己的 prompt 和工具集，但共享记忆系统。

---

## 模块三：记忆系统

SQLite 持久化，三张核心表。

### 表 1：主线池（theme_lines）

```sql
CREATE TABLE theme_lines (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,              -- "AI算力"、"军工信息化"
    status TEXT DEFAULT 'watching',  -- watching/active/peak/declining/archived
    strength INTEGER DEFAULT 0,     -- 0-10
    created_at TEXT,
    updated_at TEXT,
    catalyst TEXT,                   -- 创建时的催化事件
    core_stocks TEXT,                -- JSON: [{code, name, role}] role=龙头/弹性/跟风
    leader_code TEXT,                -- 龙头股代码，用于判断主线健康度
    notes TEXT                       -- 分析师笔记（LLM 自由记录的观察）
);
```

主线状态流转：`watching → active → peak → declining → archived`

主线自动发现条件（满足 2+）：
- 板块连续 2-3 天资金净流入且跑赢大盘
- 出现政策/产业催化事件
- 龙头股放量突破
- 涨停板集中出现在某方向

主线自动降级条件（满足 2+）：
- 板块资金连续 3 天净流出
- 龙头股破关键位置（如5日均线）
- 涨停板从该方向消失
- 更强主线虹吸资金

同时活跃主线上限：8 条。满了淘汰 strength 最低的。

### 表 2：预测记录（predictions）

```sql
CREATE TABLE predictions (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,               -- 推荐日期
    report_type TEXT,                 -- morning/intraday/review
    code TEXT NOT NULL,               -- 股票代码
    name TEXT,
    direction TEXT,                   -- bullish/bearish
    confidence TEXT,                  -- high/medium/low
    theme_line TEXT,                  -- 关联主线
    entry_price REAL,                 -- 推荐时价格
    reason TEXT,                      -- 推荐理由
    -- 回测字段（复盘时填充）
    next_day_return REAL,            -- 次日收益率
    week_return REAL,                -- 周收益率
    hit INTEGER,                     -- 1=命中, 0=未命中, NULL=待验证
    review_note TEXT                 -- 复盘归因
);
```

### 表 3：市场认知（market_cognition）

```sql
CREATE TABLE market_cognition (
    id INTEGER PRIMARY KEY,
    sector TEXT NOT NULL,             -- 板块名称
    date TEXT NOT NULL,               -- 更新日期
    position TEXT,                    -- high/mid/low（板块相对位置）
    fund_trend TEXT,                  -- inflow/outflow/neutral（资金趋势）
    pe_percentile REAL,              -- PE分位数 0-100
    recent_events TEXT,              -- 最近的关键事件（JSON数组）
    assessment TEXT,                  -- LLM 对该板块的简短判断
    UNIQUE(sector, date)
);
```

### 记忆的使用方式

- **晨扫 Agent** 读取主线池 + 市场认知，生成预判
- **盘中 Agent** 读取主线池，只关注活跃主线标的的异动
- **复盘 Agent** 更新全部三张表
- **验证 Agent** 读取市场认知（判断位置），读取预测记录（参考历史命中率）

---

## 模块四：数据层

### 新增数据工具（按优先级）

**P0 — 资金行为（核心差异化数据）：**

| 工具 | akshare 函数 | 用途 |
|------|-------------|------|
| get_lhb_detail | stock_lhb_detail_em | 龙虎榜机构/游资席位明细 |
| get_margin_data | stock_margin_detail_szse | 融资融券余额变化 |
| get_north_flow | stock_hsgt_north_net_flow_in_em | 北向资金净流入（含个股明细） |
| get_block_trade | stock_dzjy_mrtj | 大宗交易折价率 |
| get_stock_fund_flow | stock_individual_fund_flow | 个股主力/散户资金流 |

**P1 — 相对强弱 & 量价：**

| 工具 | akshare 函数 | 用途 |
|------|-------------|------|
| get_sector_ranking | stock_fund_flow_concept + stock_fund_flow_industry | 板块强弱排名 |
| get_market_snapshot | stock_zh_a_spot_em（部分字段） | 量比、换手率异常检测 |

**P2 — 外盘 & 宏观：**

| 工具 | akshare 函数 | 用途 |
|------|-------------|------|
| get_us_market | stock_us_spot_em | 美股三大指数 |
| get_bond_yield | bond_zh_us_rate | 中美国债收益率 |

### 现有工具复用

| 工具 | 保留/修改 | 说明 |
|------|----------|------|
| 12 个新闻源 | 保留 | 晨扫/夜扫的输入 |
| get_sector_data | 保留+增强 | 加行业板块维度 |
| get_stock_quotes | 保留 | 个股行情验证 |
| get_financial_data | 保留 | 基本面验证 |
| get_earnings_calendar | 保留 | 业绩排雷 |
| get_market_breadth | 保留 | 市场情绪 |
| 期货工具（4个） | 保留 | 期货分析 |
| get_pizzint | 保留 | 地缘评估 |
| search_stocks | 保留 | 概念检索 |
| filter_stocks | 保留 | 个股过滤 |
| web_search / web_fetch | 保留 | 追因搜索 |

### 数据更新频率

| 数据 | 频率 | 触发 |
|------|------|------|
| 板块资金流 | 盘中每15分钟 | 调度器 |
| 个股资金流（主线标的） | 盘中每15分钟 | 调度器 |
| 龙虎榜/大宗交易/融资融券 | 盘后一次 | 复盘任务 |
| 北向资金 | 盘中实时 + 盘后明细 | 调度器 + 复盘 |
| 外盘/宏观 | 06:30 + 20:00 | 晨扫/夜扫 |
| 新闻源 | 06:30 + 20:00 + 事件触发 | 晨扫/夜扫 |

---

## 模块五：输出体系

### 报告类型

| 类型 | 时机 | 长度 | 特点 |
|------|------|------|------|
| 晨报 | 06:30 | 30秒可读完 | 隔夜外盘 + 主线预判 + 昨日验证 |
| 开盘提醒 | 09:15 | 3-5行 | 竞价异动 + 预期差 |
| 盘中提醒 | 异动时 | 5-8行 | 异动描述 + 追因 + 建议 |
| 复盘报告 | 15:30 | 2-3分钟可读完 | 推荐验证 + 主线更新 + 龙虎榜 + 明日展望 |
| 夜报 | 20:00（有变化时） | 30秒可读完 | 外盘动态 + 对明日影响 |
| 周报 | 周末 | 5分钟可读完 | 周战绩 + 主线回顾 + 经验总结 |

### 每个报告必须包含

1. **推荐验证**：之前推荐的标的现在表现如何（命中率透明化）
2. **信心水平**：高/中/低，基于交叉验证结果
3. **反向风险**：什么条件会让推荐失效

### 推送规则

- 晨报、复盘报告：每交易日固定推送
- 盘中提醒：仅异动时推送，每小时最多 2 条（避免刷屏）
- 夜报：仅外盘有重大变化时推送
- 周报：每周末推送

---

## 实施路线

### Phase 1：记忆系统 + 调度器（基础设施）
- 创建三张记忆表（theme_lines, predictions, market_cognition）
- 实现交易日调度器替代 NewsMonitor
- 实现主线池的 CRUD 和自动发现/淘汰逻辑

### Phase 2：资金行为数据工具
- 新建 P0 工具：龙虎榜、融资融券、北向资金、大宗交易、个股资金流
- 新建 P1 工具：板块排名、量价异常检测

### Phase 3：多模式 Agent
- 重写晨扫 Agent（模式2）
- 重写盘中 Agent（模式1：异动追因）
- 重写复盘 Agent（模式3）
- 重写验证 Agent（模式4：交叉验证）

### Phase 4：输出体系
- 实现多报告格式（晨报/盘中/复盘/周报）
- 推荐验证闭环（复盘时自动回测）
- 推送渠道适配

### Phase 5：外盘 + 宏观
- P2 工具：美股、国债收益率
- 跨市场信号集成到晨扫/夜扫

---

## 与现有系统的关系

- **保留**：所有新闻源、所有现有工具、Web UI、SQLite 存储、CF Worker 代理
- **替代**：NewsMonitor → Scheduler、单一策略师 → 多模式 Agent、无状态分析 → 记忆系统
- **增强**：数据层新增资金行为工具、输出体系从单一报告变为多场景报告
