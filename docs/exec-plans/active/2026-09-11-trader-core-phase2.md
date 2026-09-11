# Trade Learn Evolve：第二阶段完整交易内核

状态：实施中。负责人：本次开发会话。创建：2026-09-11。

## 目标

第一阶段把账算对了：现金含已实现损益、逐笔退出各自成笔、归属用列不用重推、预测与交易结果分离。
但**账本身还不是一个账本**：现金是一个公式算出来的数，下单不冻结任何东西，没有任何机制能
机器化地证明这个数是对的。

本阶段把交易内核补完整：**下单即冻结、派生值必须自平、每笔状态迁移合法**。
完成后「可信」不再是一句声称，而是每晚跑一遍的检查。

## 范围

设计 §14 的 Phase 2 是「完整交易内核」。按用户 2026-09-11 的决定实施，**减去费用引擎**
（见「非目标」）。六个切片，按依赖顺序：

### S1 订单状态机与合法迁移

现在状态只有 `pending → open → stopped/target_hit/expired/cancelled`，散布在 `_fill_order`、
`_cancel_order_unlocked`、`close_position` 三处的裸 `UPDATE`，没有地方声明哪些迁移是合法的。

- 声明唯一的状态集与合法迁移图，集中在 `alpha_agents/data/order_state.py`。
- 每一次状态写入都过 `assert_transition(from, to)`；非法迁移抛错，不静默改。
- 补 `rejected`（提交即被风控/校验拒绝）与 `cancel_pending`（撤单请求中，未确认）。
- `submitted / accepted` 随 S5 的统一意图路径引入。

**为什么先做这个**：储备金（S2）需要「撤单确认才释放」，没有状态机就无处表达「未确认」。

### S2 储备金

现在下单不冻结任何东西：`create_pending_order` 只写一行 `pending`，不占资金；
`_fill_order` 到成交时才重算 `available`。于是两笔挂单可以被同一笔现金同时「担保」——
现金约束在当前实现里不是真的约束。

- 新表 `reservations`：`(order_id, trader_id, amount_or_shares, kind, state, created_at)`。
- 建单即冻结：按 `entry_high`（或现价）估算所需现金，写一笔 `held` 预留。
- 成交即消耗：`_fill_order` 把该预留转为 `consumed`，实际用量以成交价为准，差额释放。
- 撤单/过期/拒绝即释放：终态时把预留转 `released`，并记录释放原因。
- `get_available_capital` 改为 `总现金 − 未释放预留`；新增 `get_total_cash` 保留旧口径。

**这是本阶段最实的一块**：它修的是一个真实的正确性漏洞，不是偏好问题。

### S3 对账

- 新模块 `reconciliation.py`（位于 `data/`）：把派生值（现金、持仓股数与成本）从
  账本（`position_exits` + `virtual_portfolio` + `reservations`）独立重算一遍，逐 trader 比对。
- 差异按 `trader_id` 记录到 `reconciliation_runs` / `reconciliation_diffs`，不自动「修正」——
  自动改账会把一个能发现的 bug 变成一个不能发现的。
- 提供可执行入口（`scripts/` 下），供定时任务与本地核查调用。

**性价比最高的一块**：它把前面所有「我们说它成立」的说法变成机器检查。

### S4 T+1 结算批次

现在 T+1 是一句日期比较（`open_date < today`，见 `position_monitor.py:184`），
无法表达「同一持仓部分可卖、部分不可卖」。

- 新表 `settlement_lots`：每次成交记一批 `(position_id, shares, settle_date)`。
- 可卖数量按 `settle_date <= today` 求和；卖出按批消耗（先到期先出）。
- `available_cash` 与 `total_cash` 正式分家：卖出所得在 T+1 前计入 `total` 不计入 `available`。

### S5 统一执行路径

- 引入 `TradeIntent`（action/evidence/cutoff/price-size 约束/expiry/owning policy），
  建仓、加仓、减仓、平仓、自动保护动作全部经同一个 `submit_intent` 入口。
- `submitted → accepted | rejected` 由这一层产生；业务路径不再直接改持仓或写收益。
- 现有 `create_pending_order` / `open_position` / `add_to_position` / `close_position`
  保留为兼容包装，内部改走意图入口。

**改动面最大的一块**，放在储备金与状态机稳定之后，避免在流沙上重构。

### S6 交易内核的时间边界

- `evolution.replay_mode` 已存在（`get_replay_as_of` / `effective_eod_cut_date`），
  数据层与工具层已广泛使用，**交易内核没用**。
- 让 `portfolio` / `trade_ledger` / `attribution` 的「今天」都取自该时钟，
  使重放时不会用真实当日时间判定过期、成交与结算。
- 成交时用 `information_cutoff` 校验：本次决策引用的数据不得晚于该时刻（S3 起可强制）。

## 非目标与迁移安全

- **不建费用引擎。** 用户 2026-09-11 明确：不想要计算印花税、交易费这类东西。理由比「不重要」更硬：
  设计 §6 自己写着「这不是普遍固定的市场常数」，§16 把「市场规则核实」列为非目标；要做对需查
  真实费率表、按标的与生效日分版本、处理最低佣金，而它只占名义金额的 0.03%~0.05%，且没有券商
  对账单就永远不会「对」。**现有 `COMMISSION_RATE` / `STAMP_DUTY_SELL_RATE` / `SLIPPAGE_RATE`
  等常数原样保留**，继续标注为「手工版本化的假设」，本阶段不动它们，也不把它们升级成引擎。
- **不引入部分成交模拟器。** 设计的 `partially_filled` 指模拟撮合的部分成交；本仓库没有撮合量与
  参与率的模拟，凭空造一个状态会让它永远进不去。退出侧的部分成交已由多条 `position_exits` 腿
  表达。若后续要建模，是独立的一件事。
- 不迁移生产数据：存量行不重算、不回填；新表对历史行一律为空，符合「未知保持未知」。
- 不调用真实下单，不启动调度任务，不扩充 lint 豁免基线。

## 验收（机器可检查）

- **状态机**：非法迁移被拒绝并且不改变存储；每个合法迁移有用例；终态不可再次迁移。
- **储备金**：两笔挂单不能共用同一笔现金——第二笔的冻结额使 `available` 下降，不足以再开一手时
  第二笔被拒绝或缩减；撤单/过期后 `available` 恢复到冻结前；成交后预留转 `consumed` 且差额释放；
  重复释放同一次预留不产生二次经济效果。
- **对账**：构造一笔账实不符（直接改现金或持仓）后，对账必须报出该差异并指名 trader 与字段；
  干净的账本必须报零差异。
- **T+1**：同一持仓当日买入部分不可卖、隔日可卖；卖出所得在 T+1 前不进 `available`；
  按批消耗先到期先出。
- **统一意图**：建仓、加仓、减仓、平仓、撤单全部经同一入口；任何业务路径不直接 `UPDATE` 持仓
  状态（以 lint 或测试断言）；`submitted → accepted|rejected` 在无风控时不被跳过。
- **时间边界**：重放模式下过期、成交、结算判定使用重放时钟而非真实当日；`information_cutoff`
  被真实读取用于校验至少一处输入。
- 新增回归测试先证明旧实现失败，再证明新实现通过。
- 全量口径 `.venv/bin/python -m pytest tests/ -q`（**不加 `--ignore`**）、
  `scripts/lint_harness.py`、`scripts/lint_docs.py` 执行并记录实际结果。
- 不扩充 lint baseline；单个新增模块不超过 1200 行。

## 决策记录

- 2026-09-11：按用户选择实施**完整 Phase 2**（六个切片全做），但**费用引擎一律砍掉**；
  理由是设计文档自身已把费率核实列为非目标，且其为真实费率表的外部核实工作。
- 2026-09-11：先做状态机与储备金，因为「撤单确认才释放预留」依赖状态机；统一执行路径放后面，
  避免在大改路线的同时改账。
- 2026-09-11：对账只报告差异、不自动修正。自动改账会把一个能被发现的 bug 变成一个不能被发现的。
- 2026-09-11：不引入部分成交模拟器。造一个永远进不去的状态是死代码，不是完整度。

## 实施与验证记录

（随切片落地逐条补充。）
