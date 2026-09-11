# Trade Learn Evolve：第二阶段完整交易内核

状态：六切片全部完成（S1–S6），验收见「实施与验证记录」。负责人：本次开发会话。创建：2026-09-11。

## 目标

第一阶段把账算对了：现金含已实现损益、逐笔退出各自成笔、归属用列不用重推、预测与交易结果分离。
但**账本身还不是一个账本**：现金是一个公式算出来的数，下单不冻结任何东西，没有任何机制能
机器化地证明这个数是对的。

本阶段把交易内核补完整：**下单即冻结、派生值必须自平、每笔状态迁移合法**。
完成后「可信」不再是一句声称，而是每晚跑一遍的检查。

## 范围

设计 §14 的 Phase 2 是「完整交易内核」。按用户 2026-09-11 的决定实施，**减去费用引擎**
（见「非目标」）。六个切片，按依赖顺序：

### S1 订单状态机与合法迁移 ✅（commit a996963）

现在状态只有 `pending → open → stopped/target_hit/expired/cancelled`，散布在 `_fill_order`、
`_cancel_order_unlocked`、`close_position` 三处的裸 `UPDATE`，没有地方声明哪些迁移是合法的。

- 声明唯一的状态集与合法迁移图，集中在 `alpha_agents/data/order_state.py`。
- 每一次状态写入都过 `assert_transition(from, to)`；非法迁移抛错，不静默改。
- 补 `rejected`（提交即被风控/校验拒绝）与 `cancel_pending`（撤单请求中，未确认）。
- `submitted / accepted` 随 S5 的统一意图路径引入。

**为什么先做这个**：储备金（S2）需要「撤单确认才释放」，没有状态机就无处表达「未确认」。

**实际产出**：`alpha_agents/data/order_state.py`（8 状态 + 合法迁移图 + `assert_transition`），
接入 `_fill_order` / `_cancel_order_unlocked` / `close_position` 三个写入点。`tests/test_order_state.py`
（20 用例）+ `tests/test_order_state_integration.py`（27 用例）。全量 1253 passed。

### S2 储备金 ✅（commit 0fbe89a）

现在下单不冻结任何东西：`create_pending_order` 只写一行 `pending`，不占资金；
`_fill_order` 到成交时才重算 `available`。于是两笔挂单可以被同一笔现金同时「担保」——
现金约束在当前实现里不是真的约束。

- 新表 `reservations`：`(order_id, trader_id, amount_or_shares, kind, state, created_at)`。
- 建单即冻结：按 `entry_high`（或现价）估算所需现金，写一笔 `held` 预留。
- 成交即消耗：`_fill_order` 把该预留转为 `consumed`，实际用量以成交价为准，差额释放。
- 撤单/过期/拒绝即释放：终态时把预留转 `released`，并记录释放原因。
- `get_available_capital` 改为 `总现金 − 未释放预留`；新增 `get_total_cash` 保留旧口径。

**这是本阶段最实的一块**：它修的是一个真实的正确性漏洞，不是偏好问题。

**实际产出**：`alpha_agents/data/reservations.py`（held/consumed/released 生命周期 + `unconsumed_total(trader_id)`），
接入 `create_pending_order`（建单即 reserve `MAX_POSITION_PCT * (1+SLIPPAGE_RATE)`）、
`_fill_order`（成交即 consume at `actual_cost`）、`_cancel_order_unlocked`（终态后 release；
二次 cancel 是 no-op 不二次释放）、`get_available_capital`（减 `unconsumed_total`）。
`tests/test_reservations.py`（20 用例，含模块契约 + portfolio 串联通路）。
全量 1273 passed, 18 skipped；lint_harness 140 文件；lint_docs clean。

### S3 对账 ✅

- 新模块 `reconciliation.py`（位于 `data/`）：把派生值（现金、持仓股数与成本）从
  账本（`position_exits` + `virtual_portfolio` + `reservations`）独立重算一遍，逐 trader 比对。
- 差异按 `trader_id` 记录到 `reconciliation_runs` / `reconciliation_diffs`，不自动「修正」——
  自动改账会把一个能发现的 bug 变成一个不能发现的。
- 提供可执行入口（`scripts/` 下），供定时任务与本地核查调用。

**性价比最高的一块**：它把前面所有「我们说它成立」的说法变成机器检查。

**实际产出**：

- `reconciliation_runs` / `reconciliation_diffs` 两个新表（memory_store.py 的 _SCHEMA 内）。
  runs 表存每次运行的 status/diff_count/trader_count/summary_json；diffs 表存每个不变量
  违例的 (run_id, trader_id, invariant, severity, detail_json, position_id, reservation_id)。
  无外键约束——审计日志应该比生产表活得久，删生产表不应该带走历史。
- `alpha_agents/data/reconciliation.py`：八个不变量独立通过 SQL 直接读生产表，**不调用
  portfolio.py 函数**——对账工具自身依赖被对账对象会让 bug 永远看不见：
  1. `orphan_exit` (major) — exit 引用不存在的 position。
  2. `exit_trader_mismatch` (critical) — exit 与所属 position 的 trader_id 不一致，
     实现盈亏会被记到错的账本。
  3. `exit_math_inconsistent` (major) — net_amount ≠ gross_amount − costs。
  4. `orphan_reservation` (critical) — reservation 引用不存在的 order。
  5. `missing_reservation` (critical) — open 仓位无 held/consumed 预留。
  6. `stale_reservation` (major) — 终态 order 上仍挂着 held 预留。
  7. `consumed_amount_mismatch` (critical) — consumed_amount ≠ shares × open_price ×
     (1 + SLIPPAGE_RATE)，或 consumed 引用未成交的 position。
  8. `oversold_position` (major) — Σ(exits.shares) > position.shares。
- `scripts/reconcile.py`：CLI 入口；`--json`、`--trader <id>`、`--no-fail`；
  退出码 0/1/2（clean 或仅 minor / 有 major 或 critical / runner 自身失败）。
- `tests/test_reconciliation.py`：17 用例，含空库、良好构建、八种不变量分别的篡改注入、
  多不变量同存、summary 准确性、审计日志持久化、CLI 序列化往返。
- 全量 1290 passed, 18 skipped（+17）；lint_harness 141 文件（+1 reconciliation.py）；
  lint_docs clean。
- 在真实 DB 上首次运行即发现 1 条 critical（旧仓位 000510 无预留），证明不变量抓得到
  历史引入的问题，不是只对新建数据有效。

### S4 T+1 结算批次 ✅

现在 T+1 是一句日期比较（`open_date < today`，见 `position_monitor.py:184`），
无法表达「同一持仓部分可卖、部分不可卖」。

- 新表 `settlement_lots`：每次成交记一批 `(position_id, shares, settle_date)`。
- 可卖数量按 `settle_date <= today` 求和；卖出按批消耗（先到期先出）。
- `available_cash` 与 `total_cash` 正式分家：卖出所得在 T+1 前计入 `total` 不计入 `available`。

**实际产出**：

- 两个新表（memory_store.py 的 _SCHEMA 内）：
  - `settlement_lots`：`(position_id, trader_id, code, shares, remaining_shares,
    open_date, settle_date, open_price, source)`，`source` 区分 initial / add。
  - `pending_settlements`：`(exit_id, trader_id, code, net_amount, exit_date,
    settle_date, released, released_at)`，`UNIQUE(exit_id)` 保证退出重试不重复计。
- `alpha_agents/data/settlement.py`：
  - `next_settle_date(fill_date)` = 日历 +1。对 A 股 T+1 是正确的：判据是
    `settle_date <= today`，周五建仓 → 周六 settle ≤ 周一 → 周一可卖，与券商一致。
  - `create_lot` / `consume_lots_fifo(today=...)` / `sellable_shares` / `has_lots` /
    `lot_count`；现金侧 `record_pending` / `unreleased_pending_total` /
    `release_due_settlements` / `pending_summary`。
  - `consume_lots_fifo` **只取 settled lot**（`settle_date <= today`），settled 不足即
    `ValueError` 拒绝——写侧兜底，防止绕过读侧 T+1 门卖掉未结算的份额。
- 接入点四处：
  - `_fill_order` / `open_position`：成交即建 initial lot。
  - `add_to_position`：加仓建独立 lot，settle 期为加仓日 +1（这正是旧
    `open_date < today` 表达不了的场景）。
  - `close_position`：先 `consume_lots_fifo` 按批扣减（FIFO by settle_date），再
    `record_pending` 记 **proceeds**（`cost_basis + net_amount`，不是 P&L——记 P&L 会把
    返还的本金提前一天记回来）。无 lot 的历史行跳过扣减，走旧的 `open_date < today` 口径。
- `position_monitor.check_positions` 的 T+1 门改为一句话：有 lot 则要求存在
  `settle_date <= today 且 remaining_shares > 0`；无 lot（历史行）才回退 `open_date < today`。
- `get_available_capital` 再减 `unreleased_pending_total`；新增 `get_total_capital`
  = `trader_capital + realized_total`，即
  `total = available + invested + reservations + pending`——卖出只是把钱从 available
  挪到 pending，total 不动，这正是不该有的「卖出即变富」的消失。
- `tests/test_settlement.py`：35 用例，含 `next_settle_date` 边界（跨月/跨年/周五）、
  lot 读写、FIFO 跨批消耗、跳过未结算批、结算不足拒绝、pending 幂等与释放、
  以及三处接线与 `check_positions` 的 settled 门（含历史回退）。
- 全量 1325 passed, 18 skipped（另修了一个既有 `token_usage` 时区 bug，见下）；
  lint_harness 142 文件（+1 settlement.py）；lint_docs clean。

**顺带修掉的既有 bug（与本切片无关）**：`token_usage.summary` 用 SQLite 的
`date('now')`（UTC）与 `record` 写的本地 `datetime.now()` 日期比较，在 GMT+8 的
00:00–08:00 之间差一天，导致看板「今日」读成 0。已把四处查询改为
`date('now','localtime',...)`。独立提交，便于单独回退。

### S5 统一执行路径 ✅

- 引入 `TradeIntent`（action/evidence/cutoff/price-size 约束/expiry/owning policy），
  建仓、加仓、减仓、平仓、自动保护动作全部经同一个 `submit_intent` 入口。
- `submitted → accepted | rejected` 由这一层产生；业务路径不再直接改持仓或写收益。
- 现有 `create_pending_order` / `open_position` / `add_to_position` / `close_position`
  保留为兼容包装，内部改走意图入口。

**改动面最大的一块**，放在储备金与状态机稳定之后，避免在流沙上重构。

**实际产出**：

- 新表 `intents`：`(action, status CHECK submitted/accepted/rejected, trader_id, code,
  position_id, order_id, information_cutoff, policy_ref, evidence_json, reject_reason,
  result_json, created_at, decided_at)` + 四个索引。**无外键**——一次拒绝（重复单、主线太弱）
  发生在任何持仓行存在之前，审计必须记得「尝试过」，而不只是成功的那部分。
  `status` 用 `CHECK` 约束钉死三态。
- `alpha_agents/data/intent.py`（435 行）：六个 action（`open` / `open_now` / `add` /
  `trim` / `close` / `cancel`）、意图自身的状态机（`assert_intent_transition`，与
  `order_state` 同一姿态：声明状态与合法迁移，写入时断言）、`TradeIntent` 数据类
  （无行为——会跑代码的契约就是把规则写两遍）、`_reject_reason` 形状校验、
  `_dispatch` 分派表、`submit_intent` 唯一入口，以及读侧 `never_decided` / `history`。
- **`submit_intent` 不吞异常**：`_dispatch` 抛出的异常先记为 `rejected`（留下痕迹）
  再 `raise`。一笔被数据库拒绝的卖出是系统故障，不是「业务上不卖」——把它降级成
  `rejected` 就是本仓库明令禁止的静默吞异常。
- `alpha_agents/data/portfolio_intent.py`（140 行）：四个兼容包装。
  **连接由调用方解析并显式下传**（订单/加仓取 `portfolio._get_conn()`，平仓取
  `portfolio_exit._get_conn()`）。否则审计行会写在 `intent` 碰巧绑定的那个连接上，
  而指向另一个库的调用方（或测试）会得到「动作在一个连接、记录在另一个连接」。
- 实现改名 `_*_impl`（`_create_pending_order_impl` / `_open_position_impl` /
  `_add_to_position_impl` / `_close_position_impl` / `_cancel_order_impl`），
  公开名一律变成包装。静态核对：五个 `_impl` **只被 `intent._dispatch` 调用**，
  没有任何旁路。
- **修掉一个真实 bug**：`_check_add_position`（回调补仓规则）此前自带一份 sizing，
  并直接 `UPDATE virtual_portfolio`——第二条加仓路径，绕过意图层、绕过 `settlement_lots`，
  于是规则加的仓位**当天就可卖**（T+1 被绕开），也绕过主题/集中度/情绪上限。
  现在只决定「规则是否触发」，写入交给 `add_to_position(..., recalc_stop=True)`。
  `recalc_stop` 是**规则的**策略而非 agent 的：规则补仓时停损保持相对新均价的同一百分比，
  不让摊平偷偷放大每股风险；agent 主动加仓则不动停损。
- `portfolio.py` 1169 行（在 1200 上限内）：S5 包装一度把它顶到 1296 行，
  因此按职责再切一刀——sizing 助手移入 `portfolio_sizing.py`，包装移入 `portfolio_intent.py`。
- 文件底部导入顺序改为**依赖顺序**（`portfolio_intent` 在 `position_monitor` 之前）：
  `position_monitor` 会从 `portfolio` 的命名空间里读回 `add_to_position`，而那是文件**末尾**
  才注入的，于是导入顺序变成了承重结构。同时 `position_monitor` 改从**属主模块**直接导入
  `add_to_position`（`portfolio_intent`）与 `close_position`（`portfolio_exit`）——
  拆包的收益就是从属主导入，而不是绕一层转发。
- 顺手清掉 `position_monitor.py` 的 6 个死导入（HEAD 上就在、我这次让它们更刺眼），
  现在 0 个。
- `tests/test_intent.py`：27 用例。生命周期（畸形意图也是「被记录的拒绝」而非异常、
  没有任何行会停在 `submitted`、证据冻结在行上）、迁移图、六个 action 的包装各自留下
  一条意图行、以及**护栏**：`virtual_portfolio.status` 只允许 `portfolio.py`（成交/撤单）
  与 `portfolio_exit.py`（平仓）写，且两者都必须含 `order_state.assert_transition`；
  `intent.py` 不得出现 `UPDATE/INSERT INTO virtual_portfolio`；
  `pipeline` / `agents` / `server` / `tools` 不得直接写持仓。
- 全量 **1352 passed, 18 skipped**（1325 + 27）；lint_harness **145 文件**（+3 新模块，
  存量 47 条未扩充）；lint_docs clean。
- **测试强度自查**：原先那条「六个 action 都有分派分支」是静态 grep，而
  `OPEN = "open"` 这行常量定义本身就满足它——**永远不会失败**。已改成行为测试：
  每个 action 提交一个形状合法的意图，断言唯一的阻挡只能来自业务规则、
  绝不能是 `no dispatch for action`。并用变异探针验证过：注释掉 `CANCEL` 分支后
  该用例确实变红（`ValueError: no dispatch for action 'cancel'`），随后复原。
- **已知遗留（非本切片引入）**：`import position_monitor` **先于** `portfolio` 仍会失败，
  因为 `portfolio` 末尾反向导入 `position_monitor` 的名字。已用 `git worktree` 在 HEAD
  上验证这是**既有**结构（不是我引入的回归，HEAD 同样失败）。当前所有调用方与测试都先
  导入 `portfolio`，故不影响运行；彻底修需把 `portfolio` 末尾的转发改成惰性
  （PEP 562 模块 `__getattr__`），是独立的一小件事，不塞进 S5。

### S6 交易内核的时间边界 ✅

- `evolution.replay_mode` 已存在（`get_replay_as_of` / `effective_eod_cut_date`），
  数据层与工具层已广泛使用，**交易内核没用**。
- 让 `portfolio` / `trade_ledger` / `attribution` 的「今天」都取自该时钟，
  使重放时不会用真实当日时间判定过期、成交与结算。
- 成交时用 `information_cutoff` 校验：本次决策引用的数据不得晚于该时刻（S3 起可强制）。

**实际产出**：

- 新模块 `alpha_agents/data/clock.py`（143 行），`data/` 下唯一回答「今天几号」的地方：
  - `today()`：有重放取重放时刻的**日期部分**，否则取本地日期。
  - **刻意不用 `effective_eod_cut_date`**：那个函数回答「我能看到哪天的收盘数据」，
    盘前会回滚到 T-1。内核问的是「今天几号」——06:30 下的单是 3/20 的单，T+1 批次 3/21 结算。
    用数据切点会让每个盘前成交早一天，并让每个 lot 与它的订单脱钩。
  - `LookAheadError(ValueError)`：前视是**系统故障**，不是被业务规则拒掉的交易。
  - 两个守卫，按**日期**（前十字符）比较，即内核实际工作的时间粒度：
    `assert_not_from_the_future(stamp, what=…)`（不得写入世界尚未到达的那一天）、
    `assert_decided_no_later_than(cutoff, stamp, what=…)`（不得用晚于动作本身的时点当依据）。
    方向是容易反的那个：cutoff **早于**动作是正常情形（用昨收决策，今天动手），
    cutoff **晚于**动作才是不可能的情形。
  - `guard_fill(fill_date, *, order_id, conn)`：把上面两条合成一次前置检查，
    于是 `_fill_order` 仍然只谈资本与仓位。
- 堵掉内核的**三处墙钟泄漏**（这是本切片修的真实缺陷）：
  `portfolio._add_to_position_impl` 的加仓批次日期、`portfolio_exit._close_position_impl`
  的平仓日、`intent._today()` 的默认下单日。此前重放三月时这三处会把行写到九月：
  T+1 窗口从重放尚未到达的日期起算，写进 `settlement_lots` 的 `settle_date`
  是后续任何重放日都无法满足的日期，持仓看起来永远不可卖。
- `information_cutoff` **从只写不读变成真被读取**：填充 `intents.order_id`
  （该列此前存在且从未被写入——与「字段只写不读」同类），新增
  `intent.cutoff_for_order(conn, order_id)`，`_fill_order` 据此查验
  「下单时的决策依据不得晚于成交日」；`attribution.freeze` 拒绝晚于内核时钟的边界。
- **修掉一条吞异常的路径**：`attribution.freeze` 的调用被 `except Exception`
  包着（本意是「审计写不进去不该阻止交易」）。`LookAheadError` 是 `ValueError`，
  会落进这个宽容处理器被降级成一行 warning：交易照做、日志全绿、学习数据带着泄漏。
  `_create_pending_order_impl` / `_open_position_impl` 现在先 `except clock.LookAheadError: raise`。
  已用变异探针验证：去掉这一句，`test_the_order_path_refuses_rather_than_warns` 变红。
- 管线侧的业务日期改取内核时钟（否则重放会直接抛错）：`intraday_monitor` 的
  `today_str`（→ 成交日）与下单选单日期、`morning_scan` 的下单选单日期。
  `intraday_monitor` 的午休判断保留墙钟——那是「此刻此地市场是否开门」的调度事实，不是被模拟的事实。
- `tests/test_kernel_clock.py`：21 用例，分四组——时钟语义（含与 `effective_eod_cut_date` 的分野、
  上下文退出不泄漏）、守卫（将来日期的成交被拒、重放时刻约束成交、依据晚于动作被拒、
  方向正确性、无声明不算违例）、账本确实写在重放窗口内（成交建 lot、平仓落 `exit_date`、
  加仓建 lot、意图默认日期）、以及管线取时 + 不吞前视两处契约。
- 全量 **1373 passed, 18 skipped**（1352 + 21）；lint_harness 146 文件（+1 clock.py，
  存量 47 条未扩充）；lint_docs clean。
- **三个变异探针**（都先红后复原）：① `clock.today()` 退回墙钟 → 8 个用例变红，
  含 `test_a_close_is_booked_on_the_replay_day`（三月重放里把平仓日写成九月）；
  ② 停用 `assert_not_from_the_future` → 4 个守卫用例变红；
  ③ 去掉前视的 re-raise → 吞异常用例变红。

**边界说明（本切片不做，明确划出）**：非内核写入路径仍读墙钟——`tools/`（vpa、行情）、
`evolution/`（holdout、playbook 的窗口）、`memory_store` 的统计查询、`pipeline` 的
review/scheduler（其「今天」是调度语义）。S6 的对象是**交易内核的写路径**；把这些也改掉
是另一件事，且其中多数问的是「现在」，不是「模拟到哪一天」。

**已知遗留**：`portfolio.py` 1190/1200 行。本切片把它从 1169 推到 1198（+29），
已把守卫抽到 `clock.guard_fill` 并清掉一个死导入，压回 1190。**下次再动这个文件必须先拆**，
拆的口子是主题暴露（`_theme_too_weak` / `resolve_theme` / `_cluster_room` / `get_theme_exposure`），
不要扩 lint 豁免基线。

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
- 2026-09-11：S6 的内核时钟用重放时刻的**日期**，不用 `effective_eod_cut_date`。
  后者是「我能看到哪天的数据」，盘前回滚 T-1；内核问的是「今天几号」。两者混用会让
  每个盘前成交早一天，并使 `settlement_lots.settle_date` 与订单日期脱钩。
- 2026-09-11：`LookAheadError` 继承 `ValueError`（与 `order_state.IllegalTransition` 一致的姿态），
  但**必须**在宽容的 `except Exception` 之前被 re-raise。继承谁只决定「能被谁接住」，
  不决定「该不该被接住」——后者是调用点的责任。
- 2026-09-11：S6 只改**交易内核的写路径**。`tools` / `evolution` / `memory_store` 的统计
  查询 / `pipeline` 的 review、scheduler 仍读墙钟，它们问的是「现在」，不是「模拟到哪一天」。

## 实施与验证记录

（随切片落地逐条补充。）
