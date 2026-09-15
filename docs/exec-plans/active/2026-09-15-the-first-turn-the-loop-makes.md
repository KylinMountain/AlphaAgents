# The first turn the loop makes

状态：active
创建：2026-09-15

## 目标

让闭环转第一圈真实证据。四件事；**第 1、2 步已于 2026-09-15 完成并落地**（见决策记录与验收），
剩下第 3、4 步：

1. **定样本门槛（D15）—— 已定 = 20，已收口。** `GOLDEN_PRINCIPLES` §7、`TRADER_CORE_DESIGN` §12、
   `README` 三处原先声明 `n < 50`，而代码只执法 20，于是 n 落在 20–49 的裁决
   **同时满足闸门与在效版本、却违反 §7**，没有任何东西拦它。**收口方向是「把规则降到 20 去就代码」**，
   不是把代码抬到 50 —— 这一句是重点：`MIN_VALIDATION_SAMPLES` 在
   `policy_sources._RULE_SOURCES` 里，抬它会让**所有已冻结版本立刻变 drifted**；
   而 `GOVERNANCE_MIN_SAMPLES` **不在**那个元组里，改它不动任何交易行为。
2. **停止规则口径统一（D16）。** `pipeline/tasks/shadow_run.py` 拿 `remaining`（**配对样本数**）
   与 `verdict["validation_days"]`（**天数**）比同一个 `needed`，于是实验在样本上早就过关、
   闸门**每天**被问一次 —— 正是该模块 docstring 说它写出来要避免的形态。口径跟第 1 步走
   （单位 = 配对样本），所以是**配对和配对比**。
3. **把正在跑的配置冻结成版本（D17）。** 主题门（`d927a0b`）往
   `scoring.DEFAULT_DECISION_PARAMS` 加了 `theme_gate`，而这一块被决策指纹覆盖、
   `decision_params_of` 又把默认值**合并在版本自身值之下** —— 于是在效版本 #1 的声明
   已经不等于交易路径正在读的配置。冻结出 #2，把运行中的配置入册。
4. **开第一个可晋升的影子实验。** `shadow_runs` 现在只有 #1，`producer='constant_0.5'`
   ——那就是无技能 baseline，**结构上不可晋升**。开 #2，producer 用 `remap_confidence`。
   **`--report-type` 尚未决定**，见决策记录的待定项。

**这一步之后为真的：** 仓库声明的门槛与代码执法的门槛是**同一个数**；运行中的配置**上了记录**；
一个可晋升的实验开始累积配对样本，且停止规则只在一处判断；「第一次晋升」从**结构上不可能**
变成**只差样本**。

**这一步之后仍不为真的（别读成已交付）：**
- **没有任何版本被晋升。**
- **在效指针仍在 #1** —— `freeze` 不动指针，所以 `policy.py status` 的
  `live configuration still matches` **仍然是 `False`**。这是**预期**，不是失败。
- **#2 与 #1 行为等价**：它只是把默认值写下来（`theme_gate` 的五个值与
  `DEFAULT_DECISION_PARAMS` 逐值相同）。所以这一圈证明的是**管道**，不是策略改进。
- **门槛 20 是「降低自我要求」，不是「达标」。** 20 个配对样本是个弱依据，
  文档里已如实写出这一代价（§7 / §12 / README 三处都有这一句），别把它读成一项成就。

## 验收

- [x] **D15（2026-09-15 完成）**：`GOVERNANCE_MIN_SAMPLES == MIN_VALIDATION_SAMPLES == 20`；
      `GOLDEN_PRINCIPLES` §7、`TRADER_CORE_DESIGN` §12、`README` 三处都写 20；
      `promotion_floor_gap(MIN_VALIDATION_SAMPLES - 10, when=…)` 返回 1 条，
      `promotion_floor_gap(20)` 返回 `[]`；`tests/test_holdout_gate.py` 全绿。
- [x] **D15**：`MIN_VALIDATION_SAMPLES` **未被改动** ⇒ `policy_registry.drifted()` 仍为空，
      没有任何已冻结版本因这次收口漂移。
- [x] **D16（2026-09-15 完成）**：`_ask_the_gate` 的 `asked_already` 与 `_position_line`
      都比 `verdict["n"]`（配对样本），不再比 `validation_days`；模块 docstring 同步改写。
      原「断言缺陷」的用例按它自己 docstring 的指示翻面为
      `test_a_verdict_that_reaches_the_sample_bar_ends_the_asking`；两个原本依赖混用单位的
      兄弟用例改为显式写出 `n`。**变异探针取证**：把比较改回 `validation_days` →
      `test_shadow_run_task.py` **恰好两条变红**，复原后 16 passed。
- [ ] **D17**：`freeze --dry-run` 先打印 diff，再真跑 → `status` 的版本数 **1 → 2**。
- [ ] **D17**：`policy_sources.verify_live(2) is True` 且 `verify_live(1) is False`。
- [ ] **D17**：`scoring.in_force_decision_params()["theme_gate"]` 与 #2 声明的整块逐值相等
      （`w_flow 0.45 / w_rel 0.35 / w_confirm 0.20 / admit_score 0.50 / cancel_score 0.35`）。
- [ ] **D17**：`status` 的 `live configuration still matches` **仍为 `False`** ——
      这一条断言的是「**指针没动**」，不是「修好了」。
- [ ] **实验**：`shadow_runs` 有 2 条；#1 仍是 `constant_0.5`，#2 是
      `producer='remap_confidence'`、`status='open'`、`policy_version_id=2`。
- [ ] **实验**：`shadow.scope_for("remap_confidence") == policy_registry.SCOPE_CANDIDATE`，
      且 `scope_for("constant_0.5")` 仍是不晋升的那个 scope。
- [ ] **实验**：次日调度之后，`status` 的实验进度**不再停在 `0/20`**。
- [ ] `scripts/lint_harness.py` 与 `scripts/lint_docs.py` 通过；
      `scripts/lint_baseline.txt` **未新增行**。

## 决策记录

- **第 1 步的决定：门槛 = 20（2026-09-15，操作者）。** 被拒的选项是抬代码到 50。
  拒绝的理由具体而非偏好：`MIN_VALIDATION_SAMPLES` 是行为来源，抬它会把**每一个已冻结版本**
  判为 drifted，还会顺带抹掉 `rollback` 的可选目标（drifted 的版本不能被回滚到）。
  降 `GOVERNANCE_MIN_SAMPLES` 到 20 两样代价都没有。**买到的**是「文档说的和代码做的一致」；
  **卖掉的**是「门槛至少 50」这条自我要求 —— 已经在 §7 里写明，不许含糊。
- **由此，原定的顺序约束解除。** 本计划初版写的是「必须先定门槛、再冻结 #2」，
  理由是「先冻 #2 再抬门槛 ⇒ #2 自证漂移」。既然最终没有动 `MIN_VALIDATION_SAMPLES`，
  第 3 步与第 1 步**互相独立**，冻结 #2 不再需要等任何人。
- **拒绝**：在 #1 上开实验 —— #1 的声明 ≠ 正在跑的配置（D17），那等于测量一个虚构版本。
- **为什么 #2 应当是行为等价的**：让「第一次晋升」是**不可能伤到自己的**那一圈，
  先把 emit → score → pairing → gate → approve → promote 的管道走通。
  真正的候选（改一个决策参数）留给 #3。
- **未做**：`docs/exec-plans/active/` 此前为空，本计划是第一份。
- **待定项（2026-09-15，为写验收常测算样本速度时发现）：第 4 步的 `--report-type`
  不能想当然用默认的 `morning`。** `shadow.paired_count()` 只数「**已经打完分**的
  `(date, code)` 配对」，且**按 run 自己的 `report_type` 过滤** ——
  而 `morning` 这条路径**自 2026-09-10 起没有任何产出**：`predictions` 与 `theses` 里
  最后一条 morning 记录停在 09-10，09-11 起只有 `intraday` / `intraday_signal`。
  在 `morning` 上开实验会**长期停在 `0/needed`**，原因不是门槛，是**没有东西可配对**。
  备选是 `intraday_signal`（历史 29–53 条/日）。**这是「机会面板」的定义（§12 要求预注册），
  不是实现细节，必须由人定。**
- **`needed` 与门槛是同一个常量**：`shadow.py:768` 写 `needed = holdout_gate.MIN_VALIDATION_SAMPLES`
  ⇒ 门槛**直接就是实验进度条的分母**。这也是「门槛定 20 还是 50」之所以牵动工期的地方。
- **时间下限由窗口长度决定，不由门槛决定。** 每笔预测要 `DEFAULT_HORIZON_DAYS = 5` 个交易日
  **+1 根 bar** 才收口（`need = horizon + 1`），而样本要求双方 `brier IS NOT NULL`。
  所以第 0 天发出的预测，要到**第 6 个交易日**才一起变成样本 ⇒
  **「攒够样本」的下限 ≈ 6–7 个交易日**，与门槛是 20 还是 50 基本无关。
  门槛与账本决定的是**第 6 天之后还要几天**：快账本（`intraday_signal` ~40 条/日）第 7 天就够；
  慢账本（`morning` ~2.7 条/日）约第 13 天（门槛 20）或第 24 天（门槛 50）。
- **「历史不能用来晋升」是结构强制的，不是规矩。** `paired_count` 的 SQL 里有
  `AND s.date > <version.frozen_at>`（`shadow.py:725`）：样本必须**晚于版本冻结日**。
  版本今天才冻结 ⇒ 冻结之前的任何一天都**永远不可能满足这个条件**。
  用历史晋升 = 把冻结日改到过去 = 伪造一条「我当时就冻结了」的声明，正是设计 §2 禁止的事。
  设计 §12 原话：*"Historical replay is development evidence, and forward paper trading is
  necessary but still not proof of executable real-market returns."* 历史能当**开发证据**
  （重放、账目对账、机制验证 —— 仓库里 `scripts/replay_*.py` 就是干这个的），
  **不能给策略颁发晋升证据**。
- **当前的 `shadow_runs` #1 在结构上到不了 20。** 它的 `report_type='morning'`，而 morning 自
  09-10 起零产出 ⇒ `paired_count` 恒为 0。它本来就不可晋升（baseline），现在连「跑着」都不算，
  只是一条占着编号的记录。**开 #2 之前不必先关 #1**（`shadow-open` 会拒绝同一
  `(policy_version_id, report_type, producer)` 的重复 open，但 #2 是不同的 producer + 不同版本）。

## 风险

- ~~抬门槛会让 #1 变成 drifted ⇒ 回滚目标暂时消失~~ —— **已解除，代价未付**。
  这条风险是为「抬代码到 50」定价的；最终选了降规则，`MIN_VALIDATION_SAMPLES` 一个字节都没动，
  所以 `drifted()` 仍为空，`rollback` 的可选目标不受影响。留在记录里是因为它解释了
  为什么当初把「先定门槛」排在第 3 步之前 —— 那个顺序约束现在也一并解除了。
- **门槛降到 20 意味着首次晋升建立在弱证据上。** 20 个配对样本不足以支撑一个自信的结论，
  这是这个决定的已知代价，文档三处都写明了。**信号**：第一次晋升之后，
  想要更硬的结论只能靠后续的候选实验，不能靠把 20 重新解释成 50。
- **首圈实验的样本口径改过（D16）⇒ 历史裁决的 `validation_days` 与新口径不可比。**
  现在裁决表中只有 0 条，所以代价为零。**信号**：`status` 里出现早于本计划的裁决行时，
  先确认它写在哪一种口径下，别混着算。
- **`status` 本身不是纯只读。** 读模型与读函数进入即 `init_schema`。按仓库惯例，
  对公开库的验证**要么跑副本、要么接受「打开即一次写」并先备份**。
- **`gate` 故意没有 `--dry-run`**（设计如此）；`freeze` / `install` / `approve` / `promote` /
  `rollback` 的 `--by` 与 `--reason` 均为必填。`shadow-open` 有 `--dry-run`，先用它。
- **可证伪的观察点**：第一批非空 `brier` 应出现在 **2026-09-16 的复盘**（D18）。
  若 09-16 之后 `predictions.brier` 仍是 0 行，说明 D18 的分析错了，回到 D18 重查，
  不要继续往实验上堆东西。
