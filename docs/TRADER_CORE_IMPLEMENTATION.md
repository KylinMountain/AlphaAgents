# Trader Core Implementation: Phase 1–2 实际状态

- 记录日期：2026-09-11（第四轮补记 Phase 2，同日）。
- 依据：[Phase 1 计划](exec-plans/completed/2026-09-11-trader-core-phase1.md)、
  [Phase 2 计划](exec-plans/completed/2026-09-11-trader-core-phase2.md)、
  [设计文档](TRADER_CORE_DESIGN.md)、[架构图](../ARCHITECTURE.md)、[金律](GOLDEN_PRINCIPLES.md)。
- 本文件只记录**代码里已经存在的行为**。每一项标注已实现或未实现，未实现项不因本文件存在而被视为完成。
- 代码是唯一事实来源；本文件与代码冲突时，以代码为准并修正本文件。
- 第 1–6 节描述 Phase 1 的交付；Phase 2 的逐项交付见第 10 节，未实现项一律留在第 7 节。

## 1. 现金反映累计结果

**已实现。**

`get_available_capital`（`alpha_agents/data/portfolio.py`）的语义从「本金 − 持仓成本」改为：

```
本金 + 已实现净损益 − 在手持仓成本
```

旧式读法把已实现损益完全排除在账户之外：赚了钱的交易员花不掉利润，亏了钱的仍能花掉已不在的钱。账户反映的只是持仓，不是交易。现在买入能力随真实结果变化。

两个刻意的边界：

- **仓位规模不受已实现损益放大。** 下单量仍取 `trader_capital(trader_id) * MAX_POSITION_PCT`，用固定名义本金而非可用现金计算。否则一笔盈利会抬高下一笔的敞口上限，把单次运气复利成风险。
- **这不是逐笔现金账本。** 现金仍是「本金 + 累计损益 − 在手持仓」的估值口径，未按成交逐笔扣减手续费与结算资金。版本化的成本假设见 `_estimate_net_close_result`，不声称已复核全部市场收费规则。

## 2. 逐笔退出记录

**已实现（作为后续完整账本的基础）。**

新模块 `alpha_agents/data/trade_ledger.py`，表 `position_exits`（DDL 在 `alpha_agents/data/memory_store.py`）。

- **幂等键是调用方给的 `command_id`，不是「日期 + 价格 + 数量」。** 同日同价同量的两笔独立卖出是两笔指令，不是一次重试；用执行条款当身份会在真实场景下静默合并两笔交易。显式重试返回原记录 ID；同一 ID 携带不同参数被拒绝。
- **每笔退出都留原始指令快照**（`request_json`），冻结调用时的参数，而不是被库存裁剪后的数量。
- **部分退出与最终退出各自成笔**，累计值由 `realized_for_position` 求和得出，不再由「最后一次平仓」覆盖前次。混合收益率 `_blended_return_pct` 以全部已卖出的成本为基数，使中途止盈不被最终一笔抹掉。
- **写入与持仓更新同一事务。** `close_position` 用 `BEGIN IMMEDIATE` 序列化竞争写者，持仓 UPDATE 失败则整笔回滚，账本与持仓不会只落一边。
- **旧聚合损益保留为 legacy，不伪造成交。** `legacy_realized_amount` 单独存放，`realized_total` 把它计入现金但从不生成合成腿；缺失的历史卖出数量、价格与费用都不做推断。

数学与输入护栏：价格须为有限正数，数量须为正整数且符合整手约定，金额须有限，成本与费用不得为负。`NaN`/`Infinity`/非数值类型/布尔值都在入口被拒绝，不产生任何经济效果。

## 3. 订单归属与标签分离

**已实现。**

- **订单显式指名它的预测。** `virtual_portfolio.prediction_id` 是外键，`create_pending_order` 与 `open_position` 显式接收并落库。
- **关联在写入前校验。** `_valid_prediction` 要求 prediction 的 `code` 与 `trader_id` 都与该订单一致；不匹配即拒绝建单。旧的「按股票代码 + 附近日期找最近一条预测」不是归属关系 —— 同一只股票上两个交易员时，它会把一个交易员的结果记到另一个的账上。
- **无关联的旧记录保持未知来源。** 不猜测、不回填；`prediction_id` 为空即为空。
- **成交结果不覆盖固定期限标签。** 平仓只写 `position_exits` 与 `virtual_portfolio.return_amount`。prediction 的 `hit` 是声明期限内的方向标签（由 `review.py` 在期限到达时填写），止损离场不等于预测失败，把两者压成一个字段会让预测成绩不可恢复。

## 4. 决策归属链与不可变边界

**已实现。** 新增 `alpha_agents/data/attribution.py`，承担两件事：把执行链每一跳落成列，把决策时点的信息边界冻结下来。

**链条。** 设计 §4 的目标链是 `thesis_id → order_id → fill_id → ledger_entry_id`。代码里落地的是其中**不需要凭空造记录**的那部分：`theses.id` ← `virtual_portfolio.thesis_id` ← `position_exits.thesis_id`。**本仓库没有独立的 fills 表**，入场成交就是持仓行本身（`open_price` / `open_date` / `shares`），账本条目就是退出腿，所以 `ledger_entry_id` 实际解析为 `position_exits.id`。`resolve_chain` 只走这条链，不多声称。

- **订单端记录 thesis。** `virtual_portfolio.thesis_id` 与 `position_exits.thesis_id` 是显式列，`create_pending_order` / `open_position` 接收并在写入前用 `valid_thesis` 校验（要求 `code` 与 `trader_id` 同时一致）。不匹配即拒绝，不留到事后发现。
- **退出腿自带归属。** `record_exit` 写入 `position_exits.thesis_id`（从持仓复制，不是查询派生），所以一笔已实现损益即使持仓行被改动，也仍然指名它属于哪个论点。
- **链条可整体解析。** `resolve_chain(order_id)` 返回 thesis / order / exit 腿 / 快照 ID，不需要靠扫描重推。
- **修掉一处串线缺陷。** `_fill_order` 原先绑定 thesis 时扫的是「该代码下所有活跃未绑定论点」，**没有按交易员过滤**；`thesis_monitor` 与 `settle_orphans` 都过滤了，只有成交这条路径漏了。同股双交易员时，后成交的一笔会绑到先者的论点上，随后监控拿 A 的论点去比 B 的成本。现改为优先用订单显式 `thesis_id`，回退也按 `trader_id` 限定。回归测试先证明旧实现失败（1 failed），再证明新实现通过。

**信息边界。** 不变量 4 要求决策只使用决策时点可得的信息，且事后修正不得改写该边界。

- **建单即冻结。** `decision_snapshots` 记录声明的决策输入（介入区间、止损、目标、理由、来源）、`information_cutoff`（决策被允许使用的最晚时刻，不是任务运行时刻）、`decided_at`、producer 引用与 `content_hash`。
- **由数据库保证只增不改。** 表上装了 `BEFORE UPDATE` / `BEFORE DELETE` 触发器直接 `RAISE(ABORT)`，而不是指望每个调用方记得不要改。修订视图是**新增一行并指名被替代者**，旧行始终可读 —— 它是证据，不是草稿。
- **哈希可独立复算。** `content_hash` 覆盖冻结字段，即使触发器被移除也能检测出改写。`verify_snapshot(conn, snapshot_id)` 是它的可调用入口：重算哈希并返回是否仍匹配（未知 ID 返回 `False`，因为「不存在」与「被改写」都是不可信）。写入与校验共用同一份字段清单 `_FROZEN_FIELDS`，避免校验的字段集与写入的字段集悄悄分叉。

**结果拆分（§9）。** 三类结果各自可独立读出，互不覆盖：

- `forecast_outcome(prediction_id)` 只读 `predictions`，返回声明期限标签，并以 `graded` 区分「已评分」与「尚未到期」——`hit=None` 不能被误读为未命中。
- `trade_outcome(thesis_id)` 只从 `position_exits` 求和，返回 `fills` / `net_amount` / `return_pct`。未成交是 `fills=0` 且 `return_pct=None`，**不是零收益**：「没交易」与「交易了但打平」是两个不同事实，只有一个构成技能证据。
- 过程结果由 `alpha_agents/evolution/process_quality.py` 提供，位于更高层；`data/` 不向上引用，这是分层要求而非遗漏。

测试见 `tests/test_attribution_chain.py`，其中 `test_a_correct_forecast_survives_a_losing_trade` 与 `test_a_winning_trade_does_not_upgrade_a_wrong_forecast` 直接钉住「正确预测 + 亏损交易」和「错误预测 + 盈利交易」两种必须共存的组合。


## 5. 学习边界

**已实现。**

- **未验证的经验只进候选区。** `alpha_agents/data/learning_candidates.py` 拥有独立的 `learning_candidates` / `learning_observations` 两张表，与 `memory_store` 的迁移解耦。表上只有 `status = 'candidate'` 这一个允许值，模块**不提供晋升或批准 API** —— 收集到证据不等于获得交易许可。
- **候选指纹去重、证据变更另立。** 精确重试返回原候选 ID，证据变了生成新候选而不是覆盖旧的，原始 payload 与来源始终保留。来源（source）与来源日期（source_date）为必填的溯源信封，但**只校验溯源，不校验提案的真伪或交易价值**。
- **决策提示词不再携带模型的自身成绩单。** `inject_playbooks` 只渲染规则字段（名称、状态、权重、批注），不再注入 `hit_rate` / `wins` / `total_trades`。这些是可变的产出度量，喂回模型等于让它从自己产出的反馈里推理 —— 正是金律第 2 条禁止的。
- **原始教训只对研究用途开放。** `inject_recent_lessons` 增加 `purpose` 参数，默认与未知用途直接返回空且不查询存储；只有复盘的研究上下文显式传 `purpose="research"` 才读取，且标注为「未经批准的研究材料」。morning / chat 决策上下文完全不注入。

## 6. 测试与存储隔离

**已实现。**

- **测试默认不打开项目 `data/` 下的 SQLite 文件。** `tests/conftest.py` 在收集期就重定向数据目录，并安装审计钩子：任何指向项目 `data/` 的连接（含字符串、字节、相对路径、`..`、`file:` URI、编码 URI、目录/文件符号链接、大小写别名）在真正连接前被拒绝；`ATTACH DATABASE`、`VACUUM INTO`、自定义 factory 旁路、真实网络 I/O 同样被阻断。等价测试见 `tests/test_storage_isolation.py`。
- **分词词表随仓库提供。** `tests/fixtures/tiktoken/` 保存 `cl100k_base` 词表，`conftest` 指向它。否则首次分词会联网下载，与「测试不联网」直接冲突。重建命令写在 `tests/conftest.py` 顶部注释里。
- **上一交易日查询改用配置真值。** `_prev_trading_day`（`alpha_agents/tools/vpa/data.py`）原先用 `__file__` 硬拼项目 `data/market_history.db`，绕过一切数据目录覆盖。现改为在调用时读取 `market_history.DB_PATH`。
- **需要真实语料的遗留测试显式跳过，而非放宽守卫。** `tests/conftest.py` 的 `_NEEDS_MARKET_HISTORY` 列出要读业务库的用例名，其余用例保持可运行；纯逻辑用例改写为注入依赖，不靠跳过换绿灯。

## 7. 明确未实现

以下**当前代码中不存在**，不要按本文件或设计文档误读为已交付。分三类：Phase 3/4 的后续阶段、
Phase 2 里**主动不做**的项（理由见 Phase 2 计划的「非目标」）、以及 Phase 2 只做到一半的项。

**后续阶段**

- **结果生命周期的完整状态机**（Phase 3）。§9 的 `pending / matured / censored / revised` 尚未落到独立状态列；当前只能从 `hit` 是否为 NULL 与 `scored_at` 推断，未成交、删失、修订三种情况无法区分。
- **策略版本注册与前向影子实验**（Phase 4）。无 frozen policy、无独立前向评估闸门、无批准的晋升路径。`policy_ref` / `model_ref` 字段已预留但当前写入为空 —— 没有注册表可引用。

**Phase 2 主动不做（非目标，不是遗漏）**

- **费用引擎**。无逐笔建仓手续费落账。`COMMISSION_RATE` / `STAMP_DUTY_SELL_RATE` / `SLIPPAGE_RATE` 仍是**手工版本化的假设**，只在计算净收益时参与，不作为独立记账条目。设计 §6 自己写着「这不是普遍固定的市场常数」，§16 把「市场规则核实」列为非目标。**这是用户明确定的非目标，不要当作待办补上。**
- **部分成交模拟器**。无 `partially_filled`。本仓库没有撮合量与参与率的模拟，凭空造一个状态会让它永远进不去；退出侧的部分成交已由多条 `position_exits` 腿表达。
- **`rejected` / `cancel_pending` 两个订单状态**。`order_state.py` 声明了状态集与合法迁移，但这两个状态**尚未被任何写入路径产生** —— 它们是设计 §6 的目标态，当前只在状态机里被允许，未在业务里出现。
- **global 事件日志**。无跨表统一事件流。

**Phase 2 只做到一半**

- **信息边界的强制校验**。现在**有消费者了**（第四轮新增）：`attribution.freeze` 拒绝晚于内核时钟的边界，成交时 `clock.guard_fill` 校验「下单决策的 `information_cutoff` 不得晚于成交日」。但设计 §15 的完整合同「Every actionable input satisfies the availability cutoff」**仍未成立**：只有这两处，校验的是**声明的边界本身**是不是未来，而不是「这次决策引用的每一个输入都落在边界内」。当前强制的仍然是「边界不可事后改写」（触发器 + 哈希）与「边界不得是未来」；**不是**「每个输入都被逐一验证过」。

## 8. 行为契约变更（供 review 对照）

| 旧行为 | 现行为 |
|---|---|
| 现金 = 本金 − 持仓成本 | 现金 = 本金 + 已实现净损益 − 持仓成本 |
| 部分退出被最终退出覆盖 | 每次退出独立成笔，累计求和 |
| 同日同价同量视为重复 | 以 `command_id` 判重，执行条款不作身份 |
| 平仓覆盖 prediction 的 `hit`/`week_return` | 平仓只写交易侧；prediction 标签由期限决定 |
| 按代码 + 日期就近匹配预测 | 显式外键，校验 code 与 trader 一致 |
| 成交时扫「该代码下第一个未绑定论点」 | 用订单显式 `thesis_id`；回退按 `trader_id` 限定 |
| 归属只存在 `theses.position_id` 一侧 | order 与 exit 腿各自持有 `thesis_id`，链条可解析 |
| 决策依据不留痕，事后无从核对 | 建单即冻结 `decision_snapshots`，触发器禁止改写 |
| 改写检测只有触发器一道锁 | 触发器之外另有 `verify_snapshot` 重算哈希，绕过触发器也能查出来 |
| 预测与交易结果混在一个标签里 | `forecast_outcome` / `trade_outcome` 各自读独立存储 |
| 提示词含 playbook 胜率与原始教训 | 只含规则字段；原始教训仅研究用途 |
| 未验证经验可直接进活跃知识 | 一律进候选区，无晋升 API |
| 上一交易日硬编码项目数据路径 | 读取配置的数据目录 |
| `portfolio.py` 一个文件承担下单与平仓 | 平仓切片（摩擦模型 + 退出记账）移入 `portfolio_exit.py` |
| 状态只有裸 `UPDATE`，无处声明合法迁移 | `order_state.assert_transition` 在每个写入点断言，非法迁移抛错 |
| 下单不冻结资金：两笔挂单可共用同一笔现金 | 建单即 `reservations` 冻结，成交转 `consumed`，终态 `released` |
| 「现金」一个口径，卖出当日即可再花 | `get_available_capital`（可花）与 `get_total_capital`（总）分家，卖出所得 T+1 前只进后者 |
| T+1 是一句 `open_date < today` | `settlement_lots` 按批结算 FIFO，同一持仓可部分可卖 |
| 派生值是否正确只能靠人看 | 八个不变量由 `reconciliation` 独立重算并留审计（只报告，不自动改账） |
| 四条写入路径各自回答「成了吗」 | 全部经 `intent.submit_intent`，写一行 `intents` 记录决定与结果 |
| 加仓规则自带一份 sizing 并直接 `UPDATE`（绕过 T+1） | 规则只决定是否触发，写入交给 `add_to_position(recalc_stop=True)` |
| 内核的「今天」= 机器当天 | `clock.today()`：重放时取重放时刻，前视被 `LookAheadError` 拒绝 |
| `information_cutoff` 只写不读 | `attribution.freeze` 与 `clock.guard_fill` 都会真的读它并拒绝未来边界 |

## 9. 验证记录

### 第一轮（隔离与可信事实）

2026-09-11，本仓库根目录：

| 命令 | 结果 |
|---|---|
| `.venv/bin/python -m pytest tests/ -q`（`--ignore=tests/test_web_no_monitor.py`） | 1199 passed, 18 skipped |
| `.venv/bin/python scripts/lint_harness.py` | 通过（136 个文件），存量 47 条待偿还 |
| `.venv/bin/python scripts/lint_docs.py` | 知识库校验通过 |

说明：

- 18 个跳过项均为显式声明需要真实市场语料的用例，跳过原因由 `tests/conftest.py` 给出，非静默失败。
- `lint_harness` 的 47 条存量不在本轮扩充，也不在本轮清偿；基线未放大。
- 测试中的数字均为合成样例，不构成对任何策略收益的主张。

### 第二轮（归属链、不可变边界与结果拆分）

| 命令 | 结果 |
|---|---|
| `.venv/bin/python -m pytest tests/ -q`（`--ignore=tests/test_web_no_monitor.py`） | 1216 passed, 18 skipped |
| `.venv/bin/python scripts/lint_harness.py` | 通过（138 个文件），存量 47 条待偿还 |
| `.venv/bin/python scripts/lint_docs.py` | 知识库校验通过 |

说明：

- 新增 `tests/test_attribution_chain.py`（17 个用例）。按计划要求**先证旧后证新**：把 `_fill_order` 的绑定逻辑临时退回未按交易员过滤的版本，`test_the_fill_binds_its_own_thesis_not_the_first_live_one` 变红（1 failed / 16 passed），恢复后转绿。
- 拆分 `portfolio_exit` 后，三处测试的 patch 目标随之更新（`tests/test_portfolio.py`、`tests/test_trader_ledger.py`、`tests/test_learning_loop.py`）。这些测试原本就靠显式列出「所有绑定 `_get_conn` 的模块」来重定向 I/O，本次只是把新模块补进那张表 —— 是拆分本身的代价，不是行为回归。
- `portfolio.py` 因新增归属校验与冻结逻辑一度达 1239 行、越过 1200 行上限。按职责把平仓切片（摩擦模型 + 退出记账）移入 `portfolio_exit.py`，现分别 1032 / 249 行。**未扩充 lint 豁免基线。**

### 口径更正：前两轮少报了 7 个用例

第一、二轮的 pytest 命令带了 `--ignore=tests/test_web_no_monitor.py`，**这是多余的**：该文件单独跑 7 passed，全量跑也通过。它一直在少报 7 个用例，而 CI（`.github/workflows/harness.yml`）并不 ignore。第三轮起一律按全量口径记录；上面的 1199 / 1216 保留原样，按此更正理解。

### 第三轮（Phase 1 收口：审计缺口与完成判定）

| 命令 | 结果 |
|---|---|
| `.venv/bin/python -m pytest tests/ -q`（**全量，不 ignore**） | 1226 passed, 18 skipped |
| `.venv/bin/python scripts/lint_harness.py` | 通过（138 个文件），存量 47 条待偿还 |
| `.venv/bin/python scripts/lint_docs.py` | 知识库校验通过 |

本轮修掉的缺口，都是「文档承诺了代码没做的事」一类：

- 补 `ARCHITECTURE.md` 的「The trader book」一节——范围第 1 项要求更新 ARCHITECTURE，此前只更新了 AGENTS。
- 四跳链过度声称：`fill_id` / `ledger_entry_id` 不是列（无 fills 表），已改为真实链 `thesis → order → exits`。
- 新增 `verify_snapshot`，让 `content_hash` 的「可检测改写」成为可调用的保证，而不是只写在文档里；补 2 个用例（含绕过触发器改写后校验失败）。
- 补 `test_the_same_command_id_with_different_arguments_is_refused`：验收里有这条，实现也有 `raise ValueError`，但此前无测试钉住。
- `information_cutoff` 的「已冻结」与「已强制」被混为一谈，已在 §7 分开表述。

**Phase 1 判定为已完成**，依据是设计 §14 对它的范围定义，而不是「所有后续阶段都做完了」。残留项的分期见 §7。

### 第四轮（Phase 2 完整交易内核：S1–S6）

六切片，逐条见 [Phase 2 计划](exec-plans/completed/2026-09-11-trader-core-phase2.md)。
用户 2026-09-11 决定：做完整 Phase 2，**减去费用引擎**。

| 命令 | 结果 |
|---|---|
| `.venv/bin/python -m pytest tests/ -q`（**全量，不 ignore**） | 1373 passed, 18 skipped |
| `.venv/bin/python scripts/lint_harness.py` | 通过（146 个文件），存量 47 条待偿还 |
| `.venv/bin/python scripts/lint_docs.py` | 知识库校验通过 |

| 切片 | 交付 | 备注 |
|---|---|---|
| S1 | `order_state.py`：8 状态 + 合法迁移图 + `assert_transition`，接入 3 个写入点 | `rejected` / `cancel_pending` 被允许但尚未被业务产生，见 §7 |
| S2 | `reservations.py`：建单冻结、成交消耗、终态释放；`get_available_capital` 减去未释放预留 | 修的是真实正确性漏洞：此前两笔挂单可共用同一笔现金 |
| S3 | `reconciliation.py` + `scripts/reconcile.py`：八个不变量独立重算，只报告不自动改账 | 首次在真实库上跑即抓到 1 条 critical（旧仓位 000510 无预留） |
| S4 | `settlement.py` + `settlement_lots` / `pending_settlements`：T+1 按批结算 FIFO；`available` / `total` 正式分家 | 卖出所得在 T+1 前只进 `total`，「卖出即变富」消失 |
| S5 | `intent.py` + `intents` 表：六个 action 经 `submit_intent` 一个门，写 `submitted → accepted \| rejected` | 顺带修掉 `_check_add_position` 的第二条加仓路径（曾绕过 T+1） |
| S6 | `clock.py`：内核时钟 + 两个前视守卫；`information_cutoff` 从只写不读变成真被读取 | 修掉内核三处墙钟泄漏；修掉 `except Exception` 吞掉前视的路径 |

**本轮的三个变异探针**（先证明测试会红，再复原）：S5 注释掉 `CANCEL` 分派 → 对应用例变红；
S6 把 `clock.today()` 退回墙钟 → 8 个用例变红（含「三月重放里把平仓日写成九月」）；
S6 去掉前视的 re-raise → 吞异常用例变红。

**Phase 2 判定为已完成**，同样依据设计 §14 的范围定义与计划里的「非目标」，
而不是「设计里提到的每一件事都做了」：费用引擎、部分成交模拟器、global 事件日志是
**明确定的非目标**，`information_cutoff` 的完整 §15 合同是**只做到一半并已如实标注**的一项。

## 10. Phase 2 逐项交付（S1–S6）

- **订单状态机**：`alpha_agents/data/order_state.py` 声明唯一状态集与合法迁移；
  `portfolio.py`（成交、撤单）与 `portfolio_exit.py`（平仓）是仅有的两个写入
  `virtual_portfolio.status` 的模块，两者都必须经过 `assert_transition`。
  该约束由 `tests/test_intent.py::TestOnlyTheStateMachineWritesStatus` 机械钉住。
- **现金预留**：`alpha_agents/data/reservations.py`。建单即写 `held`，
  成交转 `consumed`（差额释放），撤单/过期/拒绝转 `released`。
  `get_available_capital` = 总现金 − 未释放预留 − 未结算卖出所得。
- **对账**：`alpha_agents/data/reconciliation.py`，八个不变量：`orphan_exit`、
  `exit_trader_mismatch`、`exit_math_inconsistent`、`orphan_reservation`、
  `missing_reservation`、`stale_reservation`、`consumed_amount_mismatch`、
  `oversold_position`。写入 `reconciliation_runs` / `reconciliation_diffs`。
  **只报告，不修正** —— 自动改账会把一个能被发现的 bug 变成一个不能发现的。
- **T+1 结算**：`alpha_agents/data/settlement.py` + 两个新表。
  `next_settle_date` = 日历 +1（判据是 `settle_date <= today`，周五建仓 → 周一可卖，与券商一致）。
  卖出按 `settle_date` FIFO 消耗批次；无批次的历史行回退 `open_date < today`。
- **统一执行路径**：`alpha_agents/data/intent.py` + `portfolio_intent.py` + `intents` 表。
  `create_pending_order` / `open_position` / `add_to_position` / `close_position`
  保留同签名同返回值的兼容包装，内部一律走 `submit_intent`。
  **`submit_intent` 不吞异常**：先记录再抛出，因为「数据库拒绝这笔卖出」是系统故障，
  不是「业务上不卖」。
- **内核时间边界**：`alpha_agents/data/clock.py`。`today()` 重放感知；
  `assert_not_from_the_future` 与 `assert_decided_no_later_than` 拒绝前视；
  `guard_fill` 把两条合成成交前置检查。管线侧的业务日期（`intraday_monitor` 的
  `today_str` 与下单选单日、`morning_scan` 的下单选单日）也改取该时钟。
  不覆盖 `tools` / `evolution` / `memory_store` 统计查询 / `pipeline` 的 review、scheduler ——
  它们问的是「现在」，不是「模拟到哪一天」。
