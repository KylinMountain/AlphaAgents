# Trader Runtime 收尾：行情打分的学习闭环与 T9

状态：active
创建：2026-09-26
上游：[Trader Runtime](2026-09-25-trader-runtime.md) · [finalization](2026-09-25-trader-runtime-finalization.md) · [无止损止盈](2026-09-26-no-system-exit-lines.md)

## 为什么

用户 2026-09-26："继续推进这个完整的 Trader Runtime 设计，直到所有的 T 都完成。"

T1–T8 的管道已接通（T3/T4 生产侧 Morning 与 Intraday 已不直接下单，经 Runtime 决策；
T5 持仓由交易员决定且已无任何价位出场）。但学习闭环 T6/T7 在实测中不成立：

1. **自评**：`trader_review` 让复盘模型给同一交易员的决策打 `decision_quality`，并自报
   LessonCandidate 的 `support_count` / `counterexample_count` / `confidence`。违反
   AGENTS.md 不变量 2。30 日实测 326 条复盘 319 条 `good`、0 条 `bad`。
2. **永不聚合**：`trader_learning._group_candidates` 以 claim 原文精确匹配分组；模型措辞
   每次不同，30 日 343 条候选、0 条 Lesson、0 条 Rule。阻塞的不是 n 门槛，是分组键。
3. **T9 未开始**：没有任何证据表明一条 Lesson/Rule 会改变交易员的行为。
4. 报告看不见无止损后的风险暴露。

## 交付

- **C1 风险可见**：回放报告写出窗口内最大单票浮亏、期末持有中的亏损仓数及最深一只，
  以及 agent 被询问后仍持有的浮亏 ≤ −8% 的仓位-日数。只记录，不平仓。
- **C2 决策由行情打分**：新模块对每个封存的 TraderDecision，在其 horizon（默认 5 个交易日）
  闭合后，用日线计算该标的前瞻收益减同期全市场**中位**收益（median-to-median）。
  BUY/ADD/HOLD：超额 > 0 为 right；SELL/REDUCE/REJECT/WAIT：超额 < 0 为 right；
  |超额| < 0.5pp 为 flat。只读 as-of 之前已闭合的数据。复盘模型不再输出任何质量评级，
  只写理由与候选经验文字。
- **C3 候选经验的证据来自行情**：候选经验必须引用一个受控词表中的**情形标签**
  （由代码从决策当时可见的面板字段计算，如 `t1_change>=7`、`theme_flow<0`、`gap_up_open`、
  `no_gain_since_entry`）加一个动作。分组键 = (动作, 标签, timeframe, horizon, scope)，
  与措辞无关。support / counter 不由模型填写，而是由 C2 对该组所有**同标签同动作**决策
  的 right/wrong 计数得出；confidence = right / (right + wrong)。
- **C4 T9 对照**：用 `walk_checkpoint` 封存一个前缀，`walk_branch` 开两支：对照组与注入
  一条已批准 Rule 的实验组（人工批准，走 `trader_rules` 事件，不是模型自批）。报告两支
  在同一持续 TraderState 起点下的决策差异（动作分布、被 Rule 条件命中的决策数、是否
  出现相反动作）。
- **C5 文档**：runtime 计划的 T3–T9 勾选与实际一致；IMPLEMENTATION 记录实测。

## 验收（机器可检查）

- [x] C1：报告含上述三项；单元测试以构造账户验证数值。
- [x] C2：单元测试覆盖六种动作 × right/wrong/flat；未闭合 horizon 不打分；只读 as-of 之前
  的 bar（构造一根未来 bar 并断言不影响结果）；比较基准是市场中位数而非均值。
- [x] C2：`trader_review` 的 schema 与提示词不再含 `decision_quality` / `outcome_quality` /
  `support_count` / `counterexample_count` / `confidence`；测试断言模型写的这些字段被忽略。
- [x] C3：两条措辞不同、标签与动作相同的候选合并为一组；support/counter 等于 C2 计数；
  模型自报的计数不影响结果（测试）。
- [x] C3：用 30 日回放 `t8-20260105-30d-final3` 的已录决策离线重算，报告 right/wrong 在各
  动作上的分布（不再是 98% good），以及有多少组达到 Lesson 门槛。不花 token。
  实测（2026-09-26，as_of 2026-02-13，335 个决策中 291 个窗口闭合）：

  | 动作 | right | wrong | flat | 判对率 | 中位超额 |
  |---|---|---|---|---|---|
  | buy | 18 | 30 | 0 | 38% | −3.07pp |
  | reject | 107 | 53 | 6 | 67% | −4.47pp |
  | wait | 42 | 34 | 1 | 55% | −1.38pp |

  18 个格子中 14 个 n≥10；生成 9 条 Lesson、3 条 Rule（reject·run_up_5d 81/109、
  reject·off_5d_high 65/99、wait·run_up_5d 32/51）。旧自评为 319/326 good。
  **限定**：同一窗口内的决策共享交易日、持有期重叠，n 不是独立样本数；这是对该窗口
  的描述，不是前向验证。Rule 在之后的决策中生效，其效果由 C4 与之后的前向窗口检验。
- [ ] C4：两支分支各 ≥3 个交易日退出 0；注入分支中至少一条决策的上下文含该 Rule，
  且报告两支的动作差异数（可以为 0——为 0 是结论，不是失败）。
- [ ] 全量测试、`lint_harness`、`lint_docs` 通过。

## 决策记录

- 2026-09-26：情形标签由代码从决策当时的面板计算，而不是让模型描述"适用场景"。模型描述
  的场景无法被代码在其他决策上复核，因此既不能聚合也不能被行情检验。
- 2026-09-26：行情打分用市场中位数作基准（AGENTS.md 证据规则：A 股截面右偏）。
- 2026-09-26：T9 的 Rule 由人工批准注入。设计 §2 不变量 6 禁止 LLM 自批；Rule 的自动
  晋升仍由 C3 的行情计数门槛决定，T9 只回答"一条 Rule 是否改变行为"。
- 2026-09-26：T9 显示 Rule 以文字注入后行为几乎不变。用户决定"让 agent 自己学"：
  不加代码拦截、不做逐候选的 Rule 标注或强制回应。Rule 只作为带行情计数的证据出现在
  上下文里，采纳与否由交易员决定，其后果仍由行情打分记录。
- 2026-09-26：回放的生产库判定从"文件 hash 未变"改为"本进程未以可写方式打开生产库"
  （sqlite3 审计钩子）。本机实盘调度整天写这个文件，hash 只作诊断保留在报告里。
