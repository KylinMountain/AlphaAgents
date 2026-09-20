# 剩下七项：做什么、为什么、多大

状态：active
创建：2026-09-18
来源：GPT-5.6 Sol review 中除四项 minimum fix 之外的全部条目。
**前四项的完成情况见
[`2026-09-17-from-architecture-to-a-loop-that-turns.md`](2026-09-17-from-architecture-to-a-loop-that-turns.md)。**

这份文件的目的不是把所有条目标成"要做"，而是**每一条先说清它买到什么**，
再决定顺序。评审的框架判断我们接受——「缺的是证明，不是更多功能」——
所以下面的排序按**"能解锁什么证据"**排，不按"改起来顺手"排。

---

## 排序原则

一条改动的价值 = 它能解锁的证据 ÷ 它的成本。三类价值：

- **解锁证据**：做完之后能回答一个**现在无法回答**的问题（最高）
- **消除误导**：做完之后报告/文档不再说错话（次高，通常也便宜）
- **对齐设计**：做完之后实现与设计一致，但**不产生新信息**（最低）

---

## P1：解锁证据（两件）

### ① 变体只有一个基因位点，且该位点已实测为惰性

**评审原话**：「你已经造好了进化机器，但它当前会进化的基因位点大概只有一个。」

**我们已有自己的测量**（见 2026-09-17 计划的路 1 结果）：
窗口内 5 天 × 4 主题、20 个组合，`w_rel ±0.05` **一个都没翻转**，
因为每个评分距阈值 0.0445–0.4592，而权重位移上限只有 0.05。

**所以这条现在不是"设计不够丰富"，而是**：唯一的变异维度在真实窗口里
是惰性的——**进化机制即使转起来也不改变行为**。

**要买到的证据**：一个**能作用于现实**的变异维度。
三条路，成本递增：

| 方案 | 做法 | 成本 | 买到什么 |
|---|---|---|---|
| A. 加大步长 | `w_rel` 步长 0.05 → 0.15 | 极小 | 位点有作用域，但**步长是拍脑袋的** |
| B. 加维度 | 让 variant 支持 `admit_score`、`entry_zone` 宽度等 | 中 | 变异空间变大，仍要逐维验证 |
| C. 构造贴阈值窗口 | 专挑评分在阈值附近的日期做实验 | 中 | 让**现有**位点可观察，不改策略 |

**建议先 C 再 A**：C 不改策略、只改实验窗，能立刻验证"位点是否真的
能起作用"；A 在没有 C 的证据前是调参。B 等 C 的结论。

**验收**：单步反事实在**至少一个**窗口上给出 `changed=True`，
且该翻转可复算（同输入同输出）。

**2026-09-20 补记：A/B 的前提已被远端实现回答——新的位点已存在。**

拉取 origin/main（147 个提交）后，`selection_rank.change_share` 成了一个**真正可执行、可变异**的基因：它决定候选池里涨幅档与换手档各贡献多少名字（`data/selection_policy.py`），`walk_forward._build_panel` 的实盘路径直接读它（`scripts/walk_forward.py:736`），`evolution/variant.py` 把 `t1_change_rank` 证据映射到它的 ±0.10，`evolution/selection_shadow.py` 为它提供前向影子证据。**A（加大步长）与 B（加维度）因此都不再是空缺**：位点不止一个，且证据→干预→评估器现在指向同一个行为（旧版把这条证据错映射到 `theme_gate.w_rel`，改的是准入而不是被批评的排序）。

**仍需 ② 的是**：这个新位点的前向配对样本还没有——它和 ①C 的翻转一样，证明的是「位点能起作用」，不是「哪个取值更好」。

**2026-09-18 晚：C 做了，验收通过，而且结论改写了"惰性"的表述。**

`scripts/threshold_window_counterfactual.py`（+
`tests/test_threshold_window_counterfactual.py`，9 条测试）：在
`sector_flow_snapshots` 能支持的重建窗口（2026-09-08 起）里，对
**每一个** (日, 主题) 都跑 `single_step_counterfactual` 两臂
（`w_rel ±0.05`）。翻转判定不重算——直接调用路 1 实测所用的同一个函数，
两臂的参数集是它唯一的输入差异。

| | |
|---|---|
| 快照日 | 10（09-12 周六与 09-11 帧逐行相同，去重后 **9 个唯一帧**） |
| 可比对 | 1134（63 主题 × 9 帧 × up/down 双向） |
| **翻转** | **33**（up 20 / down 13），跨 **8 个交易日**、28 个主题 |
| 翻转边际 | 0.0083–0.0486，全部落在 0.05 步长能及的范围内 |

- **复算**：两次运行输出逐字节一致；光纤概念 09-17 可从原始库值独立复算
  （flow_pct 0.9738×0.45 + rel_pct 0.1656×0.35 = 0.4962 < 0.5，
  换 0.40 权重 = 0.5045 ≥ 0.5——拒绝翻为准入）。
- **口径**：重建的 confirm=0（无历史源，继承 `rebuild_theme_scores`），
  两臂相同、不影响翻转判定，但重建评分系统性低于生产——所以这个数字
  是关于**阈值邻近结构**的，不是关于"那天生产会不会准入"的。
- **结论**：09-17 的"位点惰性"应表述为——**在评分远离阈值的窗口里惰性**。
  那 5 天 × 4 主题确实如此，但**同一窗口的其他主题**就有评分落在
  ±0.05 能及的范围内（光纤概念 09-17 score 0.4953、苹果概念 0.4835）。
  位移够不着阈值不是位点的性质，是那次窗口选择的产物。
- **对 A 的影响**：A 的前提（位点没有作用域）被 C 削弱——位点在贴阈值
  场景已被证明能起作用。加大步长的问题从"能不能起作用"变成
  "翻得多是否更好"，那是收益归因问题，要等 ② 的配对前向样本。

### ② 首次真实 shadow

**评审原话**：「第一场真实 shadow。不 promote，只要求真正积累一个
challenger vs incumbent 的 forward sample。」

**现状**：`shadow_runs` 表存在但**0 条**；`open_run()` 需要
`policy_version_id` + 已注册的 producer，生产里有 1 个版本在效
（`trader#1`），producer `remap_confidence` 已注册可达。

**要买到的证据**：**第一个前向样本**。这是整条链上唯一还没有任何真实
数据的一环——`learning_candidates` 有 8 条、`outcomes` 有 243 条，
而 shadow 是 0。

**成本**：小（机制已交付，缺的是"开一次并让它积累"）。
风险：它会开始写生产库，需要先确认 `shadow_*` 交易员前缀不会污染
在效账本（`SHADOW_TRADER_PREFIX` 已存在，但要验证隔离）。

**验收**：`shadow_runs` ≥ 1 且 `shadow_predictions` 有已评分行；
报告能说出"挑战者在 N 个配对样本上的 Brier 与在效版本相比如何"。

**2026-09-18：已开。**

- 干跑先暴露一个真问题：对**在效版本** #1 开 shadow 会让挑战者跑与
  冠军相同的参数——实验会变成"自己比自己"。已改用 **version #3**
  （2026-09-17 冻结的"首个生产观察"，`w_rel 0.35 → 0.40`），
  与我们在位点惰性测量里用的基因位点相同。
- **shadow run #3 已开**（producer `remap_confidence`，
  report_type `intraday`，shadow trader `shadow-3`），并完成了首次 emit：
  **3 条 forecast**（002290 / 300684 / 301511，prob 0.53）。
  证据窗口收口后由 `score_due` 评分——样本会按交易日累积。
- 隔离已核实：`SHADOW_TRADER_PREFIX = "shadow-"`，
  写的是 `shadow_predictions`，**不产生订单、持仓、intent**。
- 顺带发现：run #2 在 2026-09-17 已被打开（version #2），
  7 条 forecast——所以"零样本"的实际状态是**1 个旧 run + 1 个新 run**，
  两者都还在等窗口收口。
- 2026-09-18 晚：run #2 的前 4 条（date 09-16，deadline **09-19**）今晚
  23:40 检查时窗口尚未收口——`score_due` 的 `evidence_window_closed`
  在 09-19 收盘前不会放行。**首次评分最早出现在 09-19 收盘之后。**

**2026-09-19：基因契约生效，run #2/#3 关闭——验收回到未达成。**

- shadow_runs 3 条 closed，10 条 forecast 全部未评分。验收「shadow_runs ≥ 1 且有已评分行」未达成。
- 原因见 [2026-09-18-shadow-gene-contract.md](2026-09-18-shadow-gene-contract.md)：producer remap_confidence 只执行 confidence_priors，而 version #2/#3 的变化在 theme_gate——配对样本即使填满也是零差异。
- 重新满足验收的前提：一个真正执行目标位点的 producer，或一个其变化落在 confidence_priors 里的候选版本。选哪条路是下一个决策。

**2026-09-20：合规路径已存在，但生产侧仍无样本——验收仍未达成，阻断点换了一层。**

拉取 origin/main（118 提交，RP-01–RP-11）后重查。生产库状态未变：
`shadow_runs` 3 条全 closed、**0 条 open**；`shadow_predictions` 10 条、**0 条已评分**。

- **合规的 producer/evaluator 现在有了**：`evolution/selection_shadow.py` 对
  `gene_registry.SELECTION_RANK_GENES` 做**逐叶精确覆盖**（`assert_exact_coverage`，
  禁止父前缀匹配），`selection_gate.py` 接封存后的 gate。
- **已实测可开**：在 data/ 的临时副本上，用 learning candidate #4
  （`t1_change_rank/up`）对 parent v2 / v3 构建变体，`changed_genes` 恰好是
  `[decision.selection_rank.change_share]`，`open_run` 成功返回 run id。
  这与 2026-09-19 那次不同——那次 v2/v3 的变化在 `theme_gate`，producer 只执行
  `confidence_priors`，配对样本必然零差异。
- **但样本进不来**：`selection_shadow.process` 的样本源是 `opportunity_sets`
  表（`day > opened_on` 且 `policy_ref` 等于 parent）。全仓库唯一写这张表的地方是
  `scripts/walk_forward.py:2035` 的 `OJ.record_decision`；`alpha_agents/pipeline/`
  里没有任何 opportunity 引用，`main.py` 的 15:45 `shadow_run` 任务也不碰
  `selection_shadow`。生产库里 `opportunity_sets` 表**根本不存在**。
- **结论**：② 的阻断从「没有合规的位点执行者」变成「合规的执行者没有生产样本源」。
  开一个 selection shadow run 现在能成功，但它会永远停在 `sample_count=0`，
  除非先让生产管线把每个交易日的机会集写进 `opportunity_sets`（或把
  `shadow_run` 接上 selection shadow + 一个真实的机会集写入者）。

**2026-09-21：把两条 shadow 路分开看之后，② 对概率 shadow 其实已经解锁。**

之前的诊断把两条路混在一起了。它们的样本源不同，阻断也不同：

| | 概率 shadow（`shadow.py`） | selection shadow（`selection_shadow.py`） |
|---|---|---|
| 样本源 | 冠军的 `predictions`（带 prob 的那本） | `opportunity_sets` 表 |
| 生产写不写 | **写**：`intraday` 59 行、59 行带 prob、38 行已评分、8 天 | **不写**：全仓库唯一写入点是 `scripts/walk_forward.py:2035`；`alpha_agents/pipeline/` 无任何 opportunity 引用；生产库里**这张表不存在** |
| 位点是否被执行 | `remap_confidence` 执行 `confidence_priors` | `selection_policy.change_share`，但生产实盘按 `score` 排序（`intraday_monitor.py:510`），**从不调用** `selection_policy` |
| 现在能不能攒样本 | **能**，只要有一个变化落在 `confidence_priors` 的版本 | **不能**，缺一个生产侧的机会集写入者 |

**实测（`/tmp` 副本）**：冻结一个只改 `confidence_priors` 的版本（parent v1），
`producer_compatibility` 返回 `compatible=True`，`changed_genes` 恰好是
`confidence_priors.{high,low,medium}`；`open_run` 成功，manifest 里
`forward_rule="date >= opened_at"`、`minimum_samples=20`。

**关于 `selection_rank.change_share` 的一句更正**：2026-09-20 的补记说它
「决定候选池里涨幅档与换手档各贡献多少名字，`walk_forward._build_panel` 的
实盘路径直接读它」——`_build_panel` 是**回放**路径（`scripts/walk_forward.py`），
不是生产。生产（`main.py run-v2` → `intraday_monitor`）选股走
`get_sector_best_stocks_fn` + 按 `score` 排序，从不读这个基因。所以
①A/B 的结论要改成：**位点存在于回放与评估器，生产尚未执行它**——
这正是 `docs/DREAM_RSI_SELECTION.md` §6 第 1 条（"the live/replay trading path
actually reads it"）对 selection 基因尚未满足的地方。

**2026-09-21：已开第一条合规的生产 shadow（run #4）。**

- 配置：`policy_version_id=1`（在效版本）、`producer=constant_0.5`（无技能 baseline）、
  `report_type=intraday`（唯一带 prob、能配对的那本）、`opened_at=2026-09-21`。
- **为什么是 baseline 而不是挑战者**：`variant._SUPPORTED_DELTAS` 只把
  `t1_change_rank` 映到 `selection_rank.change_share`，**没有任何路径产出
  `confidence_priors` 的变体**。而生产唯一能配对的位点执行者是
  `remap_confidence`（只观察 `confidence_priors`）。所以「证据 → 变体 → 合规
  挑战者」这条自动链**目前是断的**：能攒样本的位点没有证据产出，有证据的位点
  生产不执行。baseline 不依赖这条链（它按定义豁免基因契约），是当前唯一能
  真正开始积累前向样本的配置——它回答「冠军是否胜过无技能」，不是「哪个取值更好」。
- 隔离已核实：`shadow-1` 前缀、只写 `shadow_*` 两表。开启前后
  `intents` 117 不变、`virtual_portfolio` 137 不变、`active_policy` 仍是 v1。
- 15:45 的 `shadow_run` 任务会自动喂它（已在 scheduler 注册）。

**② 仍未达成，但这次是「等时间」而不是「等代码」**：验收要求
`shadow_predictions` 有已评分行。run #4 开启当天冠军尚无当日选股（0 条），
需要真实的交易日推进。

**一个仍然存在的结构性阻断（值得单列）**：`paired_count` 要求**冠军那一侧也
已评分**，而冠军的 `intraday` 行自 **2026-09-10** 起就没有再评分——15:30 的
`review` 任务负责评分，本地这次重启的调度器还没走到那个时点。实测：21 条待评分
中只有 6 条窗口已收口。所以在 review 恢复评分之前，即使 run #4 攒满预测，
`paired` 仍会是 0。**这不是 shadow 的缺陷，是生产评分链的节奏问题**，
但它决定了 ② 什么时候真的能达成。

---

## P2：消除误导（三件，都便宜）

### ③ 14:55 应标注为 synthetic close

**评审原话**：「报告里应该把它叫 synthetic close decision assumption，
而不能叫严格无前视的 14:55 decision。」

**我们的立场**（已定）：**实现保留近似，报告层如实标注**。
用户已明确接受"收盘前基本等于收盘"，改实现等于放弃这个真实业务动作。

**要买到的**：报告不再暗示它是严格 point-in-time。
`_limitations()` 里加一条，限定条件与其它条目同样格式。

**成本**：极小（一行）。

### ④ concept membership 历史前视

**评审原话**：P1 第 7 条。**现状**：`t1_decide.md` 已注记
「概念是**当前**成分，不是当天的成分」，但**这只是备注，不是修复**——
面板里的概念标签仍取自 `stocks.db` 的当前成分。

**要买到的**：一个**可核验的边界**，而不是一句免责声明。
选项：(a) 确认这个前视在选定窗口内**不改变任何订单**（可测）；
(b) 回放里去掉概念字段。**建议先 (a)**：先量化影响，再决定要不要付
(b) 的代价。

**成本**：中（要做一次消融回放）。

**2026-09-19 结果：量化完成，按跑前写死的规则保留现状。**

- 双臂 20 天 LLM 回放（2026-08-18 起，mimo-v2.5）：17 个有买入日中 15 天订单不同；已平仓收益中位数保留臂 +0.98% vs 消融臂 -7.99%。概念列承载了真实的板块判断，**不能删**。
- 前视仍在：成分表依旧无日期，面板仍标「当前成分」。修复的触发条件是拿到 dated membership 源；在那之前警示即边界。证据与解读：[2026-09-19-concept-lookahead-ablation.md](2026-09-19-concept-lookahead-ablation.md)。

### ⑤ knowledge render 未逐 decision 落盘

**评审原话**：P2——「causal trace 还不能证明具体哪条经验被读取」。

**现状**：`causal_trace` 的 `note` 已明说这一点，且
`decision_snapshots.policy_ref` 现在**已经写进去了**（本轮新增），
所以"哪个版本"可查了；缺的是"读到了哪段文本"。

**要买到的**：`rendered_knowledge_hash` 落到每个决策旁边，
让"这条经验被引用过"从推断变成事实。

**成本**：小（一列 + 写入点）。**但价值依赖 ②**：没有真实 shadow/promotion
时，没有多少决策值得追这条链。**所以排在 ② 之后。**

**2026-09-20 复查：仍未做。** 全仓库（`alpha_agents/`、`scripts/`）没有
`rendered_knowledge_hash` 或任何等价物。远端新增的
`evolution/forward_observation.py` 把 `policy_ref` / `world_read_set_hash` /
`research_packet_hash` / `request_hash` 都落到了 `forward_decisions`，
**唯独没有"渲染出来的知识文本"的 hash**——它记录的是"读了哪个世界/哪个请求"，
不是"读到了哪段经验"。所以 ⑤ 的缺口没有被远端顺手补上。
另注：生产库 `knowledge_snapshots` 是 **0 行**，即当前生产路径下
本就没有已批准的知识快照在流转，这进一步说明 ⑤ 排在 ② 之后是对的。

---

## P3：对齐设计（两件；2026-09-20 复查后：⑥ 已完成，⑦ 缩小）

### ⑥ 容量强制

**现状（2026-09-20 更新）**：**已强制**。远端 RP-08 把 ADV20 容量接进了
真实成交路径：`portfolio.check_pending_orders` 现在接受 `max_shares_by_code`
（`alpha_agents/data/portfolio.py:455`），`_fill_order(..., max_shares=...)` 把它
并进 `max_amount` 的 `capacity_amount` 项（同文件 ~751），一手兜底路径也走同一个
上限（~771）。`scripts/walk_forward.py` 在 `_settle` 里传 `ctx.capacity`
（~2358），`LIMITATIONS` 的措辞已从「reported, but not yet enforced」改成
「enforced as a hard share cap on open-time fills」（~257）。
测试：`tests/test_pending_orders_finishable.py::TestCapacityIsEnforcedAtFill`
（成交被截到 300 股 / 容量不足一手则撤单）。

**因此本项从「缓做」变为「已完成」**，触发条件（规模上升）已被远端主动提前满足。
注意仍有一条独立路径：14:55 synthetic-close 买入不带上限，报告里单列
（`capacity_oversize` 记 False），限制条文也明说了这一点。

**原记录（2026-09-18，保留以说明当时的判断）**：`capacity_shares` 已测量、已计数、已报告，**未参与下单**
（`_fill_order` 从资金算，不接容量参数）。

**当时的缓做理由（2026-09-18）**：这是**模拟器的真实性问题**，不是学习问题。
当前资金 100 万、单笔 ~2.9 万，容量在这个规模下**几乎不会触发**——先做它
不会改变任何现有结论，只会让报告上的「已计数、尚未强制」变成「已强制」。
远端在 RP-08 里没有等这个触发条件，直接做了——结论正确，
但当时的判断（"现在做不改变任何结论"）本身没有被推翻，
只是它不再是排期的依据。

### ⑦ close replay 滑点

**现状（2026-09-20 实测修正）**：原记录说"回放的成交价不含滑点"，
**这个说法对了一半，需要分开看**：

- **已实现盈亏：滑点已在里面。** `portfolio_exit.close_position`
  （`walk_forward.py:2896` 调用的就是它）走 `_estimate_net_close_result`，
  对买入腿加 `(1 + 0.0005)`、卖出腿减 `(1 − 0.0005)`。
  实测：10.00 买、10.00 卖、1000 股，`realized_total = -25.2`，
  其中纯双边滑点 **-10.0**，其余 -15.2 是佣金/过户费/印花税。
- **未实现与记录价：滑点不在里面。** `virtual_portfolio.open_price`
  记的是原始成交价（无滑点），`_value` 的浮盈按
  `(close − open_price) × shares` 算，所以建仓当日的净值不含买入腿滑点，
  要等平仓才通过 realized 体现。

**因此真正的缺口是**：建仓当日 equity 少扣一次买入滑点（每笔约 `金额 × 5bps`），
而整个持仓周期的总收益在平仓后是对的。这比原描述窄得多。

**为什么仍不紧急**：影响量级是**每边 5bps**，对 20 天、几万元的窗口
影响在**个位数元**，且只在持仓未平的当日净值上；它不会改变任何
"agent 会不会卖"这类结论。

**什么时候做**：当比较两臂的**收益差**（而不是行为差）时——
那时 5bps 会在配对样本里累积，必须建模。**与 ② 同期做。**

**2026-09-21：已完成。** 触发条件已满足——`sector_experiment_compare.py:105`
与 `selection_experiment_compare.py:202` 都通过 `performance.equity_metrics`
读 `equity.csv` 的**日收益**来比较两臂，滑点会在配对样本里累积。

修复中发现**原诊断仍偏窄**：只改 `invested` 不够。`walk_forward._value` 的
equity 是 `total + unrealized`，而 `total` 已含滑点、`unrealized` 又按原始价把
它加回来，两者**恰好抵消**——所以只改 `invested` 时 equity 仍是 1,000,000
而不是 999,985。实测数据与完整推导见
`docs/exec-plans/active/2026-09-21-buy-side-slippage-in-equity.md`。

---

## 顺序（建议）

```
第一批（解锁证据，成本小）— 已完成（commit 406ed3f）
  ✅ ② 首次真实 shadow
  ✅ ③ 14:55 标注

第二批（解锁证据，成本中）
  ✅ ①C 构造贴阈值窗口       ← 2026-09-18 晚：33 个翻转，见上
  ✓ 基因契约（插队）        ← 2026-09-19 commit f61ca8d：shadow 证据
                              必须绑定 producer 执行的基因位点；生产
                              run #2/#3 因因果无效被关闭（0 配对样本）。
                              见 2026-09-18-shadow-gene-contract.md
  ✓ ④ concept 前视的消融    ← 2026-09-19 完成：15/17 日订单改变，消融臂中位 -7.99% vs +0.98%
  ✅ ①A/B 扩展变异维度       ← 2026-09-20：远端已给出 selection_rank.change_share 位点 + 变体映射 + 前向影子证据
  ✅ ⑥ 容量强制              ← 2026-09-20：远端 RP-08 把 ADV20 接进真实成交
                              路径（max_shares_by_code → _fill_order），
                              限制条文已改为 enforced；本项从"缓做"变"已完成"
  ◦ ② 重开合规 shadow      ← 2026-09-20：合规 producer/evaluator 已存在且
                              实测可开，但生产库无 opportunity_sets 表、
                              管线也不写——样本源缺失，开了也是 0

第三批（依赖前两批的结论）
  ⑤ knowledge hash 落盘      ← 有真实决策可追时才有价值（复查仍未做）
  ⑦ 滑点建模                 ← 已实现盈亏已含滑点；缺的只是建仓当日
                              浮盈少扣一次买入腿（比较收益差时才需要）
```

**一句话**：先把 **shadow 开起来**（唯一零样本的证据），
再把 **变异维度是否真的能起作用**测清楚（①C），
其余六项按"是否已有真实数据值得它处理"排后。

## 决策记录

- 2026-09-18：本文件按「能解锁什么证据」排序，不按工作量。
  评审的框架判断（缺证明不是缺功能）被接受并用作排序标准。
- 2026-09-18：③（14:55 标注）**只在报告层**做，不改实现——用户已
  明确接受该近似，评审的建议在报告层采纳。
- 2026-09-18：④（concept 前视）先量化再修，不直接改行为——
  影响未知时改行为是拿结论换一个更小的不确定性。
- 2026-09-18：⑥⑦ 明确**缓做**并写明触发条件，避免它们以
  "评审提过"为由占用优先位置。
- 2026-09-18 晚：①C 用**扫描全部 (日, 主题)** 代替"构造"——窗口不是挑
  出来的，是把可重建的每一天都测一遍，翻转带由 `single_step_counterfactual`
  自己判定，扫描器不携带第二份公式。A（加大步长）的决策顺延：位点
  "能否起作用"已由 C 回答（能），"步长多大算对"是收益归因问题，
  等 ② 的前向样本。
- 2026-09-19：基因契约插队（commit f61ca8d）——shadow 证据因因果无效的积累中止，run #2/#3 关闭（0 配对样本）。② 的验收回到未达成，重开前先决定由谁执行 theme_gate 位点。
- 2026-09-19：④ 量化完成——概念列删掉后 88% 交易日订单改变、消融臂中位数 -7.99%（保留臂 +0.98%）：列承载真实板块判断，保留现状，警示即边界，等 dated 源再修。

- 2026-09-20：拉取 origin/main（147 提交）并合并。远端与本地平行实现了同一基因契约（远端为 manifest 版 producer_compatibility，语义等价且测试覆盖更广），冲突处以远端为准；本地独有资产保留：--no-concepts 消融、①C 扫描器、两份计划文档。合并后 2850 passed / 双 lint 通过。
- 2026-09-20：复核七项。①A/B 由远端新位点满足；② 仍未达成（3 runs 全 closed、0 open、10 条预测 0 评分）；⑤⑥⑦ 按原计划仍缓做。
- 2026-09-20（第二次拉取，118 提交 / RP-01–RP-11）：合并为 `feb5bf2`，2968 passed、harness 222 文件、docs lint 全绿。逐项重查后有三处需要改写结论——**⑥ 已完成**（ADV20 真的接进了成交路径，不再是"已计数未强制"）；**⑦ 的缺口比原描述窄**（已实现盈亏早已含双边滑点，缺的只是建仓当日浮盈少扣买入腿）；**② 的阻断换了一层**（合规 producer/evaluator 已存在且实测可开，但 `opportunity_sets` 在生产里根本不存在，样本进不来）。⑤ 复查仍未做。
- 2026-09-21：跑 20 天小批量验证（`logs/session-20260920/replay-20d.log`，20 日 0 报错），它暴露了两处真实缺陷，均已修复并推送：**概率 shadow 缺前向门**（`emit_for_date` 不检查日期与 `opened_at`，回填历史能被评分成前向样本；`fc7a03b`）、**容量"已强制"但报告说"尚未强制"**，且 `capacity_oversize` 恒为 False（`732037b`）。同时把 ② 拆成两条路重看：概率 shadow 的样本源生产**在写**，selection shadow 的**不在写**；已开第一条合规生产 run #4（baseline，隔离核实，生产 117 单/137 仓/active v1 全部未变）。
