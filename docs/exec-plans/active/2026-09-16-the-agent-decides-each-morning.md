# The agent decides each morning, and history can replay it

状态：active（**修订 2 · 2026-09-16** · 依外部评审重构）· **提案，未实现**
创建：2026-09-16

> **修订 2 说明。** 修订 1 被外部评审驳回为 **REWORK**（方向通过，证据链不成立）。
> 评审的两条 P0 我**独立复核并确认**，已立为债：**D19**（现金侧 T+1 用错了市场规则，
> 内核里已有的缺陷）与 **D20**（涨跌停规则没有按日期版本化）。评审还指出修订 1
> **自相矛盾**（一边说「没有分钟数据所以盘中不可回放」，一边又写「用开到收的全路径判限价」——
> 我们只有 OHLC，没有路径），这条我认。逐条采纳见 §7。

## 目标

把主模式改成 **T+1 日频**：每个交易日**开盘前**，agent 只读「当时已存在」的信息
（T+0 及更早的日线 + 当日 09:00 前的新闻），做一次分析，然后为当日下单。这样任何
2020-01 → 今的窗口都能被**逐日走前重放**，把「等几周才攒到二十个配对样本」换成「一晚上跑完几年」。

**但真正的定义是这样一句**（评审给的，我采纳）：

> 不是「T+1 回测计划」，而是**一个时间隔离的 Trader 生命周期模拟器**：
> 让一个 Trader 把「看到信息 → 犯错 → 亏钱 → 留下 episode → 总结假设 → 改变行为 →
> 再遇到类似情况 → 看它有没有真的变」压缩到几个小时里跑完。

**要验证的第一件事不是「赚不赚钱」，而是闭环最后一根箭头：昨天学到的东西，今天真的改变了决定。**

## 一条硬约束：加速的是**学习**，不是**晋升**

`paired_count` 的 SQL 里有 `AND s.date > <version.frozen_at>`，设计 §11/§12 也写明
*"Historical replay is development evidence"*。所以：历史用来**快速筛候选**，
前向窗口花在活下来的候选上。**历史永远替代不了那 20 个前向配对样本。**

## 一、必须先修的两件事（在跑任何长窗口之前）

### 1. 市场规则错了，而且是内核里的错（D19）

A 股把**可用**与**可取**分开：T 日卖出的钱**当天就能继续买入**，只是 **T+1 才能取现**。
本仓库把**取现规则套在了买入力上**：`portfolio.get_available_capital` 会减掉
`settlement.unreleased_pending_total`，而 `record_pending` 把结算日写成 `next_settle_date(exit_date)`。

后果：任何有卖出的日子，买入力都被低估 ⇒ **当天卖旧买新被禁止** ⇒ 换手、暴露、
现金曲线、回撤全部失真。而且既然本计划主张「复用同一个内核」，**不修就等于忠实地复现一个错误的市场规则**。

已独立复核（证监会河南证监局案例 + 券商资料，引文见债表 D19），并已钉成
`tests/test_cash_settlement_semantics.py`（**三个断言缺陷**的用例，命名使其不可能被读成认可）。

### 2. 涨跌停没有按日期版本化（D20）

`TRADABLE_PREFIXES` 默认**含创业板 300/301**，而**创业板涨跌幅在 2020-08-24 从 10% 改成 20%**。
代码里现在**没有任何可执行的涨跌停规则**（只有统计涨停池的函数）。从 2020 年起的走前回放
必须调用 `market_rules(code, date)`，而不是 `if code.startswith(...)`。

## 二、M0 —— Evidence Contract（评审建议新增，先于一切）

六件事，全部是「让历史证据可信」的前置条件：

- [ ] **语料只读**：`market_history.db` 与新闻库以只读方式打开，回放期间不得写入
- [ ] **回放状态全新，不是生产库副本** —— 见 §3
- [ ] **修正 A 股股票/资金结算语义**（D19）
- [ ] **市场规则按 `code + 生效日期` 版本化**（D20）
- [ ] **第一版只有开盘执行**，不发明日内路径 —— 见 §4
- [ ] **记录 LLM 的 request / response / tool results / policy 与 memory 哈希** —— 见 §6

## 三、回放的状态从哪里开始（修订 1 在这里错得最重）

修订 1 写的是「从生产库分叉一份 `data/walk/`」。评审指出：**这对文件隔离是安全的，
对时间隔离是危险的。**

```
2026 production memory          copy →   2020 replay
├─ 2026 的 principles                    （行情与新闻 as_of=2020，
├─ 2026 的 lessons                        但 agent 的「脑子」是 2026 的）
├─ 2026 的 policy / themes
└─ 2026 的 candidates
```

即使行情与新闻全部 as_of，**agent 的知识来自 2026** —— 这是严重的未来泄漏。

**正确的形态**：

```
历史语料（只读）                    回放状态（全新）
market_history.db ─┐
news 历史库        ─┼──→  新建空 memory.db · 新 ledger · 新组合
                    │     新 episodes · 新 candidates
                    └──→  显式 seed 一份 policy · run_id = walk_xxx
```

**市场历史库可以完整存在（读时 as_of）；Trader 状态必须从空白或显式 seed 开始，
不能 clone 当前生产状态。** 设计文档本来就要求 historical replay 是 *separate run and ledger*。

> 修订 1 的「分叉」初衷只是**文件隔离**（别把生产库写坏）。评审说得对：
> 我把两件事混成了一件。文件隔离解决不了时间隔离。

## 四、执行模型（第一版刻意做窄）

**只有开盘执行。** 理由是我在修订 1 里自己写反了：我们说「没有分钟数据 ⇒ 盘中不可回放」，
却又说「用开到收的全路径判限价」——**我们没有路径，只有 O/H/L/C**。
给定 `open=10.30, high=10.50, low=9.40` 与「限价 10.00、止损 9.50」，
我们**不知道**是先成交后止损、还是先跌到 9.40 再反弹到 10.00。

第一版：

| 情形 | 处理 |
|---|---|
| 市价 | 按 open 成交 |
| 限价，且**只看 open 是否满足** | 可成交 |
| 限价，需要日内触价（high/low） | 成交不了就如实记 **`intraday_ambiguous`**，不猜路径 |
| 当日新仓的止损/止盈 | **不在当日用 high/low 成交** —— 推到下一交易日，或整体标 `ambiguous/censored` |

**宁可少模拟，也不要制造不存在的分钟路径。**（评审也指出，本仓库的评审规范第一批就要求
检查 same-bar signal/execution 这类错误。）

**容量约束用 `ADV20`（截至 D-1）**，不用 D 日总成交量：若在 09:30 成交，
则 15:00 才知道的当日总量**不能**反过来决定 09:30 能不能成交——那是执行侧前视。
D 日总量只作**事后流动性诊断**输出。
（评审的建议成立。一个补充：理论上「开盘集合竞价成交量」在 09:25 就是已知的，
但日线里没有这个字段，所以第一版就用 ADV20。）

## 五、T+1 规则

1. **可卖数量 = 昨收持仓**（今日买入不可卖）。「做 T」的合法形态是卖昨天的、买回的明天才能卖。
2. **涨跌停**：`limit = prev_close × (1 ± pct(code, date))`，`pct` 由 §1.2 的规则表给。
   **开盘即涨停 ⇒ 买单不成交**——不建模这条会让回测虚高最多。
3. **停牌**：当日无 K 线 ⇒ 不可交易，持仓冻结、估值沿用上一有效价。
4. **资金**：卖出所得**当日可用**（D19 修好后），T+1 可取。

## 六、可复现性：存档不等于可复现

修订 1 的风险项写「保存 decision input snapshot」，那只做到「知道当时给了它什么」，
做不到「第二次得到相同判断」。设计文档已经给了正确答案：应保存
**model request / response / known model identifiers / tool results**，而 seed 与 temperature 不够。

落这些字段：`observation_hash` · `prompt_hash` · `policy_hash` · `knowledge_snapshot_id` ·
`model_provider` · `model_id` · `request_json` · `response_json` · `tool_results` · `decision_json`，
并分两个模式：`--llm-mode live` 与 `--llm-mode replay-recorded`。
否则 M2 的「有新闻 / 无新闻」对照里会混进两次 LLM 的随机差异。

**以及一句必须写进任何历史报告的话**：今天的模型权重本身已经见过 2020–2025 的公开事件，
所以历史 agent 的收益曲线**不是干净的 point-in-time alpha 回测**。它用来调机制、发现坏规则、
做消融、筛候选，但报告上要标 **`mechanistic walk-forward ≠ point-in-time model backtest`**。

## 七、逐条采纳评审（决策记录）

- **(a)/(b) 的选择：先做 (a) 平行 T+1 模式**，与修订 1 一致。评审的细化被采纳：
  **不是另写一条平行交易链**，而是**新增一个 Trader / PolicyVariant，复用同一个
  `intent → risk → reservation → fill → ledger → episode` 内核**。盘中模式暂作 incumbent，
  T+1 作 candidate trader。
- **主题门不能偷偷绕过。** 现有 OPEN 路径会真的执行 `resolve_theme()` / `theme_admits()`。
  若实验要取消它，必须写成 **T+1 trader 自己的 PolicyVersion** 的差异
  （例如 `policy.daily_t1.theme_gate = disabled`），让这个变化**进入 policy hash**，
  而不是在历史 runner 里 bypass。否则「历史一条路、生产一条路」。
- **M3 要够得像「Learn」。** 修订 1 只写「把 lessons/principles 放进分叉库」——太弱。
  Learn 的定义要求 **supporting + opposing evidence → 可证伪候选**，而 `learning_candidates.py`
  早就要求 `claim / applicable_context / proposed_behavior_delta / evidence_episode_ids{支撑,反对}`。
  真正的生产缺口恰恰是**五个调用点的 `evidence_episode_ids` 全为空**。所以 M3 改成：
  `Decision → Episode → Outcome → Reflection → Candidate(带真实 episode ids) →
  历史检验 → retire / survive → 只作用于回放内的行为`。
- **状态隔离从「分叉」改成「全新 + 显式 seed」**（§3）。
- **执行模型收窄到只有开盘**（§4），并修掉修订 1 的自相矛盾。
- **容量用 ADV20**（§4）。
- **市场规则版本化**（D20）与**现金结算语义**（D19）先修。

## 里程碑

**M0 — Evidence Contract**（见 §2 的六项）。

**M1 — 最小可跑。** 历史语料只读 + 全新回放库 + 开盘执行的 T+1 撮合，跑通 30 个交易日，
产出 equity 与成交明细，以及一张「被我标为 ambiguous 的有多少」的计数。

**M2 — 接新闻。** 把 `read_news(as_of="D 09:00")` 折成信号。先做**「只当过滤器」**那条：
只保留当天被提及的板块/个股，看同样规则下分离是否变好。**必须用 `replay-recorded` 模式跑**，
否则对照里混进 LLM 随机性。

**M3 — 接学习闭环**（按 §7 升级后的形态）。

## 验收

机器可查。**评审要求的 12 条测试 + 报告口径都在这里**，先过它们，再跑长窗口。

**M0 / 内核**
- [ ] **状态隔离**：往 2026 生产库的 principles/candidates 里人工塞一条，2020 回放**检索不到**；
      且全程**生产库内容 hash 不变**
- [ ] **现金结算**：T 日卖出旧仓后所得资金 **T 日可买另一只**；T 日新买股票**仍不可卖**
- [ ] **历史市场规则**：创业板规则切换前后各一条（**2020-08-21 / 2020-08-24**）
- [ ] **无同 bar 幻想**：同一日 high/low 同时跨过 entry 与 stop 时，**不得**生成乐观路径
- [ ] **无未来成交量**：D 日开盘成交判定**不得**读取 D 日 total volume
- [ ] **严格时间束**：D 09:00 的上下文里没有 D 的 close/high/low/volume；新闻必须 `<= information_cutoff`
- [ ] **universe 也要 as-of**：未上市、当日停牌、当时不属于候选池的证券不得被选中
- [ ] **LLM record/replay**：存档 request/response/tools 后，`replay-recorded` 模式能逐行复现
      intents / fills / episodes
- [ ] **不得绕过账本**：agent 的任何交易都必须留下 `TradeIntent`；禁止为历史方便直接改账
- [ ] **对抗性新闻**：正文里放 `"Ignore previous instructions..."` 不能突破输出 schema

**M1**
- [ ] 生产库内容 hash 跑前跑后不变（与 M0 第一条合跑一次即可）
- [ ] 同一窗口在 `replay-recorded` 下重跑两次，成交明细逐行相同
- [ ] 开盘一字涨停的标的**没有**成交；停牌标的没有成交
- [ ] `intraday_ambiguous` 计数被输出（不许静默吞掉）

**M2**
- [ ] 产出「有新闻过滤 / 无新闻过滤」两份结果，差异可被一条命令复现
- [ ] 报告同时给出 n（板块-日数）、覆盖交易日数，并声明**只有免费快讯流**
      （付费栏目不可回放——回填报废了这条；见另一计划）

**M3**
- [ ] 生成的 candidate **带真实 supporting/opposing episode IDs**，不是一段散文 lesson
- [ ] 历史 candidate **不得**进入生产 active policy；历史演化必须在独立 run/policy 命名空间里

**报告口径**（评审要求，与设计 §12 一致）——长期报告**不能只有 equity curve**，至少一并输出：
`net return` · `benchmark excess` · `max drawdown` · `turnover` · `exposure` · `fill rate` ·
`unfilled rate` · `suspended/pending/censored count` · `ambiguous same-bar count` ·
`distinct trading days` · `episode count` · `candidate count` · `candidate survival rate`

## 风险

- **回测漂亮、前向平庸**：走前回放最大的诱惑就是过拟合。**信号**：M2 若在历史上分离很强
  （例如 Q5−Q1 > 1%），**先怀疑泄漏与前视，再高兴**。这也是历史只用来**筛候选**的原因。
- **模型权重本身就是未来信息**（§6）：历史结果不能当 point-in-time alpha。
- **LLM 不可复现**（§6）：必须 record/replay，否则对照实验不可解释。
- **付费栏目缺口**：重建语料只有免费快讯流，结论只能声称快讯流的效果。
- **`theme_score` 的 `strength` 没有历史**：绝对阈值在回放里不可用（排序仍忠实）。
- **`replay_evolution.py` 会写生产库**：在 M0 落地前**不要再跑它**。
