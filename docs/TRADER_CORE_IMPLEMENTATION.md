# Trader Core Implementation: Phase 1–3 实际状态

- 记录日期：2026-09-12（第六轮补记 Phase 3 的 T2）。
- 依据：[Phase 1 计划](exec-plans/completed/2026-09-11-trader-core-phase1.md)、
  [Phase 2 计划](exec-plans/completed/2026-09-11-trader-core-phase2.md)、
  [Phase 3 计划](exec-plans/active/2026-09-12-trader-core-phase3.md)、
  [设计文档](TRADER_CORE_DESIGN.md)、[架构图](../ARCHITECTURE.md)、[金律](GOLDEN_PRINCIPLES.md)。
- 本文件只记录**代码里已经存在的行为**。每一项标注已实现或未实现，未实现项不因本文件存在而被视为完成。
- 代码是唯一事实来源；本文件与代码冲突时，以代码为准并修正本文件。
- 第 1–6 节描述 Phase 1 的交付；第 10 节是 Phase 2 的逐项交付；第 11 节是 Phase 3 的逐项交付。
  **Phase 3 尚在进行中**：T1、T2 已交付，T3–T4 未开始，未实现项一律留在第 7 节。

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

- **候选证据的结构化与生命周期**（Phase 3 的 T3，**未开始**）。`learning_candidates` 仍缺
  `claim` / `applicable_context` / `proposed_behavior_delta` / `evidence_episode_ids` 四列，
  状态列仍被 `CHECK(status = 'candidate')` 钉死成单值。
- **已批准知识快照**（Phase 3 的 T4，**未开始**）。无 `knowledge_snapshots`，
  于是「保留」与「生效」之间仍没有可审计的关口。
- **策略版本注册与前向影子实验**（Phase 4）。无 frozen policy、无独立前向评估闸门、无批准的晋升路径。
  `policy_ref` / `model_ref` 字段已预留但当前写入为空 —— 没有注册表可引用。

**Phase 2 主动不做（非目标，不是遗漏）**

- **费用引擎**。无逐笔建仓手续费落账。`COMMISSION_RATE` / `STAMP_DUTY_SELL_RATE` / `SLIPPAGE_RATE` 仍是**手工版本化的假设**，只在计算净收益时参与，不作为独立记账条目。设计 §6 自己写着「这不是普遍固定的市场常数」，§16 把「市场规则核实」列为非目标。**这是用户明确定的非目标，不要当作待办补上。**
- **部分成交模拟器**。无 `partially_filled`。本仓库没有撮合量与参与率的模拟，凭空造一个状态会让它永远进不去；退出侧的部分成交已由多条 `position_exits` 腿表达。
- **`rejected` / `cancel_pending` 两个订单状态**。`order_state.py` 声明了状态集与合法迁移，但这两个状态**尚未被任何写入路径产生** —— 它们是设计 §6 的目标态，当前只在状态机里被允许，未在业务里出现。
- **global 事件日志**。无跨表统一事件流。

**Phase 2 / Phase 3 只做到一半**

- **信息边界的强制校验**。现在**有消费者了**（第四轮新增）：`attribution.freeze` 拒绝晚于内核时钟的边界，成交时 `clock.guard_fill` 校验「下单决策的 `information_cutoff` 不得晚于成交日」。但设计 §15 的完整合同「Every actionable input satisfies the availability cutoff」**仍未成立**：只有这两处，校验的是**声明的边界本身**是不是未来，而不是「这次决策引用的每一个输入都落在边界内」。当前强制的仍然是「边界不可事后改写」（触发器 + 哈希）与「边界不得是未来」；**不是**「每个输入都被逐一验证过」。
- **挂单有效期是死代码**（T1 期间查出来的第三例「字段只写不读」）。`PENDING_EXPIRE_DAYS` 被写进每个订单行，
  `check_pending_orders` 每次都算 `days_pending`，**两者都没有任何读者**：有主线的挂单跟随主线生命周期，
  无主线的挂单在更早的一行就被当作「无关联主线」撤掉了，固定期限永远走不到。因此
  `episode_events.kind` 里**没有 `expire`** —— 声明一个没有写入路径的状态，正是本仓库已经犯过两次的
  「承诺无可调用入口」。该结论由
  `tests/test_episodes.py::TestEveryDeclaredKindHasAWriter::test_the_pending_order_expiry_is_still_dead_code`
  机械钉住。
- **平仓/减仓的 episode 事件只指向持仓，不指向账本腿**。`episode_events.ref_id` 对 `close` / `trim`
  记的是 `virtual_portfolio.id`；设计想要的那条「事件 → 账本行」的边**没有建立**，因为
  `portfolio_exit._close_position_impl` 的返回契约是 `bool`（S5 定下、有测试钉住），
  加宽它不属于 T1。要从事件找回当次腿，只能按 `position_id` + 日期在 `position_exits` 里查 ——
  同一天减两次仓就无法区分。**这是已知缺口，不是设计**；补法是让 `_close_position_impl` 返回 `exit_id`。
- **`episodes` / `outcomes` 的生产读者还很薄**。`episodes` 有一个只读入口
  （`scripts/episode_coverage.py`，读 `episodes.coverage` / `open_episodes`）与
  `episodes.get_episode` / `events_for`；它**不进 prompt、不进检索、不进决策上下文** ——
  这是刻意的（§10 说验证不等于授权）。T2 给了 `outcomes` 一个真实的写者
  （`pipeline/tasks/review.py` 的 `_label_outcomes`）与一个只读入口
  （同一个运维脚本，读 `outcomes.counts` / `pending_labels` / `integrity`），
  但**标签同样不进任何决策上下文**，也没有任何按 label 过滤/加权的读路径。
- **`outcomes` 的标签不与 `predictions.hit` 对账**。T2 新增的三条标签链是**增量**的：
  `predictions.hit` / `scored_at` 仍是原路径，写标签时**不校验两者一致**。
  两套记录目前可以互相矛盾而无人发现 —— 合并它们属于后续阶段。
- **`information_cutoff` / `content_hash` 的强制面仍与 §7 描述一致**，T2 未触及。
- **`sweep_trade_labels` 的 `pending` 标签不记当日的浮动盈亏**。开仓中的持仓只标
  `state=pending` + `legs=0`，**不写估值**：估值是市场数据的函数，标签是决策的函数，
  把 mark 写进标签会让「这个决策本身好不好」变成「今天行情好不好」。

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
| 学习单元 = 走完的盈利仓位（`thesis → order → exits` 只能表达成交过的决策） | 每个决策一个 `episodes` 行，**被拒 / 撤单 / 形状不合法的意图同样是一个 episode**；「决定了多少次、成了多少次」可机器统计 |
| 一次决策的经过只散落在各表里 | `episode_events`（append-only）按发生顺序记 `intent / order / fill / add / trim / close / cancel`，事件只引用行号，不复制金额 |
| 撤单是日志里的一行 | 撤单在 `_cancel_order_unlocked` 这个唯一落点记事件并结束 episode —— 含成交路径内部发起的两次撤单（回撤闸门、资金不足） |
| 「某个标签是哪版规则算的」只能靠 `scored_at` 猜 | `episodes` 挂在冻结的 `decision_snapshots.id` 上，边界只写一次、不可改指 |
| 覆盖度（决定数 vs 成交数）无法回答 | `episodes.coverage()` + `scripts/episode_coverage.py`：decisions / verdicts / refused / cancelled / traded / fill_rate |
| `predictions.hit` 是一个可被下一次评分覆盖的单元格 | 标签是 append-only 的 `outcomes` 行，带 `state` / `evaluator_version` / `available_at`；修正**追加**一行 `supersedes_id` 指回被修正行 |
| 「未成交 / 删失 / 修订」三种情况只能从 `hit IS NULL` 猜 | `pending / matured / censored / revised` 四态由 `outcomes.assert_outcome_transition` 断言，非法迁移抛 `IllegalOutcomeTransition` 且不落任何行 |
| 一个标签同时表示「预测对了」和「这笔赚了」 | `kind='forecast' / 'trade' / 'process'` 三条独立链；trade 标签只带 `exit_ids` 这类**引用**，不带金额 |
| 程序违规可以因为这笔赚了而被放过 | `assert_no_pnl_in_process` 拒绝 process 标签携带 `return_pct` / `pnl` / `profit` / `win` 等任一字段，抛错不 warning |
| 预测的评估窗口是一刀切的默认天数 | `predictions.horizon_days` / `deadline` 由写预测时**声明**；未声明的历史行在标签 evidence 里标 `legacy_horizon`，**不回填**一个它没做过的声明 |
| 撤单且从未成交的挂单会被当成一笔「打平的交易」 | `sweep_trade_labels` 直接跳过（无 exit 腿即无标签）；这次决策的痕迹留在 episode 里 |

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

### 第五轮（Phase 3 的 T1：决策 episode）

| 命令 | 结果 |
|---|---|
| `.venv/bin/python -m pytest tests/ -q`（**全量，不 ignore**） | 1401 passed, 18 skipped |
| `.venv/bin/python scripts/lint_harness.py` | 通过（147 个文件），存量 47 条待偿还 |
| `.venv/bin/python scripts/lint_docs.py` | 知识库校验通过 |

新增 `tests/test_episodes.py`（28 个用例），分五组：非成交决策的覆盖、一次决策一个 episode、
声明的诚实性、不可改写、读侧。

**计划里写下的验收条目，逐条对照**：

- 被拒绝的意图留下完整 episode（有 event、状态可知）→ `TestANonTradeIsStillADecision` 三条用例
  （重复建单被拒 / 形状不合法 / 撤单），外加一条**绕开意图门的撤单**（成交路径内部的资金不足撤单）。
- 同一 order 的 fill/add/trim/close 归到同一个 episode → `test_a_whole_trade_is_one_episode`
  用真实重放日期跑完 `open → fill → add → trim → close`，断言事件序列恰好是
  `intent, order, fill, intent, add, intent, trim, intent, close`。
- 两个 trader 同日推荐同一 code 是**两个** episode → `test_two_traders_on_one_stock_are_two_episodes`。
- 对 `outcomes` 行直接 UPDATE/DELETE 被触发器拒绝 → **T1 期间未做**（`outcomes` 无写者），
  已由 T2 补上：`tests/test_outcomes.py::test_a_label_cannot_be_updated_or_deleted`。

**本轮的四个变异探针**（先证明测试会红，再复原）：

| 探针 | 结果 |
|---|---|
| 门不再开 episode（`_start_episode` 直接 `return None`） | 21 failed / 7 passed |
| 成交不再记事件（注释掉 `episodes.note_fill`） | 4 failed / 24 passed |
| `episode_events` 的 append-only 触发器失效（`RAISE(ABORT)` → `SELECT 1`） | 1 failed / 27 passed |
| `link_snapshot` 不再 write-once（去掉 `IS NULL` 守卫） | 1 failed / 27 passed |

**本轮查出的「承诺超过代码」三例**，均已在 §7 如实标注，未靠改文档蒙过去：

1. 计划里点名的 `expire` 事件类型**没有写入路径**（挂单有效期是死代码），因此**没有**写进
   `episode_events.kind` 的 `CHECK`；用一条静态断言把「它仍然是死代码」钉住，
   这样将来真做有效期的人必须先删掉那条测试、再加事件类型。
2. 计划里点名的 `hold` / abstention 同样没有写入路径，理由是「本仓库没有任何地方**决定过**
   『今天不买』」，造一个空调用点就是本文件反复在抓的那类缺陷。
3. `close` / `trim` 的事件只能指向持仓行，**指不到账本腿**，因为
   `_close_position_impl` 的返回契约是 `bool`。按「不重建归属」的既有约定，
   这里没有用「同持仓最近一条腿」去凑，而是作为已知缺口写进 §7。

**`portfolio.py` 1197 行**（上限 1200），已是本阶段最紧的一处。T1 只往里加了 2 个调用点与 2 行注释；
下一次需要改动该文件前应先把「资金与主线敞口」那一组读函数拆出去，不要靠压缩注释换行数。

### 第六轮（Phase 3 的 T2：三种结果的生命周期）

2026-09-12：

| 命令 | 结果 |
|---|---|
| `.venv/bin/python -m pytest tests/ -q`（**全量，不 ignore**） | 1441 passed, 18 skipped |
| `.venv/bin/python scripts/lint_harness.py` | 通过（149 个文件），存量 47 条待偿还（**未扩充**） |
| `.venv/bin/python scripts/lint_docs.py` | 知识库校验通过 |

新增 `tests/test_outcomes.py`（40 个用例），分五组：状态机、不可变性与链形状、
§9 的三条禁令、声明的期限、三个扫描与 episode 归属。

**计划里写下的验收条目，逐条对照**：

- `pending → matured | censored` 合法、`matured → revised` 合法且追加、
  `matured → pending` 被拒且**不落任何行** → `TestTheStateMachine` 九条用例，
  含「被拒的迁移不得写入任何东西」的断言。
- 直接对 `outcomes` 行 UPDATE / DELETE 被触发器拒绝 → `test_a_label_cannot_be_updated_or_deleted`。
- 一条链只有一个头、一个行只能有一个后继 → 两条部分唯一索引各有一条用例证明索引本身会咬。
- §9 的三条禁令 → `TestTheThreeSubstitutionsAreProhibited` 六条用例；其中
  「不因赚钱而豁免」断言两条 process 标签**除主体身份外逐字段相同**，
  且 `virtual_portfolio` 里根本没有持仓 —— 让「胜负只能从账本读出」成为可验证的陈述。
- 3 天期限的预测 3 天后到期、5 天的 5 天后到期 → `TestTheHorizonIsDeclared`；
  未声明期限的行标 `legacy_horizon` 而不是被回填。
- 标签可归因到产生它的决策 → 成交过的预测其标签带 `episode_id`，
  没成交的预测标签 `episode_id IS NULL`（这就是覆盖度的陈述）。

**本轮的六个变异探针**（先证明测试会红，再复原）：

| 探针 | 结果 |
|---|---|
| `ensure_label` 退回 `declare`（拿到链头而不是活行） | 8 failed / 32 passed |
| 状态迁移守卫变成恒真（`assert_outcome_transition` 直接 `return target`） | 3 failed / 37 passed |
| `outcomes` 的 append-only 触发器失效（`RAISE(ABORT)` → `SELECT 1`） | 1 failed / 39 passed |
| `assert_no_pnl_in_process` 恒不报错（`found = []`） | 1 failed / 39 passed |
| 已 `matured` 的预测标签被重新派生（`if state == MATURED` → `if False`） | 1 failed / 39 passed |
| `sweep_process_labels` 的计数重新重叠（`elif` → `if` + 无条件 `unchanged`） | 2 failed / 38 passed |

**本轮查出并修掉的两个真缺陷**（都是测试先红，不是靠读代码发现的）：

1. **三个标签生产者拿到了链头而不是活行。** `outcomes.declare` 的契约是「返回这条链的
   **第一**行」，而生产者要的是「当前活着的行」。第二次跑扫描时它们去 `resolve` 一行
   已经被 `supersedes_id` 占用的行，撞 `UNIQUE constraint failed: outcomes.supersedes_id`。
   修法是新增 `outcomes.ensure_label`（`declare` 之后返回 `current`），三个生产者改用它。
   **`declare` 的返回语义本身没有改** —— 幂等的「我确保它存在」与「把它给我」是两件事。
2. **`sweep_process_labels` 的三个计数会重叠。** 一条 thesis 先成熟、当次又因为已结束而
   被追加修正时，`matured` 与 `revised` 各记一次；而未结束的成熟 thesis 被记进
   `matured` **又**记进 `unchanged`，于是「评了 40 条、其中 29 条什么都不用做」这句话
   在数上不成立。改成互斥分支，并在文档里写下「三者之和 = 本次评过的 thesis 数」这条不变量，
   由 `test_the_process_counts_partition_the_theses_graded` 钉住。

**本轮查出的「承诺超过代码」一例**，已在 §7 如实标注：

- `predictions.hit` / `scored_at` 与新的 `outcomes` 标签是**两套并行的记录**，
  写标签时**不对账**。§9 要求「预测标签」可独立于旧路径存在，但没要求它们一致，
  于是现在两套可以互相矛盾而无人发现。合并/对账属于后续阶段，不假装已经做完。

**一处操作风险（非代码缺陷，但值得记下）**：`alpha_agents.config.MEMORY_DB_PATH` 是**固定路径**，
不读 `TMPDIR`，测试靠 `conftest.py` 的 monkeypatch 才重定向。因此**临时脚本 / 冒烟脚本
默认会写进开发者本地的 `data/memory.db`**。T1 的 `scripts/episode_coverage.py` 是纯只读的所以没事；
本轮一次冒烟写入误加了 2 行 `predictions` + 1 行 `theses` + 50 行 `outcomes`，
已按时间戳逐行核对后清除、并验证 append-only 触发器复原。
**写临时脚本时先把 `memory_store.MEMORY_DB_PATH` 指到临时目录**（README 无此提示，故记在这里）。

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

## 11. Phase 3 逐项交付（T1–T4，进行中）

计划见 [Phase 3 计划](exec-plans/active/2026-09-12-trader-core-phase3.md)。本阶段有一句硬约束：
**不新增任何生效路径** —— 候选、快照都不进 prompt / 检索 / 决策上下文（§10：验证不等于授权）。

| 切片 | 状态 | 交付 |
|---|---|---|
| T1 episode 关联 | ✅ 已交付（第五轮） | `episodes` / `episode_events` 两表 + `alpha_agents/data/episodes.py`；门在 `intent.submit_intent`，成交/撤单两个钩子在 `portfolio`；只读入口 `scripts/episode_coverage.py` |
| T2 三种结果的生命周期 | ✅ 已交付（第六轮） | `outcomes` 状态机 + `alpha_agents/data/outcomes.py` 写读；三个标签生产者在 `evolution/outcome_labels.py`；写入者在 `pipeline/tasks/review.py::_label_outcomes`；`predictions.horizon_days` / `deadline` 两列 |
| T3 候选证据与生命周期 | ⬜ 未开始 | 四列与状态迁移均未做 |
| T4 已批准知识快照 | ⬜ 未开始 | `knowledge_snapshots` 未建 |

### T1 的边界，逐条说清

- **一个决策一个 episode**，在**业务规则跑之前**落库。因此「一个被拒绝的意图」「一个撤单」
  「一个形状不合法的意图」都留下完整的 episode，而「进程死在动作中间」表现为
  episode 的 `intent` 事件指向一行状态仍是 `submitted` 的意图 —— 这是可查的，不是空白。
- **归属用列不用重推**（沿用 Phase 1 的约定）：episode 存 `order_id` / `position_id`，
  事件存 `ref_id`。`order_id` 与 `position_id` 是同一张 `virtual_portfolio` 行的同一个 id
  （成交是原地 UPDATE，不是插新行），**但它们是两个不同的断言**：
  `position_id IS NOT NULL` 才等于「这次决策成交了」。
- **事件只引用行号，不复制金额**。`detail_json` 只在撤单时放一句原因（截断 200 字），
  金额一律留在 `position_exits` / `virtual_portfolio`。第二份 P&L 就是第二份真相。
- **不可变性交给数据库**：`episode_events` 的 `BEFORE UPDATE` / `BEFORE DELETE` 触发器 `RAISE(ABORT)`；
  `episodes` 的 `trader_id` / `code` / `opened_at` / `closed_at` / `decision_snapshot_id` 一经写入不可改，
  已关闭的 episode 不可重开。事后把学习单元改指到另一个决策，是每一种事后归因偏差的形状。
- **`episodes.code` 可为 NULL**，用于「这个决策没点名任何标的」（形状不合法的意图，
  或对一个不存在的持仓下动作）。丢掉这些行等于把不合格的决策从分母里删掉 —— 正是这张表要消除的偏差。
- **两个钩子是尽力而为**：`episodes.note_fill` / `note_cancel` 失败只记 warning，不阻断成交或撤单。
  「审计写不下」不能变成「交易没发生」，但必须留痕 —— 与 `attribution.freeze` 在其调用点的姿态一致。
  两者**都不取 `_write_lock`**：调用方已经在临界区里，而该锁不可重入。

### T2 的边界，逐条说清

- **标签是 append-only 的行，不是一个可写单元格。** `predictions.hit` 可以被下一次评分覆盖，
  而 §9 要的是「一个事后事件无法改写它的标签」+「事后能看出是哪个版本的评估器给出的」。
  因此每条标签带 `state` / `evaluator_version` / `available_at`；修正是**追加**一行
  `supersedes_id` 指向被修正的那行，`BEFORE UPDATE` / `BEFORE DELETE` 触发器 `RAISE(ABORT)`
  把不可变性交给数据库。
- **一条链只能有一个头、一个后继。** `declare` 幂等（`kind + subject_type + subject_id` 上
  的部分唯一索引，`WHERE supersedes_id IS NULL`），每个后继只能被认领一次
  （`supersedes_id` 上 `WHERE supersedes_id IS NOT NULL` 的部分唯一索引）。
  两条索引都在 SQLite 的「唯一索引里 NULL 互不相等」语义下才咬得住。
- **三个标签生产者都在 `evolution/`，不在 `data/`。** 分层方向是
  `data → sources → tools → evolution → …`，而过程评分器本来就在
  `evolution/process_quality.py`；把 `forecast` / `trade` / `process` 三个标签放一起，
  是因为它们是同一个决策的三个不同提问，不是为了整齐。
- **三条禁令各有一个可执行形式**（§9 的「不能拿一个替换另一个」）：
  - 平仓**不得重写**预测标签 —— `kind='trade'` 的标签只从 `position_exits` 派生。
  - **预测正确不等于已实现盈利** —— trade 标签只带 `exit_ids` 这类**引用**，不带金额；
    `assert_no_pnl_in_process` 拒绝 process 标签里出现 `return_pct` / `net_amount` /
    `pnl` / `profit` / `hit` / `win` 等任一字段（抛异常，不是 warning）。
  - **程序违规不因赚钱而豁免** —— 同一断言把「读 P&L 的评分器」挡在门外。
- **期限是「声明」，不是「假定」**。`predictions.horizon_days` / `deadline` 由写预测的人给出；
  历史行没有声明，`get_predictions_due_for_scoring` 回退 `DEFAULT_HORIZON_DAYS` 并在标签的
  evidence 里打 `legacy_horizon: true` —— **不为老数据回填一个它没做过的声明**。
- **未成交的订单拿不到 trade 标签，但决策仍被记录**。撤单且从未成交的挂单
  `sweep_trade_labels` 直接跳过（「没成交」和「成交后打平」是两件不同的事实，
  只有后者是关于技能的证据）；这次决策的痕迹留在 episode 里。
- **`sweep_process_labels` 的三个计数互斥**：每个被评的 thesis 只落进
  `matured` / `revised` / `unchanged` 之一，三者之和 = 本次评过的 thesis 数。
  一次扫描里「刚成熟又立刻修正」的 thesis 只记 `revised` 一次 —— 否则
  「多少条什么都不需要做」无法回答。
- **标签**不进**任何决策上下文。** T2 只写标签、只读标签；`hit` 的旧路径保持不变，
  两者尚未对账（见 §7）。

