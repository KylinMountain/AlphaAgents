# The first turn the loop makes

状态：active
创建：2026-09-15

## 目标

让闭环转第一圈真实证据。四件事，**顺序不可颠倒**：

1. **定样本门槛（D15）。** `GOLDEN_PRINCIPLES` §7、`TRADER_CORE_DESIGN` §12、`README` 三处声明
   `n < 50` 不上线；代码里只有 `holdout_gate.MIN_VALIDATION_SAMPLES = 20`，且晋升重检读的是
   **冻结版本自己声明的**那个数 —— 于是 n 落在 20–49 的裁决**同时满足闸门与在效版本、却违反 §7**，
   没有任何东西拦它。定下 50。
2. **停止规则口径统一（D16）。** `pipeline/tasks/shadow_run.py` 拿 `remaining`（**配对样本数**）
   与 `verdict["validation_days"]`（**天数**）比同一个 `needed`，于是实验在样本上早就过关、
   闸门**每天**被问一次 —— 正是该模块 docstring 说它写出来要避免的形态。口径跟第 1 步走。
3. **把正在跑的配置冻结成版本（D17）。** 主题门（`d927a0b`）往
   `scoring.DEFAULT_DECISION_PARAMS` 加了 `theme_gate`，而这一块被决策指纹覆盖、
   `decision_params_of` 又把默认值**合并在版本自身值之下** —— 于是在效版本 #1 的声明
   已经不等于交易路径正在读的配置。冻结出 #2，把运行中的配置入册。
4. **开第一个可晋升的影子实验。** `shadow_runs` 现在只有 #1，`producer='constant_0.5'`
   ——那就是无技能 baseline，**结构上不可晋升**。开 #2，producer 用 `remap_confidence`。

**这一步之后为真的：** 运行中的配置**上了记录**；一个可晋升的实验开始累积配对样本，
且停止规则只在一处判断；「第一次晋升」从**结构上不可能**变成**只差样本**。

**这一步之后仍不为真的（别读成已交付）：**
- **没有任何版本被晋升。**
- **在效指针仍在 #1** —— `freeze` 不动指针，所以 `policy.py status` 的
  `live configuration still matches` **仍然是 `False`**。这是**预期**，不是失败。
- **#2 与 #1 行为等价**：它只是把默认值写下来（`theme_gate` 的五个值与
  `DEFAULT_DECISION_PARAMS` 逐值相同）。所以这一圈证明的是**管道**，不是策略改进。

## 验收

- [ ] **D15**：`holdout_gate.MIN_VALIDATION_SAMPLES == 50` 且 `== GOVERNANCE_MIN_SAMPLES`。
- [ ] **D15**：`promotion_floor_gap(50, when="version #2") == []`；
      `promotion_floor_gap(20, when="version #1")` 仍返回 1 条。
- [ ] **D15**：`tests/test_holdout_gate.py::TestTheDeclaredFloorAgainstTheRepositorysRule` 全绿，
      其中 `test_a_floor_below_the_rule_is_a_complaint` **改为传字面量 `20`** ——
      它现在传的是常量本身，常量为 50 时该用例会自相矛盾（「声明 50 却低于规则 50」）。
- [ ] **D16**：`tests/test_shadow_run_task.py::test_the_stopping_rule_asks_again_while_the_two_units_disagree`
      **翻面**为断言修复后的行为（`n` 与 `validation_days` 不一致时闸门只被问一次）；
      另留一条**变异探针**证据：把单位改回混用，该用例变红。
- [ ] **D17**：`scripts/policy.py shadow-open --dry-run` 之外，
      `freeze --dry-run` 先打印 diff，再真跑 → `status` 的版本数 **1 → 2**。
- [ ] **D17**：`policy_sources.verify_live(2) is True` 且 `verify_live(1) is False`。
- [ ] **D17**：`scoring.in_force_decision_params()["theme_gate"]` 与 #2 声明的整块逐值相等
      （`w_flow 0.45 / w_rel 0.35 / w_confirm 0.20 / admit_score 0.50 / cancel_score 0.35`）。
- [ ] **D17**：`status` 的 `live configuration still matches` **仍为 `False`** ——
      这一条断言的是「**指针没动**」，不是「修好了」。
- [ ] **实验**：`shadow_runs` 有 2 条；#1 仍是 `constant_0.5`，#2 是
      `producer='remap_confidence'`、`status='open'`、`policy_version_id=2`。
- [ ] **实验**：`shadow.scope_for("remap_confidence") == policy_registry.SCOPE_CANDIDATE`，
      且 `scope_for("constant_0.5")` 仍是不晋升的那个 scope。
- [ ] **实验**：次日 15:45 调度之后，`status` 的实验进度**不再停在 `0/20`**。
- [ ] `scripts/lint_harness.py` 与 `scripts/lint_docs.py` 通过；
      `scripts/lint_baseline.txt` **未新增行**。

## 决策记录

- **顺序：先 D15，再 D16 与 D17，最后开实验 —— 这是依赖不是偏好。**
  `MIN_VALIDATION_SAMPLES` 在 `policy_sources._RULE_SOURCES` 里 ⇒ **改它会把所有已冻结版本
  判为 drifted**。若先冻结 #2 再抬门槛，#2 会**立刻自证漂移**，比现状更糟。
  所以先抬门槛、再冻 #2，让 #2 把 50 记进自己的 `rules` 指纹。
- **拒绝**：把门槛留在 20（保留一个「文档说 50、代码放行 20」的无人区，比门槛数值本身更贵）。
- **拒绝**：在 #1 上开实验 —— #1 的声明 ≠ 正在跑的配置（D17），那等于测量一个虚构版本。
- **拒绝**：先冻 #2 再抬门槛（同上，理由更硬）。
- **为什么 #2 应当是行为等价的**：让「第一次晋升」是**不可能伤到自己的**那一圈，
  先把 emit → score → pairing → gate → approve → promote 的管道走通。
  真正的候选（改一个决策参数）留给 #3。
- **为什么是现在**：当前在效版本 1 个、晋升 0 次、裁决 0 条、样本 0/20 ——
  抬门槛的代价**单调上升**，这是它最便宜的时刻。
- **未做**：`docs/exec-plans/active/` 此前为空，本计划是第一份。
- **新增待定项（2026-09-15 下午，为写验收常测算样本速度时发现）：第 4 步的 `--report-type`
  不能想当然用默认的 `morning`。** `shadow.paired_count()` 只数「**已经打完分**的
  `(date, code)` 配对」，且**按 run 自己的 `report_type` 过滤**（`shadow.py:703`）——
  而 `morning` 这条路径**自 2026-09-10 起没有任何产出**：`predictions` 与 `theses` 里
  最后一条 morning 记录停在 09-10，09-11 起只有 `intraday` / `intraday_signal`。
  在 `morning` 上开实验会**长期停在 `0/needed`**，原因不是门槛，是**没有东西可配对**。
  备选是 `intraday_signal`（历史 29–53 条/日）。**这是「机会面板」的定义（§12 要求预注册），
  不是实现细节，必须由人定。**
- **`needed` 与门槛是同一个常量**：`shadow.py:768` 写 `needed = holdout_gate.MIN_VALIDATION_SAMPLES`
  ⇒ D15 的门槛**直接就是实验进度条的分母**。抬到 50 = 把首圈实验拉长约 2.5 倍
  （按早盘历史速度：20 条约 8 个交易日、50 条约 19 个；两者都还要再加上每笔等自己窗口收口的时间，
  因为只有**打完分**的配对才计入）。
- **「历史不能用来晋升」是结构强制的，不是规矩。** `paired_count` 的 SQL 里有
  `AND s.date > <version.frozen_at>`（`shadow.py:725`）：样本必须**晚于版本冻结日**。
  版本今天才冻结 ⇒ 冻结之前的任何一天都**永远不可能满足这个条件**。
  用历史晋升 = 把冻结日改到过去 = 伪造一条「我当时就冻结了」的声明，正是设计 §2 禁止的事。
  设计 §12 原话：*"Historical replay is development evidence, and forward paper trading is
  necessary but still not proof of executable real-market returns."* 历史能当**开发证据**
  （重放、账目对账、机制验证 —— 仓库里 `scripts/replay_*.py` 就是干这个的），
  **不能给策略颁发晋升证据**。
- **时间下限由窗口长度决定，不由门槛决定。** 每笔预测要 `DEFAULT_HORIZON_DAYS = 5` 个交易日
  **+1 根 bar** 才收口（`need = horizon + 1`），而样本要求双方 `brier IS NOT NULL`。
  所以第 0 天发出的预测，要到**第 6 个交易日**才一起变成样本 ⇒
  **「攒够样本」的下限 ≈ 6–7 个交易日**，与门槛是 20 还是 50 基本无关。
  门槛与账本决定的是**第 6 天之后还要几天**：快账本（`intraday_signal` ~40 条/日）第 7 天就够；
  慢账本（`morning` ~2.7 条/日）约第 13 天（门槛 20）或第 24 天（门槛 50）。
- **当前的 `shadow_runs` #1 在结构上到不了 20。** 它的 `report_type='morning'`，而 morning 自
  09-10 起零产出 ⇒ `paired_count` 恒为 0。它本来就不可晋升（baseline），现在连「跑着」都不算，
  只是一条占着编号的记录。**开 #2 之前不必先关 #1**（`shadow-open` 会拒绝同一
  `(policy_version_id, report_type, producer)` 的重复 open，但 #2 是不同的 producer + 不同版本）。

## 风险

- **抬门槛会让 #1 变成 drifted ⇒ 回滚目标暂时消失。** `policy_registry.drifted()` 的 docstring
  写明「an older version that drifted cannot be rolled back to either」。
  于是在 #2 被晋升之后、下一个干净版本出现之前，`rollback` 没有可用目标。
  **缓解**：#2 与 #1 行为等价，也就是此刻「回滚目标」的价值本就接近零；但必须写下来，
  别让人以为晋升之后还有一条退路。**信号**：晋升 #2 之前先跑一次
  `policy_registry.drifted()`，把结果记进本计划的决策记录。
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
