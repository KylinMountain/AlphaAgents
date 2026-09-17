# The agent decides each morning, and history can replay it

状态：active（**修订 4 · 2026-09-16** · 依两轮外部评审与 RSI 文献审计重构）· **提案，未实现**
创建：2026-09-16

> **修订 2 说明。** 修订 1 被外部评审驳回为 **REWORK**（方向通过，证据链不成立）。
> 评审的两条 P0 我**独立复核并确认**，已立为债：**D19**（现金侧 T+1 用错了市场规则，
> 内核里已有的缺陷）与 **D20**（涨跌停规则没有按日期版本化）。评审还指出修订 1
> **自相矛盾**（一边说「没有分钟数据所以盘中不可回放」，一边又写「用开到收的全路径判限价」——
> 我们只有 OHLC，没有路径），这条我认。逐条采纳见 §7。
>
> **修订 3 说明（2026-09-16）。** M3 从「接学习闭环」升级为 **Governed RSI v1**，具体设计写进 §8，
> 依据是 RSI 文献审计（`docs/self_improvement_roadmap.md`）与一条边界：**Trader 可演化，physics 不可自改**。
> 复核时发现一个真实缺陷并立为债 **D25**（候选可以带着空证据走到 `validated`）。
> **§8 仍然只是设计，没有一行代码。**
>
> **修订 4 说明（2026-09-16）。** 修订 3 收到外部评审 **PASS WITH CAUTION**，四点全部采纳：
> 证据升级为 **EvidenceBundle**（§8.2，`opposing: []` 必须声明过搜索才被接受）；
> **只向前的历史验证**落成硬不变量（§8.4，`max(evidence.cutoff) < evaluation_window.start`，
> 评估器在代码里拒绝违规请求）；判定改为**三层**（§8.3，诊断分量不参与爬山，防 metric shopping）；
> **v1 只做第一层递归**（§8.8，六个部件全部冻结，Meta-RSI = M4）。ON/OFF 反事实增加
> `decision_change_rate` 与 `causal_trace_rate`（§8.6）。D25 的验收同步升级（债表已更新）。

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

六件事，全部是「让历史证据可信」的前置条件。**进度：6/6**（2026-09-16 当天全部完成）。

- [x] **语料只读**（D22）—— 新增 `alpha_agents/data/corpus_access.py`：**symlink 就是「共享」的
      事实标记**，共享文件以 `mode=ro` 打开，写入在 SQLite 层**硬失败**
      （`attempt to write a readonly database`），不再是约定；两个库在共享时**跳过 WAL pragma
      与建表脚本**（两者都是写，而且共享历史上不存在「我们的表」）。自己拥有的文件行为不变，
      **实盘照常入库**。测试**成对**写：每次拒绝都配一次必须成功的读（只读得连读都不行会假通过），
      并断言 `walk_bootstrap` 建的链接**正是**这里判定为共享的路径——两个机制不能各自「看起来对」。
- [x] **回放状态全新，不是生产库副本**（D21，`94234bb`）——
      `config._data_dir()` 认 `ALPHAAGENTS_DATA_DIR`（空值回落到 `data/`，否则会把所有库
      指到工作目录）；`scripts/walk_bootstrap.py` 建**「分裂而非快照」**的回放目录：
      `memory.db` 空建、**永不链接**，语料按引用共享，已有交易状态的目录**拒绝**覆盖（除非 `--force`）。
      **评审的 1 号测试已落地**：`tests/test_walk_bootstrap.py` —— 往语料里种一条 2026 候选
      与一条 2026 持仓，回放里**必须都查不到**，且语料文件跑前跑后**逐字节一致**（夹具先被证明，
      否则「隔离」断言会因为什么都没种而假通过）。
- [x] **修正 A 股股票/资金结算语义**（D19，`e0489da`）
- [x] **市场规则按 `code + 生效日期` 版本化**（D20，`26fa9d1`）
- [x] **第一版只有开盘执行**（D23）—— `alpha_agents/data/t1_execution.py` + 27 条测试。
      市价按开盘成交，**开盘即涨跌停（无对手盘的那一侧）直接拒单**；限价由开盘与 low/high 判定
      （对**单个挂单**这是可判定的）；**同一日两个价位都被触及 ⇒ `ambiguous`**，不猜先后
      ——评审举的那个例子就是其中一条测试。
      **D 日成交量在类型上不可表达**：`DayBar` **没有 volume 字段**，容量只走
      `capacity_shares(adv20)` 与「截至 D-1」的 `average_daily_volume`，并有测试断言该字段
      **不存在**——为了图方便把它加回来会红，而不是静默通过。历史不足 20 日返回 `None`，
      不给一个更短的均值当代理。
- [x] **记录 LLM 的 request / response / tool results / policy 与 memory 哈希**（D24）——
      `alpha_agents/llm_journal.py`：`ALPHAAGENTS_LLM_MODE` 取 `live`（默认，不落盘）/
      `record` / `replay-recorded`。缝在**客户端**：`create_model` 把 `AsyncOpenAI` 过
      `journaled()`，live 模式下**返回同一个对象**。每条记录落在
      `<DATA_DIR>/llm_journal/<run_id>.jsonl`（因此跟随 `walk_bootstrap` 的回放目录），含
      `request_json`/`response_json`、双向 hash、`model_provider`/`model_id` **与
      `response_model`**（OpenRouter 降级链下换模型 = 换交易员）、`policy_hash`/
      `knowledge_snapshot_id`、`tool_calls`/`tool_results`。**两个只有读 SDK 才能发现的坑**：
      `with_options` 返回**新客户端**（重试路径正是调它的返回值），朴素委托会让记录**静默为空**；
      包装资源必须叫 `chat` 而非 `_chat`，否则被 `__getattr__` 透传掉。**失败的调用也记录**
      （否则重试后的序列在重放时全体错位）。流式、请求漂移、截断日志、追加旧记录——全部**拒绝
      而不静默兜底**。**未覆盖并如实命名**：digest / embedding / VPA 三个客户端不在其中。
      **尚未证明**：整段重放能逐行复现 intents/fills/episodes——机制已按调用点钉住，
      端到端要等 M1 的 runner。

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

**落地形态（D24）**：字段是 `run_id` · `seq` · `model_provider` · `model_id` ·
`response_model` · `request_json` · `response_json` · `request_hash` · `response_hash` ·
`policy_hash` · `knowledge_snapshot_id`（**取不到时写一句 `policy_error` 的原因，
不是一个像 hash 的占位**）· `tool_calls` · `tool_results` · `omitted` · `lossy`。
模式通过 `ALPHAAGENTS_LLM_MODE` 取 `live` / `record` / `replay-recorded`。
`response_model` 是**实际回答的那个模型**：OpenRouter 降级链下换模型 = 换交易员。

**两个上面列过、但没有落进日志的字段，如实说明。** `observation_hash` 属于**组装 prompt 的
那个 runner**（M1），日志层拿不到它——而且它已被 `request_hash` 覆盖：观测一变，请求就变，
hash 就变。`decision_json`（模型的最终决定）同样是 runner 层的事。日志记的是**交换**，不是
决定；把两者塞进同一条记录，只会让「请求变了」和「决定变了」再也分不开。

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

## 八、M3 — Governed RSI v1（设计）

**一句话定义。** M3 不是「再写一个自改进循环」，而是**把六个已经存在、却从未接上线的部件接起来**，
并给唯一缺失的输入（真实 episode id）造一个生产者。

`docs/self_improvement_roadmap.md` 是同一次 RSI 文献审计的另一半（G1–G6）。**仓库里已经有一整套零件**：
候选隔离层（`learning_candidates`）、演化门（`holdout_gate`）、版本注册表与六个来源
（`policy_registry` + `policy_sources`）、只前向的影子（`shadow`）、冻结归因快照（`attribution`）。
**M3 的目标是把这两半合成一条闭环，不是新造第三个系统。**

用户给的回路我采纳，并逐段挂到部件上：

```
Episode ──▶ Candidate ──▶ BehaviorDelta ──▶ PolicyVariant
（分析器）   （提议器）     非空且可证伪     （变体：YAML + 冻结版本）
   ▲                                        │
   │      Archive ◀── Retire ◀── 只向前看的评估 ◀┘
   │                             （评估器：分层判定）
   └── Forward Shadow：唯一不是历史的证据（n ≥ 20）
```

### 8.1 六个部件：职责、已有承载、缺什么

| 部件 | 职责 | 已有承载 | 现状（2026-09-16 实测） | M3 要补的 |
|---|---|---|---|---|
| **Analyzer** 分析器 | 从 episode 与结果里读出「哪个决定导致了什么」 | `data/episodes.py`；`data/attribution.py`（`resolve_chain` / `trade_outcome` / 冻结快照）；`evolution/outcome_labels.py`；`evolution/feedback.py`；`scripts/episode_coverage.py` | 逐日**散文** lesson 有；**跨 episode 的模式没有** | 一个把 N 个 episode 折成一条 observation 的接口，输出必须能指名 episode id |
| **Proposer** 提议器 | 把 observation 变成可证伪候选 | `learning_candidates.save_candidate`（四个字段强制：`claim` / `applicable_context` / `proposed_behavior_delta` / `evidence_episode_ids{supporting,opposing}`）；`lessons.consolidate_principles`（principle）；`playbook._collect_playbook_candidates`（playbook） | **5 个生产调用点全部写空证据桶**（`playbook.py` ×3、`lessons.py` ×2）；原因是输入是散文，不是 T1 episode | 让证据桶有东西可写；提议器**只能**写 `observation` |
| **Variant** 变体 | 把候选落成一个**有固定 schema 的可评审差异** | `traders/*.yaml`（一交易员一 YAML，两个现存交易员的差异只有 `extra_prompt`）；`policy_registry.freeze`；`policy_sources.collect` 的六来源 | 从候选到 YAML **没有自动路径**，今天靠人手写 | candidate → variant 的路径，且差异**必须进入 policy hash** |
| **Evaluator** 评估器 | 只用市场事实判定；报**向量**，且边界事前声明 | `holdout_gate.run_gate`（只前向 + 配对 `(date, code)` + `n ≥ 20` + 弃权保 champion）；`scoring.residual_alpha`（G1）；`attribution` | 判定是**一个标量（Brier）**；归因残差还没进判定 | 分层判定（§8.3）；配对标量**仍在 Level 2** |
| **Archive** 档案 | 让「试过、被拒」也留下可查痕迹 | `learning_candidates` 生命周期 + `candidate_transitions`（append-only 触发器）+ `gate_decisions`（**拒绝也落行**）+ `knowledge_snapshots` | `advance_candidate` **零生产调用者**（只有测试）；`candidates_citing` 只被一个脚本用 | 让 `observation` 之后的每个状态真的出现过；迁移的 actor 必须是代码（§8.2） |
| **Forward Shadow** 前向影子 | 唯一能替代历史的证据 | `shadow.open_run` / `emit_for_date` / `score_due`；`PRODUCERS`；`paired_count` | 机制完整，**配对样本 ≈ 0** | 让影子跑到 `n ≥ 20`——这一格是「等日历」，不是「等代码」 |

**这张表的读法**：右两列的关系就是 M3 的全部内容。「现状」列里每一格**都是「有机制、没用户」**。
按本仓库的规矩那不是 bug、不该顺手补（见 §7 与 `TRADER_CORE_IMPLEMENTATION.md` §7 的同类判断）——
**M3 就是那个用户。**

### 8.2 谁有权让候选走一步（这条决定整个设计）

§10 只写了一句：*"The learner may propose and request evaluation; it has no permission to write
active status."* 而 `advance_candidate` 今天**一个生产调用者都没有**，且**不检查证据是否为空**
（后者是债 **D25**：一个从未引用任何决策的候选可以合法走到 `validated`）。

设计决定：**迁移的 actor 只可能是评估器，不可能是提议器。**

- 提议器（LLM）只能写 `observation`。
- `observation → hypothesis` 由**评估器**（确定性代码，无 LLM）在「证据桶非空、且每个 id 能反查到真实
  episode」时推进。
- `hypothesis → testing → validated / retired` 由**前向窗口收口后的门判定**推进。
- `candidate_transitions.actor` 是**代码拥有的名字**（`gate` / `forward_window`），并有测试断言它
  不在模型输出的可达集合里——否则「actor」只是模型自己签的名。

理由：这是「不许 LLM 给自己的产出打分」的机器形态，也是 G3 那条 Echo Gap 证据的直接落地——
agent 把自己**错误的**记忆判为正确的概率是 31%–54%，且**换更强的裁判模型不解决**（残差误差相关 ≥ +0.30），
只有接地的验证解决（+0.05）。

**被否掉的替代方案**：让定时任务按天数推状态（「满 5 天进 hypothesis」）。那正是 §10 禁止的
自我授权——**迁移必须由证据触发，不能由日历触发**；日历推进会让 `testing` 变成一个时间戳，
而不是一个正在被检验的假设。

**证据不止「非空」——它是一次有出处的搜索（EvidenceBundle）。** 评审补的这一刀是对的：
`supporting=[随手 3 个 episode]、opposing=[]` 同样通过 schema，但什么都没证明——
schema 合法不代表系统**找过反例**。所以 M3 里证据从一列 id 升级为：

```json
{
  "supporting_episode_ids": [120, 131],
  "opposing_episode_ids": [],
  "search_scope": "2021-01-01 .. 2022-12-30 · 该主线下的全部 episode",
  "matching_rule": "claim.applicable_context 的可重放匹配谓词",
  "eligible_episode_count": 412,
  "excluded_episode_count": 38,
  "cutoff": "2022-12-30",
  "generated_at": "…"
}
```

于是 `opposing: []` 第一次有了含义：**在事先声明的 eligible set 里找过、没找到**。
仓库里本来就有那句话——「empty list 和 omitted 是两回事」——这是它的第三层：
**searched-and-empty 和 never-searched 也是两回事。** `search_scope` 与 `matching_rule`
同时是防作弊的：评估器按声明**重放**这次搜索，eligible/excluded 数对不上
⇒ 证据无效（§8.3 的 Level 0），而不是「反例恰好没找到」。

### 8.3 判定分三层，不是一个向量压成一分

理由来自 KTD-Fin（CSI300，2024–2026，548 个交易日，10 个前沿模型）：**被动市场 + 风格贡献了
+11%~+29%，而 selection alpha 只有 1 个模型接近 0，其余 9 个全负。** 只优化一个标量，学到的就是
beta。而评审的那一刀更准：**十二个平级的门，跑久了必然退化成 metric shopping**——Brier 不够好
就去看 residual alpha，还不行就去看 drawdown、再看 regime 3……评估器自己变成最严重的过拟合源。
所以分层：

| 层 | 内容 | 判定 |
|---|---|---|
| **Level 0 — 有效性** | 泄漏检查 · 样本覆盖 · **可复现**（`replay-recorded` 逐行复现，D24）· 执行歧义计数（D23）· **EvidenceBundle 与声明的搜索一致**（§8.2） | 任一失败 ⇒ **INVALID**：不是「差」，是「这次评估不算数」 |
| **Level 1 — 硬风险否决** | `max_drawdown` · `turnover` · `exposure` · 容量/流动性 | 任一越过**事前声明**的边界 ⇒ **REJECT**（Level 2 再漂亮也拒） |
| **Level 2 — 改进证据** | 配对 Brier（现有门）· log score · residual alpha（G1）· selection alpha | 决定是否值得继续 |

剩下的 **diagnostics 只用于解释，不参与爬山**：`market_beta` · `style_exposure` · `fill rate` ·
regime 分解……它们进报告、不进门；一条分量从「诊断」升进「门」必须改这份设计，不许在评估中途改
——§12 *"Freeze the hypothesis, primary metric, ... stopping rules"* 的意思就是这个清单在评估
开始前冻结。

**Level 2 内部守现有机制**：配对检验（只前向、配对 `(date, code)`、`n ≥ 20`、弃权即保 champion）。
晋升需要 **Level 0 通过 AND Level 1 无越界 AND Level 2 不退化**，三者同时成立。
**验收必须成对写**：一个 Level 2 通过、却在 Level 1 越界的候选**被拒**；一个 Level 2 漂亮、
但 EvidenceBundle 与声明搜索对不上的候选判 **INVALID**——只有单向用例时，「通过」就是单条件假象。
另有一条**缺失值纪律**：`NULL`（没测到）与 `0`（测到是零）必须可区分——本仓库已经在
`gate_decisions` 的 `evidence_scope` 上踩过这个坑：*"A guard whose missing case is the permissive
one is not a guard."*

这也补上了 §12 要求的 *"Evaluate net equity performance, drawdown and tail risk, exposure, turnover,
capacity assumptions, fill rate"*——本计划的**报告口径**一节已经在要求同一批数，M3 只是把它们
从「报表上的数」变成「判定里的数」。

### 8.4 只向前的历史验证：一条硬不变量

G2 已经写了 "validation 严格晚于 train，且**永不参与反思**"。M3 把它落成可执行的**硬不变量**，
而不是复述原则：

```text
max(candidate.evidence.cutoff)  <  evaluation_window.start
```

候选来自哪个窗口的反思，验证窗口就必须**整体晚于**那个窗口——否则 walk-forward 会偷偷退化成
iterative backtest optimization：同一批 2021–2022 的 episode 先被提炼成 candidate C17，再被拿来
证明 C17 有效。**评估器在代码里拒绝违反这条的评估请求**，不是警告后照跑——一个知道自己违反了
时间切割还继续的评估器，输出什么都不能信。

历史回放（M1/M2 的 T+1 走前重放）是**筛候选**的（本计划开头那条硬约束），**只向前的是验证**这一步：
candidate 冻结之后验证窗口才开始，并且 C17 在验证期**不能因为验证表现改自己**——改了就是新候选、
新边界（§11 的原话）。验证过的 holdout 一旦释放给研究，就不再是干净的验证数据
（§11："Once a holdout is released for research, it is no longer untouched validation data"）。

### 8.5 Physics 不可自改（边界清单）

用户给的分界采纳，并落成**两条可执行的规则**，不是一句原则：

| | |
|---|---|
| **Trader 可演化** | prompt · retrieval 预算 · 新闻过滤 · 主线过滤 · 入场规则 · 出场规则 · 仓位 · 工具选择 · 推理工作流 |
| **不可自改** | 风控 · 账本 · 执行 · 结算 · 费用 · 市场规则 · 泄漏护栏 · **评估器** · **晋升门** · **审计日志** |

**它的机器形态不是一列文件路径，而是「六个来源里哪两个可被变体 staging」。**
`policy_sources.collect` 的六来源中，`knowledge` 与 `decision` 是**指针控制**的——变体只能动这两个；
`prompts` / `model` / `retrieval` / `rules` 是从磁盘读的**指纹**，改它们等于改一个要被评审的代码/提示词，
而不是「运行中改一个交易员的风格」。于是：

- 变体**表达不了** `holdout_gate.MIN_VALIDATION_SAMPLES`：它不在 `decision` 块里，门读自己的模块常量。
  这也解释了为什么**抬高那个常量曾被拒绝**——它是行为来源，会 drift 掉所有已冻结版本，连回滚目标
  一起带走（D15）。
- 需要一条**负向断言**：候选→变体这条路径不能写评估器 / 账本 / 执行 / 结算那几族模块。
  **再配一个变异探针**，否则这条断言很可能是自证的（本仓库已多次踩过静态 grep 自证）。

### 8.6 反事实：ON vs OFF，以及两个机制指标

唯一能回答「学习到底有没有做事」的实验，是**同一个交易员跑两遍**：一版带记忆与演化（ON），
一版 `knowledge.snapshot_id = None`（OFF——`policy_sources.knowledge_fingerprint` 已支持显式 `None`）。
同一 mandate、同一机会面板、同一前向窗口，**账本各自独立**。

这是 §12 的 whole-trader comparison，所以：**不得只比「两边都买过的票」的交集**——那正好把两个交易员
不同的选择丢掉，而**不同的选择才是实验的内容**。配对仍在 `(date, code)` 上，但分母必须报出来，
包括只被一边选中的机会；并按 §12 报 `independent_trading_days`，不是窗口长度。

评审补的两个指标让这个实验从「比收益」升级成「比机制」——PAST-Bench 的核心正是：headline gain
相近的 agent，在「是否真的经由保存/检索/更新的机制路径得到提升」上可以完全不同：

**`decision_change_rate`** —— 有多少个未来 decision 因为历史学习而**真的改变**。
学习没改变任何决定的 run，收益差异是噪声，不是学习在起作用。

**`causal_trace_rate`** —— 每一个变化能否回答完整的一串：

```text
episode 120 ─▶ candidate 42 ─▶ policy variant 17 ─▶ decision 388 改变 ─▶ outcome 388
```

这是 Trade → Learn → Evolve 里 **Evolve evidence** 的机器形态：链上任何一环断了
（episode id 是编的、variant 没进 policy hash、决定没变），trace 就断，`causal_trace_rate` 就掉。
两个数都进 ON/OFF 报告，**排在收益之前**。

### 8.7 文献给了什么、没给什么

| 引用 | 许可什么 | **不**许可什么 |
|---|---|---|
| Reflexion `2303.11366` | 语言反馈可替代权重更新，并留下可查的反思轨迹 | 把反思本身当证据 |
| DGM `2505.22954`（SWE-bench 20%→50%） | 归档 + 自我修改在**可自动判定**的域上有效 | 它自己把沙箱与人类监督列为前提 ⇒ 不能读成「自动通过即自动晋升」 |
| AlphaEvolve（DeepMind） | 自动评估器 + 程序数据库 | 它的前提是进展 *"clearly and systematically measurable"*，**这恰恰是交易域断掉的那一环** ⇒ 本仓库只借「档案」，不借「自动晋升」 |
| MetaSkill-Evolve `2607.05297` | 两个时间尺度（快=技能 / 慢=演化）与 Analyzer / Retriever / Allocator / Proposer / Evolver 的分工 | 跳过门；分工不等于许可 |
| PAST-Bench `2608.04003` | 闭环是 save→retrieve→apply→update 四段路径，每段要各自有证据 | 用端到端指标替代分段证据 |
| KTD-Fin `2605.28359`（CSI300 2024–2026） | 归因（掩码 + Barra 式）揭示模型返回的主要是市场/风格暴露 ⇒ §8.3 的分层判定 | 用绝对收益当 reward |
| AI4AI-Bench `2608.20318`（最好系统 = 最优的 0.250） | 对「一晚上跑完几年就等于有 alpha」的预期校准 | 把 0.25 当成我们也能拿到 |
| EvolveR `2510.16079`（ICML 2026） | 经验驱动的生命周期；原理库要**去重**（fingerprint 已在做）与**动态打分** | 打分来自模型自己（G3 禁止） |

**这一节的作用是防「引文装饰」**：每一条都写清它许可到哪一步、在哪一步停。

### 8.8 v1 只做第一层递归

MetaSkill-Evolve 的快慢两层（改 Trader / 改「怎么改 Trader」）很诱人，M3 **只做第一层**：

```text
M3 / RSI v1：Analyzer · Retriever · Proposer · Evaluator · 演化规则——全部 frozen，
只有 trading policy 演化。
```

先证明 **policy 能经证据改善自己**（§8.6 的两个指标就是证明本身）；「改进算法改进它自己」
留给 **M4 — Meta Evolution**，不是本计划的一部分。理由是顺序性的：
candidate → changed decision → better future behavior 这条最基本链路还没有一个样本的实证，
这时候允许动 Proposer / Retriever，等于一辆还没证明方向盘有效的车，先让它自动改方向盘。
这也和 §8.7 的文献结论一致：AlphaEvolve 的「自动」建立在可系统度量的进展上——
而「改进器本身」目前没有可度量的先例。

### 8.9 M3 的诚实上限

`validated` **不激活任何东西**（§10）。激活 = 被收进一个已批准的 knowledge snapshot，或被 promote 的
policy version。而 §11 写明 *"automatic evaluation success is not permission to promote"*。

所以 M3 的闭环**终点是「已验证 + 归档 +（可选）存在一个变体文件」**，指针仍由人拨。
可交付的形态是 **「闭环可运行、晋升仍需授权」**——不是「自动演化已交付」。

### 8.10 第一版实现（2026-09-16）：三个环节接在哪里，终点停在哪里

**跑起来了**，位置在 `scripts/walk_forward.py`，每个交易日收盘后一次 `_learn`，下一个交易日开盘前一次
`_knowledge_block`。

**环节一 —— 打标。** `outcome_labels.sweep_trade_labels(conn, as_of=day)`。这不是新机制，是**把它接上**：
40 天窗口跑完时 `episodes` 有 16 行、`episode_events` 63 行（内核早就在写），
而 `outcomes` **0 行** —— 因为从来没有人调用那个 sweeper。打标之后每个平仓仓位有一条
带 `available_at` 的 `trade` 标签，"这个结果什么时候变得可知" 才有答案。

**环节二 —— 提炼。** `_distil` 写的是一条**可证伪、且由市场数据打分**的命题，就是决策器自己的那条：
*T-1 涨得更多的候选，实际收益更差*（与「按 T-1 涨幅排序」所假设的方向相反）。
一笔交易在「入场涨幅」与「实现收益」**符号相左**时支持它，相同时反对它。
**没有任何模型被问它怎么看自己**：命题是两个桶的中位数，特征从语料按订单自己的 T-1 重读 ——
"效用只能来自市场数据" 这条规则在这里是**构造性**成立的，不是靠承诺。
`supporting` / `opposing` 两个列表**永远都写**，空也写：`save_candidate` 拒绝缺键，
理由是对的 ——「查过、没有」必须能与「没人查」区分。

**环节三 —— 让下一天看得见。** `_knowledge_block` 把本交易员**自己的**观察读进提示词的
`{knowledge}`。这里有一个必须说清楚的偏离：**生产里规则只能经已批准的 knowledge snapshot 到达决策**
（`feedback.inject_principles`），而回放里没有快照，所以生产里这一块是空的、代理会做出和从前一样的决定。
回放喂回去的**不是规则，是这个交易员关于自己已平仓交易的带日期的笔记** —— 是交易员会翻的交易日记，
不是对它职责的改动。**闸门属于「晋升」（笔记变成规则），不属于「交易员记得自己身上发生过什么」。**
完全没有反馈的闭环不是保守的闭环，是**不是闭环**。这条偏离写进了报告的「不能说明什么」一节。

**终点停在哪里。** 全部候选停在 `observation`，**管线不调用 `advance_candidate`** ——
`learning_candidates` 的设计注记明确要求 `status` 不被管线驱动，晋升是有审计的人的动作（§8.2）。
而且 n 远低于 50，它本来也不可能是别的东西。**所以这一版能诚实地说的是
「闭环可运行、证据被记下并引用、晋升仍需授权」，不是「它自己进化了」。**

**两个跑出来才知道的事实。**

1. **第一版每天都在写同一条观察。** 判据只有 `n >= 4`，没有「今天有没有新证据」，
   于是第四次平仓之后**每一天**都写一份同样的句子 —— 180 天窗口实测
   **11 条候选对应 2 个不同事实**，而且每一条都**是真的**，所以别的东西都不会报警。
   `save_candidate` 也去不了重：`source_date` 是指纹的一部分，"同一条观察在更晚的一天" 按构造就是另一行。
   修法是只在**当天有平仓**时提炼。钉住它的是
   `TestTheObservationIsWrittenOnlyWhenSomethingWasLearned`（2 用例）；探针「去掉新证据闸门」→ 1 条变红。
2. **闭环以交易员的速度闭合，不是以回测的速度。** 决策器每天最多 2 单、仓位按周持有，
   所以平仓本来就稀疏：120 天窗口的第四次平仓落在**第 47 个交易日**。
   这不是缺陷，是**"交易 → 学习 → 进化" 这件事本身的节奏**，也是为什么历史回放只能**筛选**候选：
   它把候选快速证伪，而通过筛选的那些仍然要花掉 20 个前向配对样本（§8.4）。

## 里程碑

**M0 — Evidence Contract**（见 §2 的六项）。

**M1 — 最小可跑。** 历史语料只读 + 全新回放库 + 开盘执行的 T+1 撮合，跑通 30 个交易日，
产出 equity 与成交明细，以及一张「被我标为 ambiguous 的有多少」的计数。

**M2 — 接新闻。** 把 `read_news(as_of="D 09:00")` 折成信号。先做**「只当过滤器」**那条：
只保留当天被提及的板块/个股，看同样规则下分离是否变好。**必须用 `replay-recorded` 模式跑**，
否则对照里混进 LLM 随机性。

**M3 — Governed RSI v1**（设计见 **§8**）。把六个已有部件接上线，并给「空证据桶」造一个真实生产者。
**闭环终点是「已验证 + 归档」，不是「自动晋升」**——指针仍由人拨（§8.9）。

## 验收

机器可查。**评审要求的 12 条测试 + 报告口径都在这里**，先过它们，再跑长窗口。

**M0 / 内核** —— **2026-09-16 逐条核对：本节列出 10 条，其中 8 条已由机器检查**（标 `[x]` 的都给了用例名），
**2 条未完成**（标 `[ ]` 的写明缺哪一半）。核对方式是**先读代码与测试再下结论**，不凭 §2 的进度条 ——
§2 写「M0 6/6」，那是**六件事**的进度，与本节这 10 条**不是同一张清单**：两处都叫 M0，
条目集合却不同。（抬头写「评审要求的 12 条」，本节实际列出 10 条；这个差数没找到出处，如实记在这里。）
核对之初是 **6/4**；核对当天补掉了「严格时间束」缺的那一半（见下）→ **7/3**；
同一天又补掉了「对抗性新闻」→ **8/2**（那一条里同时记下了原文**判断错在哪**，
因为它是一句比事实更悲观的话，而这类错误同样是错误）。
剩下的 2 条**都不是「缺一条断言」** —— 一条缺**数据源**（as-of 的候选池），
一条缺**一次跨模式的对照运行**（record → replay 同窗口）—— 所以补不动，如实留着。
- [x] **状态隔离** —— `tests/test_walk_bootstrap.py::test_state_seeded_in_the_corpus_cannot_reach_the_replay`：
      往语料的 `memory.db` 里种一条 candidate + 一条持仓，断言回放账本为空、语料文件**逐字节未变**；
      「生产库内容 hash 不变」由 `test_walk_forward.py::test_a_run_leaves_the_live_book_and_the_corpus_alone`
      在 runner 之外**独立**量一次（并先断言指纹真的看到了文件，否则是在比两个空）。
      **口径**：这是**合成语料**级别的证明；**仍缺真实生产库副本 + 真实 2020 窗口**。
- [x] **现金结算** —— 前半
      `test_cash_settlement_semantics.py::TestSaleProceedsAreSpendableTheSameDay::test_recording_the_sale_does_not_reduce_available_capital`
      （记一笔卖出，可用资金**不变** ⇒ T 日即可再买另一只）；后半
      `test_t1_settlement.py` 的 `may_sell("2025-07-01", "2025-07-01") is False`
      （T 日新买的当日不可卖）。恒等式另由 `TestTheDocumentedIdentityHolds` 钉住。
- [x] **历史市场规则** —— `tests/test_market_rules.py` 里 `DAY_BEFORE_REFORM = "2020-08-21"` /
      `REFORM_DAY = "2020-08-24"`，配 `test_the_last_session_before_the_reform_is_ten_percent`、
      `test_the_reform_day_is_twenty_percent`、`test_the_two_dates_disagree` —— 正是要的那对日期。
- [x] **无同 bar 幻想** —— `tests/test_t1_execution.py::TestNoSameBarFantasy::test_both_levels_touched_is_ambiguous_not_a_round_trip`
      （两个价位都被触及 ⇒ `ambiguous`，不猜先后）；配套「只触上沿 / 只触下沿 / 都不触」三条。
- [x] **无未来成交量** —— `tests/test_t1_execution.py::TestTheDayBarCannotCarryTheFuture::test_it_has_no_volume_field`
      加模块级的 `test_the_module_exposes_no_way_to_pass_a_fill_day_volume`：
      **不是「不去读」，是「读不到」** —— 把字段加回来会红，而不是静默通过。
- [x] **严格时间束** —— 新闻那一半：`test_news_window.py::TestWindowedRead::test_as_of_still_bounds_above`
      （`as_of` 上界），另有 `test_decision_context.py::TestReplayDay::test_recovers_the_news_that_was_visible`。
      「D 09:00 的上下文里没有 D 的 close/high/low/volume」这一半**原本只是结构性的** ——
      runner 的 `_decide(ctx, day, prev_day)` 只接 `prev_day`，签名上就拿不到 D 的 bar ——
      **2026-09-16 补上用例**：`test_walk_forward.py::TestTheDecisionCannotSeeTheDayItTrades::test_a_name_that_only_leads_today_is_not_picked`
      让窗口日出现一个**只在今天领先**的名字（600003 当日 +9%，过 `LIMIT_UP_SKIP_PCT` = 9.5），
      断言下单仍落在 **T-1 的领先者** 600001 上（读账本，不读结果 —— 被撤单的名字在结果里没有 code）。
      探针 N（把 `_decide` 改成读 `day`）会让它变红，报的正是
      「the decider ranked on the day it trades: it ordered {'600003'}」。
      **结构性保证不是被钉住的保证 —— 这一条现在是了。**
- [x] **universe 也要 as-of** —— **2026-09-16 补齐选股侧。** 停牌在结算侧有测试
      （`test_walk_forward.py::test_a_name_with_no_bar_is_suspended_not_cancelled`），
      **未上市**由 `market_rules.listing_day_exemption` 覆盖（`test_market_rules.py`）；
      缺的是选股侧，现在有了：`market_history.first_bar_dates()` 给出
      `code → 语料里第一个交易日`，`Corpus.is_listed(code, day)` 是 `first_bar < day`，
      两个决策器共用同一个 `_eligibility` 闸门。
      **这条闸门原本是死代码，而且是量出来的**：两个调用点都遍历 T-1 的 bar，
      而「T-1 有 bar」就蕴含「已上市」，于是 `not_listed` 对每一行都返回 `None`。
      实测 2020 窗口有 **1,986** 个 code 尚未上市，而它们**全部**也没有 T-1 bar ——
      所以旧循环里它们是**碰巧**被排除的，规则本身无法被测试。
      改成遍历**整个 instrument 表**之后闸门可达、每个排除理由都被计数：2025-07-01 窗口
      实测 `eligibility:not_listed 409`、`eligibility:board 3702`、`eligibility:st 1122`。
      面板**内容不变**（没有 T-1 bar 的名字本来也排不了名），变的是「universe 缩小」
      从**缺席**变成了**数字**。
      **边界**锚在**决策日**且**严格小于**：当日上市（IPO）没有前收盘，排不了名也定不了价；
      前一日上市的有前收盘、09:00 已在交易，在池内。
      钉住它的是两条测试：`test_a_name_listed_on_the_window_day_is_counted_not_listed`
      （断言**计数器**，不是「没被选中」—— T-1 排序规则下后者构造性成立，断言它是废话）
      与 `test_the_boundary_is_the_decision_day_not_short_history`。
      探针：「删掉 `not_listed` 分支」→ 这两条变红，报的是 `no_prior_bar`；
      「`first < day` 改成 `<=`」→ 同样两条变红。
      顺带修掉同族第三处：`adv20` 的 docstring 写着「不足二十个交易日返回 `None`」，
      代码却对**任何**长度取均值 —— 昨天上市的名字会拿到一个「一根 bar 的 ADV20」，
      正是它自己 docstring 说调用方不要的东西。2025 窗口只涉及 6 个 code，2020 窗口则不然。
- [x] **LLM record/replay** —— **2026-09-16 端到端证明。** 机制（D24，32 条测试按调用点钉住：
      不碰网络、逐次同答案、耗尽/漂移/流式/截断全部拒绝）之外，现在有了真正的对照跑：
      同窗口先 `record` 再 `replay-recorded`，**`fills.csv` / `settlement.csv` / `equity.csv` /
      `events.csv` 逐字节相同**，journal 重放后 sha256 不变
      （`ade0882234ca52decf9b9d2e66ca551d1bba891d407b8ad6312dd941609f40bd`），
      `run.json` 只差 provenance（`generated_at` / `llm_mode` / `model_calls_made` /
      `model_journal_total` / `model_usage_detail` / `limitations`）。
      **声明与实测的双向核对**：占位决策器不得新增 journal 记录、`record` 必须新增、
      `replay-recorded` 必须**不新增且原有非空**；`--decider llm` 在 `live` 下直接 `SystemExit`
      （live 不记录，同窗口两次会因采样而不同，两个实验臂就没法比）。
      这三条由 `TestTheDeclarationAndTheMeasurementMustAgree`（5 用例）钉住。
      第一版判据是 `journal_after > 0`，而**正确的 replay 会判否**（它读一个已经非空的 journal
      且不新增）—— 这是**跑出来**的，record 那一遍是过的。
      **诚实边界**：runner 自身的确定性由 `TestTheLearningStepIsAFunctionOfTheDaysBook`
      用占位决策器钉住（不碰网络），journal 自己的 record/replay 由 `test_llm_journal`
      按调用点钉住。**跨过 openai-agents `Runner` 的桩模型端到端测试没有写** ——
      那需要伪造 SDK 的 Model 协议，测试会比被测的东西更脆。端到端的依据是**真跑了一遍**，
      记在 `TRADER_CORE_IMPLEMENTATION.md` §9。
- [x] **不得绕过账本** —— `tests/test_intent.py::TestOnlyTheStateMachineWritesStatus`：
      `virtual_portfolio.status` 只有状态机那两个模块能写，绕过账本直接改账会被机械拦下。
- [x] **对抗性新闻** —— **2026-09-16 补齐，且核对发现原来的判断写错了两处。**
      原文写「**没有测试**」，并把次序设计说成「**意图不是断言**」；实测**次序本来就被断言**
      （`test_vpa_v10_helpers.py::test_build_vpa_user_content_safety_at_top`、
      `test_build_vpa_user_content_section_order`），**输出侧的门也是**
      （`test_call_llm_vpa_rejects_missing_fields_before_defaults` 用桩客户端驱动一条
      缺 `reason`/`signals`/`scenarios` 的 VERDICT，断言返回 `status="error"` + `VerdictParseError`）。
      **真正零测试的是「围栏容纳」**：`_max_backtick_run` 存在的唯一理由，就是把数据块的围栏
      加宽到盖住正文里的反引号串，而**没有任何测试** —— 拆掉它的调用点，这个文件**全绿**。
      现在由 `TestAnAdversarialBodyCannotLeaveItsBlock`（5 用例）钉住：正文里**同时**塞入
      **伪造的区块标题**和**用来逃出自己块的围栏**，断言**包级可见的标题集合不变**。
      **口径**：断言**不是** `count(标题) == 1` —— 实测伪造的标题**确实以子串出现**（3 次），
      它是数据，数据可以说任何话；要守的是它**到不了包级作用域**。
      探针 O（`_max_backtick_run` 返回 0，围栏退回 3 个反引号）让 4 条变红，报的正是
      「an adversarial body forged a packet-level section header」；
      探针 P（把 `analysis_meta` 块挪到安全声明之前）只让 1 条变红。
      **这条钉住的是代码侧的两端 —— payload 出不了它的块、schema 不合法的回复变成 error ——
      不是「模型不会被说服」。后者不可断言，也不该假装断言。**

**M1** —— **4 条全部由 `tests/test_walk_forward.py`（16 用例）机器检查，2026-09-16。**
在这之前 runner **零测试**：`tests/` 下没有任何文件导入它，所以它的第一次真实执行是打在生产历史上
的 —— D27（成交路径自死锁）与 D28（负预算掐断整个挂单循环）就是这么被找到的。
- [x] 生产库内容 hash 跑前跑后不变（与 M0 第一条合跑一次即可）
      —— `test_a_run_leaves_the_live_book_and_the_corpus_alone`：在 runner 之外**独立**量一次
      生产库 sha256（runner 自己也报这个数，两处都断言，报错了也拦得住），并同时断言
      共享语料的 size/mtime 指纹前后相同 —— 这两条是一件事：跑一次只写自己的账本。
      两处都先断言「指纹真的看到了文件」，否则「前后相同」是在比两个空。
      **M0 那半（「往生产库塞一条、回放检索不到」）不在本文件里，但它并不是没做** ——
      它在 `tests/test_walk_bootstrap.py::test_state_seeded_in_the_corpus_cannot_reach_the_replay`：
      往语料的 `memory.db` 里塞一条 candidate + 一条持仓，断言回放账本为空、
      且语料文件逐字节未变（那个文件的模块 docstring 原文引的就是这条评审要求）。
      **仍然缺的是**：拿**真实生产库的副本** + 真实 2020 窗口跑一次 —— 现在拿到的是
      合成语料级别的证明，不是生产数据级别的。（本节初稿写成「没有做」，是没查就下的结论。）
      **2026-09-16 补记，关于这条用例的效力**：它钉住的是 `result["corpus"]["untouched"] is True`，
      而夹具里的语料是 `tmp_path` 下**合成的**、**没有第二个写者**。所以这条断言在夹具里**不可能失败**；
      同一个布尔值在**生产还活着的机器上永远报「否」**——写者是生产侧的实时新闻入库（D39）。
      结论：**这条用例钉住的是「runner 自己不写语料」，而报告里那行问的是「语料变了没有」，
      是两个不同的问题。** 前者已经有答案；后者要改成问前者（D39 的修法），
      否则报告里那行红是一个永远亮着、于是被读者学会忽略的灯。
- [x] 同一窗口在 `replay-recorded` 下重跑两次，成交明细逐行相同
      —— `test_two_runs_agree_row_for_row`。**口径**：M1 的占位决策器**不调模型**，
      所以这条钉住的是时钟、结算与账本（并断言 journal 记录数为 0）；模型那一半是 D24
      按调用点钉住的机制。用例先断言「有成交可比」，否则两个空列表逐行相同是空话。
- [x] 开盘一字涨停的标的**没有**成交；停牌标的没有成交
      —— 两条用例。**原设想的 `--orders-file` 夹具并不存在**（`walk_forward.py` 没有这个
      参数），场景改由语料构造：一字板 = `open == round(前收×1.10, 2)`，停牌 = 当日无 bar。
      另配一条对照用例，但它证明的是拒绝的**理由**变了（`limit_blocked` → `no_fill`），
      不是「会成交」—— pullback 的区间上限是 `1.005×前收`，比涨停低一分的开盘价
      按算术就在区间之外。写成「会成交」是错的，第一版就是这么写错的。
- [x] `intraday_ambiguous` 计数被输出（不许静默吞掉）
      —— **机制早已存在**（`walk_forward.py:667` 的 `exits_ambiguous`，`:757` 列进
      `settlement.csv`），缺的从来是断言。现在由
      `test_a_touch_the_open_missed_is_counted_and_written_out` 钉住：结果里的计数、
      CSV 里的列、以及报告 meta 里「占位决策器没调模型」三处一起断言。

**五条变异探针**（改坏一处、看对应用例是否变红）全部按预期变红，探针 D 是实质性的：
去掉重跑前的 store 句柄重置，第二次运行会读到第一次留下的持仓（改选 600002/600003），
即「同窗口重跑」这件事本身需要那句话才成立。

**第一次真实 30 交易日窗口（`2025-07-01 → 2025-08-11`，2026-09-16）**：
exit 0，口径三项全「是」，结算抛错 0 天；期末 equity `1,000,226.03`（`+0.023%`）、
最大回撤 `1.321%`、买入 12 笔 / 卖出 9 笔。**这次运行抓出两处汇总缺陷，都是「跑」出来的**：

1. 撤销分布被**截断后当成完整的**印出来：总数 47，各项加起来 41。
2. 计数器的键是 alert 的 reason，而 `价格涨走(7.51)` **把价格写进键里** ⇒ 每次涨走都是
   只出现一次的类别 ⇒ **6 次涨走全部没进前三**。所以缺陷 1 的「差额 6」就是缺陷 2 的
   「6 次涨走」——同一批事件的两个方向。

修法是 `_cancel_class()` 按 **ASCII** `(` 切掉实例（**全角**的 `到期未到价（5天）` 故意保留），
并让 `_top_reasons()` 把余量说出来。修完自洽：
`挂单撤销 47（资金不足×36；价格涨走×6；到期未到价（5天）×3；另有 2 条未列出（1 个原因））`。
修前 / 修后的 `summary.txt` 逐行 diff 只有那一行不同 —— **修的是显示，不是行为**。

**新增两条用例（14 → 16）**，其中 `test_a_runaway_is_summarised_as_its_kind` 是**穿过 runner
跑出一次真实撤单**、再读 `summary.txt` 的。理由：`_cancel_class` 的四个函数级单测在
「`_settle_entries` 不再调用它」这个变异下**全绿** —— 这正是本仓库吃过三次的
「声明了但没有调用」那一类缺陷，所以必须有一条端到端用例把它钉住。配探针 K / M。

**另立 D30（未修）**：汇总数的是 **alert 的词汇**，账本记的是 **`close_reason` 的词汇**，
两者不总是一回事。实测该窗口 47 行 cancelled、44 个不同的 `close_reason`；按汇总印的标签
去账本里找，**四个原因里三个找不到**（`价格涨走` → 0/47，账本写 `价格已涨走(…)`；
`到期未到价（5天）` → 0/47，账本写 `挂单到期未到价（挂5天，期限5天）`）。细节没丢，丢的是索引。

**M2**
- [ ] 产出「有新闻过滤 / 无新闻过滤」两份结果，差异可被一条命令复现
- [ ] 报告同时给出 n（板块-日数）、覆盖交易日数，并声明**只有免费快讯流**
      （付费栏目不可回放——回填报废了这条；见另一计划）

**M3**（§8；每条都机器可查，且**成对**——只有单向用例的那几条等于没测）
- [x] 生成的 candidate **带真实 supporting/opposing episode IDs**，不是一段散文 lesson：
      至少一条候选的 `supporting` 非空，且**每个 id 都能经 `candidates_citing(episode_id)` 反查回来**
      —— **2026-09-16 补齐**，`TestTheObservationIsAttributable::test_a_note_cites_the_episodes_it_came_from`：
      断言 `candidates_citing(101..104)` 各自恰好返回这一条，且 `cited_as == ["supporting"]`。
      端到端的依据是**真跑**：120 天窗口里每条观察都带着它引用的 episode 号写进 `{knowledge}`。
- [x] `opposing` 的**空与缺不同**：断言写的是「查过、没有」，而不是默认值——
      把 `opposing: []` 与「这个字段从没被填过」区分开的用例
      —— **2026-09-16 补齐**，两条：`test_a_note_cites_the_episodes_it_came_from` 断言
      `evidence_episode_ids == {"supporting": [101,102,103,104], "opposing": []}`
      （空列表**被写下来**），`test_opposing_evidence_is_stated_not_omitted` 断言两个桶都非空时
      `candidates_citing(102)` 的 `cited_as == ["opposing"]`。
      写入侧本来就有 `_episode_citations` 拒绝缺键，这两条钉的是**读回来之后**仍然分得清。
- [x] **支持的计数必须是关于收益的，不是分组规模** —— **2026-09-16 加，因为跑出来的第一条观察
      就是反例。** 四笔全部亏钱时 `(t1 > cut) != (return_pct > 0)` 退化成 `t1 > cut`：
      「2 笔支持、2 笔反对」就是两个分组的大小，一个不可能与旁边中位数不一致的恒等式；
      而且把窗口里最好的一笔（T-1 最低、亏损最小）记成了**反对**。
      用例：`TestASupportCountMustBeAboutReturnsNotGroupMembership` 三条 ——
      一条钉住亏损窗口里的两侧归属，一条把单笔收益挪过窗口中位而 T-1 全部不动、要求那笔换边，
      一条断言 `payload["trades"]` 逐笔带 `supports` 使计数可从记录重推。
      探针：把 `> ret_med` 退回 `> 0` → 恰好 2 条红（第 3 条钉的是记录自洽，不是判据方向）。
- [ ] **生命周期真的走过**：`learning_candidates.status` 出现过 `observation` 以外的值，
      且每次迁移的 `actor` 在一个**代码拥有的名字集合**里（断言它不是模型输出的一部分）
      —— **刻意未做，不是未完成。** `learning_candidates` 的设计注记明确要求 `status` 不被管线驱动，
      `advance_candidate` 是它唯一的写者，晋升是**有审计的人的动作**（§8.2）。
      让回放自己走这一步，等于把「证据驱动未来的策略变更」偷换成「回放自己批准自己」——
      而那正是这一阶段要建立的东西。这一条要等**人**在真实的前向窗口上拨指针（Phase 4 的 U4）。
      能诚实说的是：**管线不写 `status` 这件事本身是可查的**（`readmodels/learn.py` 的注记 + 本版无调用点）。
- [ ] **D25 已修**：带空证据的候选**不得**离开 `observation`；且证据是 **EvidenceBundle**——
      声明的 `search_scope` / `matching_rule` / `eligible` 计数与实际重放一致，
      `opposing: []` 只在声明过搜索时被接受（负向用例 + 变异探针）
- [ ] 历史 candidate **不得**进入生产 active policy；断言**在效指针**所指的 knowledge snapshot
      不含任何由历史 run 产出的条目；历史演化必须在独立 run/policy 命名空间里
- [ ] **分层判定**：`gate_decisions`（或后继表）里十二个分量**每个都有列、每个都标了层**
      （Level 0 / 1 / 2 / diagnostic），缺一列或一列未分层即红；诊断分量**不参与判定**
      （测：改 beta/style 不改结果）；且 `NULL`（没测到）与 `0`（测到是零）可区分
- [ ] **Level 1 与 Level 0 都能单独否决**：一个 Level 2 通过、但回撤/换手越过事前声明边界的
      候选**被拒**；一个 Level 2 漂亮、但 EvidenceBundle 与声明搜索对不上的候选判 **INVALID**
      （正向用例与两条负向用例都要有）
- [ ] **时间切割是代码拒绝**：`max(evidence.cutoff) >= evaluation_window.start` 的评估请求
      **被拒绝**，不是警告后照跑（负向用例）
- [ ] **physics 不可自改**：负向断言——候选→变体这条路径写不到评估器/账本/执行/结算那几族模块；
      **配变异探针**（静态 grep 断言在本仓库反复被证明会自证）
- [ ] **ON vs OFF**：`knowledge.snapshot_id = None` 的一版与带记忆的一版，同窗口同面板，
      由一条命令复现两份结果；输出含**配对分母**（含只被一边选中的机会）与 `independent_trading_days`；
      **且输出 `decision_change_rate` 与 `causal_trace_rate`**——后者要求每条变化能追到
      `episode → candidate → variant → decision` 的完整链（§8.6）
- [ ] 生产库内容 hash 跑前跑后不变（与 M0 第一条合跑一次即可）

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

M3 专属（§8）：

- **只从自己的成功里学习，会收敛到自己身上**：一个只会拿自己的历史当先验的系统，学到的是
  自己的偏见。这正是「必须有 external prior + 反对证据桶」的理由，也是 §8.1 里
  `opposing` 必须被真的写进去的原因。
- **提议器是 LLM，所以评分绝不能是 LLM**（G3 / Echo Gap）：任何一处让模型给候选打分，
  都会把 31%–54% 的自评错误率灌进晋升链。**接地的市场核验是唯一被许可的分**。
- **向量会退化成「永远不够好」的拒绝机器**：分量越多，越容易找到一条否决理由，
  结果是系统稳定地什么都不改。所以边界**事前声明**，且「配对检验通过 + 无越界」是一个
  **可达成**的条件，不是十二条都要改善。
- **历史候选与前向候选会混在一起**：历史只是筛候选。混进同一个 policy 命名空间，
  就等于用「早已知道答案的窗口」去喂养一个声称在做前向实验的系统。
- **AlphaEvolve 的自动评估前提在交易域不成立**：它的前提是进展 *"clearly and systematically
  measurable"*。**不能**把「自动评估通过 ⇒ 自动晋升」写进设计（§8.6）。
- **`validated` 容易读成「在跑」**：它不激活任何东西（§10）。报告里必须写成
  「已验证、未激活」，否则一个只在档案里的候选会被读成一个正在生效的策略。
- **metric shopping**：分量一多，评估器自己就成了最严重的过拟合源——Brier 不行看 residual、
  residual 不行看 drawdown。§8.3 的三层（诊断只解释、不进门）与「清单评估前冻结」就是为它设的。
- **walk-forward 退化成 iterative backtest optimization**：允许 candidate 回头在产生它的
  窗口上「验证」自己，走前重放就只剩「重放」没有「走前」。§8.4 的硬不变量就是为它设的。
