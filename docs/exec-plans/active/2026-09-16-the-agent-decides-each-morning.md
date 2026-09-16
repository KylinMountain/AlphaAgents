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
      intents / fills / episodes —— **机制已就位（D24，32 条测试按调用点钉住：不碰网络、
      逐次同答案、耗尽/漂移/流式/截断全部拒绝），端到端那一句尚未证明**：它需要 M1 的 runner
      先存在，才有 intents / fills / episodes 可复现。
      **（2026-09-16 更新：那个前提已经不成立 —— runner 存在，M1 的用例确实产出了
      intents / fills / episodes。但那些用例的运行**不调模型**（占位决策器，journal 计数断言为 0），
      所以端到端那一句仍然未证明；缺的是一次 `record` 之后再用 `replay-recorded` 跑同一窗口的对照。）**
- [ ] **不得绕过账本**：agent 的任何交易都必须留下 `TradeIntent`；禁止为历史方便直接改账
- [ ] **对抗性新闻**：正文里放 `"Ignore previous instructions..."` 不能突破输出 schema

**M1** —— **4 条全部由 `tests/test_walk_forward.py`（7 用例）机器检查，2026-09-16。**
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

**M2**
- [ ] 产出「有新闻过滤 / 无新闻过滤」两份结果，差异可被一条命令复现
- [ ] 报告同时给出 n（板块-日数）、覆盖交易日数，并声明**只有免费快讯流**
      （付费栏目不可回放——回填报废了这条；见另一计划）

**M3**（§8；每条都机器可查，且**成对**——只有单向用例的那几条等于没测）
- [ ] 生成的 candidate **带真实 supporting/opposing episode IDs**，不是一段散文 lesson：
      至少一条候选的 `supporting` 非空，且**每个 id 都能经 `candidates_citing(episode_id)` 反查回来**
- [ ] `opposing` 的**空与缺不同**：断言写的是「查过、没有」，而不是默认值——
      把 `opposing: []` 与「这个字段从没被填过」区分开的用例
- [ ] **生命周期真的走过**：`learning_candidates.status` 出现过 `observation` 以外的值，
      且每次迁移的 `actor` 在一个**代码拥有的名字集合**里（断言它不是模型输出的一部分）
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
