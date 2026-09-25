# Trader Runtime：连续认知、跨时间尺度交易员

**状态：active**  
**起点：main @ 67827141b44b9ed7bc6f26b496e1d3a0b897bf1c**  
**替代方向：关闭且不合并 PR #35；暂停继续扩建 M2-C scheduler / experiment harness。**

## 1. 为什么现在做这个

AlphaAgents 已经有 morning scan、intraday monitor、T1 decider、portfolio、thesis、
review、handbook/playbook、shadow、forward evaluation 等大量组件，但这些组件仍然
分别拥有局部状态或局部决策权。系统能“做很多事”，却还不能严格回答：

> 今天 09:05 为什么没有买？10:40 什么事实改变了昨天/早盘的判断？  
> 这次经验属于日频 swing，还是盘中 execution？  
> Replay 里的这个交易员和线上真正开仓的交易员是不是同一个？

本计划停止继续扩实验基础设施，先建立一个真正持续存在的 **Trader Runtime**。

核心目标不是“更多 Agent”，而是：

```text
Observation
    ↓
Trader State
    ↓
Trader.step()
    ↓
BUY / ADD / HOLD / WAIT / REDUCE / SELL / REJECT
    ↓
Intent / Execution
    ↓
Outcome
    ↓
Trader.review()
    ↓
LessonCandidate → Lesson → Rule
```

## 2. 第一条架构约束：Trader 与时间粒度解耦

Trader 本身既不是“日频 Trader”，也不是“日内 Trader”。

**同一个 Trader Core 必须同时支持：**

1. Daily Replay：历史只有日线时，用日频 Observation 逐日经历市场；
2. Intraday Replay：以后有分钟历史时，直接增加 Observation Adapter，不重写 Trader；
3. Live：实时价格、分钟量价、资金流、新闻、成交事件触发同一个 Trader.step()。

差异只能来自“当时可见的 Observation”，不能来自另一套策略：

```python
# 允许
observations = adapter.available_observations(cutoff)
decisions = trader.step(state, observations, context)

# 禁止
if replay:
    strategy_a()
else:
    strategy_b()
```

### 2.1 经验必须携带时间尺度

日频数据不能证明日内规律。所有学习对象必须至少携带：

- `evidence_timeframe`: 1d / 60m / 30m / 5m / tick
- `decision_horizon`: intraday / 1d / 3-5d / position
- `evidence_scope`: replay_daily / replay_intraday / live_daily / live_intraday
- `support_count`
- `counterexample_count`
- `confidence`
- `last_validated_at`

日频 replay 可以训练 swing / daily 经验；上线后同一个 Trader 再积累 intraday
execution 经验。不同证据域不能因为“更新更晚”而互相覆盖。

## 3. 唯一交易决策 Owner

未来只有 Trader 有权新增或减少策略风险。

Morning Scan、Intraday Monitor、新闻、资金流、异动检测、VPA 等都只产生
Observation / Candidate，不直接拥有最终交易策略。

```text
Data / Research / Detector
          ↓
      Observation
          ↓
        Trader
          ↓
   TraderDecision
          ↓
       Intent
          ↓
      Execution
```

代码级验收目标：

- morning path 不再直接决定最终买入；
- intraday path 不再拥有独立“选股 → 定价 → 下单”策略；
- position monitor 保留硬风险与执行职责，但策略 HOLD/ADD/REDUCE/SELL 由 Trader 决定；
- replay 也调用同一个 Trader Core。

## 4. 核心领域模型

新增 `alpha_agents/trader/`，优先保持纯领域层，不绑定 LLM/provider/实时数据源。

### 4.1 Observation

```python
@dataclass(frozen=True)
class Observation:
    observed_at: datetime
    available_at: datetime
    type: ObservationType
    subjects: tuple[str, ...]
    data: Mapping[str, object]
    source: str
    evidence_refs: tuple[str, ...]
    timeframe: Timeframe
```

初始类型：

- MARKET_OPEN / MARKET_CLOSE
- DAILY_BAR
- PRICE_MOVE / VOLUME_CHANGE
- SECTOR_FLOW / THEME_CHANGE
- NEWS / EVENT
- ORDER_FILLED
- POSITION_CHANGED
- THESIS_SIGNAL

`available_at` 是硬边界：review/replay 不得把后来才知道的事实写回旧 Observation。

### 4.2 DecisionContext

```python
@dataclass(frozen=True)
class DecisionContext:
    mode: replay | live
    observation_resolution: Timeframe
    decision_horizon: DecisionHorizon
    session: pre_open | open | intraday | close
    information_cutoff: datetime
```

`mode` 只声明来源/可证明精度，不得选择另一套交易策略。

### 4.3 TraderState

最少包括：

- market_view
- theme_views
- watchlist
- active_theses
- positions
- pending_orders
- recent_decisions
- recent_observations
- lessons
- rules

TraderState 是结构化认知状态，不是 prompt 文本。

### 4.4 WatchItem

必须区分：

- `WAIT`：观点仍成立，只是条件未满足；
- `REJECT`：当前投资逻辑不成立。

WatchItem 至少记录：

- why
- missing_conditions
- invalidation_conditions
- created_at
- last_checked_at
- next_check

这是连接 daily replay 与 live intraday 的关键对象。

### 4.5 Thesis 三层语义

同一 Trader 支持不同 horizon 的 thesis：

1. Market Thesis：数天到数周的市场/主题判断；
2. Trade Thesis：1～5 日的单票交易假设；
3. Execution Thesis：分钟到小时的入场/减仓/退出判断。

短周期 execution 不能反向伪造长期 market thesis 的证据。

### 4.6 TraderDecision

统一输出：

- BUY / ADD / HOLD / WAIT / REDUCE / SELL / REJECT
- code
- thesis_id
- confidence
- reasoning
- size_pct
- entry_zone
- stop_loss
- target_price
- invalidations
- next_check

WAIT 必须携带可重新触发的 `next_check`，否则它只是“今天没买”的另一种写法。

## 5. Trader Core

唯一核心入口：

```python
async def step(
    state: TraderState,
    observations: Sequence[Observation],
    context: DecisionContext,
) -> StepResult
```

T1 初期先实现 deterministic state transition，不调用模型：

1. 校验 Observation 未越过 information_cutoff；
2. 合并新的 Observation；
3. 更新 Watchlist / Thesis 的生命周期状态；
4. 产生“需要重新决策”的 subject；
5. 保持 state/version/lineage 可追踪；
6. 返回新的 immutable snapshot + decisions/events。

后续 LLM 只是 Trader 的 reasoning engine，不拥有状态机。

## 6. Review 与 Learning

### 6.1 Review 评价三件不同的事

1. Decision Quality：按当时可见信息，这个决定是否合理；
2. Execution Quality：决定对，但价格/订单/执行是否有问题；
3. Outcome Quality：后来真正赚亏、回撤、机会成本如何。

后验结果不能改写当时事实。

### 6.2 学习层级

```text
Memory（事实）
   ↓
LessonCandidate（一次经历产生的假说）
   ↓
Lesson（多次证据支持）
   ↓
Rule（稳定、带适用域和反证的规则）
```

禁止：

```text
一次 review → 永久 RULE
```

Rule 必须带：

- scope / timeframe / horizon
- evidence refs
- support / counterexample
- confidence
- version
- expiry / retirement

旧 Handbook 后续迁移为 LegacyRule，不直接删除历史。

## 7. Daily Replay 的角色

Replay 不再首先被定义为“测一个30天收益”。

它首先是：

> 给同一个 Trader 制造严格按时间推进的历史经历。

当只有日频数据：

```text
T 日决策：只能看到 T-1 及之前的信息
        ↓
Trader.step()
        ↓
T 日 execution simulator
        ↓
MarketClose / Outcome
        ↓
Trader.review()
        ↓
Learning
        ↓
T+1 TraderState
```

跑数百个交易日，本质是让 Trader 经历不同市场状态并累积 **daily/swing**
经验。上线后实时 Observation 继续推进同一个认知模型。

## 8. 实施阶段

### T0 — 收敛范围

- [x] PR #35 关闭且不合并；
- [x] 暂停 scheduler isolation；
- [x] M2-A / M2-B 保留为未来验证工具，不继续扩建；
- [x] 不新增 shadow / evaluator / experiment harness，直到 T8。

### T1 — Trader Core

**状态：completed and merged to main at 6bce80e2.**

新增：

```text
alpha_agents/trader/
    __init__.py
    types.py
    observation.py
    state.py
    runtime.py
```

交付：

- Timeframe / EvidenceScope / DecisionHorizon / Session / Action；
- Observation / DecisionContext；
- WatchItem / ThesisState / TraderState / TraderDecision；
- immutable/canonical serialization；
- deterministic `TraderRuntime.step()`；
- information_cutoff fail-closed；
- state version + parent hash lineage；
- 无 provider 的单元测试。

### T2 — Watchlist + Thesis Lifecycle

**状态：implemented on feature branch; awaiting regression/merge.**

交付状态机：

- watch: watching / triggered / rejected / expired / converted;
- thesis: proposed / active / strengthened / weakened / invalidated / closed;
- Observation → Watch/Thesis transition；
- WAIT → 条件满足 → re-evaluate 的端到端测试。

### T3 — Morning 接管

Morning Scan 只输出 research/candidate Observation。

Trader 决定：

- BUY
- WAIT
- REJECT

生产代码中 Morning Scan 不再直接新增风险。

### T4 — Intraday 接管

Intraday Monitor 变成 Significant Observation Detector：

- 监控既有 watchlist / thesis / position；
- 发现有意义变化才触发 Trader.step()；
- 删除独立的“盘中选股→定价→下单”策略 owner。

### T5 — Position Management

策略动作统一：

- HOLD / ADD / REDUCE / SELL

硬约束继续由代码执行：

- A 股 T+1
- 涨跌停
- 现金
- ADV20 / 成交容量
- 订单合法性

### T6 — Review

新增 `Trader.review_day()`：

输入 Observation / Decision / Fill / Position / Outcome，输出：

- DecisionReview[]
- LessonCandidate[]

Review 不直接写 Rule。

### T7 — Learning

实现：

```text
Memory → LessonCandidate → Lesson → Rule
```

并强制 timeframe / horizon / evidence_scope。

### T8 — Replay 切到 Trader Runtime

HistoricalObservationAdapter → Trader.step()。

验收：

- replay 不再拥有另一套交易策略；
- live / replay 的 Trader Core 相同；
- 差异仅来自 Observation stream 与 execution environment。

### T9 — 重新启用已有实验工具

届时 M2-A/M2-B 才用于回答：

> 相同 TraderState 下，一条 Lesson/Rule 的差异是否导致行为与连续轨迹改变？

## 9. 第一阶段验收场景（T1～T4）

必须能表达：

```text
Day 1 09:00
A 属于强主题，但价格太高
→ WAIT A
→ next_check = price <= 30 OR breakout confirmation

Day 1 10:30
price 31
→ 仍 WAIT

Day 1 14:00
price 29.8 + volume confirmation
→ 同一个 WatchItem 被触发
→ BUY A

Day 2
sector flow stronger
→ HOLD / thesis strengthened

Day 3
sector flow negative + support broken
→ SELL / thesis invalidated

Close review
→ “WAIT→BUY 条件本次有效”
→ LessonCandidate
```

Daily Replay 可以把 10:30/14:00 合并成日频事件；Live 可以使用分钟级事件。
**两边使用同一个状态机与决策对象。**

## 10. 本轮明确不做

在 T8 前暂不扩：

- scheduler lanes / complex async orchestration
- 多 Agent 投票
- RL / model fine-tuning
- 自动参数搜索
- 新 shadow infrastructure
- 新 experiment harness
- 自动 promotion 扩建
- 单纯追求 backtest 收益

## 11. 完成定义

Trader Runtime 完成后，AlphaAgents 的核心定义从：

> 多 Agent + 规则 + Pipeline 的量化框架

变为：

> **一个能够在不同时间粒度的 Observation Stream 上保持连续市场认知、
> 维护 Watchlist/Thesis、做统一交易决策、事后复盘并按证据域演化经验的
> AI Trader Runtime。**

Agent、VPA、新闻、资金流、Memory、Playbook 都是 Trader 的能力组件；
**Trader 才是唯一策略主体。**
