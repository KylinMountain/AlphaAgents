# Trade Learn Evolve：第三阶段 episode learning

状态：active。负责人：本次开发会话。创建：2026-09-12。

## 目标

Phase 1 把账算对了，Phase 2 把交易内核补完整了（下单即冻结、派生值可对账、每笔状态迁移合法、
内核有自己的时钟）。但**学习仍然不可归因**：

- 结果标签**长在来源行上**。`predictions.hit` / `brier` / `scored_at` 是 `predictions` 表的可写列，
  评估器直接 `UPDATE` 它们。设计 §9 要求「平仓收益不得覆盖原始固定期限预测标签」、
  「标签订正只能追加修订」——**在一行可变状态上表达不了这两条**，而且事后无从知道
  某个标签是哪版评估器在什么时刻算出来的。
- **没有 episode**。设计 §9 说「学习单元是决策 episode，不是复盘报告，也不只是走完的盈利仓位」。
  现在只有 `thesis → order → exits` 这条链，它**只能表达成交过的决策**；
  被拒的意图、撤单、过期未成交、只减了一次仓就放弃的决策，在链上都看不出是一个完整的学习单元。
  而 §9 明确要求「暴露选择与覆盖」。
- **候选知识没有结构**。`learning_candidates` 的表存在，但 `claim` /
  `applicable_context` / `proposed_behavior_delta` / `evidence_episode_ids` **一个列都没有**，
  全塞在无结构 `payload_json` 里；状态列被 `CHECK(status = 'candidate')` 钉死成单值，
  设计 §10 的 `observation → hypothesis → testing → validated → retired` 无处表达。
- **没有「已批准知识」这个对象**。§10 说「候选可以被保留，但在被纳入已批准策略快照之前，
  必须留在生产决策检索之外」。现在没有快照，于是「保留」与「生效」之间没有可审计的关口。

本阶段的目标：**让学习可归因，且不隐式改变行为**（§14 Phase 3 的完成含义）。
后半句是硬约束：本阶段一律**不新增任何生效路径**——候选不能自己变成行为，
快照只是一个记录，批准权不在代码里。

## 范围

四个切片，按依赖顺序。计划里的编号沿用 Phase 2 的 `S` 前缀，这里是 `T`。

| 切片 | 状态 |
|---|---|
| T1 episode 关联 | ✅ 已交付 2026-09-12（`tests/test_episodes.py`，28 用例；四个变异探针全红） |
| T2 三种结果的生命周期 | ✅ 已交付 2026-09-12（`tests/test_outcomes.py`，40 用例；六个变异探针全红） |
| T3 候选证据结构化与生命周期 | ⬜ 未开始 |
| T4 已批准知识快照 | ⬜ 未开始 |

### T1 episode 关联（学习单元）✅

- 新表 `episodes`：一次决策 = 一个 episode，挂在**冻结的决策边界**上
  （`decision_snapshots.id`），携带 `trader_id` / `code` / `thesis_id` / `prediction_id` /
  `information_cutoff` / 状态与时间。
- 新表 `episode_events`：这一次决策引发的**每一个**动作，
  `(episode_id, kind, ref_id, at, detail_json)`。`kind` 覆盖§9 点名要记的那些，
  且**只覆盖本仓库真有写入路径的**：`intent`（含被拒）/ `order` / `fill` / `add` /
  `trim` / `close` / `cancel` / `expire`。
- 关键：**没有成交的决策也是一个 episode**。被拒的意图、撤单、过期未成交
  都留下完整的 episode，于是「我们决定了多少次、成了多少次」可以机器统计，
  而不是只看得见赚过钱的那些。这正是 §9 的「selection and coverage」。
- 归属用**列**不用重推（Phase 1 的既成约定）：episode 存 `thesis_id` / `prediction_id`，
  event 存 `ref_id`。

**实际结果（2026-09-12）**

- 新模块 `alpha_agents/data/episodes.py`（378 行）+ `memory_store` 里的两表两触发器。
- 门：`intent.submit_intent` 在**业务规则跑之前**开 episode、写 `intent` 事件，
  动作事件在得知结果后追加；`_start_episode` / `_note_outcome` 失败只记 warning，
  不改 `submit_intent` 的契约（拒绝仍是被记录并返回，不抛）。
- 两个钩子：`portfolio._fill_order` 记 `fill`，`_cancel_order_unlocked` 记 `cancel` 并结束 episode。
  后者是撤单的**唯一落点**，因此成交路径内部发起的两次撤单（回撤闸门、资金不足）也在记录里。
- 只读入口：`episodes.coverage` / `open_episodes` / `get_episode` / `events_for`
  \+ `scripts/episode_coverage.py`。**不接进 prompt / 检索 / 决策上下文。**
- **计划与实现的偏差（三处，均已写进 §7）**：
  1. `expire` 被**去掉**了 —— `PENDING_EXPIRE_DAYS` 与 `days_pending` 都是死代码，
     没有任何写入路径能产生它，声明它就是「承诺无可调用入口」。
  2. `hold` / abstention 同样未声明，理由相同（没有任何地方决定过「今天不买」）。
  3. `close` / `trim` 的事件只能指向持仓行，指不到账本腿（`_close_position_impl` 返回 `bool`）。
- `episodes.code` 改为**可为 NULL**（原计划写的是 `code` 必填）：一个形状不合法的意图
  或一个指向不存在持仓的动作，没有任何标的可点名，而丢掉这些行等于把不合格的决策从
  覆盖率分母里删掉 —— 正是这张表要消除的偏差。
- `portfolio.py` 现 1197 行（上限 1200）。下次要改它之前先拆「资金与主线敞口」那组读函数。

### T2 三种结果的生命周期

- 新表 `outcomes`：**append-only**，`(kind, subject_type, subject_id, episode_id, state,
  evaluator_version, evidence_json, available_at, supersedes_id, created_at)`。
  - `kind` ∈ `forecast` / `trade` / `process`（§9 的三张表，互不覆盖）。
  - `state` ∈ `pending` / `matured` / `censored` / `revised`。
  - `evaluator_version`：算这个标签的那版评估器，**不再只能靠 `scored_at` 猜**。
  - `available_at`：这个结果**何时可知**，与「何时算出来」分开——重放时前者才是可用的。
  - `supersedes_id`：订正是**追加**一行指向前一行，不是改前一行（§9）。
- 不可变性交给数据库：`BEFORE UPDATE` / `BEFORE DELETE` 触发器 `RAISE(ABORT)`。
- **预测按声明的期限成熟**：`predictions` 增加 `horizon_days` / `deadline`（声明），
  评估器读它而不是读全局常量 `DEFAULT_HORIZON_DAYS`；历史行没有声明 → 回退全局常量，
  并在 outcome 里标 `evidence_json.legacy_horizon = true`，不假装它声明过。
- 三条 §9 的禁止替换，逐条变成可检查的规则：
  - 平仓**不得**改写预测标签（trade outcome 只写 `kind='trade'`）。
  - 预测正确**不得**计为已实现盈利（trade outcome 只从账本派生）。
  - 违反规则**不因赚钱而豁免**（process outcome 独立成行，不看 P&L）。
- 未平仓的仓位留 `pending`，**不编造终值**；缺终局证据留 `censored` 或 `pending`（§9）。
  （交付时**去掉了「中间估值」** —— 见下方实际结果。）

**实际结果（2026-09-12）**

- 新模块 `alpha_agents/data/outcomes.py`（375 行）：`_LEGAL` 状态机 + `declare` / `ensure_label` /
  `resolve` 三个写入口 + `get` / `initial` / `current` / `history` / `for_episode` /
  `pending_labels` / `counts` / `integrity` 读侧。
- 三个标签生产者放 `alpha_agents/evolution/outcome_labels.py`（301 行），
  **不是** `data/`：过程评分器 `process_quality` 本来就在 `evolution/`，
  而分层方向是 `data → … → evolution`，放 `data/` 会反向 import。
- 生产写入者：`pipeline/tasks/review.py` 的 `_label_outcomes`，
  每轮 review 调 `declare_outstanding_forecasts` / `sweep_trade_labels` /
  `sweep_process_labels`（best-effort，失败只记 warning）；
  `_score_due_predictions` 按**声明的期限**评分，并对每个到期行写标签（无论是否拿到分数）。
- 只读入口：`scripts/episode_coverage.py` 扩了结果段（`counts` / `pending_labels` /
  `integrity`）。**标签不进任何决策上下文。**
- **与计划的偏差（两处）**：
  1. 计划写「未平仓的仓位留 `pending` + **中间估值**」，实现**不写估值**。
     估值是市场数据的函数、标签是决策的函数；把 mark 塞进标签会让
     「这个决策本身好不好」退化成「今天行情好不好」。只记 `state=pending` + `legs=0`。
  2. `sweep_process_labels` 的三个计数改为**互斥**（计划未规定口径）。
     原实现让一条 thesis 同时落进 `matured` 与 `unchanged`，
     使「评了 N 条、其中几条什么都不用做」在数上不成立。
- **修掉的两个真缺陷**（均由测试先红发现）：`ensure_label`（`declare` 返回链头而非活行，
  第二次扫描撞 `UNIQUE constraint failed`）；上述计数重叠。详见
  `docs/TRADER_CORE_IMPLEMENTATION.md` §9 第六轮。

### T3 候选证据结构化与生命周期

- `learning_candidates` 补四个列：`claim` / `applicable_context` /
  `proposed_behavior_delta` / `evidence_episode_ids`（JSON 数组，指向 T1 的 episode）。
  §10 要求这四个字段是**可证伪的陈述**，不是自由文本。
- 放开状态：`CHECK(status IN ('observation','hypothesis','testing','validated','retired'))`，
  默认 `observation`（现默认 `candidate`，迁移时映射到 `observation`）。
  §10 明确「validation does not itself activate knowledge」——`validated` 仍然**不生效**。
- 状态迁移走状态机 + `assert_transition`（沿用 Phase 2 §S1 的姿态），
  每次迁移记录 actor / time / reason。
- 保留原有的「同一证据指纹只存一份」语义（`fingerprint UNIQUE`）。

### T4 已批准知识快照

- 新表 `knowledge_snapshots`：一次批准 = 一个不可变快照，
  `(id, approved_by, approved_at, reason, content_hash, frozen_at, notes)`。
- 新表 `knowledge_snapshot_items`：快照包含哪些知识版本，
  `(snapshot_id, entity_type, entity_id, candidate_id, version_hash)`。
- **不变量**：`content_hash` 可被独立重算校验（沿用 Phase 1 的 `verify_snapshot` 姿态：
  写入方与校验方投影同一个字段元组）。
- 提供只读入口回答「这条候选/这条知识，现在是否在某个已批准快照里」——
  这是 §10「必须留在生产决策检索之外」的机器形式。
- **本切片不接线任何生效路径**：不改变 prompt 内容、不改变检索权重、
  不改变任何 `_get_*` 的返回。快照是记录，批准是人的动作，代码只负责存证。

## 非目标

- **不新增任何生效路径**。本阶段候选、快照都**不进** prompt / 检索 / 决策上下文。
  §10 说得很清楚：验证不等于授权，学习者的权限只有「提议并请求评估」。
- **不给 abstention / hold 造写入路径**。§9 提到要记 abstention 和 hold，
  但本仓库没有任何地方**决定过**「今天不买」这个动作（morning scan 只产出被选中的标的）。
  为它造一个空调用点就是「承诺无可调用入口」那类缺陷，所以 T1 的 `kind` 只覆盖真有写入路径的事件，
  并在 §7 如实标注 abstention/hold 未覆盖。
- 不改 `predictions.hit` 现有语义与读者（`review` 的次日校验继续用它），
  新标签走 `outcomes`，两者并存并在文档里说清哪一个是新的。
- 不迁移生产数据：历史 prediction 不补 `horizon_days`、不追溯建 episode。
- 不引入真实下单、不启动调度、不扩充 lint 豁免基线。

## 验收（机器可检查）

- **episode 覆盖**：一个被**拒绝**的意图留下完整 episode（有 event、状态可知）；
  一个**撤单**、一个**过期未成交**同样留下完整 episode——即「没成交的决策也看得见」。
- **一次决策一个 episode**：同一 order 的 fill/add/trim/close 归到同一个 episode；
  两个 trader 同日推荐同一 code 是**两个** episode，不互相覆盖。
- **预测标签不可被平仓改写**：平仓后 `outcomes` 里 `kind='forecast'` 的行数与其值不变。
- **期限是声明的**：`horizon_days=3` 的预测按 3 天成熟；未声明的历史行回退全局常量并如实标注。
- **状态机**：`pending → matured|censored` 合法，`matured → revised` 合法（追加新行），
  `matured → pending` 非法且不改变存储；终态不可回退。
- **不可变性**：对 `outcomes` 行直接 `UPDATE` / `DELETE` 被触发器拒绝。
- **候选结构**：四个新列有值；`validated` 状态的候选**不产生任何行为变化**
  （以「快照未包含它时，检索/上下文逐字节相同」断言）。
- **快照**：`content_hash` 可独立重算并通过；篡改一行后校验失败。
- 新增回归测试先证明旧实现失败，再证明新实现通过；关键不变量用**变异探针**验证测试会红。
- 全量口径 `.venv/bin/python -m pytest tests/ -q`（**不加 `--ignore`**）、
  `scripts/lint_harness.py`、`scripts/lint_docs.py` 执行并记录实际结果。
- 不扩充 lint baseline；单个新增模块不超过 1200 行。

## 风险

- **最大的风险是「造了一堆没人读的表」**。本仓库已经有两个先例：
  `learning_candidates`/`learning_observations` 写了但生产无人读；
  `intents.policy_ref` 恒为空。对策：每个新表都必须有**生产读者**或**明确的只读入口**，
  并在验证记录里逐个点名「谁读它」；没有读者的表不建。
- **第二个风险是把它做成「新的账」**。`outcomes` 与 `position_exits` /
  `predictions` 存在语义重叠，可能变成第三份真相。对策：`outcomes` **只**存
  「标签与状态」，不重算金额；金额一律引用账本行（`evidence_json` 里存 ref，不存值）。
- **第三个风险是隐式改变行为**。候选状态放开后，若某处按 status 过滤，
  行为会变。对策：本切片把「检索是否读 status」查一遍并写成断言。
- 迁移风险：`learning_candidates` 的 `CHECK` 约束在 SQLite 里无法 `ALTER`，需要重建表。
  重建必须保住已有行：建新表 → 复制数据 → 删旧表 → 改名。顺序上要记住
  Phase 1 的教训——`executescript` 先于迁移执行，依赖新列的 `CREATE INDEX` 必须放在迁移之后。
