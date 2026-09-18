# 从「有自进化架构」到「自进化跑起来」：review 的四项 minimum fix

状态：active
创建：2026-09-17
来源：外部评审（GPT-5.6 Sol）对 `v0.1.0` / 最近一轮真实 walk-forward /
RSI-M3 实现的 REWORK 判定。**本文先把它的技术主张逐条对代码核实，再排任务。**

## 核实结论（2026-09-17，对 main @ 77d4951）

评审的技术主张**基本全部属实**，逐条给出代码证据：

| # | 主张 | 核实 | 证据 |
|---|---|---|---|
| 1 | T1 Trader 没有工具、只有 1 turn | ✅ 属实 | `agents/t1_decider.py:320` `max_turns: int = 1`；`:338-340` `Agent(...)` **无 `tools=`** |
| 2 | Morning Analyst 有 26 工具 / 40 turns | ✅ 属实 | `agents/morning.py:37` `MORNING_TOOLS`（26 个）、`:143` `max_turns=40` |
| 3 | `decision_change_rate` 测的是血缘不是因果 | ✅ 属实 | `evolution/causal_trace.py:174` `changed = sum(... if c.candidate_id is not None)`——跑在候选派生版本上即计为 changed，**没有比较 ON/OFF 下的 intent** |
| 4 | Variant 只支持一个基因位点 | ✅ 属实 | `evolution/variant.py:74-76` `_SUPPORTED_DELTAS` 只有 `("t1_change_rank","down"/"up") → theme_gate.w_rel ∓0.05` |
| 5 | prompt 里有未经治理的"交易真理" | ✅ 属实 | `prompts/t1_decide.md:25-32`「这是 A 股**最重要的情绪指标**」「**价格是结果，资金是原因**」「**A 股是板块驱动的**」 |
| 6 | tool docstring 里有策略判断 | ✅ 属实 | `tools/registry.py:319`「ROE > 15% = 优质企业」、`:332`「涨跌比 > 2 = 乐观（适合看多）」、`:402`「主力净流入 = 大资金看好」、`:462-468`「综合4个维度给出**操作建议**」 |
| 7 | 6 个问题型工具只有 1 个落地 | ✅ 属实 | `docs/trader_toolkit.md:163-168` 列了 6 个；代码里只有 `get_limit_ladder`（`tools/limit_ladder.py:111`），`get_theme_leader` / `get_intraday_shape` / `get_stock_memory` / `get_my_state` / `replay_trade` **均无实现** |
| 8 | 容量未强制、close replay 无滑点 | ✅ 属实 | `walk_forward.py:1157` 只把 `participation` 传给容量**计数**；全文件无 `SLIPPAGE` |
| 9 | 14:55 用完整日线是显式近似 | ✅ 属实，且已在代码注释中声明 | `exit_decision.py:121-123` 明说"今天的开高低收量你**都已经看到**"；报告**未**把它标为 synthetic close |
| 10 | `n < 50` 与闸门 `20` 的语义需统一 | ⚠️ **评审略滞后，但指出的是真问题** | `holdout_gate.py:58` `MIN_VALIDATION_SAMPLES = 20`；`GOLDEN_PRINCIPLES §7` 已于 2026-09-15 改为 `n < 20`——**规则与代码已是同一个数**。真问题在**表述层**：`docs/releases/v0.1.0.md` 当时仍写 `n < 50`（已修） |

**核实中发现的额外问题（评审未提，属我们自己的账）**：

- `docs/releases/v0.1.0.md` 有两处过时的 `n < 50`，与 §7/闸门不一致 → 已在本轮修正为 `n < 20`。
- 这正是评审 P2 说的"不应该让读者猜"：**旧数字残留在对外文档里**，而文档是读者建立信任的入口。

**评审结论中我们不接受的一处**：评审把"14:55 决策"称为应改名为
`synthetic close decision assumption`。**工程口径我们保留**（用户已明确接受"收盘前基本等于收盘"），
但**报告层要如实标注**——这正是本计划第 4 项的一部分。

## 目标

把评审的 4 项 minimum fix 落成**可机器验收**的交付。做到之后，
评审承诺的评价变化是：从"具备 self-evolution architecture"到
"governed self-evolving trader 已经跑起来"。

**四件事，按依赖顺序：**

### ① `decision_change_rate` 从血缘指标改为反事实指标（P1，最独立）

**问题**：现在的实现把"跑在候选派生版本上"当作"决定变了"。一个
`candidate_id != None` 但 intent 与 OFF 完全相同的决策，也被计为 changed。

**收口**：新增反事实比较——同一 `WorldSnapshot`（同一日、同一面板、同一账本、
同一模型交换），分别在 policy **OFF** 与 **ON** 下取 intent，逐字段比较
`code / action / size / entry_low / entry_high / stop / target / abstain`，
任一不同才算 changed。

**验收（可机器检查）**：
- 新指标 `counterfactual_change_rate`，与旧 `decision_change_rate` **并存且命名区分**；
  旧指标改名为 `lineage_rate`（或保留但 docstring 明说它是血缘率），不再叫 change。
- 测试：构造两个决策，OFF/ON 的 intent 完全相同 → `counterfactual_change_rate == 0`
  且 `lineage_rate == 1`；OFF/ON 不同 → 前者 > 0。
- 测试：`WorldSnapshot` 不同的两次决策**不得**进入同一配对（否则比的是两天的市场）。

### ② 未经治理的策略断言：标注来源或移出（P1）

**问题**：`prompts/t1_decide.md` 与 `tools/registry.py` 里有大量
**hypothesis/policy** 被写成事实（"最重要的情绪指标"、"资金是原因"、"ROE>15% 是优质企业"、
"综合评分给出操作建议"）。它们绕过了 candidate→validate→promote 这条唯一入口。

**收口**（按可执行性分两级，先做 ① 级）：
1. **区分事实与判断**：tool 输出只保留可核验事实（`broken_rate = 29.3%`），
   把"建议降低仓位"这类结论移出 tool 层。
2. **给策略性断言标来源**：prompt 中的策略句必须能回答"它凭什么获得真理身份"。
   在 prompt 文件里对每一条加结构化来源标注（`invariant` / `approved knowledge` /
   `candidate`），无来源者移入 knowledge 层。

**验收**：
- 新测试 `tests/test_policy_contamination.py`：扫描 `prompts/*.md` 与
  `tools/registry.py` 的 docstring，命中**策略性断言模式**（"= 优质"、"适合看多"、
  "操作建议"、"是原因"、"最重要的"）而**没有来源标注**的，测试失败并点名文件行号。
- 白名单必须显式（每条注明来源），不允许"因为现在改不动所以豁免"的笼统豁免。

### ③ 把 replay-safe 的问题型工具接进 T1 Trader（P1，工作量最大）

**问题**：T1 Trader `tools=[]` / `max_turns=1`——它只能读平台预先做好的包，
不能自己转头观察。`docs/trader_toolkit.md` 自己列的 6 个工具里，5 个没实现。

**收口**：**不是把 30 个 registry tools 塞进去**，而是先接 5–6 个
replay-safe、只输出事实的问题型工具（评审 §11 的清单）：

| 工具 | 回答的问题 | 数据源 |
|---|---|---|
| `get_market_regime(as_of)` | 今天是什么市场 | breadth / ladder / 炸板率 / 昨日涨停溢价 / 最高连板 / 板块集中度 / 换手水位 |
| `get_theme_state(theme, as_of)` | 这条线在生命周期哪一段 | 龙头 / 连板高度 / 梯队 / 昨日龙头今日表现 / 跟风数 / 炸板数 / 资金流 / 扩散或收缩 |
| `get_stock_context(code, as_of)` | 这只票是什么状态 | 20/60 日结构 / ATR / 换手 / 成交额 / 资金流序列 / 相对板块强弱 / 涨停史 / 龙虎榜 / 融资 / 新闻时间线 |
| `get_intraday_shape(code, as_of)` | 今天分时怎么走的 | open / VWAP / high-low / 上午下午量占比 / 冲高回落 / 尾盘加速 / relative volume |
| `get_stock_memory(code)` | 我认识这只票吗 | 我过去看过它几次、买过几次、每次的理由与结果 |
| `get_my_state()` | 我今天手顺不顺 | 当日/本周 P&L、回撤、连亏次数、今日决策数、敞口、主线集中度、最近 10 笔校准 |

**硬约束（评审 §17 第 4 条，本计划的重点）**：
每个开放给 Trader 的工具必须声明 **`supports_replay` / `as_of` / `source_time`**，
**读未来直接失败**。这是回放诚实性的契约，不是文档要求。

**验收**：
- `tests/test_trader_tools_time_travel.py`：对每个接进 T1 的工具，
  在 as-of 早于数据时点的调用上断言**抛错或返回空**，且错误信息点名时间边界；
  在 as-of 覆盖数据时返回内容。用一个**未来数据**的 fixture 证明它不会泄漏。
- T1 Trader 的 `tools=` 非空、`max_turns > 1`；一次真实回放里能看到至少一次
  **模型主动发起**的工具调用（journal 里有 tool_calls）。
- 分层不变量仍通过（工具在 `tools/`，不得反向 import）。

### ④ RSI golden path 第一次真实跑通（P1，本计划的验收核心）

**问题**：`variant.py` / `shadow.py` / `holdout_gate.py` / `policy_registry.py` /
`causal_trace.py` 全都在，但生产 promotion = 0、真实 shadow = 0、
ON/OFF 行为差异证据 = 0。**机制可达 ≠ 在跑。**

**收口**：跑通**一次完整链条**（不要求自动 promote，人审批这道门保留）：

```
episode → candidate（带 EvidenceBundle）
        → variant（冻结）
        → 未见过的 future window
        → ON / OFF 反事实比较
        → decision 真的不同
        → outcome
        → evaluator
        → survive / retire
```

**验收（本计划的最终判据，全部要真实数据、非桩）**：
- 一次真实 walk-forward 里，存在一个 `candidate_id`，其 variant 在
  **候选诞生之后**的窗口上产生了至少一次 `counterfactual_change_rate > 0` 的决策。
- 该链条的 `causal_trace` 完整（`episode → candidate → variant → decision`），
  且每一步都能在库里查到行。
- evaluator 给出 survive 或 retire 的**明确裁决**（不是 abstain）。
- **不 promote**——promotion 仍需人授权；本项只证明"链条能转一圈"。

## 决策记录

- 2026-09-17：**评审的技术主张逐条核实后基本全部接受**。P0 无新增（评审也确认
  上一轮那些真 P0 已系统性修掉）。接受的核心是评审的框架判断：
  **缺的是证明，不是更多功能**。
- 2026-09-17：**评审第 10 条（`n < 50` vs 20）按"表述滞后"处理**，不是代码缺陷——
  §7 与 `MIN_VALIDATION_SAMPLES` 已同为 20；残留的 `n < 50` 在 release notes 里，已修。
- 2026-09-17：**14:55 口径保留工程近似，但报告必须标注**。评审建议改名
  `synthetic close decision assumption`——我们在**报告层**采纳，在**实现层**不改：
  用户已明确接受该近似，改实现等于放弃"收盘前决策"这个真实业务动作。
- 2026-09-17：**工具接入不做 tool sprawl**。只接评审 §11 的 5–6 个问题型工具，
  且必须先满足 time-travel 契约；registry 里那 30 个不整体接入。
- 2026-09-17：**顺序**：① 最独立且能立刻修正一个错误命名 → 先做；
  ② 是纯静态检查、风险低 → 紧随；③ 依赖新工具与 journal，工作量最大；
  ④ 是总验收，依赖 ①②③ 全部到位。

## 边界（本计划不做什么）

- **不新增市场数据源。** 评审明确建议："我反而不建议现在继续堆市场数据源了。"
- **不自动 promote。** 自动产生 → 自动验证 → 自动淘汰，**人审批** → 生产。
  这仍然叫 Governed Self-Evolution。
- **不重写 Trader Core。** 评审明确：不是 Trader Core REWORK。
- **不为通过测试而放宽 time-travel 契约**（这是 ③ 里最容易被"先跑通"压力侵蚀的一条）。

## 进度（2026-09-17）

### ✅ ① `decision_change_rate` 改为反事实指标 —— 已完成

`evolution/causal_trace.py`：

- 旧指标保留在 `decision_change_rate` 键下，并新增**诚实的别名 `lineage_rate`**
  与 `lineage_note`，明说它测的是"跑在候选提出的版本上"而不是"决定变了"。
- 新增 `counterfactual_changes()`：配对条件是 **同 trader + 同
  `information_cutoff` + 同 code + 不同 `policy_ref`**，逐字段比较
  `action / code / shares / size_pct / entry_low / entry_high / stop_loss /
  target_price`。`reason` 散文**不**参与比较（换句话说不算行为改变）；
  `None` 与 `0` **不**相等（"没设止损"和"止损=0"是两条不同指令）。
- 新增 `counterfactual_change_rate`，**无配对时为 `None` 而不是 0.0**——
  没被测过 ≠ 没有改变。
- 刻意**不**猜谁是 incumbent：`decision_snapshots` 只记录"哪个 policy 做了决定"，
  不记录谁是父版本，按字符串排序猜血缘正是本模块存在的意义所反对的。
- 报告行把「血缘」与「行为改变」分开打印。

测试：`tests/test_causal_trace.py` 新增 `TestTheCounterfactualIsAPairNotALedger`
（9 个用例：同 policy 不算配对、不同日不算配对、intent 相同不算改变、
size 差异算改变、弃权 vs 下单算改变、理由不同不算改变、0≠None）。

### ✅ ② 策略污染检查 —— 已完成（存量入册，T1 prompt 已清理）

`scripts/lint_policy.py` + `scripts/lint_policy_baseline.txt`（14 条存量）
+ `tests/test_policy_contamination.py`（13 个用例）+ CI 新增一步。

- **检查器**扫 `alpha_agents/prompts/` 与 `alpha_agents/tools/`，模式包括
  「阈值=结论」「适合看多」「操作建议」「是原因」「最重要的指标」「…驱动的」
  「大资金看好」。
- **豁免机制是这套规则能落地的前提**：命中句子只要**声明来源**即放行
  （`〔先验〕` / `〔approved〕` / `〔invariant〕`，可写在上一行）。问题从来不是
  「有先验」，而是「有先验却不说」。
- **已实质修复 `t1_decide.md`**：「价格是结果，资金是原因」「A 股是板块驱动的」
  「这是 A 股最重要的情绪指标」三处断言改写为**带 `n=0` 标注的先验**，
  并改掉了"通常/才"这类把未验证规律说成普遍事实的措辞。
- 两处误伤已收窄（`futures.md` 的「情绪驱动」是分类标签；
  `price_levels.py:153` 是计算说明且原文写着"不含任何建议"），
  分别用收紧正则与**逐行豁免+理由**处理，不用整文件豁免。

**未做**：`tools/registry.py` / `institutional_position.py` 的 14 条仍在存量里。
它们需要的是**改输出**（把 `recommendation: 可介入` 换成可核验事实），
属于 ③ 的工具改造范围，不与本步混做。

### ✅ ③ 问题型工具接进 T1 Trader —— 已完成

`alpha_agents/tools/trader_tools.py`（六个工具 + 时间契约）+
`agents/t1_decider.py`（接线）+ `prompts/t1_decide.md`（告诉它可以用）+
`scripts/walk_forward.py`（`--no-trader-tools` / `--max-turns`）。

**六个工具**：`get_market_regime` / `get_theme_state` / `get_stock_context` /
`get_intraday_shape` / `get_stock_memory` / `get_my_state`。
只输出事实，运行时（payload 断言）与静态（`lint_policy.py` 扫描述）双重把关。

**时间契约是硬约束**：回放目录里 `market_snapshots.db` / `market_history.db`
是指向生产库的**符号链接**，库隔离挡不住泄漏；每个工具按
`replay_mode` 的 as-of 截断，`tests/test_trader_tools_time_travel.py`
在两个时钟上驱动每个工具。

**真实运行结果（2026-08-18 起 20 天，record 模式）**：

| | |
|---|---|
| 前 8 条决策的工具调用 | **24 次** |
| 分布 | `get_stock_context` 11、`get_theme_state` 5、`get_market_regime` 3、`get_my_state` 2、`get_intraday_shape` 2、`get_stock_memory` 1 |
| 每轮并行调用 | 2–5 个 |
| 模型自带的 `as_of` | 每次调用都传（如 `2026-08-18 09:00`） |

**六个工具全部被用到**，包括「我认识这只票吗」和「我今天手顺不顺」——
它开始问关于**自己**的问题了。

**这一轮抓到三个真缺陷**（都由真实运行暴露，不是单测）：

1. **契约测试抓到 `get_market_regime` 的日期泄漏**：只按 `captured_at <= cut`
   过滤，于是查询一个没有快照的日期会返回**上一个交易日**的宽度数据，
   还标成 `as_of: 那天`——报告一个从未存在过的市场状态。已限定到当天。
2. **`max_turns` 有两个主人，runner 的那个赢了**：`propose` 声明 8、
   runner 的 `--max-turns` 声明 3，而 runner 总是传自己的值——函数默认值
   不可达。后果：20 天窗口跑 2.5 小时，**40 次决策全部 `MaxTurnsExceeded`**，
   0 成交、收益 +0.000%。现在数字只有一个主人
   （`t1_decider.DEFAULT_MAX_TURNS`），`--max-turns` 默认 `None`
   （= 不覆盖），三个测试钉住它。
3. **`MaxTurnsExceeded` 会杀死整个窗口**：它逃出 `propose`，
   一个想得太多的早晨让 40 个模拟日归零。现在收成"这一天没决策"
   （`decider_unreadable`），与"它选择不下单"区分开。

**修复后的 3 天窗口（2026-08-18 → 08-20）**：

| | |
|---|---|
| 成交 | 买入 2 笔 52,107 元 / 卖出 2 笔 53,298 元（全部 agent 清仓） |
| 已实现 | **+1,079** |
| 区间收益 | +0.108%（等权大盘 −2.839%，超额 +2.947%） |
| `decider_unreadable` | **5/9**（修复前 40/40） |

**但 5/9 这个数字是这一轮最重要的发现**：给它工具之后，模型平均
**每次决策调用 ~23 次工具**（9 次买入决策共 211 次调用），8 轮预算
在**超过一半**的决策里被用满。

**它不是死循环**——26 个有工具调用的决策里，**只有 1 次重复调用**
（同工具同参数），其余 210 次都是不同的问题。它在认真侦察不同标的，
不是空转。但"认真"和"停不下来"在预算用尽这一点上长得一样。

**下一步必须先回答的问题**：这是勤勉还是失控？

- 若是勤勉：8 轮太小，或者工具该返回更聚合的答案（一次问一只票的
  全部状态，而不是让模型自己拼）。
- 若是失控：需要一个"够了"的信号——例如工具结果里带上"你已经查过
  这只票了，现在必须回答"，或者按决策强制一个收敛轮。

**在这件事定下来之前，不该跑 20 天窗口**：2.5 小时一轮，
而一半的决策不产出订单。

### ⏸ ④ RSI golden path —— 机制已补齐，还差最后一步真实跑通

**2026-09-18 更新：路 A 已做完。**

`scripts/rebuild_theme_scores.py` + `theme_score_history` 表 +
`theme_gate._score_as_of`。真实数据验证：

| as-of | 国企改革评分 | 门的裁决 |
|---|---|---|
| 2026-09-08 | 无（历史从该日收盘起算） | 通过（无量） |
| 2026-09-09 | 0.73 | 通过 |
| 2026-09-10 | 0.33 | **拒绝：主线偏弱** |
| 2026-09-14 | 0.18 | **拒绝：主线偏弱** |

**同一个主题在不同日期得到不同裁决**——这正是 `theme_gate.w_rel` 能改变
决定的前提，而在此之前它在回放里恒等于 0 影响。

三个设计决定：

- **按日期存（`theme_score_history`），不覆盖 `theme_lines.trend_score`。**
  后者是单列现值、每周期重写；把历史写进去会让每个回放日都读到最后一个
  重建日的分数——把未来写进历史。
- **只在回放里读历史**；实盘继续读现值列（实盘里现值列就是今天的真相）。
  回放查不到历史行时返回 `None`（门"量不出来就放行"的分支），
  **绝不回落到现值列**——那在回放下就是未来。
- **源数据的边界是硬边界**：`sector_flow_snapshots` 从 **2026-09-08** 起，
  更早的窗口**直接拒绝**而不是拿后一天回填。本该更合适的
  `kpl_concept_daily` / `hm_daily` **都是 0 行**（已核实）。
- **`confirm` 项无源**（生产的"连续确认天数"不在任何快照里），
  按 0 写入并逐行记录，这让重建评分**系统性低于**生产评分——
  回放里的门更严，不会凭空放行。

**还差的一步**：回放必须用**真实主题名**下单（合成主题
`WALK-PLACEHOLDER` 对不上任何板块，这是它评分为 NULL 的根本原因），
然后跑一次 ON/OFF 配对。这是 ④ 的最后一段。

**2026-09-18：主题门在真实回放里第一次生效。**

用 `--theme 国企改革`（真实主题，历史评分已重建）跑 5 天窗口
（2026-09-08 → 09-14），端到端结果：

| | |
|---|---|
| 成交 | 买入 3 笔 80,634 元 / 卖出 3 笔 79,629 元 |
| 已实现 | −1,175 |
| 区间收益 | −0.117%（等权大盘 −3.268%，**超额 +3.151%**） |
| **挂单撤销** | **2 笔（主线明显走弱×2）** ← 关键 |
| `decider_unreadable` | **0**（解析修复后不再出现） |
| 工具调用 | 138 次 / 3 笔买入 ≈ **46 次/笔** |
| 模型调用 | 65 条记录 |

**「主线明显走弱×2」这一行在此之前恒为 0。** 合成主题没有评分，门按
设计放行；现在门读重建的历史评分，在 09-10 之后（评分 0.33→0.28→0.18，
低于 admit_score 0.5）**真实拒绝了订单**。这是主题门第一次在回放里
参与决策，也就是 `theme_gate.w_rel` 第一次具备改变决定的能力。

**④ 剩余的最后一步**：跑 ON/OFF 配对，让
`counterfactual_changes()` 产出 `pairs > 0`，从而得到
`counterfactual_change_rate > 0`。机制全部就位，只差这一步执行。

**2026-09-18：执行时发现两个必须补的结构缺口。**

在带主题门的 5 天窗口上直接问反事实，得到：

```
decision_snapshots: 5   pairs: 0   unpaired: 5
counterfactual_change_rate: None
```

两个原因，都已定位到行：

**缺口 1：门的拒绝不落决策快照。**
主题门在 `portfolio.create_pending_order` 里 `theme_admits()` 返回非空时
**`return None`**（`data/portfolio.py:389-394`），发生在
`attribution.freeze()` **之前**。所以"被门拦下的决定"在
`decision_snapshots` 里**没有行**。

而 ON/OFF 的差异**恰好落在这一侧**：OFF 组会下单、ON 组被拦。
`counterfactual_changes()` 的配对要求两侧都有快照，于是把这种差异
计成 `pairs=0` 而不是"一次被门改变的决定"。

这正是评审 §8.6 说的「**不得只比两边都买过的票的交集**——那正好把两个
交易员不同的选择丢掉，而不同的选择才是实验的内容」。**分母必须包含
只被一边选中的机会。**

修法（下一步）：门拒绝时也写一条决策记录，标记为
`refused_by=theme_gate`，并带上 `policy_ref`。这样配对能看见
"同一 (trader, cutoff, code) 下，一侧下单、另一侧被拦"，
这正是 `changed=True`。
**不能**只把 cancel 路径的 `close_reason` 当证据——那是订单被撤，
与"建单被拒"是两件事（本窗口 2 笔是 `cancel_score` 撤单，
不是 `admit_score` 拒单）。

**缺口 2：回放的决策快照 `policy_ref` 全是 NULL。**
5 条快照的 `policy_ref` 都是 NULL（回放没把在效版本记进去），
所以 `counterfactual_changes()` 按 `policy_ref` 分组时只有一组，
连"两个不同 policy"这个前提都不成立。

修法（下一步）：回放在写决策快照时带上它在效的 policy 版本
（`policy_registry.active_ref()`，或 OFF 臂显式的"无"标记），
否则 ON/OFF 两臂在账本上无法区分。

**这两处补完，④ 才真的能跑出 `pairs > 0`。** 在那之前
`counterfactual_change_rate` 保持 `None`（未定义），而不是 0——
"没测过"与"没有改变"必须继续分开。

**2026-09-18：两个缺口都补完了，`pairs > 0` 第一次跑出来——但结论是"不可归因"。**

三个改动：

1. `attribution.record_refusal()`：门拒绝时写一条
   `payload.action = "refuse"` 的决策快照，与下单共用同一个 `terms` dict
   （两者是同一个 intent 的不同 action）。弃权仍不写——"被门拦下"与
   "没找到值得买的"是两件事。
2. `scripts/replay_policy.py`：每臂装**一个**版本。OFF 装基线
   （`w_rel=0.35`），ON 从 candidate #4 构建 variant 并装它
   （`w_rel=0.40`）。已验证两臂 `policy_ref` 不同：
   `trader#1@7609...` vs `trader#2@c230...`。
3. `counterfactual_changes(other_conn=...)`：**跨账本**配对（设计明确
   两臂账本独立），并把只被一边行动的机会报为 `one_sided`。

实测（2026-09-08 起 4 天，两臂）：

| | OFF | ON |
|---|---|---|
| 区间收益 | −0.338% | +0.058% |
| 超额 | +3.439% | +3.835% |
| 买入 | 4 笔 | 3 笔 |
| 挂单撤销 | 1（主线走弱×1） | 3（主线走弱×2、价格涨走×1） |
| 工具调用 | 73（18.2/笔） | 95（31.7/笔） |

配对结果：**`pairs=3, changed=3, one_sided=12, unpaired=0`**。

**但这个数字不能归因给 `w_rel`，原因是我的实验设计有缺陷：**

- **两臂的门评分完全相同**（逐日 0.7295 / 0.3252 / 0.2833）——重建脚本
  对两臂跑同一份历史，所以 `w_rel` 0.35→0.40 在这批裁决上没有产生差异。
- 3 个「changed」是**同一 prompt 下两次独立采样的差异**（601126 目标价
  48.5 vs 49.0；600256 区间 7.15-7.55 vs 6.95-7.30），量级是模型噪声。
- 12 个 `one_sided` 同理：两臂各调一次模型，选中的票不同。

**我控制了世界状态，但没控制模型交换**——正是评审 §5 点名的
"WorldSnapshot identical, **Model exchange controlled**"。
所以这一轮证明的是**基础设施成立**（两臂可区分、拒绝可记录、
跨账本可配对、单边机会进分母），**不是**"学习改变了行为"。

**下一步（④ 的真正收尾）**：让两臂**共用同一份模型录制**——
`ALPHAAGENTS_LLM_MODE=replay-recorded` 读同一个 journal，
这样模型交换被完全控制，两臂的唯一差异只剩 policy。
那时候 `counterfactual_change_rate` 才是关于 policy 的数字。

**2026-09-18 续：共享录制试过了，不可行，而且原因是根本性的。**

先查了一个关键事实：两臂**第一次**调用的 `request_hash` **完全相同**
（`sha256:589962e9...`，31,464 字节）——说明 `w_rel` 只影响下单前的
准入判定，**不进入提示词**。所以第一轮本来是可控的。

但 journal 逐条对比：**39 条里只有第 1 条相同，第 2 条起全部分歧**。
原因不是 policy，是**级联**：

```
第 1 次采样不同（模型噪声）
   ↓
两臂买进不同的票
   ↓
第 2 次的【我的账本】块不同
   ↓
prompt 不同 → request_hash 不同 → 录制不可复用
```

而 `llm_journal` 的 `replay-recorded` 会**校验 `request_hash`**，
不一致时抛 `ReplayDivergence` 而不是硬塞一个答案——这个守卫是对的，
它挡住的是"用一个问题的答案回答另一个问题"。

**所以"控制模型交换"在整窗口尺度上做不到**：模型第一次采样就改变了
世界状态，之后两臂面对的不是同一个世界了。这正是评审 §5 那句话的
真正难点，只是它没有说透。

**可行的收尾只有两条，都需要你选：**

- **路 1：单步反事实。** 只对**决策那一刻**做 ON/OFF：同一份
  WorldSnapshot（同账本、同面板、同新闻、同录制），只换 policy，
  跑一次 `theme_admits` 对比结果。这能给出干净的
  `counterfactual_change_rate`，但测的是**准入判定**，
  不是完整交易行为。
- **路 2：多采样。** 两臂各跑 N 次、把差异当作分布而不是单个数字，
  用配对统计判断 policy 的作用是否超过采样噪声。
  这才是"比收益"那类指标的正当做法，但成本是 N 倍，且需要
  预先声明 N（n<20 不构成证据，见 §7）。

**我的建议是路 1**：它能在不增加成本的前提下，把
`counterfactual_change_rate` 变成一个**关于 policy 的、可复算的数字**，
而不是关于模型噪声的。路 2 是后续想量化收益差时再做的事。

**2026-09-18 续二：路 1 做了，结论是量化的，而且很重要。**

`counterfactual_changes.single_step_counterfactual()`：固定世界
（同板块、同日、同评分、同一 frame），只换参数。差异**只能**来自参数。

对窗口内 5 天 × 4 个主题逐日跑 `w_rel: 0.35 → 0.40`：

| 日期 | 主题 | 评分 | 距 admit_score(0.5) | 权重位移 Δ | 翻转？ |
|---|---|---|---|---|---|
| 09-08 | 锂电池概念 | 0.5445 | **−0.0445**（在阈值上方） | 0.0173 | ❌ |
| 09-09 | 锂电池概念 | 0.5619 | **−0.0619** | 0.0369 | ❌ |
| 09-08 | 国企改革 | 0.7295 | −0.2295 | 0.0400 | ❌ |
| 09-09 | 国企改革 | 0.3252 | +0.1748 | 0.0388 | ❌ |
| 09-11 | 金属铜 | 0.0408 | +0.4592 | 0.0010 | ❌ |

**20 个 (日, 主题) 组合里一个都没翻转**，因为**每一个的"距阈值"
都大于权重变化能带来的最大位移**（Δ = 0.05 × rel_pct，上限 0.05）。

**这解释了前面那个 100% 是怎么来的**：那 3 个 `changed` 与 `w_rel`
无关，是模型采样噪声。而现在有了一个**可复算的数字**：
在这个窗口里，`theme_gate.w_rel` 唯一的基因位点**无法改变任何准入决定**。

这不是坏消息，是**第一个关于 policy 的真实测量**，而且它顺带解释了
评审的另一个观察——**变体只有一个基因位点、且位点太窄**：

- 位移上限是 0.05（权重步长），而实际评分离阈值动辄 0.17–0.46；
- 要让这个基因位点起作用，需要**评分落在阈值附近**的场景才能观察到；
- 换句话说：**当前这个变异维度在这类窗口里是惰性的**。

**下一步（有数据支撑的）**：要么加大权重步长（让位点有实际作用域），
要么引入振幅更大的变异维度（评审 §4 说的"只进化一个基因位点"），
要么专门构造评分贴近阈值的窗口来检验它。三者都需要你定方向。

### 🔴 这一轮又抓到两个真缺陷（都是我自己引入的）

1. **`max_turns` 有两个主人，runner 的赢了**（见上）——已修。
2. **模型先写散文再给 JSON，被当成"读不出"**：10 天窗口的 24 轮运行里，
   20 次买入决策有 **14 次**被计为 `decider_unreadable`。它们**不是空的、
   也不是坏的**——每一条正文里都有合法的 `{"orders": [...]}`，只是出现在
   模型的推理之后（"Let me finalize my decision… Rationale summary:"）。
   `_strip_fence` 只在**整条回复以 ``` 开头**时才剥围栏，而工具让模型开始
   解释自己之后，围栏不再在开头，答案就被丢掉了。
   修复后对同一批录制回复重测：**14 → 0**。
   测试同时钉住"完全无 JSON 的散文仍算读不出"——否则这个计数器
   就不再能抓到下一个同类 bug。

**连续两轮的同一个模式**：失败的不是模型没做决定，而是系统读不懂
已经做出的决定。
