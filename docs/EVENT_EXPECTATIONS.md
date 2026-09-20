# Event Expectations Layer

> 股票交易的是相对预期的变化，不是新闻标题本身。

AlphaAgents 现在有新闻、价格结构、资金流、主题状态和盘中反应，但还缺一层：
**事件发生前，市场原来在押什么。**

没有这层，Agent 容易把：

- “利润增长 80%”
- “降息 25bp”
- “拿到大订单”
- “政策正式发布”

直接翻译成“利好”。但价格真正面对的是：

```text
Expectation before event
        ↓
Actual / guidance / communication
        ↓
Surprise = realized - expected
        ↓
Was it already priced?
        ↓
Price / volume / flow reaction
```

## 为什么需要单独建模

宏观政策的研究通常把“预期部分”和“意外部分”分开；企业财报研究也把
earnings news 定义为 actual 相对 announcement 前投资者预期的 surprise。
因此“好结果但股价跌”不应自动叫反常：

- actual 好，但低于更高的隐含预期；
- headline 好，但 guidance / margin / cash flow 弱；
- 事件前已经连续上涨，仓位拥挤；
- 事件本身符合预期，真正 surprise 接近 0；
- 宏观/行业 beta 当天压过公司信息。

同样，“坏消息后上涨”也不自动等于市场失灵：结果可能只是没有预期那么坏。

## 不把 priced-in 做成一句 prompt

最终要做成 point-in-time 数据，而不是让 LLM 凭感觉判断。

### 1. Event Calendar

```text
event_key
event_type          macro / earnings / policy / product / commodity
scope               global / market / sector / stock
subject
scheduled_at
first_known_at
source
```

只记录“当时已知会发生”的事件。事后补录的日历不能进入历史 replay。

### 2. Expectation Snapshot

预期会随时间变化，必须 append-only：

```text
event_key
captured_at
consensus
market_implied
distribution
source
```

例：

- FOMC：利率期货/OIS 隐含概率、未来路径；
- CPI/就业：调查一致预期；
- 财报：EPS/Revenue consensus、公司 guidance；
- A 股业绩预告：公司给出的区间 + 可获得的一致预期。

没有可靠 consensus 就标 `unavailable`，不能让模型补。

### 3. Pre-event Positioning

这是“是不是已经提前交易”的市场代理：

```text
return_1d / 5d / 20d
sector_relative_return
volume_percentile
turnover_percentile
fund_flow
gap/run-up
option_implied_volatility   # 有数据时
```

它不等同于“预期”，但能回答市场是否已经明显向一个方向下注。

### 4. Realization

```text
actual
guidance
management_tone
announced_at
source
```

actual 与 expectation 必须共享可比较的单位/定义。

### 5. Reaction

事件后只记录事实：

```text
open_gap
intraday_high_low
close_return
sector_relative_return
volume
fund_flow
1d / 3d / 5d follow-through
```

不要直接存“利好兑现”“利空出尽”这种标签。

## Agent 最终应该看到什么

已经增加事实型工具：

`get_event_context(code, as_of)`

返回类似：

```json
{
  "event": "FY2026 earnings",
  "scheduled_at": "...",
  "expectation": {
    "eps_consensus": 3.20,
    "revenue_consensus": 120.0,
    "captured_at": "..."
  },
  "positioning": {
    "return_20d": 28.4,
    "sector_relative_20d": 19.2,
    "volume_percentile": 91
  },
  "realization": {
    "eps": 3.35,
    "revenue": 118.0
  },
  "reaction": {
    "gap_pct": 4.2,
    "close_pct": -3.1
  }
}
```

Agent 自己判断，不让工具返回：

`"priced_in": true`

因为这是 policy，不是事实。

## 一个重要拆分：三种 surprise

不要只做一个 `surprise_score`。

1. **Fundamental surprise**：actual 相对 consensus。
2. **Communication surprise**：guidance / forward path / 管理层语气相对预期。
3. **Market surprise**：价格实际反应相对事件前定位。

例如财报：

```text
EPS beat
Revenue miss
Guidance cut
Stock +35% in prior 20d
High open → close -6%
```

这不是一句“利好落地”。它是五个不同事实，Trader 应该基于它们决策。

## 与 Dream Agent 的关系

Event Expectations 必须是 DreamWorld 的一部分，而不是事后标签。

```text
Event calendar
+ expectation snapshot as-of T
+ pre-event price state
+ realization available only after announcement
        ↓
DreamWorld
        ↓
Policy variants
```

如果 replay 在公告前能看见 actual，整个 Dream evidence 作废。

## 数据源发现：先探测，不先判定“有没有历史”

这里不把任何 provider 简化成“有历史 / 没历史”。需要区分至少四件事：

1. **历史事件本身**是否存在；
2. **公告时间**是否足够精确，可以做 point-in-time 截断；
3. **一致预期是否保存了历史版本（vintage）**，还是只返回今天看到的最新值；
4. 本地账号/积分/权限是否真的能调到需要的字段与时间跨度。

当前值得优先探测的候选包括：

- **Tushare forecast / forecast_vip**：官方文档明确提供业绩预告历史，并带
  ann_date / first_ann_date；
- **Tushare express / fina_indicator / disclosure_date**：可补实际结果、
  财务指标和披露计划；
- **Tushare 券商（卖方）盈利预测特色数据**：官方资料称数据从 2010 年开始，
  这很可能比“从今天开始自己 snapshot”更有价值，但必须在本地验证接口名、
  字段、报告发布日期、是否保留 revision/vintage，以及账号权限；
- **AKShare** 的业绩预告、业绩快报、业绩报告、盈利预测相关接口；
- **已有本地 SQLite / 抓取归档**：优先看是否已经保存公告时间、研报时间或历史
  consensus，不重复拉一份相同数据。

因此实现里要有一个 **source probe**，输出 capability，而不是把 provider 名称
硬编码成结论：

```text
source
dataset
available
history_start
time_field
revision_key
point_in_time_grade
permission / error
sample_fields
```

只有通过 probe 的数据才进入 Event Expectations ingest。

## 当前立即可做的版本

在 provider probe 完成前，T1 Trader 已经有：

- 新闻窗口；
- `get_stock_context` 的 5/20/60 日位置与资金；
- `get_intraday_shape` 的事件后盘中反应；
- `get_market_regime` / `get_theme_state` 的市场与行业背景。

因此 prompt 先要求：

**新闻 → 事件前价格路径 → 事件后反应**

而不是：

**新闻标题 → 买卖。**

如果某次决策没有 point-in-time consensus，Trader 必须把“priced in”写成假设；
但这不等于断言数据源没有历史，是否存在历史版本由 probe 决定。

## 实现顺序

1. Opportunity Journal（已实现）：保存当时没买的机会。
2. Event Calendar + append-only Expectation Snapshot。
3. `get_event_context` 事实型工具。**已实现**
4. Replay Capability Matrix 增加 event_expectation 覆盖率。**已实现**
5. Opportunity/DreamWorld 纳入 event snapshot hash。**已实现：Opportunity Journal
   冻结 calendar / expectation / realization 的 PIT content hash，DreamWorld hash
   随 context 一起覆盖这些 refs**
6. 之后才研究 “priced-in / surprise” policy 是否真的改善选择。

核心原则：

> **不要教 Agent 背“利好落地就是利空”；要给它当时的预期、提前走势和落地反应。**
