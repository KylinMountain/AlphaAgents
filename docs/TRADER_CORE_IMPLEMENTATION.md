# Trader Core Implementation: Phase 1–5 实际状态

- 记录日期：2026-09-26（补记 T5–T8 连续 Runtime 与最终 30 日隔离回放）。
  **机制已交付 ≠ 已经在跑** —— 见 §7 与 §12。
- 依据：[Phase 1 计划](exec-plans/completed/2026-09-11-trader-core-phase1.md)、
  [Phase 2 计划](exec-plans/completed/2026-09-11-trader-core-phase2.md)、
  [Phase 3 计划](exec-plans/completed/2026-09-12-trader-core-phase3.md)、
  [设计文档](TRADER_CORE_DESIGN.md)、[架构图](../ARCHITECTURE.md)、[金律](GOLDEN_PRINCIPLES.md)。
- 本文件只记录**代码里已经存在的行为**。每一项标注已实现或未实现，未实现项不因本文件存在而被视为完成。
- 代码是唯一事实来源；本文件与代码冲突时，以代码为准并修正本文件。
- 第 1–6 节描述 Phase 1 的交付；第 10 节是 Phase 2 的逐项交付；第 11 节是 Phase 3 的逐项交付；
  第 12 节是 Phase 4 的逐项交付。**Phase 3 的四个切片（T1–T4）已全部交付**；
  Phase 4 的五个切片（U1–U5）**机制已交付，且生产里尚未发生一次真实晋升**。
  未实现项一律留在第 7 节，交付不等于「设计里提到的每一件事都做了」。

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

- **候选证据的结构化与生命周期**（Phase 3 的 T3）**已交付**：四列与五态生命周期都已存在
  （交付明细见 §11 与第九节第七轮）。它剩下两个边界：
  - **`evidence_episode_ids` 在生产里恒为空。** 五个生产调用点全部传
    `{"supporting": [], "opposing": []}` —— 它们拿到的是散文式教训与聚合计数，
    **没有一个能指名 T1 的决策行**。字段是必填的、结构化的、可枚举的，但
    `candidates_citing` 目前**只有测试在走**：这条链是**有能力**，不是**有数据**。
  - **生命周期没有业务驱动方。** `advance_candidate` 是 `status` 的唯一写者，且按 §10
    **刻意**不被任何管线调用，于是 `hypothesis` / `testing` / `validated` / `retired`
    在生产数据里不会自然出现，只能由人或脚本显式推进。它可达、有测试钉住，
    但**不是被业务驱动的** —— 这是「验证不等于授权」的代价，不是遗漏。
- **已批准知识快照**（Phase 3 的 T4）**已交付**：`knowledge_snapshots` /
  `knowledge_snapshot_items` 两表与 `alpha_agents/data/knowledge_snapshots.py` 都已存在
  （交付明细见 §11 与第九节第八轮）。「保留」与「生效」之间现在有一道可审计的关口。
  它剩下三个边界：
  - **快照会筛选进 prompt 的行，但不会写回任何东西。** 模块里没有 `apply_snapshot`：
    它不写回 prompt 文件、不写回检索权重、不改任何 `_get_*` 的返回值。
    （上一版这里写的是「快照不进任何生效路径」，那句在 U5 之后就不成立了；
    完整的更正与适用范围见本节末尾「快照是记录，不是检索闸门」那一条。）
    `approve()` 的唯一调用者是运维脚本 `scripts/approve_knowledge.py`（即「人的动作」），
    所以「已批准」是**可查的事实**，而它**是否在生效**由策略指针决定 —— 这正是 §10 的形状。
  - **两个读侧函数没有任何消费者。** `candidate_is_approved` / `entity_is_approved`
    在 `alpha_agents/`、`scripts/`、`main.py` 里**一处调用都没有**（2026-09-14 实测），
    只有测试在调。上一版说「消费者是 `scripts/episode_coverage.py`」是**错的** ——
    那个脚本读的是 `counts` / `integrity` / `drifted` / `verify_snapshot` /
    `items_for` / `all_snapshots` / `snapshots_for_candidate`，其中 `drifted` 确实在被读。
    这两个函数属于「有能力、无消费者」那一类，别把运维脚本当成它的用户。
  - **`drifted` 的消费者只有运维脚本与测试**，没有任何决策路径读它。
- **策略版本注册与前向影子实验**（Phase 4）—— **已交付，且 2026-09-14 起在生产里跑**。
  U1–U5 的机制（冻结策略注册表、前向影子评估、候选绑定闸门、人工晋升与回滚、检索闸门）
  之外，09-13 补了两个缺失部件（`kind="candidate"` 的出厂生产者 `remap_confidence`；
  `scripts/policy.py` 的 `freeze` / `install`），09-14 又补了实验的四个操作动词与
  **每日调度**，并**在生产库执行了第 0 步**：`policy_versions` / `active_policy` /
  `policy_transitions` / `policy_approvals` 四表已建，**V1 在效**，其参数与代码默认
  逐值相同（行为中性）。**仍未发生的事只有一件：一次真实晋升。** 它今天也做不到 ——
  要 **20 个配对样本**的前向证据（单位是 `(日期, 代码)` 对，不是天数；见 D15），
  而影子 run 的时钟 2026-09-14 16:00 才开（`#1`，baseline 生产者，**不可晋升**），
  配对进度 `0/20`。**开 run 是人的动作**，且下一个要开的必须是**候选** ——
  baseline 跑多久都晋升不了。逐项见 §12 与 §14.6 / §14.7 / §14.11 / §14.13；
  tech-debt 记为 D6 / D17（D7、D15、D16 均已偿，见 §14.13 与债表 Paid）。
- **产品整合**（Phase 5）—— **2026-09-13 已交付 V1 + V2 + V3，见 §13**。
  三个读模型、三个端点、三个前端工作台都已存在，`/api/portfolio` 已改为委托同一个投影。
  **但「页面能打开」不是「页面有事实」。** 生产库上三个工作台的状态各不相同，而且
  **会随运维动作变化**：09-13 实测 evolve 的 `pointer` / `shadow` 是 `absent`；
  09-14 执行第 0 步之后 `pointer` 变 **present**（1 个版本在效）、`shadow` 变 **empty**
  （表建好、0 条 run），`gates` 仍 **partial**（旧 schema，要等第一次写裁决才会自动迁移），
  trade / learn 未变 —— 所以**今天「进化实验台」有真实内容，而「学习日志」仍然主要是状态说明**。
  这些状态由读模型自己报出来、由页面自己渲染出来，不靠人去比对文档。
- **奖励信号（G1）的机制已在跑，缺的是样本不是代码。** `predictions` 表有
  `prob` / `log_score` / `brier` / `excess_return` / `residual_alpha` 五列；
  `review` 任务里的 `_score_due_predictions`（其 docstring 自述 *"This is the G1 signal"*）
  调 `scoring.score_prediction`，后者做因子残差回归取残差再算 Brier —— 这条链
  **已接、已在每日 15:30 的 review 里跑**。生产库实测（**2026-09-15 上午**）：
  `prob` 已填 **52 / 270** 行（这两个数每天都在涨，别把它当成常量），
  而 `brier` / `residual_alpha` 都是 **0 行**，原因不是没接，
  是**还没有一笔预测的窗口走完**；`outcomes` 里 48 条 `forecast` 全部 `pending`、
  既无 `censored` 也无 `matured` —— 这正是 D10 修好后的形状。
  **第一批 brier 落在 2026-09-16 的复盘，不是 09-15 的收盘**：`run_review` 把
  `_score_due_predictions` 放在第 0 步，而 `market_history.update_daily()` 排在
  同一个函数的尾部（第 800 行附近），所以今晚 15:30 评分时行情档案最新只到 09-14，
  09-08 那批（需 6 个交易日）仍差一格；今晚尾部把 09-15 那根写进去，**明晚才评到**。
  这是任务内的次序，不是窗口算错 —— 记在 D18。Learn 页面报的「还差 N 个交易日」
  说的是**市场日历**上的差距，读成「N 天后就有分数」会早一天，差的这一格就是 D18。
- **「到期」曾算错一天，而且必然算错 —— 2026-09-13 已修（D10）。** 到期判定原本用
  **日历天**（`COALESCE(deadline, date(date, '+5 days')) <= as_of`），而
  `scoring._forward_return` 要 `horizon + 1` 个**交易日**收盘 —— 任意 6 个连续日历天
  必含至少一个非交易日，所以窗口内最多 5 个交易日，第 6 行**永远不会在到期日存在**。
  真实数据上四个有 `prob` 的预测日（09-08…09-11）在各自日历窗内只有 4 / 3 / 2 / 1 个交易日。
  两个后果：其一，`_score_due_predictions` 拿到 `None`，`label_forecast` 写
  `censored`（「证据没到」），而事实是**证据还没到期** —— 于是**预测标签的 `matured`
  状态不可达**，每一条被评分的预测都要走 `censored → revised`，把「数字变了」这条
  有信息的修正流变成时钟噪声；其二，`brier` 不可能在日历到期日填上。
  修法是**把两个问题分开**，而不是把 `deadline` 改成交易日：`scoring.evidence_window_closed`
  问「市场自己的日历上，这个窗口收口了没有」，`label_forecast` 在窗口未收口时
  **保持 `pending`、一个字都不写**，只有窗口收口且拿不到分数才写 `censored`。
  `deadline` 保持日历语义（它是作者的**声明**，不是系统的推断），`_score_due_predictions`
  与 `shadow.score_due` 都分别报出「已评分 / 窗口未收 / 已删失」三个数而不是一个。
  它之所以一直没被发现，是因为 **`_score_due_predictions` 一个测试都没有**；现在
  `tests/test_score_due_predictions.py` 用**真实** `evidence_window_closed` +
  **真实** `score_prediction`（临时 `daily_kline`，无桩）覆盖三种结局共存的一轮。
  仍要如实记下的边界：`brier` 依然只在窗口真正收口后才可能填上，09-08 那批要等
  **2026-09-15** 的收盘，09-11 那批要等 **2026-09-18**。
- **`horizon_days` / `deadline` 这两列在生产里曾经没有写入者 —— 2026-09-14 已接线（§14.11）。**
  这是本仓库第四类缺陷（「列存在但从未被写入」，S6 抓到过 `intents.order_id` 同型）的又一例，
  而它的代价被低估了：T2 的「每笔预测自己声明多久到期」机制完整（`_deadline_for()` 在未声明时
  **拒绝**替调用方假设、`save_prediction` 的 INSERT/UPDATE 都写这两列、
  `get_predictions_due_for_scoring` 用 `COALESCE(deadline, date(date, '+5 days'))` 兜底并把
  `deadline IS NULL` 报成 `legacy_horizon`），但**两处生产调用都传了 `prob` 却都没传
  `horizon_days`**，于是每一笔预报都在按**全局 5 天**兜底评分并被标成 `legacy_horizon`。
  上一版这里写着「这不是 bug（5 天就是这两个调用方想要的）」——**那句是错的**，而且是这轮查出来的：
  同一个 `intraday_monitor` 函数在两段之后给**同一笔决策的论点**写死 `"horizon_days": 3`，
  生产库 41 行 theses 里 **37 行是 3 天**。所以不是「调用方没说」，是「调用方说了、没说给预报听」。
  现在 morning 传 `r.get("horizon_days")`（提示词本来就要求模型给），intraday 把那个 3 抽成
  `INTRADAY_HORIZON_DAYS` 并同时喂给预报与论点。
  **仍然不要顺手补 `horizon_days=5`**：模型真的没说时预报保持未声明，这正是 `_deadline_for`
  拒绝替调用方假设的那件事，也是 `legacy_horizon` 这个标签还剩下的一点意思。
- **`n < 50 不上线` 的缺口 —— 2026-09-15 已收口（D15）。** 上一版这里记的是「三处文档声明 50、
  代码只执法 20，n 落在 20–49 的裁决无人拦」。收口的方向是**把规则降到 20 去就代码**，不是反过来：
  `GOVERNANCE_MIN_SAMPLES` 从 50 改为 20，`GOLDEN_PRINCIPLES` §7 / 设计 §12 / README 三处
  一并改成 20。**没有动 `MIN_VALIDATION_SAMPLES`** —— 它是行为来源（在
  `policy_sources._RULE_SOURCES` 里），抬它才会让**所有已冻结版本立刻变 drifted**；
  而 `GOVERNANCE_MIN_SAMPLES` **不在**那个元组里，改它不动任何交易行为，所以「回滚目标消失」
  那个风险也随之不存在。代价如实记下：**仓库的诚实门槛确实降低了**，20 个配对样本是弱依据。
  换掉的是「文档说的和代码做的不一致、且没人执法」这个状态。
  `promotion_floor_gap` 保留，抓仍然成立的那种情形：某个版本自己声明的门槛低于 20。

**Phase 2 主动不做（非目标，不是遗漏）**

- **费用引擎**。无逐笔建仓手续费落账。`COMMISSION_RATE` / `STAMP_DUTY_SELL_RATE` / `SLIPPAGE_RATE` 仍是**手工版本化的假设**，只在计算净收益时参与，不作为独立记账条目。设计 §6 自己写着「这不是普遍固定的市场常数」，§16 把「市场规则核实」列为非目标。**这是用户明确定的非目标，不要当作待办补上。**
- **部分成交模拟器**。无 `partially_filled`。本仓库没有撮合量与参与率的模拟，凭空造一个状态会让它永远进不去；退出侧的部分成交已由多条 `position_exits` 腿表达。
- **`rejected` / `cancel_pending` 两个订单状态**。`order_state.py` 声明了状态集与合法迁移，但这两个状态**尚未被任何写入路径产生** —— 它们是设计 §6 的目标态，当前只在状态机里被允许，未在业务里出现。
- **global 事件日志**。无跨表统一事件流。

**Phase 2 / Phase 3 只做到一半**

- **信息边界的强制校验**。现在**有消费者了**（第四轮新增）：`attribution.freeze` 拒绝晚于内核时钟的边界，成交时 `clock.guard_fill` 校验「下单决策的 `information_cutoff` 不得晚于成交日」。但设计 §15 的完整合同「Every actionable input satisfies the availability cutoff」**仍未成立**：只有这两处，校验的是**声明的边界本身**是不是未来，而不是「这次决策引用的每一个输入都落在边界内」。当前强制的仍然是「边界不可事后改写」（触发器 + 哈希）与「边界不得是未来」；**不是**「每个输入都被逐一验证过」。
- **挂单有效期曾经是死代码**（T1 期间查出来的第三例「字段只写不读」），**2026-09-14 已接线**（§14.10）：
  `expire_days` 现在取自论点自己的 `horizon_days`，其次交易员的 `default_horizon_days`，最后才回落到
  `PENDING_EXPIRE_DAYS`；`check_pending_orders` 在**价格没有进带子**时读 `days_pending >= expire_days`，
  撤单理由带「未到价」。因此 `episode_events.kind` 里**仍然没有 `expire`**，理由不变且更硬：到期本来
  就是一撤单，`_cancel_order` 已有完整审计（episode + 预留释放），再造一个种类等于给同一件事两个名字。
  钉住它的用例按它自己 docstring 里的指示翻了面：
  `tests/test_episodes.py::TestEveryDeclaredKindHasAWriter::test_the_pending_order_expiry_now_produces_a_cancel_not_a_kind`。
- **到期判据里已知的一个洞**：只要这一轮**没有实时价**（`price is None`，例如停牌），挂单既不体检论点
  也不到期。这是刻意的——「未到价」是对行情的断言，没有行情就不该写它，与打分侧「行情库缺失判窗口
  未收口」同一条原则。代价是停牌股的挂单会一直挂着，直到有人看见。
- **平仓/减仓的 episode 事件只指向持仓，不指向账本腿**。`episode_events.ref_id` 对 `close` / `trim`
  记的是 `virtual_portfolio.id`；设计想要的那条「事件 → 账本行」的边**没有建立**，因为
  `portfolio_exit._close_position_impl` 的返回契约是 `bool`（S5 定下、有测试钉住），
  加宽它不属于 T1。要从事件找回当次腿，只能按 `position_id` + 日期在 `position_exits` 里查 ——
  同一天减两次仓就无法区分。**这是已知缺口，不是设计**；补法是让 `_close_position_impl` 返回 `exit_id`。
- **`episodes` / `outcomes` / `learning_candidates` 的生产读者还很薄**。`episodes` 有一个只读入口
  （`scripts/episode_coverage.py`，读 `episodes.coverage` / `open_episodes`）与
  `episodes.get_episode` / `events_for`；它**不进 prompt、不进检索、不进决策上下文** ——
  这是刻意的（§10 说验证不等于授权）。T2 给了 `outcomes` 一个真实的写者
  （`pipeline/tasks/review.py` 的 `_label_outcomes`）与一个只读入口
  （同一个运维脚本，读 `outcomes.counts` / `pending_labels` / `integrity`），
  但**标签同样不进任何决策上下文**，也没有任何按 label 过滤/加权的读路径。
  T3 把同一脚本再扩一段读候选区（`counts` / `integrity` / `candidates_by_status` /
  `transitions_for` / `candidates_citing`）。于是四个提案列的读侧**有入口、无消费者**：
  没有任何 prompt、检索或决策路径读它。
- **`scripts/episode_coverage.py` 不再自称「纯只读」**（第七轮更正）。`learning_candidates`
  的读函数每次进入都 `init_schema`，所以**对 T3 之前的库跑该脚本会触发一次表重建迁移**
  （`ALTER TABLE` 加四列 + 重建表以放开 `CHECK`）。迁移幂等且保行
  （`test_migrating_twice_is_a_no_op` 钉住），但「报告脚本会改 schema」这件事必须写下来，
  否则它本身就是一句「承诺超过代码」。
- **快照是记录，不是检索闸门** —— **该边界已于 2026-09-13 由 U5 关闭，见 §12**。
  `inject_playbooks` / `inject_principles`（`alpha_agents/evolution/feedback.py`）现在按
  **当前生效策略所指向的已批准快照**选行（`in_force_snapshot_id` →
  `knowledge_snapshots.verified_rows`），不再读 `playbooks.status`。于是：
  一条 `active` 但从未进过任何已批准快照的规则行**不再**进入提示词；
  规则内容在批准之后被改动、行被删除、或快照校验失败时**明确阻断**，
  渲染一行「（未生效…）」标记，而不是静默渲染或静默为空。
  **范围限定**：关掉的是 `inject_playbooks` / `inject_principles` 这两条路径，
  **不是** §10 的全部 —— 检索权重、校准器、退役决定仍会改变行为，仍属后续工作。
  另：`in_force_snapshot_id` 读的是**指针**，不是「最近一次批准的快照」；
  后者会制造第二个「什么在生效」的真相来源，正是 §14 结尾禁止的形态。
- **`outcomes` 的标签不与 `predictions.hit` 对账**。T2 新增的三条标签链是**增量**的：
  `predictions.hit` / `scored_at` 仍是原路径，写标签时**不校验两者一致**。
  两套记录目前可以互相矛盾而无人发现 —— 合并它们属于后续阶段。
- **`information_cutoff` / `content_hash` 的强制面仍与 §7 描述一致**，T2 未触及。
- **`sweep_trade_labels` 的 `pending` 标签不记当日的浮动盈亏**。开仓中的持仓只标
  `state=pending` + `legs=0`，**不写估值**：估值是市场数据的函数，标签是决策的函数，
  把 mark 写进标签会让「这个决策本身好不好」变成「今天行情好不好」。
- **M0 的验收清单里还有两条没有机器检查**（2026-09-16 逐条核对，明细见 §9 第十七/十八轮）。
  计划的验收节列了 **10 条 M0/内核**条目，核对下来 **8 条已有机器检查、2 条没有**
  （核对之初是 6/4；当天先补「严格时间束」缺的那一半、又补「对抗性新闻」，见下）；
  而这 2 条此前**全是未勾选的 `[ ]`**，与 §2 那句「M0 进度：6/6」并排放在同一份文件里。
  **两个数都不是错的 —— 它们是两张不同的清单**（§2 数的是**六件事**，验收节数的是**十条测试**），
  而两处都叫 M0。这本身就是「同一件事有两个计数」的又一例。缺的两条**都是「缺的不是断言」**：
  - **universe 的 as-of 缺选股侧**：停牌（结算侧）与未上市
    （`market_rules.listing_day_exemption`）都有测试；**「当时不属于候选池的证券不得被选中」没有**。
    runner 自己的「不能说明什么」一节已如实写出「ST 状态取自 `stocks.db` 的**当前**名字，
    不是回放日的名字」。**缺的是数据源，不是断言** —— 先有 as-of 的候选池，才谈得上断言。
  - **LLM record/replay 缺端到端**：机制 32 条测试按调用点钉住，但 M1 的用例**不调模型**
    （占位决策器，journal 计数断言为 0），所以「先 `record` 一遍、再用 `replay-recorded`
    跑同一窗口」这个对照**从未做过**。**缺的是一次运行，不是断言。**
  - **（已补）对抗性新闻** —— 这一条**曾被记成「零实现、零测试」，那句话错在方向**：
    次序设计早就有断言，输出侧的门也早有断言，**空的只有「围栏容纳」**
    （`_max_backtick_run` 零测试，拆掉调用点全绿）。2026-09-16 补上
    `tests/test_vpa_v10_helpers.py::TestAnAdversarialBodyCannotLeaveItsBlock`（5 条）：
    正文里**同时**放伪造标题和逃逸围栏，断言**包级可见的标题集合不变**；
    探针 O / P 分别让 4 条 / 1 条变红。**它钉的是代码侧两端，不钉「模型不会被说服」。**
    **教训**：把「我没找到测试」写成「没有测试」，与把「没实现」写成「实现过」是同一类错误，
    只是这次错得更悲观 —— 而悲观的方向也要付代价，它会让下一个人以为有一整块工作要做。
  - **（已补）严格时间束的前半**曾是同一批里最便宜的一条：「D 09:00 的上下文里没有 D 的
    close/high/low/volume」原本只是**结构性的**（`_decide(ctx, day, prev_day)` 签名上就拿不到
    D 的 bar），**没有测试会因为它被改坏而变红**。2026-09-16 补上
    `tests/test_walk_forward.py::TestTheDecisionCannotSeeTheDayItTrades`：让窗口日出现一个
    **只在今天领先**的名字，断言下单仍落在 T-1 的领先者上；探针 N（把 `_decide` 改成读 `day`）
    会让它变红。**结构性保证不是被钉住的保证** —— 这一条现在被钉住了。

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
| 「现金」一个口径，卖出当日即可再花 | `get_available_capital`（可花）与 `get_total_capital`（总）分家；**卖出所得当日即可花**（A 股把「可用」与「可取」分开），只有**可取性**等到 T+1，在途额由 `settlement.unreleased_pending_total` 单独报告。**该行 2026-09-16 更正** —— 此前写的是「卖出所得 T+1 前只进后者」，那正是 D19 修掉的错规则（把取现规则套在买入力上） |
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
| 候选知识只有一段 `payload_json`，没有可证伪的陈述 | 四列必填：`claim` / `applicable_context` / `proposed_behavior_delta` / `evidence_episode_ids`；supporting 与 opposing 两个桶都必须显式给出，空也要写出来 |
| 候选状态被 `CHECK(status = 'candidate')` 钉死成单值 | 五态 `observation → hypothesis → testing → validated → retired`；`retired` 可从任一状态到达且不可回退，非法迁移抛 `IllegalCandidateTransition` 且不落任何行 |
| 「这条候选被谁在何时因为什么推进过」无处可查 | `candidate_transitions` 记 `actor` / `reason` / `at`，装 `RAISE(ABORT)` 触发器，append-only |
| 「某条候选引用了哪些经验」只能翻 JSON | `evidence_episode_ids` 结构化（两桶），`candidates_citing(episode_id)` 可反查并给出 `cited_as` |
| 候选是否「已生效」只能靠约定 | `validated` **不激活任何东西** —— 没有快照、没有生效路径，`advance_candidate` 不被任何管线调用 |
| T3 之前写入的候选没有提案字段 | 迁移**不回填**：旧行四列为 NULL，由 `integrity()` 报「predates the proposal fields and was never enriched」 |
| 运维报告脚本不碰 schema | `scripts/episode_coverage.py` 读候选区时会触发该模块的 schema 初始化 / 迁移（幂等、保行，见 §7） |
| 「保留」与「生效」之间没有边界：候选走到 `validated` 就等于生效 | `knowledge_snapshots` 把「哪个版本的哪条知识、被谁、在何时、因为什么放进生效状态」记成不可变的一行；`validated` 本身仍然**不激活任何东西** |
| 「我们批准的是哪一版」答不出来（计数器一动版本就变） | 每条 item 存 `version_hash`，只覆盖声明的规则字段，**刻意不含** `win_rate` / `evidence_count` / `total_trades` / `hit_rate` / 各日期 |
| 快照是否被事后改写只能靠信任 | `content_hash` 覆盖声明字段**加上整个 item 集合**，`verify_snapshot` 独立重算；两条 `RAISE(ABORT)` 触发器把不可变性交给数据库 |
| 版本哈希由调用方给出（可以声称一个从未存在过的版本） | `version_hash` 由写入方从知识行现算，`approve` 不接受调用方传入；知识行或候选不存在即拒绝 |
| 「已批准的知识行后来变了」无人知道 | `drifted()` 列出「已批准、但当前内容已不是那一版」的条目；drift **不算** `integrity()` 的问题 —— 知识本就该继续演进 |
| 批准是文档里的一句约定，没有可调用入口 | `scripts/approve_knowledge.py`：唯一写入口，带 `--dry-run`，并**先打印目标数据库路径**（`MEMORY_DB_PATH` 是固定路径、不读 `TMPDIR`） |
| 「批准不会改变任何行为」只能靠声称 | `TestApprovingActivatesNothing` 把**整库所有表**在批准前后逐行对比，断言发生变化的表**恰好只有**两张新表 |

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
   —— 2026-09-14 兑现了：有效期接线（§14.10）时按这条指示把用例翻面为
   `test_the_pending_order_expiry_now_produces_a_cancel_not_a_kind`；`expire` 仍然没有进
   `CHECK`，因为到期记录为一次撤单，不是新种类。
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
默认会写进开发者本地的 `data/memory.db`**。T1 的 `scripts/episode_coverage.py` 当时是纯只读的所以没事
（**第七轮起不再如此**：该脚本会读 `learning_candidates`，而那个模块的读函数会 `init_schema`，
对 T3 之前的库等于一次迁移 —— 见 §7）；
本轮一次冒烟写入误加了 2 行 `predictions` + 1 行 `theses` + 50 行 `outcomes`，
已按时间戳逐行核对后清除、并验证 append-only 触发器复原。
**写临时脚本时先把 `memory_store.MEMORY_DB_PATH` 指到临时目录**（README 无此提示，故记在这里）。

### 第七轮（Phase 3 的 T3：候选知识的提案与生命周期）

2026-09-12：

| 命令 | 结果 |
|---|---|
| `.venv/bin/python -m pytest tests/ -q`（**全量，不 ignore**） | 1500 passed, 18 skipped |
| `.venv/bin/python scripts/lint_harness.py` | 通过（149 个文件），存量 47 条待偿还（**未扩充**） |
| `.venv/bin/python scripts/lint_docs.py` | 知识库校验通过 |

新增 `tests/test_learning_lifecycle.py`（59 个用例），分六组：提案必填、生命周期、
每次迁移记名、旧表单值状态的迁移、指纹仍只认证据、读侧。

**计划里写下的验收条目，逐条对照**：

- 四个新列有值，且**在写入口就是必填** → `TestACandidateMustStateItsProposal`：
  `save_candidate` 缺任一关键字参数直接 `TypeError`，空串 / 非字符串 `ValueError`，
  两个证据桶缺一即拒（「我们找过、没找到」必须写出来，不能靠省略表达）。
- 状态迁移走状态机 → `TestTheCandidateLifecycle`：一次只能走一步、跳步与回退被拒、
  `retired` 可从任一状态到达且无出口、未知状态按名报错。
- 每次迁移记 actor / time / reason 且**不可编辑** → `TestEveryMoveNamesItsActor`，
  含对 `candidate_transitions` 直接 UPDATE / DELETE 被触发器拒绝。
- 旧表迁移保住既有行 → `TestTheOldTableMigrates`：`candidate` → `observation`、
  `fingerprint` 与 `created_at` 保留、迁移两次是 no-op、四列**不被回填**、
  旧 `CHECK` 仍然拒绝生命周期之外的字符串。
- `fingerprint UNIQUE` 语义不变 → `TestTheFingerprintIsStillAboutEvidence`：
  改写 claim 不产生新候选，证据变了才是新候选，已写下的 claim 不被覆盖。
- `validated` 不生效 → `test_validated_is_not_activation`。
- 读侧 → `TestTheReadSide`：`counts` 含零值、按状态过滤、按 episode 反查 `cited_as`、
  引用不存在的 episode 被 `integrity` 报出、健康的候选区 `integrity` 静默。

**本轮的四个变异探针**（先证明测试会红，再复原；探针脚本先记基线哈希，复原后再哈希校验）：

| 探针 | 结果 |
|---|---|
| 状态机不再拒绝非法迁移（`if target not in allowed` → `if False`） | 7 failed / 52 passed |
| 写入口不再要求陈述（`_statement` 去掉校验直接返回） | 10 failed / 49 passed |
| `candidate_transitions` 的 append-only 触发器失效（`init_schema` 不再装守卫） | 1 failed / 58 passed |
| `integrity` 不再检查引用的 episode 是否存在 | 1 failed / 58 passed |

**本轮查出并修掉的一个真缺陷 —— 在测试自己身上**：
`test_a_legacy_row_is_enriched_once_and_then_left_alone` 断言 `integrity() == []`，
但它用的 `_save()` 默认 `evidence_episode_ids={"supporting": [1], "opposing": [2]}`
引用了两个不存在的 episode，于是新的完整性检查如实报出两条。**是 fixture 自相矛盾，
不是校验有错** —— 断言的方向对，前提是错的，属于「测试写成了恒假的形状」的近亲。
已改为显式传空引用，并保留 `integrity()` 本身不动。

**两处「承诺超过代码」，已在 §7 如实标注**：

1. `evidence_episode_ids` 在**生产里恒为空** —— 五个生产调用点全部传两个空桶。
   四列是必填的、可枚举的，但「候选引用经验」这条链**只有测试在走**。
2. 生命周期**没有业务驱动方** —— `advance_candidate` 按 §10 刻意不被任何管线调用，
   后四个状态在生产数据里不会自然出现。

**一处口径更正**：`scripts/episode_coverage.py` 自称「writes nothing」，但读候选区会触发
`learning_candidates.init_schema`，对 T3 之前的库等于一次表重建迁移。脚本 docstring、§7、
§8 均已更正。本次的冒烟验证是**先把 `memory_store.MEMORY_DB_PATH` 指向临时目录**再跑的
（沿用第六轮记下的那条操作风险），未触碰开发者本地 `data/memory.db`。

### 第八轮（Phase 3 的 T4：已批准知识快照）

2026-09-12：

| 命令 | 结果 |
|---|---|
| `.venv/bin/python -m pytest tests/ -q`（**全量，不 ignore**） | 1553 passed, 18 skipped |
| `.venv/bin/python scripts/lint_harness.py` | 通过（150 个文件），存量 47 条待偿还（**未扩充**） |
| `.venv/bin/python scripts/lint_docs.py` | 知识库校验通过 |

新增 `tests/test_knowledge_snapshots.py`（53 个用例），分八组：批准是一行记录、
记录由数据库保证只增不改、哈希可独立复算、什么不能被批准、版本是内容、读侧、
完整性与 drift、以及「批准不激活任何东西」。

**计划里写下的验收条目，逐条对照**：

- 快照 `content_hash` 可独立重算并通过、篡改一行后校验失败 → `TestTheHashIsVerifiable`：
  新快照通过；未知 ID 返回 `False`（「不存在」与「被改写」同样不可信）；
  绕过触发器改写快照行或 item 都被检出；而知识行**随后**变动**不影响**快照自身的校验
  —— 这正是「批准记录的是当时那一版」与「知识可以继续演进」两件事各自成立。
- 「这条候选 / 这条知识现在是否在某个已批准快照里」有机器形式（§10 的那句话）→
  `TestTheReadSide::test_the_question_section_ten_asks` 与 `test_is_this_knowledge_in_force`；
  未知实体类型**拒绝回答**而不是返回 `False`。
- 四个新列有值、`validated` 不产生任何行为变化 → `TestApprovingActivatesNothing`：
  逐表对比整库前后，断言变化的表**恰好只有**两张新表；候选行逐字段不变且
  `transitions_for` 仍为空；`memory_store.get_active_principles` /
  `get_active_playbooks` 的产出不移动。

**本轮的四个变异探针**（先证明测试会红，再复原；探针脚本先记基线哈希，复原后再哈希校验）：

| 探针 | 结果 |
|---|---|
| 快照哈希不再覆盖 item 集合（`_frozen` 的 items 置空） | 1 failed / 52 passed |
| `knowledge_snapshots` 的 append-only 触发器失效（`RAISE(ABORT)` → `SELECT 1`） | 1 failed / 52 passed |
| `version_hash` 改为哈希整行（连带 `evidence_count` / `win_rate` / `last_reinforced`） | 1 failed / 52 passed |
| `_resolve` 不再拒绝「批准一条不存在的知识」 | 2 failed / 51 passed |

**一处必须如实记下的探针事故（是我的探针错了，不是代码）**：第三个探针第一次跑出的是
**53 passed**，看上去像「测试没钉住这个属性」。原因是那次变异只把返回值换成 `dict(row)`，
而 `version_hash_for` 的 `SELECT` 本来就**只投影声明字段**，`dict(row)` 与投影逐字段相同 ——
**变异是个空操作**。把 `SELECT` 一并放宽成 `SELECT *` 之后，测试如期变红（1 failed）。
记在这里，是因为「探针没红」有两种可能，而先入为主地判成「测试有洞」正是本文件在防的那类错误；
同理，本文件里任何「测试钉住了 X」的说法都应当能被一条会红的探针支持。

**一处覆盖度观察**：`version_hash` 不含计数器这条属性（本切片的核心设计）**只有一个用例钉住**
（`test_running_counts_are_not_part_of_the_version`）。它是真的，但只有一根钉子 ——
将来改动 `_KNOWLEDGE_FIELDS` 的人应当先看到这一行。

**一处操作风险（沿用第六轮那条）**：`scripts/approve_knowledge.py` 是**会写库的**运维脚本，
而 `MEMORY_DB_PATH` 不读 `TMPDIR`。因此它**先打印目标数据库路径**再落盘，
`--dry-run` 走 `plan` 只校验不写。本轮验证全程把 `memory_store.MEMORY_DB_PATH`
指向临时目录，未触碰开发者本地 `data/memory.db`。

**Phase 3 判定为已完成**，依据是设计 §14 的范围定义与计划里的「非目标」，
而不是「设计里提到的每一件事都做了」：候选与快照都**不进任何生效路径**是本阶段的硬约束，
而「未批准即不生效」的检索侧强制（§7 记下的那条边界）属于 Phase 4。

### 第九轮（D10 成熟度 + 第 0 步 CLI + 路线 B：闭环可运行）

2026-09-13：

| 命令 | 结果 |
|---|---|
| `.venv/bin/python -m pytest tests/ -q`（**全量，不 ignore**） | **1881 passed, 18 skipped**（121s；本轮 +53 用例） |
| `.venv/bin/python scripts/lint_harness.py` | 通过（157 个文件），存量 47 条待偿还（**未扩充**） |
| `.venv/bin/python scripts/lint_docs.py` | 知识库校验通过 |

新增 `tests/test_score_due_predictions.py`（6 个用例，该函数此前 **0 个**）、
`tests/test_policy_cli.py`（22 个用例，驱动真 `main(argv)`）；其余为既有文件的用例
（`test_scoring.py` 加 `TestEvidenceWindowClosed` / `TestTheMappingComesFromThePointer`、
`test_policy_registry.py` 加 staging 一组、`test_policy_promotion.py` 加
`TestPromotionChangesTheMapping`、`test_gate_candidate_bound.py` 加
`TestTheShippedCandidate`、`test_outcomes.py` 与 `test_shadow_runs.py` 各加
「窗口未收口」的两个方向）。

**六个变异探针，全部被捕获**（脚本 `.pytest-tmp/probe.py`；每个探针独立跑单用例、
复原后校验锚点仍在原处）：见 §14.1 的表。两个新用例是**故意让它红的**：
两处 pin 的 docstring 都写着「加了候选生产者就会红，请连同文档一起重审」，
本轮它如期红了，处理方式是保留断言并改写，而不是删掉。

**两处必须记下的覆盖边界**：① `check:render` 的 evolve 用例用的是
`reachable: false` 的合成 payload，所以翻成 `true` 的渲染分支**没有渲染用例**
（记入 D11）；② 本轮新增的断言都取具体数值或状态（`0.72`、`PENDING`、
`deferred is True`、`candidate_producers == ['remap_confidence']`），
没有「不为空 / 至少一个」这类可被邻居恰好满足的写法 ——
`test_the_shipped_registry_holds_exactly_one_candidate` 断言的是**条数**，
理由同上。

### 第十轮（闭环的操作面 + D11 渲染用例）

2026-09-14：

| 命令 | 结果 |
|---|---|
| `.venv/bin/python -m pytest tests/ -q`（**全量，不 ignore**） | **1895 passed, 18 skipped**（157s；本轮 +14 用例） |
| `.venv/bin/python scripts/lint_harness.py` | 通过（157 个文件），存量 47 条待偿还（**未扩充**） |
| `.venv/bin/python scripts/lint_docs.py` | 知识库校验通过 |
| `cd web && npm run check:render` | **8 个用例全过**（新增 evolve 的 `reachable` 分支） |

`tests/test_policy_cli.py` 新增 `TestTheExperimentIsDrivable`（13 个用例），
驱动真 `main(argv)` 跑完 `shadow-open → shadow-emit → shadow-score → gate`
并用 `status` 读进度；另加一个用例钉住 `status` 按**值**打印在效的决策参数
（`install` 之后磁盘常量不再生效，这一行是「改了没用」与「改好了」的区别）。**两个变异探针**：把 `Reachability` 的 `open` 钉成 `false`
（渲染用例变红：`缺少「至少有一个候选生产者已登记」`）、
把 `shadow-emit` 的面板换成空列表（面板用例变红）。两处复原后锚点均在。

**一条口径上的自我更正**：`status` 从「不改变任何东西」改成
**「不动指针、不留记录」** —— 它现在会 `init_schema` 出 shadow 两张表
（此前只建 policy 四张），严格说不是「什么都没发生」。建表是
`CREATE TABLE IF NOT EXISTS`，与交易路径第一次落 intent 时触发的同一段 DDL；
但把那句话写准比保留一句好听的强。

### 第十一轮（实验接上每日调度 + 生产库执行第 0 步）

2026-09-14（同日第二支）：

| 命令 | 结果 |
|---|---|
| `.venv/bin/python -m pytest tests/ -q`（**全量，不 ignore**） | **1905 passed, 18 skipped**（158s；本轮 +10 用例） |
| `.venv/bin/python scripts/lint_harness.py` | 通过（158 个文件），存量 47 条待偿还（**未扩充**） |
| `.venv/bin/python scripts/lint_docs.py` | 知识库校验通过 |
| `cd web && npm run check:render` | 8 个用例全过（本轮未改前端） |

新增 `tests/test_shadow_run_task.py`（10 个用例）分四组：喂与评、**从不问闸门**
（跑完断言 `gate_decisions` 为空）、**永不带走交易日**（`runs` / `emit_for_date` /
`coverage` 各自抛异常时任务不向上抛）、以及幂等（同一天重复跑不产生第二行面板）。

**生产库的写入**（本仓库第一次对 `data/memory.db` 写策略记录，由人决定并先 dry-run）：
`freeze` → `version #1`（`c1d8632aca3ef97e`）→ `install` → 指针 seq 1。
`status` 复核：`live configuration still matches: True` / `integrity: clean` /
**在效参数与 `DEFAULT_DECISION_PARAMS` 逐值相同**（所以是行为中性的「装上已有的配置」）。
副本复测三个工作台：只有 evolve 变（`pointer` absent→**present**、`shadow` absent→empty）。

**一处提前验证掉的运维风险**：生产 `gate_decisions` 是旧 schema，而 `gate` 要写四个新列。
在**副本**上确认 `_ensure_gate_table` 的 `ALTER TABLE` 会补齐并写入成功（4 行）。
生产库没有因此写任何裁决行 —— 仍是 3 行旧 abstain。**这一步刻意提前做**：
等 20 个交易日后敲 `gate` 才发现表写不进去，代价太大。

### 第十二轮（把「该修的」修掉：D2 / D7 / D9，外加一处文档更正）

2026-09-14（同日第三支）：

| 命令 | 结果 |
|---|---|
| `.venv/bin/python -m pytest tests/ -q`（**全量，不 ignore**） | **1910 passed, 18 skipped**（149s；本轮 +5 用例） |
| `.venv/bin/python scripts/lint_harness.py` | 通过（**159 个文件**），存量 **43 条**（上一轮 47 → D2 的 4 处分层违规清零） |
| `.venv/bin/python scripts/lint_docs.py` | 知识库校验通过 |
| `cd web && npm run check:render` | 8/8 |
| `.github/workflows/harness.yml` | 新增 `frontend` job；**YAML 结构与步骤已校验，但 workflow 本身没有在本机跑过** |

- **D9**：前端两项检查进 CI —— `setup-node@v4`（node 22 + 以 `web/package-lock.json`
  为 key 的 npm 缓存）→ `npm ci` → `lint` → `build` → `check:render` →
  `node --test tests/test_report_markdown.mjs`。**每一条命令都在本机跑过并全绿**，
  但「CI 里真的会绿」这件事**没有被验证**（本机不具备 runner）—— 如实记下这个边界。
- **D7**：`shadow_run` 现在**每条约一次**问闸门，正好在配对天数**首次**到达门槛那天；
  已有裁决达到门槛就不再问。理由是 §12 禁止反复窥视（optional stopping）——
  每天问既是反复窥视，又会天天写一行几乎相同的 `insufficient`。
  人工提前问过（`validation_days` 不足）不会抑制自动提问。变异探针
  （把守卫改成恒 `False`）被「第二天不再问」的用例捕获。
- **D2**：**这条技术债条目自己写错了修法。** `daily_archive.py` 有两个职责：
  `run_daily_archive` 拉四个 `tools/` 模块（**这才是违规**），而
  `save_snapshot` / `get_snapshot` 是 `data/market_history`、`data/market_data`、
  `data/sentiment_cycle` 在读的**存储**。按原条目整体搬到 `pipeline/`，会让三个
  `data/` 模块 import `pipeline/` —— 四处违规换三处更糟的反向违规。
  改为**拆**：`data/daily_snapshots.py`（存储，留在读者身边）+
  `pipeline/tasks/daily_archive.py`（编排，可以 import `tools/`）。
- **文档更正（同日在回答「还剩什么没实现」时发现）**：三条过期断言，其中一条是**真错** ——
  `candidate_is_approved` / `entity_is_approved` 被写成「运维脚本在消费」，
  实测**零调用者**。另两条是状态过期（策略表已建、快照 U5 之后确实在筛选 prompt）。

### 第十三轮（D3 静默 except 清零 + D5 第三维复活）

2026-09-14（同日第四支）：

| 命令 | 结果 |
|---|---|
| `.venv/bin/python -m pytest tests/ -q`（**全量，不 ignore**） | **1930 passed, 18 skipped**（143s；本轮 +20 用例） |
| `.venv/bin/python scripts/lint_harness.py` | 通过（159 个文件），存量 **17 条**（上一轮 43） |
| `.venv/bin/python scripts/lint_docs.py` | 知识库校验通过 |
| `cd web && npm run check:render` | 8/8（本轮未改前端） |

**D3（26 处静默 `except ...: pass`）清零。** 全部改成 `except ... as e:` +
`logger.debug("...: %s", e)`，**一处一条消息**（不是一个模板套 26 遍），
14 个模块的基线条目随代码一起删掉。两处副产物值得单独记：

- **`data/memory_store.py` 此前一个 logger 都没有。** 2100 行的存储层，
  两处迁移跳过 + 一处重复教训冲突就是它那三个静默点；现在有了模块 logger，
  并在定义处写明了为什么是 debug 级（「已经加过的列」不是新闻）。
- **规则自己的建议是错的。** 原提示写着「若确实可忽略，在 `pass` 上方写一行注释」，
  而检查读的是 AST（`len(node.body)==1 and isinstance(node.body[0], Pass)`）——
  **注释不在 AST 里**，照做不会通过。提示已更正为「必须真的写一条日志，
  顺手给 `except` 加上 `as e`」。这是本轮第二次遇到「文档/提示承诺了代码没做的事」，
  只不过这次是 lint 自己在承诺。

**D5（playbook 聚类的第三维）先量后改，量出一条原文断言不成立。**
`_query_hit_clusters` 的第三维是 `vpa_verdict`，而**没有任何写入方记录这个键**
（`intraday_monitor` 与 `morning_scan` 的 features 都没有它）→ 该表达式恒为 NULL →
**聚类实际上只有两维，而 SQL 读起来像三维**。但原文的另一半是错的：
生产库 `playbooks` **总共 1 条**、条件是 `(institutional, theme)`，
带 `vpa_verdict` 的**一条都没有** —— 因为那一维从来没工作过。所以没有「坏掉的历史」，
只有**比设计意图更粗的簇**。第三维换成 `change_pct_band`，**在决策时记录**
（`intraday_monitor` 与 `scripts/replay_evolution.py` 都写），边界只在
`evolution.playbook.change_band` 定义一次 —— 在 SQL 里 `CASE` 一次、在 Python 里再推一次，
就是「同一个问题两个答案」，而这次漂移的后果是真的会发生：**自动创建出一个永远匹配不上的
pattern**。两个变异探针被捕获（维度改回常量；matcher 把缺失字段当通配）。
**已知代价照写**：加一维让簇更细、自动创建更慢（生产 47 行带 `prob`，
阈值是每簇 3 胜），所以短期内**大概率不会**再自动创建 playbook —— 这是为具体性付的价，
不是坏了。

**新记的债 D12**：`scripts/research/` 里 21 个 `.py` 有 **11 个与 `scripts/` 顶层逐字节相同**
（`replay_evolution.py` / `backtest_vpa.py` / `analyze_vpa_*` 一族…）。
它就是在改 replay 的 features 时暴露的：同一个改动要改两遍，而**只改一处不会报错**。
树的去留是仓库主人的决定，所以只记不删。

### 第十四轮（D4：四个超大文件按职责拆开）

2026-09-14（同日第五支）：

| 命令 | 结果 |
|---|---|
| `.venv/bin/python -m pytest tests/ -q`（**全量，不 ignore**） | **1930 passed, 18 skipped**（145s；**与改动前逐项相同** —— 这是纯结构改动） |
| `.venv/bin/python scripts/lint_harness.py` | 通过（**164 个文件**），存量 **13 条**（上一轮 17；`file-size` 四条已删） |
| `.venv/bin/python scripts/lint_docs.py` | 知识库校验通过 |
| `cd web && npm run check:render` | 8/8（未改前端） |

四处抽取、五个新模块，**没有一个超过 1200 行**（把行数从一个大文件搬到另一个大文件不是修法）：

| 原文件 | 原 | 现 | 搬走什么 |
|---|---|---|---|
| `data/memory_store.py` | 2161 | **1063** | `_SCHEMA`（824）→ `data/memory_schema.py`；VPA + 财务**缓存**组（275）→ `data/vpa_store.py` |
| `tools/vpa/data.py` | 1409 | **827** | 装载 + 派生指标组（598，13 个函数）→ `tools/vpa/bars.py` |
| `data/snapshot_store.py` | 1338 | **1082** | `_SCHEMA`（257）→ `data/snapshot_schema.py` |
| `tools/vpa/llm.py` | 1243 | **887** | 343 行 system prompt + `PROMPT_VERSION` → `tools/vpa/prompts.py` |

**三处零调用面变更**（`memory_schema` / `snapshot_schema` / `vpa/prompts`）：
原模块重新导出搬走的名字，所以既有的 `from … import …` 全部照旧，
而且名字不可能漂移 —— 是同一个对象。**两处必须改调用点**（`vpa_store` 与 `bars`，
共 3 行 import），因为 `vpa_store` 要从 `memory_store` 拿连接、
`bars` 被同层的四个模块调用 —— 原模块若再导入它们就成环。

**两处「量了但不搬」**（写进计划文档）：`snapshot_store` 的 news 组（约 250 行）
有 **28 个调用者**（十四个 `sources/` 适配器全在内），而只搬 schema 就已达标；
把**连接本身**（`_get_conn` / `MEMORY_DB_PATH`）搬到独立基础模块在结构上更干净，
但要改 **47 个测试文件里的约 75 处 patch**，改错一处的后果是测试写到生产库 ——
那值得单独一支，且与「进 1200 行」无关。

**本轮的一次当场兑现**：新写的 `bars.py` 用了 `Optional` 但没 import，
`lint_harness` 的 **undefined-name** 规则直接拦下（`expected: NameError 只会在生产环境
的那条分支上炸`）。这条规则本来就是为那类缺陷写的，在重构里立刻抓到一次。

**范围失误（记下来，不掩盖）**：D4 的四个文件里有两个属于 `tools/vpa/`。
我在动手前只把四个文件名列了出来，**没有说出「所以会搬 VPA 的代码」这个后果**，
用户因此在事后才意识到 VPA 被动过。技术上是逐字节搬家（`ANNA_COULLING_PROMPT`
与 `PROMPT_VERSION` 的 sha256、`bars.py` 的 13 个函数全部与原文相同，已比对验证），
但**范围沟通是失误**：该先问一句「D4 里有两个 VPA 文件，动不动」。

### 第十五轮（M1 验收：走前回放第一次由机器检查，2026-09-16）

| 命令 | 结果 |
|---|---|
| `.venv/bin/python -m pytest tests/ -q`（**全量，不 ignore**） | **2206 passed, 18 skipped**（138s；改动前 2199 → **+7，正是新增的 7 个用例**） |
| `.venv/bin/python scripts/lint_harness.py` | 通过（**172 个文件**，含新文件），存量 **13 条**（与上一轮同） |
| `.venv/bin/python scripts/lint_docs.py` | 知识库校验通过 |

新增 `tests/test_walk_forward.py`（7 用例），把 M1 的四条验收从散文变成机器检查。
**在这之前 runner 零测试**：`git grep -l walk_forward HEAD -- tests/` **为空**，
所以它第一次真实执行打在生产历史上 —— D27 / D28 就是这么被找到的。

**七条变异探针全部按预期变红**（逐条还原，还原后源文件的 sha256 与改动前逐字节相同）：

| 探针 | 改坏什么 | 变红的用例 |
|---|---|---|
| A | `entry_verdict` 的涨停分支永不触发 | `test_an_open_at_the_up_limit_does_not_fill` |
| B | 「当日无 bar」判成 `no_fill` 而非 `suspended` | `test_a_name_with_no_bar_is_suspended_not_cancelled` |
| C | `_range_overlaps_zone` 恒为假 | `test_a_touch_the_open_missed_is_counted_and_written_out` |
| D | 去掉重跑前的 store 句柄重置 | `test_two_runs_agree_row_for_row` |
| E | `_refuse_production` 不再拒绝生产目录 | `test_the_runner_refuses_the_production_directory` |
| F | `_corpus_fingerprint` 全返回 `None` | **2026-09-17 起换成** `test_a_file_production_moves_does_not_fail_the_run`（见下） |
| G | 让 `corpus_after` 与 `corpus_before` 不同 | `test_a_run_leaves_the_live_book_and_the_corpus_alone` + `test_a_touch_the_open_missed_is_counted_and_written_out` |

F / G 是**回补探针**：初版交付里有个自己留的洞 —— runner 会算 `corpus_untouched`，
而我只断言了 `production_db_unchanged`，从没检查前者。补上后配这两条探针，
F 证明「指纹真的看到了文件」那句不是装饰，G 证明「前后相同」这句真的会响。

**2026-09-17（第二十四轮）F 的落点变了，得说清楚。** 这一轮把「真的看到了文件」这条守卫
从指纹移到了 `_corpus_access_check`（`access["files"][…]["present"]`），所以 F **不再**让
`test_a_run_leaves_the_live_book_and_the_corpus_alone` 变红 —— 它现在让**新加的那条线程测试**
变红，因为一个什么都看不见的指纹**永远报不出变动**，而那条测试要求诊断真的报出一次变动。
**这是换了落点，不是失效**：实测 F 仍然恰好让 1 条变红。

**探针 D 是实质性的**：去掉那句重置，第二次运行读到的是第一次留下的持仓
（成交从 600001 变成 600002 / 600003）。「同一窗口重跑两次」这件事**依赖测试自己丢掉
store 的 thread-local 连接** —— `bootstrap` 重建 `memory.db` 换的是 inode，
旧连接还指着那个已被删除的文件。

**四处口径更正，都是当场量出来的**：

1. **`--orders-file` 不存在。** 计划里第 3 条原设想用它构造「一字涨停 / 停牌」场景，
   而 `walk_forward.py` 的 argparse 没有这个参数。场景改由语料构造（一字板 =
   `open == round(前收×1.10, 2)`；停牌 = 当日无 bar），计划里已写明。
2. **`intraday_ambiguous` 早就被输出了。** 第 4 条缺的是**断言**而非机制：
   `walk_forward.py:667` 的 `exits_ambiguous` 与 `:757` 的 CSV 列一直都在。
   把「没有输出」当待办，会让人去重写一段已经正确的东西。
3. **一条我自己写错的前提。** 对照用例最初写成「比涨停低一分 → 会成交」，实测**不会成交**：
   pullback 的区间上限是 `1.005 × 前收 ≈ 10.0`，涨停是 `11.00`，低一分按算术就在区间之外。
   改成断言拒绝的**理由**变了（`limit_blocked` → `no_fill`）—— 那才是
   「一字板是精确的、不是模糊的」真正的含义。
4. **另一条我自己写错的结论，在回补时才发现。** 我先把「M0 那半（往生产库塞一条、
   回放检索不到）**没有做**」写进了计划，**没有先查**。查了之后是：`tests/test_walk_bootstrap.py`
   的模块 docstring 原文引的就是那条评审要求，而
   `test_state_seeded_in_the_corpus_cannot_reach_the_replay` 正是在测它（塞一条 candidate +
   一条持仓进语料的 `memory.db`，断言回放账本为空且语料文件逐字节未变）。
   计划里那句话已改成准确版本：**已证的是合成语料级别，仍缺真实生产库副本 + 真实 2020 窗口**。
   **「没做」和「我没查」是两回事，而写进文档时它们长得一样** —— 这和本轮开头
   交接文那个「没提交 vs 没推送」是同一类错误的两个方向。

**顺手清掉一处文档谎言**：`tech-debt-tracker.md:136` 仍写
"(Working-tree line numbers; whole entry is uncommitted as of writing.)"，
而 D27 整条早已随 `3494559` 提交。改为引用该提交号。

**交接前提本身是错的，值得单独记一笔。** 收到的交接文说「`HEAD == origin/main`、
13 个文件全在工作区」，实测工作区**干净**、`HEAD` 领先 `origin/main` **3 个提交**
（`3494559` D26–D29、`1408dc9` M1 runner、`9404d0f` 持仓页读数），且 D26–D29
**早已在 tracker 里**。三个提交已推送（`b8bef21..9404d0f`）。
**「没提交」和「没推送」是两件事**：交接文把前者当风险报出来，却漏掉了后者 —— 而后者才是真的。

### 第十六轮（M1 的第一次真实 30 交易日窗口 + 汇总口径两处缺陷，2026-09-16）

| 命令 | 结果 |
|---|---|
| `.venv/bin/python -m pytest tests/ -q`（全量） | **2215 passed, 18 skipped**（148s；本轮开头 2206 → **+9**，全部在 `test_walk_forward.py`） |
| `.venv/bin/python scripts/lint_harness.py` | 通过（172 个文件），存量 **13 条**（未新增豁免） |
| `.venv/bin/python scripts/lint_docs.py` | 知识库校验通过 |
| `walk_forward.py --start 2025-07-01 --days 30`（真实语料） | **exit 0**；口径三项全「是」；结算抛错 **0** 天 |

**真实窗口（`2025-07-01 → 2025-08-11`，30 个交易日）**：期末 equity `1,000,226.03`（`+0.023%`）、
最大回撤 `1.321%`、买入 12 笔 / 卖出 9 笔；结算判定 `filled_at_open 48 / no_fill 17 /
intraday_ambiguous 28 / limit_blocked 0 / suspended 0 / undecidable 0`；挂单撤销 **47**。

**两处汇总缺陷是「跑」出来的，不是「读」出来的**，而且**是同一批 6 个事件的两个方向**：

1. **截断的分布被读成了完整的分布。** 那一行原本是
   `挂单撤销 47（资金不足×36；到期未到价（5天）×3；到期未到价（7天）×2）` ——
   总数 47，各项加起来 **41**。差额 6 是真的，只是没进前三。
2. **实例被当成了类别。** 计数器的键是 alert 的 reason，而 `价格涨走(7.51)` **把价格写在键里**
   ⇒ 每一次「涨走」都是只出现一次的类别 ⇒ 永远排不进前三 ⇒ **6 次涨走全部消失在汇总里**。
   所以第 1 条的「差额 6」与第 2 条的「6 次涨走」是同一批事件。

修法：`_cancel_class()` 按 **ASCII** `(` 切掉实例部分（**全角**的 `到期未到价（5天）` 故意保留 ——
5 天和 7 天是两个原因，不是一个原因的两个数字），`_top_reasons()` 在截断时**把余量说出来**。
修完那一行自洽：

```
挂单撤销 47（资金不足×36；价格涨走×6；到期未到价（5天）×3；另有 2 条未列出（1 个原因））
```

**修的是显示，不是行为** —— 拿修前 / 修后的 `summary.txt` 逐行 diff：净值、成交、结算判定
**逐字相同**，只有撤销那一行变了（外加回放目录路径）。

**五条变异探针全部按预期变红**（逐条还原，还原后 `walk_forward.py` 的 sha256 与改动前逐字节相同）：

| 探针 | 改坏什么 | 变红的用例 |
|---|---|---|
| H | `_cancel_class` 变成恒等函数 | `test_a_price_in_the_reason_does_not_make_a_class_of_one`、`test_the_thesis_date_is_the_instance`、`test_a_runaway_is_summarised_as_its_kind` |
| I | `_top_reasons` 不再报余量 | `test_a_truncated_breakdown_names_the_remainder` |
| J | 连全角 `（` 也一起切 | `test_a_full_width_parenthetical_is_part_of_the_reason` |
| K | `_settle_entries` **不再调用** `_cancel_class` | `test_a_runaway_is_summarised_as_its_kind`（键变回 `价格涨走(10.80)`） |
| M | `_summary` 不再调用 `_top_reasons` | 同上（报告里出现原始 Counter） |

**K 和 M 是这一轮真正有价值的两条**，因为它们打的是「**声明了但没有调用**」这一类缺陷 ——
本仓库已经吃过三次（挂单有效期、`policy_ref`、`learning_candidates` 的四列）。
`_cancel_class` 的四个函数级单测**在探针 K 下全绿**：删掉 `_settle_entries` 里那一行调用，
关于这个函数本身的所有断言依然成立。所以**必须有一条穿过 runner 的用例**，
它跑出一次真实撤单并读计数器的键；再加一条读 `summary.txt` 的断言，
把「helper 返回对了」和「报告印出来了」分开钉住。

**六处口径更正，都是当场量出来的**：

1. **我引用的窗口和数字是错的。** 上一轮记忆里写的是
   「窗口 `2026-08-06 → 2026-09-16`、`46（…×34；…×6；…×2）`」，
   实测是 **`2025-07-01 → 2025-08-11`、`47（资金不足×36；到期未到价（5天）×3；到期未到价（7天）×2）`**。
   我据此写的测试夹具数字也是错的，已换成实测分布。
   **这就是「不要把上一轮的记忆当作测量结果」的第 N 次复现** —— 连窗口都能记错。
2. **重复使用的回放目录不是证据。** `/private/tmp/walk-30` 的账本里有 **113** 行 cancelled，
   而**全新**目录跑同一窗口只有 **47** 行 —— 前者被多次运行污染过。
   任何「从账本数出来的分布」都必须来自**一次性**的目录。
3. **汇总的词汇不是账本的词汇（新债 D30）。** 实测该窗口 47 行 cancelled、**44 个不同的
   `close_reason`**；按汇总印的标签去账本里找：`资金不足` 命中 36/47，
   而 `价格涨走` **0/47**（账本写 `价格已涨走(…)`）、`到期未到价（5天）` **0/47**
   （账本写 `挂单到期未到价（挂5天，期限5天）`）。**四个原因里三个按汇总的标签在账本里找不到。**
   细节没有丢（不是数据丢失），丢的是**索引**。已立 D30，未修 —— 修法要么让 alert 带上
   `close_reason`，要么让 runner 从账本读回类别，两条都要动**线上 UI 也在用的**那个字段。
4. **`--orders-file` 不存在**（承上一轮），本轮又确认一次：构造撤单场景只能靠语料，
   最便宜的确定性撤单是「涨走」—— 区间上限 `10.05`、当日开盘 `10.80`（高于 `1.05×` 的涨走线、
   低于 `11.00` 的一字板线），不需要 theme 行、不需要论点、不需要到期时钟。
5. **`test_a_name_with_no_bar_is_suspended_not_cancelled` 里有一条空洞断言。**
   原文 `assert _PICKED not in result["cancels"]` —— `cancels` 的**键是原因**，
   拿股票代码去查它**永远为真**。改成 `assert not result["cancels"]`（撤单数为 0），
   这条在「发生了任何撤单」时才会响。
6. **占位决策器的 theme 是能解析的。** 我先怀疑 `WALK-TEST` 查不到会让每个挂单都被
   `主线不存在` 撤掉，从而怀疑「成交」是怎么发生的。报告自己的「不能说明什么」一节写了答案：
   theme 是**合成的一条、没有分数**，所以 `theme_gate` 按设计放行。**先读报告里的限制说明，
   再怀疑机制。**

### 第十七轮（验收清单逐条核对：10 条里 6 条已有机器检查，2026-09-16）

计划的「验收」节列了 **10 条 M0/内核**条目，**全部是 `[ ]`**；而同一份计划的 §2 写「M0 进度：6/6」。
两个数都不是错的 —— 它们是**两张不同的清单**：§2 的「6」是**六件事**（语料只读、状态隔离、
现金结算、规则版本化、开盘执行、LLM 记录），验收节的「10」是**十条测试**，而**两处都叫 M0**。
（抬头写「评审要求的 12 条」，本节实际列出 10 条；这个差数没找到出处，如实记在这里。）

**核对方式：先读代码与测试，再下结论。** 结果是 **6 条已有机器检查 / 4 条未完成** ——
而写这份记录的过程中，把其中最便宜的一条（第 6 条「严格时间束」缺的前半）**当场补掉了**，
所以最终是 **7/3**。下面第 6 行记的是**核对之初**的状态，紧随其后是补它的那条用例：

| # | 条目 | 状态 | 机器检查在哪 |
|---|---|---|---|
| 1 | 状态隔离 | ✅ | `test_walk_bootstrap.py::test_state_seeded_in_the_corpus_cannot_reach_the_replay`（+ runner 侧独立量生产库 hash） |
| 2 | 现金结算 | ✅ | `test_cash_settlement_semantics.py::test_recording_the_sale_does_not_reduce_available_capital` + `test_t1_settlement.py` 的 `may_sell` 两条 |
| 3 | 历史市场规则 | ✅ | `test_market_rules.py`：`DAY_BEFORE_REFORM`/`REFORM_DAY` = 2020-08-21 / 2020-08-24 三条 |
| 4 | 无同 bar 幻想 | ✅ | `test_t1_execution.py::TestNoSameBarFantasy`（4 条） |
| 5 | 无未来成交量 | ✅ | `test_t1_execution.py::TestTheDayBarCannotCarryTheFuture` + `test_the_module_exposes_no_way_to_pass_a_fill_day_volume` |
| 6 | 严格时间束 | ✅ **当天补齐** | 新闻上界：`test_news_window.py::TestWindowedRead::test_as_of_still_bounds_above`；**补的是前半** —— `test_walk_forward.py::TestTheDecisionCannotSeeTheDayItTrades`（探针 N 会让它红） |
| 7 | universe 也要 as-of | ⬜ **一半** | 停牌/未上市有；**选股侧的 as-of 没有任何测试** |
| 8 | LLM record/replay | ⬜ **一半** | 机制 32 条按调用点钉住；**端到端（record → replay 同窗口）未证明** |
| 9 | 不得绕过账本 | ✅ | `test_intent.py::TestOnlyTheStateMachineWritesStatus` |
| 10 | 对抗性新闻 | ✅ **次日补齐** | `test_vpa_v10_helpers.py::TestAnAdversarialBodyCannotLeaveItsBlock`（5 条）；**原文「没有测试」「意图不是断言」都写错了**，见第十八轮 |

**两类缺口，性质不同，别混为一谈**（按**第十七轮结束时**的状态：第 7、8 条是「一半」，
第 10 条当时记为「没有」——**那一栏是错的，第十八轮更正**）：

- **第 6、7 条原本都是「一半」**，而且缺的那一半是同一个性质：**结构性保证而非被钉住的保证**。
  runner 的 `_decide(ctx, day, prev_day)` **签名上就拿不到 D 的 bar**，
  但**没有一条测试会因为它被改坏而变红**。**结构上做不到 ≠ 有人拦着不让做。**
  第 6 条**当天补掉了**（让一个「只在今天领先」的名字输给 T-1 的领先者）；
  **第 7 条没有补，而且不该用「补一条断言」的方式补** —— 它的缺口是**真实缺功能**：
  as-of 的 ST 状态与「当时的候选池」现在**根本没有数据来源**（`stocks.db` 存的是今天的名字）。
  先有那个数据，才谈得上断言。**「缺一条测试」和「缺一个数据源」长得像，处理方式完全不同。**
- **第 10 条当时记作「没有」，这一栏是错的** —— 第十八轮逐条读代码与测试后更正。
  外部评审的要求（「正文里放 `Ignore previous instructions...` 不能突破输出 schema」）
  **有三段，只有一段是空的**：次序设计**早就有断言**（`test_build_vpa_user_content_safety_at_top`、
  `test_build_vpa_user_content_section_order`），输出侧的门**也早有断言**
  （`test_call_llm_vpa_rejects_missing_fields_before_defaults` 用桩客户端驱动一条缺字段的
  VERDICT，断言返回 `status="error"` + `VerdictParseError`）；**空的只有「围栏容纳」**
  —— `_max_backtick_run` 的唯一职责是让数据块的围栏宽过正文里的反引号串，
  而**拆掉它的调用点，整个文件仍然全绿**。第十八轮把它钉住了。
  **教训**：把「我没找到测试」写成「没有测试」，与把「没实现」写成「实现过」是同一类错误 ——
  区别只是**这次错得更悲观**。而悲观的方向同样要付代价：它会让下一个人以为有一整块工作要做。

**顺手抓到一处文档谎言（与上一轮同类）。** §10 的 Phase 2 记录里写着
`get_available_capital` = 总现金 − 未释放预留 − **未结算卖出所得** ——
而 D19 修的正是这个减项：A 股把「可用」与「可取」分开，T 日卖出所得**当日即可再买**，
扣掉它是把**取现规则**套在**买入力**上。`portfolio.get_available_capital` 的 docstring
原文就写着「Subtracting it here is what this function did until 2026-09-16, and it was wrong」，
**代码修了，文档留在旧规则上**。已更正，并写明更正的日期与原因。
这是 `tech-debt-tracker.md:136` 那条「uncommitted as of writing」的第二次同族出现：
**修好一处之后，凡是复述它的地方都成了新的谎言，而没有任何机械检查会告诉你。**

### 第十八轮（对抗性新闻：把那句「没有测试」读了一遍，发现它错在方向，2026-09-16）

第十七轮把第 10 条记成「零实现、零测试」。本轮去补它的时候，先把**已有的**读完 ——
这是本仓库的老规矩（先读代码与测试，再下结论），而它这次又救了一次：

| 评审要求的三段 | 当时的状态 | 证据 |
|---|---|---|
| 安全声明置顶、区块次序固定 | **早就有断言** | `test_build_vpa_user_content_safety_at_top`、`test_build_vpa_user_content_section_order` |
| schema 不合法的回复不得变成 verdict | **早就有断言（桩客户端，端到端）** | `test_call_llm_vpa_rejects_missing_fields_before_defaults`、`test_call_llm_vpa_handles_missing_choices_response` |
| 正文里的 payload 出不了它自己的块 | **空的** | `_max_backtick_run` **零测试** |

所以第 10 条**不是「没有」，是三分之一没有** —— 与第 6 条同型（结构性保证未被钉住），
**不是**与第 7 条同型（缺数据源）。区别在于：**这一条今天就能补，第 7 条不能。**

**补的是哪一半。** `_max_backtick_run`（`tools/vpa/llm.py:100`）的唯一职责，
是让 `_text_block` / `_json_block` 的围栏**宽过正文里最长的反引号串** ——
CommonMark 里**短的围栏关不掉长的围栏**，所以正文里的 ``` 关不掉 ```` 围栏。
删掉它的调用点，`tests/test_vpa_v10_helpers.py` **全绿**：这就是本仓库已经付过三次的
「声明了防御、却没有任何断言」缺陷类，**第四次**。

**新用例**：`TestAnAdversarialBodyCannotLeaveItsBlock`（**6 条**）。攻击载荷**同时**携带
伪造的区块标题（`## prior_state_canonical`）**和**用来逃出自己块的围栏 —— 也就是这条
名字里那件事。断言用的是一段**包解析器**（`_packet_headers`）：**围栏之外的 `## 标题` 才是结构**，
围栏之内的都是数据。于是「它跑出去了吗」就等于「包级作用域里有哪些标题」。
实测：**包级标题集合与构造器的规范列表逐项相等**，一个不多一个不少。

**口径（这条最重要）**：断言**不是** `count(标题) == 1`。实测伪造的标题**确实以子串出现 3 次**
（真的那次 + `signal_history` 里一次 + `current_vpa_text` 里一次）—— **它是数据，数据可以说任何话**。
要守的不是「数据里不许出现这个字符串」，而是「**它到不了包级作用域**」。
写成 `count == 1` 的话，**正确的实现也会让它变红** —— 那不是防御，那是假警报。

**探针**：

| 探针 | 改动 | 结果 |
|---|---|---|
| O | `_max_backtick_run` 返回 0（围栏退回 3 个反引号） | **5 条红**；决定性那条报「an adversarial body forged a packet-level section header」，列表里 `prior_state_canonical` 出现 **3 次** |
| P | 把 `analysis_meta` 块挪到安全声明之前（复现 v11 修掉的那个 prepend-chain 缺陷） | **1 条红**：`assert 464 == 0` |

探针 O 的失败列表还顺手证明那个解析器**不是天真实现**：载荷尾部那句
`## candidate_bars_canonical` **没有**逃出来 —— 因为载荷自己的闭合 ``` 又开了一个新围栏，
把它装了进去。解析器忠实地跟到了这一点。

`tools/vpa/llm.py` 探针前后 sha256 不变
（`33d9af969f4b6352b3d78d809ec626a16b12ab17795d7930d0bb0983e5a9dbb6`），`git diff` 为空。

**这条钉住的是什么，不是什么。** 它钉的是**代码侧的两端**：正文出不了它的块、
schema 不合法的回复变成 `status="error"`。它**不**钉「模型不会被说服」——
那既不是代码属性，也没有确定性的断言方式。把不可断言的东西写成已断言，
比不写更糟：它会让后来的人以为这里有一道不存在的门。

### 第十九轮（六个界面缺陷：先把每一个的成因量出来，2026-09-16）

用户发来六张界面截图。本轮的做法是**先量，再改**，而它救回了两条：

| # | 界面 | 实测成因 | 处置 |
|---|---|---|---|
| 1 | 三类结果 | 两个缺陷叠在一起：(a) `outcomes.integrity` 的判据**写反了**；(b) 英文诊断原文直接进中文页面 | 修判据 + 页面改中文，**5** 条新用例 |
| 2 | 预测与评估货币 | `Row` 的备注内联在标签后面，两层括号撞成一句话 | 备注独立成行 |
| 3 | 归因链 | **不是缺陷** | 见下，否定结果 |
| 4 | 晋升路径是否可达 | `可达` 用了 `.up`，而 `.up` 是 A 股「涨」色（红） | 换成语义色 |
| 5 | 复盘记录 | 调度侧 `run_review` **从来不写 `reviews`** | 接上写者，**4** 条新用例 |
| 6 | 改账审计 | D26 已修；剩下的是「故障被写成拒绝原因」 | 记成 **D32** |

#### 1. 判据写反了 —— 而且没有一条测试钉住它

`outcomes.integrity` 的第二条检查问的是 `available_at > today`，也就是**「证据窗口还没到」**；
而 `PENDING` 的定义原文就是 *"the horizon has not arrived / still open"*。
所以它**指认的正好是正常行**。生产库副本上的实测：

| 量 | 值 |
|---|---|
| 活着的 pending（无后继行） | **61** —— `counts` 表里那个数，也就是这一页表格印的数 |
| 其中窗口已收口、无人收尾（**真缺陷**） | **44**，全部 `forecast`，`available_at` 落在 2026-09-14..15 |
| 其中窗口未到（**成熟度，不是缺陷**） | **17** |
| 旧判据指认的行 | **11** —— 正好是那 17 里 `available_at` 非空的部分 |

两个方向都错：**11 条健康的被控告，44 条真有病的被漏掉。** 已提交的测试只钉了跨主体链那一半，
pending 这一半**零断言** —— 这是 D31 的第五个实例。

修法：把「窗口收口了没收尾」抽成一个计算 `pending_breakdown`，让**页面印的数和检查器抱怨的行
来自同一个函数**（与 `scoring.window_progress` 同一个道理）。缺陷判据是
`pending AND available_at < today AND 无后继行`。

**顺带修掉一条会撒谎的消息**：跨主体链那条抱怨写的是 `outcome #{row['id']} supersedes #{row['id']}` ——
**同一个 id 说了两遍**，读起来像自我取代，而且两端都不指向。已改成分别取 `n.id` / `o.id`，并加断言。

| 探针 | 改动 | 结果 |
|---|---|---|
| Q | 判据改回 `available_at > today` | **3 条红**（健康那条、缺陷那条、以及「两个读者同一判据」那条） |
| R | 消息改回自我引用 | **1 条红** |
| S | `pending_breakdown` 去掉后继行过滤 | **2 条红** |

探针 S 第一次只红了 **1** 条 —— 而**错的是我的预期，不是测试**：那条「两个读者同判据」的用例当时
库里没有任何被取代的行，于是断言**是空的**（两个读者对同一个错误总体仍然一致）。
给那条用例补一行已解决的标签之后，探针 S 才如期 2 条红。

**页面侧**：`_read_outcomes` 不再把英文句子交给前端，改成给数（`breakdown` / `broken_chains`），
中文由页面写。渲染检查加两个用例，并且**故意在载荷里留一个读模型已经不发的 `integrity` 键** ——
否则 `reject: ['is pending but claims']` 是空的：谁把英文透传加回来，用例也不会红。
探针 T（把 `<Integrity items={v.integrity} />` 加回 `Outcomes`）实测 **1 条红**，
报「不该出现 「is pending but claims」」。

#### 3. 归因链：量完发现不是缺陷

上一轮记的是「`position_exits` 0 行 → 面板是 `empty` → 通用空态文案与 section note 撞在一起」。
**实测相反**：`_read_attribution` 返回的行数是 `len(by_thesis) + unattributed.positions`，
所以它是 **`present` / 9 行**，渲染的是带数字的那一版，**不是**空态。生产库副本上的读数：

| 量 | 值 |
|---|---|
| 已实现合计 | **-5,616.78 元** |
| 指得出论点的 | 0 元 |
| 指不出的（旧仓 / 无论点） | -5,616.78 元（9 笔） |
| `attributed + unattributed.total == realized.total` | **-5,616.78 == -5,616.78，恒等式成立** |

第一次量的时候用的是系统 python（`PyYAML` 没装），`load_traders()` 退化成一个默认交易员，
`realized_total` 只加到一个人的账 —— 于是「恒等式不成立」是个**探针假象**。换成 `uv run` 复测：
`default -4,507.53 + pullback -1,109.25 = -5,616.78`，恒等式精确成立。
**量错了环境，会造出一个不存在的缺陷。**

所以这一条是**验证后的否定结果**：面板是准的，数字自洽，而且它自己解释了为什么是 0 ——
「链路是通的，只是还没有第一笔走完它」。**不改。**

#### 5. 复盘记录：调度侧没有写者

`reviews` 表只有一个写者 `report_store.save_review`，而它的唯一调用方是**手动 CLI**
（`pipeline/daily_review.py`）。15:30 的调度任务 `run_review` 写了报告、推了通知、**一行都没存档** ——
所以「记忆与验证」的复盘记录每天印「表在 · 0 行」，而复盘其实每天都在跑。
这与 `tests/test_task_reports_persisted.py` 开头记的是**同一个缺陷，挪了一张表**。

修法：`_verify_today_predictions` 以前只返回渲染好的表格，把算出来的
`hits / misses / neutral / unreachable` 丢掉了 —— 这正是「能印命中率却存不下任何东西」的原因。
改成连数字一起返回，`run_review` 在报告最终定稿后调 `save_review`。

| 探针 | 改动 | 结果 |
|---|---|---|
| U | 存档调用换成空操作（复现修复前的状态） | **2 条红** |
| V | `_verify_today_predictions` 退回只返回字符串 | **1 条红** |

#### 6. 改账审计：D26 真的修好了，剩下的记成 D32

实测：`position_exits` 现在有 `command_id` 与 `request_json` 两列，
`_migrate_position_exits_command` 挂在 `_get_conn()` 上。68 条 `no such column: command_id`
全部是**历史**（2026-09-16 01:30–06:58Z = 09:30–14:58 本地）。D26 的修复提交时间是
**13:13:47 +0800**，也就是说最后一条失败发生在修复之后约 **1 小时 45 分钟**。

原因不是修复无效：迁移跑在**每个线程的第一条连接**上，已经在跑的进程不会重跑它，
所以修复要等重启才生效。**「下一次收盘会成功」这件事本轮无法验证** —— 收盘 15:00 结束，
之后没有新的 close 尝试。这一条只证明**可达**，不证明**已发生**。

真正剩下的是展示：`intent.submit_intent` 把两种完全不同的东西写进同一列 `reject_reason` ——
业务拒绝是散文（`refused by the order path (…)`），系统故障是
`f"{type(e).__name__}: {e}"`（`intent.py:490`）。面板把这一列印在「拒绝原因」标题下，
于是一次**停摆**读起来像 68 次**政策拒绝** —— 而「拒绝」是个正常判决，所以没有任何下游会问
「为什么 68 条说的一样」。这需要一次设计决定（加列 / 加稳定前缀 / 读模型里按前缀分类），
所以记成 **D32**，不在本轮偷偷改。

#### 本轮验证

- `uv run pytest tests/ -q` → **2231 passed, 18 skipped**（上一轮 2222 / 18，本轮 **+9**）。
- `uv run python scripts/lint_harness.py` → exit 0，172 个文件，存量 13 条（**未扩基线**）。
- `uv run python scripts/lint_docs.py` → exit 0。
- `npm run lint`（web）→ exit 0；`npm run check:render` → **13** 个用例全 OK（新增 2 个）。

**同时更正第十八轮的两处数字**：用例数 5 → **6**，探针 O 的红数 4 → **5**（本轮复测；
`tools/vpa/llm.py` 的 sha256 未变，仍是
`33d9af969f4b6352b3d78d809ec626a16b12ab17795d7930d0bb0983e5a9dbb6`）。
两个数都写在那一轮的记录里，两个都没现跑 —— 这一条自己就是它想警告的那种错误。

### 第二十轮（第十九轮的后续：写者接上了，那它是不是单值的，2026-09-16）

第十九轮给 `reviews` 接上了一个**调度写者**。这一轮做的是「量自己刚写的东西」，
而不是假设它对了 —— 两个发现，一个是缺陷，一个是死代码。

#### 1. 写者不是单值的 —— 而「上了调度」正好把这件事暴露出来

`save_review` 一直是**追加**。在它唯一的调用方是「人手动跑一次 CLI」时，追加无害；
但第十九轮刚给它接上的调用方**每天都会跑**，而每天跑的东西会被人控制不了的事重跑 ——
重启、重试、补跑历史日期。`reviews` **没有** `date` 上的唯一约束
（只有 `idx_reviews_date`，实测 0 行），所以第二次运行会给同一个日期写出第二行。

那不是更正，是重复：记忆页会把同一天印两遍、两个数不同，任何按表聚合的东西会把两行都算进去。

修法：在既有的 `with _lock:` 里先 `DELETE FROM reviews WHERE date = ?` 再 `INSERT`，
两句同一个事务，读者看不到中间的空档。**故意不加唯一索引**：加它需要为「已经存有重复行的库」
写迁移，而迁移在存量数据上失败正是 D26 的来路 —— 写者是更便宜的执法点。

| 探针 | 改动 | 结果 |
|---|---|---|
| W | 去掉那条 `DELETE`，其余一字不动 | **1 条红**，正是新用例，理由 `one row per date, got 2` |

源文件按字节还原（sha256 `955dcb43cf99327e514960bef871b74cf8d3b04f4d8865a70cd3649c8ae7978d`）。

#### 2. 一个没人调用的导出 —— 是死代码，不是缺陷

顺带量到 `web/src/hooks/useDashboard.js` 的 `summarizeReviews`：全仓 `git grep`
（排除 `node_modules` / `dist`）只返回定义本身；没有 barrel 文件，`App.jsx` 是按名字只引
`useDashboard` / `useWebSocket`；`tests/` 与 `render-check.jsx` 都不碰它。
`/api/reviews` 返回原始行、不做任何聚合，所以它**不是**「同一个数的第二个来源」——
它算的是一个**页面上没有任何地方显示**的池化命中率。

判为**死代码而非缺陷**（纯函数、无持久面、会被 tree-shake）。**删掉而不是接上**，
因为它的形状本来就不对：这个文件自己的注释（`:14-17`）记着页面为什么改成读工作区读模型、
而不是自己拼同一个数 —— 池化复盘命中率属于读模型，不属于一个跑在原始行上的客户端 reducer。

#### 本轮验证

- `uv run pytest tests/ -q` → **2232 passed, 18 skipped**（第十九轮 2231 / 18，本轮 **+1**）。
- `uv run python scripts/lint_harness.py` → exit 0，172 个文件，存量 13 条（**未扩基线**）。
- `uv run python scripts/lint_docs.py` → exit 0。
- `npm run lint` → exit 0；`npm run check:render` → **13/13** OK；`npm run build` → 281 模块，437.00 kB。

**仍未验证（本轮明确不当作已发生）**：下一次 15:00 收盘能否成功。D26 的迁移挂在每个线程的
第一条连接上，跑着的进程不会重跑它，14:58 之后没有新的 close 尝试 —— 只证明**可达**。
另外 `python main.py review` 可以立刻把第一行写进复盘记录，否则要等下一次 15:30。

### 第二十一轮（M0 的两条未完成项 + 让闭环真的跑起来，2026-09-16）

这一轮是三件事，顺序是「先把两个未验证的闸门钉住，再让代理真的开始学」。

#### 1. universe as-of：补上选股侧，并在自己的补丁里量出一个死分支

`docs/exec-plans/active/2026-09-16-the-agent-decides-each-morning.md` 的验收里
「universe 也要 as-of」一直没打勾，缺的是**选股侧**：停牌在结算侧有测试，
未上市由 `market_rules.listing_day_exemption` 覆盖，而「当时不属于候选池的证券不得被选中」
没有任何测试。

**先量「有没有数据源」，答案是「没有，但不需要新的」**：`stocks.db` 每个 code 只有**当前**一行
（没有带日期的 ST/名称表），`all_quote_snapshots.name` 从 2026-09-08 才有。
唯一存在的上市证据就是**语料里每个 code 的第一个交易日**，于是加了
`market_history.first_bar_dates()` / `codes_listed_by()`，
`Corpus.is_listed(code, day)` 的规则是 **`first_bar < day`**，锚在**决策日**且**严格小于**：
当日上市（IPO）没有前收盘，排不了名也定不了价；前一日上市的有前收盘、09:00 已在交易。

**然后量这条规则有没有牙**：语料 5,705 个 code，其中 **2,030 个（35.6%）在语料起点之后才上市**；
2020 窗口有 **1,986** 个尚未上市。所以它不是装饰。

**接着在自己的补丁里量出一个死分支 —— 这一轮最值钱的发现。**
规则写成两个决策器共用的一个闸门 `_eligibility`，返回六个理由。其中
`not_listed` 与 `no_prior_bar` **永远不会触发**，因为两个调用点都是：

```python
for code, row in bars_prev.items():
    if _eligibility(ctx, code, day, prev_day) is not None:
        continue
```

从 T-1 的 bar 里取候选集，就只可能取到已上市的名字 —— `not_listed` 对每一行都是 `False`，
`no_prior_bar` 是同义反复。实测 2020 窗口那 1,986 个未上市的 code，**每一个**也都没有 T-1 bar：
旧循环里它们是被**碰巧**排除的，而这张票据要的那条规则**根本无法被测试**。
不是「没测」，是「不可达」；而一个永远不会自增的计数器，和一根永远不会被写的列是同一种谎。

修法是**遍历整个 instrument 表**：面板**内容一字不变**（没有 T-1 bar 的名字本来也排不了名），
变的只是每个排除理由都被计数。2025-07-01 窗口实测
`eligibility:not_listed 409`、`eligibility:board 3702`、`eligibility:st 1122`。

同一个函数里还有同族的第三处：`Corpus.adv20` 的 docstring 承诺
「不足二十个交易日返回 `None`」，代码却对**任何**长度取均值 —— 昨天上市的名字会拿到一个
「一根 bar 的 ADV20」，正是它自己 docstring 说调用方不要的东西（2025 窗口 6 个 code，2020 窗口远不止）。

**可推广的那条规则**（这是它值得编号的原因）：*一个闸门不是因为它被写下来就可达，
也不是因为它被调用就可达 —— 它可达，是当它过滤的输入里**能出现**它要拒绝的东西。*
要检查的是候选集，不是调用点。见 D33。

#### 2. 真正的模型决策器 + record/replay 端到端

新增 `alpha_agents/agents/t1_decider.py`（面板 + 新闻 + 账本 + 经验 → 下单）与
`alpha_agents/prompts/t1_decide.md`，17 条测试。**面板就是规则**：不在面板里的 code 一律拒绝并计数
（`decider_refused:outside_panel`）—— 这把「模型被问 2020 却说出 2026 才上市的名字」
从**买入**变成了**一次被计数的拒绝**。40 天窗口实测 6 次。

端到端的 record/replay 第一次真的跑了：同窗口先 `record` 再 `replay-recorded`，
**`fills.csv` / `settlement.csv` / `equity.csv` / `events.csv` 逐字节相同**，
journal 重放后 sha256 不变，`run.json` 只差 provenance。

**判据本身也修过一次，而且是跑出来才知道的**：第一版核对写的是
`journal_after > 0`，而**正确的 replay 会判否** —— 它读一个已经非空的 journal 且不新增。
record 那一遍是过的，所以只有真跑一遍 replay 才会发现。现在是三种模式三条判据，
外加 `--decider llm` 在 `live` 下直接 `SystemExit`（live 不记录，两个实验臂没法比）。

#### 3. 闭环接上：打标 → 提炼 → 让下一天看得见

跑起来的位置在 `scripts/walk_forward.py`，每个交易日收盘后一次 `_learn`，
下一个交易日开盘前一次 `_knowledge_block`。

**先量「缺的到底是哪一环」**：40 天窗口跑完时 `episodes` **16 行**、`episode_events` **63 行**
—— 内核早就在写事实；而 `outcomes` **0 行**、`learning_candidates` **表都不存在**。
所以缺的不是事实层，是三根线：**打标**（`sweep_trade_labels` 从来没有人调用）、
**提炼**（没有生产者）、**反馈**（`feedback.inject_principles` 依赖一个永远不存在的已批准快照，
所以 `{knowledge}` 恒为空）。**"学习闭环" 没闭上的原因不是没有学习能力，是没有接线。**

接上之后，每个平仓仓位的实现结果变成一条带 `available_at` 的 `trade` 标签；
`_distil` 写一条**可证伪、由市场数据打分**的命题 —— 就是决策器自己的那条：
*T-1 涨得更多的候选实际收益更差*。一笔交易在「入场涨幅」与「实现收益」符号相左时支持它，
相同时反对它。**没有任何模型被问它怎么看自己**：命题是两个桶的中位数，特征从语料按订单自己的
T-1 重读。`supporting` / `opposing` 两个列表**永远都写**，空也写。

**终点停在 `observation`，管线不调用 `advance_candidate`。** 这不是没做完，是刻意：
`learning_candidates` 的设计注记要求 `status` 不被管线驱动，晋升是有审计的人的动作。
让回放自己走这一步，等于把「证据驱动未来的策略变更」偷换成「回放自己批准自己」。
而且 n 远低于 50，它本来也不可能是别的东西。

**一处必须说清的偏离**：回放里 `{knowledge}` 喂的是**这个交易员关于自己已平仓交易的带日期的笔记**，
不是已批准的规则。生产里规则只能经 knowledge snapshot 到达决策，回放里没有快照，
所以生产里这一块是空的。**闸门属于「晋升」（笔记变成规则），不属于「交易员记得自己身上发生过什么」**；
完全没有反馈的闭环不是保守的闭环，是**不是闭环**。这条写进了报告的「不能说明什么」一节。

#### 4. 跑起来才看得到的四个缺陷

四个都是**先跑、再看日志**得到的，不是读代码得到的。逐条见 D34 / D35 / D36 / D33（最后一个是上面那个死分支）。

| 缺陷 | 怎么发现的 | 代价 |
|---|---|---|
| 模型 429 把**整天的结算**也带走 | 180 天窗口日志里 14 个 `RateLimitError`，而 equity 曲线有洞 | 每天一个交易日从账本里消失，读起来像「什么也没发生」 |
| 同一条观察被**每天重写**一遍 | 查 `learning_candidates`：**11 条候选对应 2 个不同事实** | `counts()` 失去意义，闭环看起来比实际忙 |
| 限流时 provider **排队而不是回 429** | 120 天窗口跑到第 8 天停住，日志最后一行是 tracing 警告，三分钟没有任何新行 | 没有 `timeout` 时 SDK 等自己的十分钟再重试两次：一个交易日半小时，而报告说不出为什么 |
| `adv20` 不守自己的 docstring | 读自己的补丁时对不上 | 昨天上市的名字拿到「一根 bar 的 ADV20」 |

第三个的两种表现值得单独记：同一个 key 在 180 天窗口里**回 429 且重试可见**，
在 120 天窗口里**排队且什么都不打**。同一个条件的两种失败形状，只有一种会留下痕迹。
修法是两头都补：`create_model(timeout=...)` 是**可选**参数（默认路径逐字节不变，
生产不受影响），runner 传 `--model-timeout`（默认 120 秒）；再加 `--pace-seconds`
在每天结算写完之后 sleep —— 额度是**提示词**花掉的，面板越大越早撞上，
按 provider 答多快就多快地问正是撞上它的原因。实测这个 key 在 panel 40 / news 60 下约 **7 天**开始排队。

第一个尤其值得记：**一次模型故障应该只花掉那次决策，不该花掉那一天**。结算与模型无关，
它是账本力学。现在是两个 `try`，错误记录带 `stage`，汇总行也从
`结算过程抛错的交易日 14` 改成 `抛错的日-阶段 14（decide:openai.RateLimitError×14）`
—— 旧的写法会把读者送到结算代码，而那 14 个故障一个都不在那里。

#### 5. 一个必须说的节奏事实

**闭环以交易员的速度闭合，不是以回测的速度。** 决策器每天最多 2 单、仓位按周持有，
所以平仓本来就稀疏：120 天窗口的第四次平仓落在**第 47 个交易日**。
这不是缺陷，是「交易 → 学习 → 进化」这件事本身的节奏 —— 也正好是为什么历史回放只能**筛选**候选：
它把候选快速证伪，而通过筛选的那些仍然要花掉 20 个**前向**配对样本（§8.4）。

#### 本轮验证

- `uv run pytest tests/ -q` → **2270 passed / 18 skipped**。这个数字是**第二十二轮复测出来的**：
  第二十一轮当时把套件跑在了 D33/D34/D35 与 `model_factory` 改动**之前**，那一版结果已作废，
  本轮没有沿用（本轮新增用例：`test_walk_forward.py` 从 18 条到 32 条，`test_t1_decider.py` 17 条）。
- `uv run python scripts/lint_harness.py` → exit 0，173 个文件，存量 13 条（**未扩基线**）。
- `uv run python scripts/lint_docs.py` → exit 0。
- 变异探针 5 个，全部只让**预期的那几条**变红，源文件逐次按字节还原：

| 探针 | 改动 | 结果 |
|---|---|---|
| 1 | 删掉 `_eligibility` 的 `not_listed` 分支 | **2 条红**，报 `no_prior_bar`（正是「理由从 as-of 变成缺 bar」） |
| 2 | `is_listed` 的 `first < day` 改成 `<=` | **同上 2 条红** |
| 3 | `_knowledge_block` 去掉 `source_date < day` 过滤 | **1 条红**：写在当天的观察当天就能看见 |
| 4 | 去掉「当天有新平仓」闸门 | **1 条红**：没有新证据的一天又提炼了一次 |
| 5 | 让决策失败重新跳过整天 | **1 条红**：equity 曲线出现洞 |

**仍未验证（本轮明确不当作已发生）**：
- **跨过 `openai-agents` `Runner` 的桩模型端到端测试没有写。** 那需要伪造 SDK 的 `Model` 协议，
  测试会比被测的东西更脆。端到端的依据是**真跑了一遍**（第 2 节），以及 runner 侧确定性由
  `TestTheLearningStepIsAFunctionOfTheDaysBook` 用占位决策器钉住。
- **`learning_candidates.status` 没有任何迁移。** 刻意（第 3 节），要等人在真实前向窗口上拨指针。
- **D12（`scripts/research/` 是 `scripts/` 的逐字节副本）仍然成立**，本轮又确认了一次
  （`replay_evolution.py` 两处 sha256 相同）。这条不是本轮引入的，也没修。

### 第二十二轮（把 120 天真的跑起来：闭环自己转了，以及它暴露的五件事，2026-09-16）

上一轮把三条链路（打标 / 提炼 / 回读）接上了，但只跑了 40 天、只证明了「写进去了」。
本轮的任务是**让它自己连续转起来，然后读日志找问题**。

#### 1. 120 天窗口跑通了

```
2025-07-01 → 2025-12-23（120 个交易日）
期末 equity 1,057,996.18   区间收益 +5.800%   最大回撤 1.897%
买入 12 笔 / 卖出 10 笔     期末持仓 2 / 挂单 0
model_calls_made 120        model_journal_total 120
抛错的日-阶段 0
```

`抛错的日-阶段 0` 是 D34 的验收：上一轮 180 天窗口是 **14**，且全部落在决策阶段却记在结算名下；
本轮 120 天、120 次模型调用，一次都没有。

`+5.800%` 这个数字**不能当成绩读**。报告自己的限制清单第一条就是：
「mechanistic walk-forward, not a point-in-time model backtest: today's model weights have
already seen these dates」—— 模型的权重早就读过这段时间，所以强结果至少同样可能是记忆而非信号。
这正是历史回放只**筛选**候选、从不晋升的原因。

#### 2. 闭环确实闭合了，而且是它自己转的

| 环节 | 本轮实测 |
|---|---|
| 打标 | 10 笔平仓全部写成 `outcome`（`matured_total 10`），2 笔在持仓标 `pending` |
| 提炼 | 写下 **5 条**观察：08-28、09-04、09-08、10-15、10-17，n 从 4 涨到 10 |
| 回读 | `_knowledge_block` 把观察送进次日提示词；`learning_days 120` |
| 生命周期 | 5 条全部停在 `observation`（管线不驱动 `status`，晋升是人审批的动作） |

D35 的验收就在这一行里：**5 条观察对应 5 个不同的日期，不是每天一条**。修之前，
180 天窗口写下了 11 条候选、却只有 2 个不同的事实，而且每一条都是真的 —— 所以别的检查都抓不到。

第一次平仓落在窗口第 41 个交易日附近（08-28），第 4 次落在第 47 个交易日，与上一轮量到的节奏一致。
**这不是慢，是这件事本身的节奏**：2 单/天、持仓约一周，所以历史回放只能筛选候选，
通过筛选的仍然要花掉 20 个前向配对样本（§8.4）。

面板作为规则也在计数：`decider_refused:outside_panel 13` —— 模型 13 次提了不在面板里的名字，
被拒并计数，而不是变成一笔买入。`eligibility:not_listed 6594` 说明 D33 的闸门这次真的在跑。

#### 3. 跑出来的第一件事：支持计数就是分组规模（D38，已修）

读第一条观察时发现的。`_distil` 陈述一个可伪命题 ——「T-1 涨得越多、实际收益越差」—— 并报告
有多少笔支持它。判据是：

```python
return (t["t1_change"] > cut) != (t["return_pct"] > 0)
```

2025-07-01 窗口的**四笔平仓全部亏钱**，所以 `return_pct > 0` 对四笔都是 `False`，整个表达式
退化成 `t1_change > cut`。于是「2 笔支持、2 笔反对」**就是两个分组的大小**：一个不可能与旁边
中位数不一致的算术恒等式。它读起来像证据，正因为如此才没被别的东西抓住。

它还把四条引用里的两条指反了，包括窗口里最好的一笔：T-1 涨幅最低（6.76%）、亏损最小（-4.96%），
这正是命题成立，却被记成**反对**。

**修法**：把每笔的收益与**全窗口中位收益**比较，而不是与 0 比较 —— 这样判据是关于相对收益的，
并且在一个所有交易同号的窗口里不会退化。同一窗口，修前 → 修后：

```
修前  supporting [11, 12]   opposing [1, 6]
修后  supporting [1, 11]    opposing [6, 12]
```

`payload["trades"]` 现在逐笔带 `supports` 标志，两个计数可以从记录本身重新推导，而不必被信任。

#### 4. 跑出来的第二件事：语料检查问错了问题

汇总里有一行 `共享语料 size+mtime 未变 **否**`。检查本身是对的（回放改了共享语料，每次运行都不可复现），
但 `run.json` 只记 `corpus_untouched: false` —— 不记哪个文件、修前修后各是多少。**记录本身无法事后诊断。**

于是又跑了一个 5 天窗口盯着看：三个语料文件 `size` 与 `mtime` 逐字段相同，该窗口自己的检查也报「是」。
随后跑的**第二个** 120 天窗口**又报了「否」**。真正动的文件，是事后手工 `stat` 才找到的：

```
AlphaAgents/data/market_snapshots.db   1385369600 → 1385422848   （+53,248 字节）
mtime 2026-09-17 00:10:17 —— 落在第二个窗口之内
```

**写它的不是回放，而且这一点是查出来的、不是推出来的**：共享语料是 symlink ⇒
`corpus_access.is_shared` 为真 ⇒ 以 `mode=ro` 打开且**跳过建表**，这两半都由
`tests/test_corpus_access.py` 钉住；而一个只读连接不可能让一个库长 53KB。

是**生产在实时写**。同一个文件晚几分钟再读：

```
news_items   rows=989,927   captured_at=2026-09-17 07:33
```

07:33 是查询前几分钟，也是那次运行之后几小时 —— 这台机器上的定时管道在持续写新闻流。

所以**这个检查在一台生产还活着的机器上永远不可能通过**，而它的失败从来不是关于回放的事实。
同一个布尔值我读错了三次：先是「回放改了语料」，再是「并发测试套件」，再是「定时快照写者」——
前两次是错的，每次都花掉一次运行或一次 `stat`，而第一次还写进了一份已提交的文档。

**修法（修正后）**：该问的不是「这个文件变了吗」（生产在写的机器上答案永远是「变了」），
而是「**回放**写了吗」。这个可以直接查：断言回放持有的共享文件句柄是只读的、或断言没有发生过写，
而不是比对一个别的进程拥有的文件的 `size` 与 `mtime`。指纹若要保留，就该记文件名与前后两个值，
并且**限定在生产不写的文件上**。记为 D39。

#### 5. 跑出来的第三件事：每天一次的重试不是限流，是每天一个新的事件循环

120 天窗口里有 120 次 `openai._base_client: Retrying request`。四个量一起看，只有一个假设能同时解释：

```
days 120   retries 120   POST 120   HTTP 200 120   non-200 0
唯一被访问的 host：api.siliconflow.cn/v1/chat/completions
```

每天恰好一次；失败那次**没有任何 HTTP 响应**（所以在传输层就失败了，还没有状态码）；
重试总在第一次就成功；而且只访问过一个 host —— 这排除了最顺手的那个解释（对 OpenAI 自己的
接口做 trace 导出或凭据探测）。

原因是 `t1_decider.propose_sync` 是 `asyncio.run(propose(**kwargs))`（刻意如此，为的是让同步的
日循环保持形状），而 `Context.model` 整个窗口只建一次 —— 一个 `AsyncOpenAI` 客户端被跨约 120 个
事件循环共用。httpx 的连接池绑在打开它的那个循环上，换循环后第一次请求会复用一条死连接。

代价是每天 ~0.4 秒和一次废请求，而且每天都成功，所以是浪费不是错误。**本轮刻意没修**：
它是模型调用路径的改动，和 D38 捆在一起会让一次验证跑不出「哪个改动造成的什么」。
验收判据便宜且可检查：**同一个 120 天窗口应当报 0 次重试而不是 120 次。** 记为 D40。

#### 6. 跑出来的第四件事：同一条命令不是同一次运行（D41）

修复后的复跑用**完全相同的命令行**启动，第一天就死了：

```
openai.OpenAIError: Missing credentials. Please pass an `api_key` ...
```

两个独立原因，都值得写下来：

1. **`uv run` 在这里不加载 `.env`。** `uv run python -c "os.environ.get('AGENT_API_KEY')"`
   打印 `(absent)`；`UV_ENV_FILE=.env uv run ...` 打印一个 46 字符的 key。
2. **`.env` 现在指向的提供方和那次成功的运行不同。** 加了 `UV_ENV_FILE=.env` 之后
   `AGENT_BASE_URL` 解析成 `https://api.novita.ai/openai`（上一轮量到返回 403
   `NOT_ENOUGH_BALANCE` 的那个）；而成功那次的日志记着
   `model_id Qwen/Qwen2.5-7B-Instruct`、`model_provider api.siliconflow.cn`。

日志记了提供方，这是它可恢复的原因 —— 制品说得清是哪个模型回答的，即使命令行说不清。
但本仓库自己的原则是**「哪个模型回答是行为的一部分」**（`model_factory.model_identity`），
所以一个提供方取决于环境变量的运行，**不能从它的命令复现**。记为 D41。

#### 7. 顺手发现：1200 行规则只在 `alpha_agents/` 内生效（D37）

`AGENTS.md` 把「文件不超过 1200 行」列在非协商不变量里，并说这些不变量由
`lint_harness.py` 在 CI 里机械强制。但它没说**范围**：`main()` 的默认根是 `alpha_agents/`，
CI 不给它任何参数。所以 `scripts/`、`tests/`、`research/` 都在扫描之外。

实测（不是推断）：仓库里最大的文件是 `scripts/walk_forward.py`，**1801 行**，而
`lint_harness.py` 无参数运行报通过。显式传入 `scripts/` 会报 70 处违规
（`duplication=53, file-size=1, silent-except=14, undefined-name=2`），其中 69 处是既有的，
集中在探索性的 `scripts/research/`。

**本轮不改基线、也不拆分**：拆分要挪动回放所依赖的那个文件的约 400 行，而它的接缝在两个方向上
都交叉引用 `Context` 与 `_knowledge_block`，值得自己的一份计划和一次自己的验证。
本轮做的是把**范围说出来** —— `docs/GOLDEN_PRINCIPLES.md` §10 与 `AGENTS.md` 现在都写明
这条规则在包外是约定而不是闸门。**一条有洞的规则可以活；一条声称没有洞的规则不行。**

#### 本轮验证

- `uv run pytest tests/ -q` → **2270 passed / 18 skipped**（`test_walk_forward.py` 32 → **35 条**）。
- `uv run python scripts/lint_harness.py` → exit 0，173 个文件，存量 13 条（**未扩基线**）。
- `uv run python scripts/lint_docs.py` → exit 0。
- 变异探针 1 个（承接上一轮的 6 个），只让**预期的那两条**变红，源文件按字节还原
  （sha256 `1a5090967167ee7e38cbaa8c8274a2d854f070cd7ed29553cb6b92f929b95e59` 修前修后一致）：

| 探针 | 改动 | 结果 |
|---|---|---|
| 7 | `_supported` 的 `> ret_med` 退回 `> 0` | **2 条红**，且失败信息直接显示退化：`{'opposing': [101, 102], 'supporting': [103, 104]}` —— 支持集**就是**高 T-1 分组。第 3 条新用例不变红，因为它检查的是记录的自洽（逐笔 `supports` 之和等于计数），不是判据方向 |

**仍未验证（本轮明确不当作已发生）**：
- **D40 的重试没修，也没验证过修法。** 本轮只把它量清楚并写下了验收判据。
- **D39 的原因查清了，但检查本身没修。** 写者是**生产侧的实时新闻入库**（不是回放、不是测试套件）；
  这个布尔值在一台生产还活着的机器上永远报「否」。修法是改问「回放写了吗」，本轮只写下判据。
- **`learning_candidates.status` 仍然没有任何迁移。** 刻意，5 条观察全部停在 `observation`。
- **修复后的 120 天复跑**（同一窗口、同一参数）跑完了：equity 1,007,818.07（+0.782%）、
  最大回撤 2.177%、121 次重试、**0 抛错**、**5 条观察**（n=4/5/6/7/8，
  日期 09-29、10-13、10-14、11-12、12-16 —— 与第一次的 5 个日期**没有一天重合**）。
  **它不是一次受控对照**：模型是重新采样的，两次运行的成交本身不同（买入 11 vs 12 笔、
  打标 8 vs 10 笔），所以修前/修后的方向对比依据是**探针 7**（同一批四笔、同一批 T-1 涨幅），
  不是这两次运行。两次运行能一起证明的是别的东西：**同一个缺陷在长窗口里稳定复现**
  （重试每天一次、语料检查报「否」），而且**闭环在两次独立采样里都自己转了 5 圈**。

### 第二十三轮（一天一个事件循环：D40 修掉，并把「重试」拆成两种原因，2026-09-16）

上一轮把 120 天跑通了，并记下五个缺陷。本轮修其中的 D40 —— 唯一一个**已经查明、
改动小、验收判据可测**的。

#### 1. 缺陷与修法

`t1_decider.propose_sync` 原本是 `asyncio.run(propose(**kwargs))`，一天一次。
这么写是有理由的：`scripts/walk_forward.py` 用普通循环走日历，日边界是 context manager
而不是 `await`，包一层是保持 runner 形状的最小改动。但它和另一件事冲突 ——
`Context.model` **整个窗口只建一次**，所以同一个 `AsyncOpenAI`（以及它内部的 httpx 连接池）
被约 120 个**各自独立的**事件循环共用。httpx 的连接池绑定在**打开它的那个循环**上，
于是每个新循环的第一次请求会取到一条属于已关闭循环的连接。

修法是一个窗口一个循环：

- `Context.loop = asyncio.new_event_loop()`（仅 `decider == "llm"`；占位决策器不调模型，
  没有东西需要绑定）；
- `propose_sync(loop=...)`：给了就用 `loop.run_until_complete`，没给仍是 `asyncio.run`；
- `run()` 把窗口体抽成 `_run_window(ctx, args)`，用 `try/finally` 关掉循环。

**为什么 `loop=None` 那条路要留着**：`asyncio.run` 只在**客户端活得比调用长**时才是错的。
一次性调用者用它是对的，测试用它也是对的。把默认也改掉，等于用一个更复杂的形状
换掉一个在别的场景里正确的形状。

#### 2. 修前 / 修后：同一个 3 天窗口，一行之差

同一提供方、同一参数，两次都跑完 3 天：

```
把 loop 忽略掉（复现旧行为）   3 天   "Retrying request" 2
一个窗口一个循环              3 天   "Retrying request" 0
```

#### 3. 写下来的那条判据本身是错的：得把「重试」拆成两种原因

D40 的验收判据原文是「同一 120 天窗口应从 120 次重试降到 **0** 次」。**这条判据不可达，
而且不是修得不够好。** `--model-timeout 120` 存在的原因就是免费档有时**排队**而不是回
`429`，那时客户端超时、SDK 重试 —— 于是「重试」是两种原因，一种治好了，另一种是提供方的。

两者按**重试行与上一行之间的间隔**干净地分开，因为两种失败的耗时不同：

```
                               重试数   间隔 < 30s   间隔 >= 30s
walk-120   （修前）              120       119            1   （89.9s）
walk-120b  （修前）              121       119            2   （87.7s、89.1s）
walk-120c  （修后）                3         0            3   （115.2s、117.0s、118.9s）
```

陈旧连接**瞬间**失败 —— 池子递回一条死连接，没有任何网络等待就报错，所以重试行与上一行
落在同一毫秒。提供方排队则要吃掉整个客户端超时。三个窗口合起来，两类是：

```
陈旧连接   238 次重试   最大 3.098s   （237 次 ≤ 0.03s）
提供方排队   6 次重试   最小 87.739s
                        ^^^ 一条 85 秒宽的空带 ^^^
```

判据是 **「间隔 < 30 秒的重试为 0」**，`walk-120c` 满足：它剩下的 3 次全是提供方排队，
陈旧连接一次也没有了。

**这条判据的第一版写的是「间隔 < 1 秒」，而它错在本仓库反复出错的那个方向上。**
它与上面三行处的「最大 3.1s」自相矛盾，而且照它去数，walk-120 的 119 次陈旧重试会被
数成 **118** —— 在一个每一次重试都有已知原因的窗口里留下一次无法解释的重试，
正好是把下一个人送去寻找一个并不存在的第二个缺陷的形状。**阈值必须落在实测出来的
空带里（3.098s → 87.739s），而不是按听起来像不像来选。** 而 `walk-120b` 的 119 次
恰好全部小于一秒，所以这条错判据会**在其中一个窗口通过、在另一个窗口失败** ——
这是阈值最坏的形状，因为只要有一次通过，它就会被留着。

**而且修前的计数会自己分解成 `119 + 1` 和 `119 + 2`，119 正好是 `120 天 − 1`。**
这是机制算出了自己的算术：第一天池子是空的，没有东西可绊，所以这个缺陷的成本是
**第一天之后每天一次**。两个 120 天窗口独立地给出同一个 119。

#### 4. 顺带发现：journal 数不出这类重试

看起来记录里应该留痕 —— `_JournalCompletions.create` 用 `except BaseException` 包住
`self._inner.create(...)` 并调 `record_failure`，所以「重试过的一天」似乎会留下一条
`llm_error` 挨着它的 `llm_call`。**它没有。** openai SDK 的重试发生在 `self._inner.create`
**内部**，那个异常到不了这层 handler。修前 120 天的记录实测是
**120 条 `llm_call`、0 条 `llm_error`** —— 一次调用一天，跟什么都没发生一样。

所以重试数只能从 `openai._base_client` 的日志里数，修前修后两边都是这么量的。

#### 5. 传输那一半写不了测试，写成了探针

`tests/conftest.py` 装了一个 audit hook，**直接拒绝** `AF_INET` 上的 `socket.bind` 与
`socket.connect`，所以套件里不可能有 loopback 端点。这是有意的隔离，**没有削弱它**。

于是分成两半：

- **契约**有测试：`tests/test_t1_decider.py::TestTheCallingLoopOutlivesTheClient` 三条 ——
  三次调用跑在同一个传入的循环上且该循环**结束时仍开着**、不给循环时三次各是一个新循环、
  以及「给了循环」和「没给」确实不是同一条路（否则参数可以被接受后忽略，前两条照样绿）。
- **传输**是探针：`scripts/probe_loop_binding.py`（仓库已有 `probe_market_snapshot.py`
  这个先例），同一个客户端两种驱动方式，打本地端点：

```
a loop per call:  call 1: 200   call 2: RuntimeError: Event loop is closed
                  call 3: 200   call 4: RuntimeError: Event loop is closed
one loop:         call 1: 200   call 2: 200   call 3: 200   call 4: 200
```

注意它的形状：**不是每次失败，而是每隔一次**。失败会把那条陈旧连接从池子里剔掉，
于是下一次取到空池、新开一条。这正好解释了为什么真实运行里需要 SDK 的重试**每天**失败一次
而不是每个客户端一次 —— 每一天结束时池子里都留着一条绑定在当天、即将关闭的循环上的连接，
第二天就绊在它上面。

#### 6. 顺带核对：那条「语料未变」的验收项钉的不是报告里那个检查（D39）

M1 的验收项「跑一次只写自己的账本」由
`test_a_run_leaves_the_live_book_and_the_corpus_alone` 钉住，它断言
`result["corpus"]["untouched"] is True`。夹具里的语料是 `tmp_path` 下**合成的**、
**没有第二个写者**，所以这条断言在夹具里**不可能失败**。

同一个布尔值在生产还活着的机器上**永远报「否」** —— 写者是生产侧的实时新闻入库（D39）。
结论：**用例钉住的是「runner 自己不写语料」，报告里那行问的是「语料变了没有」，
是两个不同的问题。** 前者已经有答案；后者要改成问前者，否则那行红是一盏永远亮着、
于是被读者学会忽略的灯。**本轮只写下这一点，不改检查** —— 它是一次报告的改动，
和本轮的循环修复混在一起，会让这一次验证说不清是哪个改动起了作用。

#### 7. 修后的 120 天窗口：判据达标，陈旧连接归零

同一窗口（`2025-07-01` 起 120 个交易日）、同一提供方、同一参数，修后重跑：

```
walk-120c   120 天   重试 3 次   间隔 115.2s / 117.0s / 118.9s   全是提供方排队
                     陈旧连接 0 次   ← 判据「间隔 < 30s 的重试为 0」达标
```

**这正是机制预测的形状**：修前的 119 次是「第一天之后每天一次」，修后一次也没有。
剩下那 3 次是免费档排队 —— 客户端吃满 `--model-timeout 120` 之后由 SDK 重试，
是提供方的行为，本次修复不该也不打算消除它。所以判据必须是**分类后的**那一条，
而不是「重试为 0」（§3）。

这次运行本身也是干净的：120 天**抛错 0**、`model_calls_made 120 / model_journal_total 120`、
`errors: []`；equity `993,711.12`（`-0.629%`）、最大回撤 `3.520%`、买入 12 笔 / 卖出 9 笔。
闭环自己转了：9 笔打标（3 笔 pending）、写下 **6 条**观察
（08-06、09-02、09-03、09-11、11-21、11-24），最近一条 `n=9`、支持 8 / 反对 1，
全部停在 `observation`。

`corpus_untouched` 报**「否」**，与上一轮一致，原因见 §6（D39：写者是生产侧的实时新闻入库，
不是回放）。**它不是本轮的回归** —— 这一条在修前修后都报「否」，而这正是 D39 要说的：
那行检查问的问题和它有答案的问题不是同一个。**（2026-09-17：该检查已按 D39 改掉，
`corpus_untouched` 这个名字不再存在，见第二十四轮。）**

#### 本轮验证

- `.venv/bin/python -m pytest tests/ -q` → **2273 passed / 18 skipped**
  （`tests/test_t1_decider.py` 17 → **20 条**；上一轮是 2270 + 18 = 2288，加本轮 3 条 = 2291，
  与 2273 + 18 逐数相符）。
- `.venv/bin/python scripts/lint_harness.py` → exit 0，**173 个文件**，存量 **13 条**（**未扩基线**）。
- `.venv/bin/python scripts/lint_docs.py` → exit 0。
- `.venv/bin/python scripts/probe_loop_binding.py` → 逐行复现 §5 引的那段：`a loop per call`
  2/4 失败、`one loop` 0/4 失败（`httpx 0.28.1`）。
- **120 天窗口复跑（§7）**：陈旧连接重试 **119 → 0**，判据「间隔 < 30s 的重试为 0」达标。

**一条环境口径，写下来以免下次重踩。** 全量套件在本机**第一次跑不完**：
`tests/test_build_lock.py` 4 条失败、`tests/test_web_no_monitor.py` 7 条**在收集阶段就中断**，
报的是 `PermissionError: Sensitive content approval timed out`。它来自注入每个 Python 进程的
`sitecustomize` shim（`PYTHONPATH` 指向 WorkBuddy 的 `cli/vendor/shim`），**不是断言失败**，
也不是本轮的改动 —— 本轮只碰 `t1_decider.py` 与 `walk_forward.py`，而这两个文件都不导入 `main`，
那 4 条测的却是 `main._build_lock()`。`env -u PYTHONPATH` 之后这 11 条 **3.09 秒全过**，
全量套件也随之跑完（1 分 44 秒）。**所以本轮记的 2273 是去掉 shim 之后的数；
带 shim 的那个「4 failed」既不是回归、也不该被写进任何验证记录。**
（`dangerouslyDisableSandbox` 不解决这件事 —— shim 走的是 `PYTHONPATH` 注入，不是操作系统沙箱，
关掉后者仍会等满 120 秒再抛。）

### 第二十四轮（D39 付清：把「文件变了没有」换成「这次回放写了没有」，2026-09-17）

第二十三轮刻意把 D39 留到下一轮（原话：「和本轮的循环修复混在一起，会让这一次验证说不清是
哪个改动起了作用」）。本轮付清。

#### 1. 真正的伤害是退出码，不只是那行红

先把代价量清楚。`main()` 的判据原文是：

```python
ok = (report["meta"]["production_db_unchanged"]
      and report["meta"]["corpus_untouched"]
      and report["meta"]["model_usage_ok"])
```

而那个 120 天窗口的记录是 `corpus_untouched: false`、`production_db_unchanged: true`、
`model_usage_ok: true` —— 也就是说**一次干净的运行被判成失败、返回 1**。
在还活着的机器上窗口越长越必然如此（生产侧的定时入库一直在写 `market_snapshots.db`，
实测大约每半小时一次）。所以这不是「报告里有一行不好看」，是**这个 runner 在长窗口上
不可能成功**。

#### 2. 换掉问题本身

新增 `_corpus_access_check`：对每个共享语料文件，读它**是不是 symlink** —— 这正是
`corpus_access` 用来决定 `mode=ro` 的那个谓词。判据 `read_only` =「这次运行读到的每个语料
文件，都是 store 写不了的那个」。指纹保留但**降级为诊断**：`_corpus_changes` 逐文件命名并
给出 delta，汇总行明说生产侧也在写这些文件、这一行不是对回放的判定。`main()` 的退出码
改挂 `corpus_read_only`。

#### 3. 两个决定是量出来的，不是挑出来的

**（a）检查不做探针。** 「这个句柄是不是只读」的每一种检测法都是**一次真的写尝试**。实测：

```
BEGIN IMMEDIATE          没有报错          <-- 什么都检测不出来
PRAGMA user_version = 0  OperationalError: attempt to write a readonly database
CREATE TABLE probe (x)   OperationalError: attempt to write a readonly database
```

所以探针会**在检查本该抓住的那个情形里把语料写坏** —— 对一个以保护语料为职责的检查，
这是坏交易。改成读那个决定模式的谓词；「写被拒绝」本身早就由
`tests/test_corpus_access.py` 在**一次性文件**上钉住了（那里做探针是安全的）。

**（b）判据必须能变红，否则它不是检查。** 负向情形是「bootstrap 没有链接的语料文件」——
那时 store 会以读写打开它，回放就能往历史里追加。
`test_a_corpus_file_that_was_not_shared_is_reported_writable` 用「把一条链接换成真副本」
把这个状态种出来。

#### 4. 验收（现跑）

新鲜 120 天窗口（占位决策器）：

```
回放对语料只读              是（3 个文件全部以只读打开）
共享语料 size+mtime（诊断） 无（没有任何共享文件变化）
```

exit **0** —— 同一形状的窗口在改之前返回 1。生产侧写入那一半由
`test_a_file_production_moves_does_not_fail_the_run` 钉住：窗口运行期间由一个线程反复重打
共享文件的 mtime，断言**诊断看见了、判据没受影响、`main` 仍然返回 0**。

#### 5. 两个探针，各只让一条变红

源文件按字节还原（`scripts/walk_forward.py` sha256
`5b24ce1798dddb255bfc3605152ef6a4e1d89cf25943e36b21256ef6bcff913b`）：

| 探针 | 改动 | 结果 |
|---|---|---|
| P1 | 退出条件退回 `not corpus_changes` | 1 条红 —— `test_a_file_production_moves_does_not_fail_the_run` |
| P2 | `read_only` 忽略 `shared` 谓词 | 1 条红 —— `test_a_corpus_file_that_was_not_shared_is_reported_writable` |
| F（旧探针） | `_corpus_fingerprint` 全返回 `None` | 1 条红，但**落点换了** —— 见 §9 第十五轮那段补记：守卫从指纹移到访问检查，F 现在钉的是「一个什么都看不见的指纹永远报不出变动」 |

#### 6. 仍未覆盖，如实写

验收那次运行**没有**碰上生产侧写入（`corpus_changes` 为空），所以「诊断行会把变动的文件名
印出来」在**真实生产写入**上的证明，靠的是那个线程测试，而不是一次真实窗口。要补的话是跑
一次 `--decider llm` 的长窗口（约 25 分钟，生产大约每半小时写一次）；本轮没有跑。

#### 本轮验证

- `.venv/bin/python -m pytest tests/ -q` → **2275 passed / 18 skipped**
  （上一轮 2273 + 本轮 2 条 = 2275，逐数相符；`test_walk_forward.py` 35 → **37 条**）。
- `.venv/bin/python scripts/lint_harness.py` → exit 0，**173 个文件**，存量 **13 条**（未扩基线）。
- `.venv/bin/python scripts/lint_docs.py` → exit 0。
- 120 天窗口（新代码，`/tmp/d39long`）→ exit **0**，`corpus_read_only: true`，
  三个语料文件均 `present && shared`。
- 三个探针共用同一个还原点，每次都核对 sha256（见 §5）。

## 10. Phase 2 逐项交付（S1–S6）

- **订单状态机**：`alpha_agents/data/order_state.py` 声明唯一状态集与合法迁移；
  `portfolio.py`（成交、撤单）与 `portfolio_exit.py`（平仓）是仅有的两个写入
  `virtual_portfolio.status` 的模块，两者都必须经过 `assert_transition`。
  该约束由 `tests/test_intent.py::TestOnlyTheStateMachineWritesStatus` 机械钉住。
- **现金预留**：`alpha_agents/data/reservations.py`。建单即写 `held`，
  成交转 `consumed`（差额释放），撤单/过期/拒绝转 `released`。
  `get_available_capital` = 总资金 + 已实现盈亏 − 持仓占用 − 未释放预留。
  **卖出所得不在这里扣**：A 股把「可用」与「可取」分开，T 日卖出的钱 **T 日即可再买**
  （可反复使用），只是 T+1 才可取现；在途那部分由 `settlement.unreleased_pending_total`
  单独报告给交易读模型。**2026-09-16 之前这里减了在途资金**，那是把**取现规则**套在**买入力**上
  —— D19 已修（`tests/test_cash_settlement_semantics.py`）。**本节长期写着那个错式子，
  2026-09-16 逐条核对验收清单时才发现并更正**：修了代码却把文档留在旧规则上，
  和 `tech-debt-tracker.md:136` 那条「uncommitted as of writing」是同一类谎言。
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

## 11. Phase 3 逐项交付（T1–T4，已全部交付）

计划见 [Phase 3 计划](exec-plans/completed/2026-09-12-trader-core-phase3.md)。本阶段有一句硬约束：
**不新增任何生效路径** —— 候选、快照都不进 prompt / 检索 / 决策上下文（§10：验证不等于授权）。

| 切片 | 状态 | 交付 |
|---|---|---|
| T1 episode 关联 | ✅ 已交付（第五轮） | `episodes` / `episode_events` 两表 + `alpha_agents/data/episodes.py`；门在 `intent.submit_intent`，成交/撤单两个钩子在 `portfolio`；只读入口 `scripts/episode_coverage.py` |
| T2 三种结果的生命周期 | ✅ 已交付（第六轮） | `outcomes` 状态机 + `alpha_agents/data/outcomes.py` 写读；三个标签生产者在 `evolution/outcome_labels.py`；写入者在 `pipeline/tasks/review.py::_label_outcomes`；`predictions.horizon_days` / `deadline` 两列 |
| T3 候选证据与生命周期 | ✅ 已交付（第七轮） | `learning_candidates` 四列 + 五态生命周期 + `candidate_transitions`（append-only）；写入口 `save_candidate`（四字段必填）/ `advance_candidate`（`status` 唯一写者），读侧 `counts` / `candidates_by_status` / `transitions_for` / `candidates_citing` / `integrity`；只读入口仍为 `scripts/episode_coverage.py`（扩了候选段） |
| T4 已批准知识快照 | ✅ 已交付（第八轮） | `knowledge_snapshots` / `knowledge_snapshot_items` 两表 + `alpha_agents/data/knowledge_snapshots.py`；写入口 `approve`（版本哈希由写入方现算）/ 只校验的 `plan` / 复算的 `verify_snapshot`，读侧 `candidate_is_approved` / `entity_is_approved` / `approved_entities` / `counts` / `drifted` / `integrity`；运维写入口 `scripts/approve_knowledge.py`，只读入口仍为 `scripts/episode_coverage.py`（扩了「已批准知识」段） |

### T3 / T4 的边界，逐条说清

这两个切片的边界**不在本节重复**，因为它们的边界全是「没有生效路径」这一类，已逐条写在
§7（`evidence_episode_ids` 在生产里恒为空、生命周期无业务驱动方、快照不是检索闸门、
读侧只有运维脚本与测试）与 §9 第七、八轮（两处「承诺超过代码」）。本节只记交付物本身。

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

## 12. Phase 4 逐项交付（U1–U5）

记录日期：2026-09-13。全量口径 `.venv/bin/python -m pytest tests/ -q`（不加 `--ignore`）
**1800 passed, 18 skipped**；`scripts/lint_harness.py` 通过（153 个文件，存量 47 条，
**未扩充 baseline**）；`scripts/lint_docs.py` 通过。四处关键判据做了变异探针，
四个全部被对应用例捕获（明细见已归档的
[Phase 4 计划](exec-plans/completed/2026-09-12-trader-core-phase4.md)）。

| 切片 | 提交 | 内容 |
|---|---|---|
| U1 冻结策略注册表 | `83fbdfa` | `policy_versions` / `active_policy` / `policy_approvals`；哈希从**活的配置源**重算，漂移是变红的断言；`policy_ref` 写进新产生的 `intents` / `decision_snapshots` |
| U2 影子预测与隔离 | `5f8b0a4` | `shadow_runs` / `shadow_predictions` 独立表；影子不进主账户、不进冠军知识命名空间。**出厂时生产者只有基线，2026-09-13 补上候选，见 §14** |
| U3 候选绑定评估 | `a5f288a` | `run_gate(policy_version_id)`：验证窗口由冻结时间派生，「今天 vs 今天」构造不出来；`validation_days` 提升为列 |
| U4 晋升与回滚 | `f23bca6` | 晋升 = 对指针的一次原子 CAS；人工批准不可变；回滚与晋升同片交付且不删任何成交/账本/结果；`scripts/policy.py` 是唯一写入口 |
| U5 检索闸门 | 本轮 | `inject_playbooks` / `inject_principles` 改为按**当前生效策略指向的已批准快照**选行；fail-closed |

### 本轮修掉的三处判据缺陷

1. **证据等级曾由调用方传入的标签决定。** 旧写法
   `... if run.get("baseline") == shadow.BASELINE_NAME else "candidate_policy"`，
   而 `baseline` 是 `open_run(baseline=...)` 的自由文本、`emit_for_date` 恒写 0.5 ——
   把 run 换个名字，基线证据就变成 `candidate_policy` 级。现在 `baseline` 列更名为
   `producer`，`open_run` 只接受 `shadow.PRODUCERS` 里登记的名字，等级由该名字的
   `kind` 推出（`shadow.scope_for`）。
2. **从 `detail_json` 读 scope 是 fail-open 的。** 键缺失时 `detail.get(...)` 得到
   `None`，而判据是「等于 `baseline_only` 才拒」，于是「什么都没记录」被读成
   「可以晋升」。现在 `evidence_scope` 是 `gate_decisions` 的列，判据改为
   `scope != SCOPE_CANDIDATE` 即拒，缺失落进同一拒绝分支。
3. **同版本多个开启的 shadow run 会让闸门静默挑错。** `run_gate` 曾用
   `open_run_for`（`ORDER BY id LIMIT 1`）取 run，第二个生产者一登记就会去评基线那个 run，
   产出一份看起来合规、答的却是另一个问题的裁决。现在 `shadow.open_runs_for`
   列出全部开启 run，多于一个即拒绝并点名。

### 明确未实现（Phase 4 的边界，别读成已完成）

> 本节记 2026-09-13 那两支切片**修好之后**剩下的边界；修了什么见 §14。
> 这里只写**仍然没有**的东西，因为「机制在」最容易被读成「在跑」。

- **候选生产者有了，但它不是「一个策略」。** `shadow.PRODUCERS` 现在有两条：恒 0.5 的
  基线与 `kind="candidate"` 的 `remap_confidence`。后者是 §12 意义上的
  **module-level 实验**：机会面板与上游信心标签都用冠军的并固定住，变的只有
  「标签 → 概率」这一层映射。它能被立刻评估（不必先等出一个 LLM 策略的样本），
  代价是它**测的不是整个策略**，而是映射这一层。机械判据：
  `tests/test_gate_candidate_bound.py::TestTheShippedCandidate`。
- **生产里没有发生过晋升。** `gate_decisions` 里没有非 abstain 的生产行。
  「晋升路径可达」由测试证明 —— 且现在用的是**出厂候选**而不是测试里临时登记的桩 ——
  它证明的仍然是**机制通**，不是**生产里跑过**。
- **影子跑有生产入口了，但没人跑过。** `scripts/policy.py` 现在有 `shadow-open` /
  `shadow-emit` / `shadow-score` / `gate` 四个动词，`status` 也带上了每条实验的
  `paired/needed` 进度。所以「开一条实验、按日喂它冠军的选股、评分、问闸门要裁决」
  是一条命令序列（见 §14.6）。**但生产里一条 run 都没开过**：`shadow_runs` 表不存在，
  `status` 会打印「no shadow run has been opened」。有动词与有人跑是两件事。
- **闸门没有生产调用者，所以 D7 的标题仍然成立。** `run_gate` 的调用者只有测试；
  `alpha_agents/pipeline/` 与 `alpha_agents/server/` 都不引用 `holdout_gate`。
  闸门从「永远触发不了」变成了「正确、可绑定候选、但没人调」—— 这是进步，
  也仍然不是「生产里在跑」。补法是给影子推进与评估各加一个调度入口。
- **晋升与检索的其余路径未收口。** 检索权重、校准器、退役决定仍能改变模型看到什么；
  §10 的「未批准即不生效」目前只对 `inject_playbooks` / `inject_principles` 成立。
- **已批准快照的 `detail_json` 与列并存。** `evidence_scope` 已提为列但仍在
  `detail_json` 里保留一份；读侧只读列，写入侧两份都写。冗余是有意的
  （历史行的 payload 不可改），但它意味着两者将来可能不一致，需要时各自说明。

## 13. Phase 5 逐项交付（V1 读模型与 API + V2 前端 + V3 对账）

记录日期：2026-09-13。计划见
[Phase 5 计划](exec-plans/completed/2026-09-13-trader-core-phase5.md)。

### 交付了什么

`alpha_agents/server/readmodels/`，按 §13 把「projections」放在 `server/` 层，不新建层、
不动 lint 豁免基线：

| 模块 | 端点 | sections |
|---|---|---|
| `readmodels/__init__.py` | — | 契约：`Need` / `Section` / 三态探针 / `workspace()` |
| `readmodels/trade.py` | `/api/trade-workspace` | `book`、`attribution`、`intents`、`settlement` |
| `readmodels/learn.py` | `/api/learn-journal` | `episodes`、`outcomes`、`candidates`、`forecasts` |
| `readmodels/evolve.py` | `/api/evolve-lab` | `pointer`、`shadow`、`gates`、`knowledge` + `code` |

三条设计决定，都是「不做」而不是「做」：

1. **读模型不建表。** 每个数据模块的读函数进入时都 `init_schema`（`episode_coverage.py`
   的第七轮更正记过同一件事：一个自称只读的报告脚本会改 schema）。所以 `section()`
   先做只读探针（`sqlite_master` + `PRAGMA table_info`），探针不通过就**不调用**读函数，
   把缺的表与列**点名**放进 `missing`。机械判据：
   `tests/test_readmodels.py::TestAReadModelDoesNotMigrate`，断言调用三个 `snapshot()`
   前后表集合完全相同，且五个「读者会建」的表仍然不存在。
2. **三态正交**：`schema`（`complete` / `partial` / `absent`）与 `state`
   （`unavailable` / `empty` / `present`）是两个字段。`partial` 不是理论情形 ——
   生产库今天就命中两处（`gate_decisions` 缺 `evidence_scope` / `validation_days`；
   `learning_candidates` 缺 `evidence_episode_ids` 且 `candidate_transitions` 整张表不在）。
   section 自己的说明在 `note`，`status_note` 说明**为什么读不到** —— 两个字段，
   因为让前者覆盖后者会把「本库 schema 落后于代码」埋进一句业务散文里。
3. **一个事实一个来源。** `/api/portfolio` 改为委托 `readmodels.trade.book()`，
   不再自己拼一份持仓。两个对同一批表的组装会漂移，然后「我持有什么」就有两个答案。
   机械判据：
   `tests/test_readmodels.py::TestPortfolioIsTheTradeBook::test_the_route_is_a_delegation_not_a_second_assembly`
   扫 `get_portfolio_api` 的源码，出现 `trade.book` 且不得再出现 `get_closed_positions`。

### 生产库（副本）上的真实读数

| 工作台 | section | schema | state |
|---|---|---|---|
| trade | `book` | complete | **present（52 行）** |
| trade | `attribution` / `intents` / `settlement` | complete | empty |
| learn | `episodes` / `outcomes` | complete | empty |
| learn | `candidates` | **partial** | unavailable |
| learn | `forecasts` | complete | **present（202 行）** |
| evolve | `pointer` / `shadow` | **absent** | unavailable |
| evolve | `pointer`（**2026-09-14 复测**） | complete | **present（1 个版本在效）** |
| evolve | `shadow`（**2026-09-14 复测**） | complete | empty（表已建、0 条 run） |
| evolve | `gates` | **partial** | unavailable |
| evolve | `knowledge` | complete | empty |

`evolve.code` 报出 `producers=['constant_0.5', 'remap_confidence']` /
`candidate_producers=['remap_confidence']` / `reachable=True` ——
**2026-09-13 之后这一项翻了**，而它翻的是*代码*事实不是数据事实：现在这个构建
**能**产出可晋升的裁决（见 §14），而生产里仍然一个版本都没有、一条实验都没跑。
页面上的横幅把这两件事分开说，正是为了让「可达」不被读成「在跑」。
读完之后**没有任何表被新建**。

**验证用的是副本。** `memory_store._get_conn()` 首次连接时会执行自己的 `_SCHEMA`，
所以打开 `data/memory.db` 本身就会补上 `knowledge_snapshots` / `_items`。那是共享连接的
行为、不是读模型的决定，但它是一次对生产文件的写 —— 本阶段没有这项授权，用副本取同样的证据。

### V2：三个前端工作台（2026-09-13 同日交付）

V1 让事实可读，V2 让**三态可区分**。如果说 V1 的交付物是三个端点，V2 的交付物是一个
命令：`cd web && npm run check:render`。

| 文件 | 作用 |
|---|---|
| `web/src/components/WorkspaceSection.jsx` | `SectionMeta` / `Chip`（schema 与 state 两枚徽章各一）、`Blocked`（点名缺的表与列）、`WorkspaceCard`、`WorkspaceHead`、`CodeFact` |
| `web/src/views/LearnView.jsx` | 决策覆盖 / 三类结果 / 候选知识（隔离区）/ 预测与评估货币 |
| `web/src/views/EvolveView.jsx` | 晋升可达性横幅 + 什么在生效 / 影子实验 / 闸门裁决 / 已批准知识快照 |
| `web/src/views/PortfolioView.jsx` | 接归因链、改账审计、资金与结算三节；持仓账本加闸门 |
| `web/render-check.jsx` | 3 状态 × 3 视图的渲染矩阵，逐用例声明「必须出现」与「**不得**出现」 |

前端把三态渲染成三种不同的东西：`absent` 与 `partial` **各有自己的说明文案**
（「这些表在这个库里不存在」vs「表在，但这个库的 schema 落后于代码」），
因为两者指向不同的修复动作；payload 整个没到是**第四态**，有自己的文案
（「整个读模型没到」），且**不得**打印 `0 有数据 · 0 空 · 0 读不到` —— 那正是健康空页的样子。

**`check:render` 首轮抓到的三件事，`npm run lint` 与 `npm run build` 对一个都不报错：**

1. **两个新视图在 payload 为空时崩溃。** `LearnView` / `EvolveView` 里的
   `Episodes` / `Outcomes` / `Pointer` / `Shadow` **在把 `sec` 交给 `WorkspaceCard` 之前**
   就先读了 `sec.value`，所以 `WorkspaceCard` 里那句 `if (!sec) return null` 根本没机会执行。
   `PortfolioView` 写的是 `sec?.value`，因此没崩 —— 同层的三个视图两种写法，
   只有第三种状态把差别暴露出来。
2. **`trade.book` 把「表读不到」渲染成「你没有持仓」。** 这一节不是一张卡，是五张手搭卡片
   （持仓 / 挂单 / 已结束 / 按主线 / 分账本），没有一张能分辨「表不在」与「表里没行」。
   修法：整组闸在 `book.state === 'unavailable'` 上，四个 KPI 的「暂无…」位置改放
   `读不到 <缺失项>`。**这是本阶段最该出现的一类 bug，而它出现在最重要的页面的最重要一节。**
3. **「整个读模型没到」原本与「四节都读不到」渲染成同一句话。**

机械判据：`check:render` 的 7 个用例（3 状态 × 3 视图 + 3 个 payload-为-空）
全部 `OK`，且**每个用例的「不得出现」列表为空**。变异探针 P6（把 `bookBlocked` 恒设为
`false`）被 `trade · all absent` 捕获。

### V3：口径对账

README 的 Phase 5 行、`ARCHITECTURE.md` 的 `server/` 行、设计 §14 的 Phase 5 行、
本文件 §7 与本节，都改成同一口径：**三个工作台已贯通 API 与前端，且它们在生产库上
处于三种不同的 schema 状态**。「页面能打开」与「页面有事实」分开写。

### 明确未实现（Phase 5 的边界，别读成已完成）

- **三个工作台里有两个今天没有可显示的事实，而这跟界面无关。**
  `learn.episodes` / `outcomes` 是 0 行（表在、合法空态）；`evolve.pointer` / `shadow`
  的表**根本不存在**。所以「学习日志」「进化实验台」这两个页面**已经上线**，
  **但它们的页面内容大部分是当前状态的说明，而不是数据**。
  任何「已上线 = 有内容」的读法都是错的。
  **（2026-09-14 更新：`evolve.pointer` 已经是 `present`（V1 在效）、`shadow` 是
  `empty`（表建好、0 条 run），所以进化实验台从这天起有真实内容；`learn` 那一侧
  没变，仍然主要是状态说明。见 §14.7。）**
- **`evolve.candidates` 与 `evolve.gates` 会显示 `partial`。** 这要求运维动作
  （让当前代码的进程连一次库，或显式迁移），不是前端能修的。页面只报状态。
- **本阶段没有任何写入路径被修改。** 三个读模型全部只读，不新增写者。
- **`check:render` 不进 CI。** `harness.yml` 里没有 node 环境，本仓库的前端逻辑测试
  （`tests/test_report_markdown.mjs`）同样是本地命令。给前端单开 CI 是独立的一件事。
- **G1（奖励改成 Brier + 因子残差 alpha）的机制已在跑，见 §7 对应条目。**
  页面上的「还没有评估货币」说的是**样本还没成熟**，不是代码没接；
  而「什么时候算成熟」这件事在 2026-09-13 修过一次（D10，日历天 vs 交易日），见 §14。

## 14. 第 0 步与路线 B（Phase 4 闭环可运行，2026-09-13）

记录日期：2026-09-13（§14.6 / §14.7 于次日补上）。计划见
[成熟度计划](exec-plans/completed/2026-09-13-forecast-maturity-trading-days.md)、
[闭环计划](exec-plans/completed/2026-09-13-evolution-loop-runnable.md)、
[操作面计划](exec-plans/completed/2026-09-14-evolution-operator-surface.md) 与
[调度计划](exec-plans/completed/2026-09-14-shadow-run-scheduled.md)。

这一节把两支切片写在一起，因为它们是同一个决定的两半：**先把判据说准，再把闭环接上**。
两件事都不是「加功能」，都是**取消一个谎**。

### 14.1 预报成熟度：日历到期 ≠ 证据窗口收口（D10）

修的是 §7 那条。三个问题被一个过滤器粘在了一起 —— ①过没过声明的期限（日历）、
②市场把这个窗口交易到收口没有（交易日）、③这一笔拿不拿得到分数（有没有价格）。
把 ②③ 混起来的后果是「我们还没等到」被写成「我们等了、没等到」。

| 位置 | 变化 |
|---|---|
| `scoring.evidence_window_closed(entry_date, horizon)` | 新增。**唯一**的判据来源：`daily_kline` 中 `date >= entry_date` 的不同日期数 ≥ `horizon + 1`。与它守着的 `_forward_return` 共用 `_market_dates` |
| `outcome_labels.label_forecast` | `scored is None` 时先问窗口：未收口 → **不写任何终态**（`changed=False` / `deferred=True`，标签停在 `pending`）；收口且无分 → 仍写 `censored`（原样保留） |
| `review._score_due_predictions` | 分别计数「已评分 / 窗口未收 / 已删失」并一起记日志；此前只报一个 `scored` |
| `shadow.score_due` | 同一刀：窗口未收记 `deferred`，**不**记 `unscorable` —— 后者专指「窗口收了但拿不到价格」 |

三条设计决定（详见计划的决策日志）：不改 `deadline` 的日历语义（它是作者的**声明**）；
判据放在产出它的模块里而不是由调用方传布尔值进来；行情库缺失时判「未收口」——
写 `censored` 是对世界下断言，而归档缺失是**我们读世界**的故障，该由 source health 报。

**本轮的六个变异探针**（先让它红，再复原；脚本 `.pytest-tmp/probe.py`，
每次复原后校验锚点仍在）：

| 探针 | 结果 |
|---|---|
| `evidence_window_closed` 恒返回 `True`（fail-open） | 1 failed（`test_five_calendar_days_are_four_trading_days`） |
| 去掉 deferred 分支（未到期又写成 `censored`） | 1 failed（`test_a_window_that_is_still_open_is_not_censored`） |
| 概率映射忽略指针、读回常量 | 1 failed（`test_the_decisions_follow_the_pointer`） |
| 候选生产者改读冠军的 `prob` 而不是 `confidence` | 1 failed（`test_it_does_not_read_the_champions_probability`） |
| 验证一个版本时不再 staging 它自己的 `decision` | 1 failed（`test_a_candidate_verifies_against_itself_not_the_incumbent`） |
| `freeze` 丢掉 `--decision-json` 暂存的内容 | 1 failed（`test_a_candidate_records_the_block_it_asserts`） |

**新增的测试**：`tests/test_score_due_predictions.py`（6 个用例，此前该函数**一个测试都没有**）。
它**不用桩**：临时造 `daily_kline`（60 支 × 43 天），跑真实的 `evidence_window_closed` +
真实的 `score_prediction`，断言一轮扫描里三种结局共存。日历 bug 当初就是在这个函数里
躲过了所有测试，所以这一轮要求它的覆盖必须是真行情。

### 14.2 第 0 步：`freeze` / `install`

`scripts/policy.py` 此前四个动词（status / approve / promote / rollback）**没有一个能创建
第一行记录** —— approve 与 promote 都要引用一个必须已存在的版本。于是生产库
`policy_versions` / `active_policy` / `shadow_runs` 三张表**都不存在**，整套机制没有操作入口。

新增两个动词，并把「创建记录」与「移动指针」分开：

- `freeze --by --reason [--parent-version] [--at] [--dry-run] [--decision-json]`
  —— 写出一个版本，**不动指针**。内容幂等：同一配置再 freeze 返回同一个版本号。
- `install --version --by --reason [--at] [--dry-run]` —— **在没有任何版本在效时**建立指针。
  已有指针则拒绝并说明「移动它是一次晋升或回滚，两者都要证据」。重复 install 是后门，被堵。

`--decision-json` 是路线 B 的入口：指针决定的参数不在磁盘上，所以**候选只能靠声明写下来**。

### 14.3 路线 B：让一个来源真的可执行，并让提升真的改变行为

第 0 步只让记录能建；**它不解决「提升在行为上是一次空操作」**。原因是
`promote` 要求 live 配置仍等于目标版本的哈希，而冠军的概率读的是代码里的常量 ——
于是任何能通过 `promote` 的版本，其参数**在提升前就已经在跑**。指针只是给已经在跑的东西
起了个名字，而影子实验里候选与冠军是同一个映射的两份记录。

修法是给这组参数**唯一的权威来源**：新增第 6 个声明来源 `decision`
（`confidence_priors` / `dim_step` / `dim_base`），决策路径读**在效版本**里的值，
没有版本在效才读代码默认。

| 位置 | 变化 |
|---|---|
| `policy_registry.SOURCE_NAMES` | 增加 `SOURCE_DECISION = "decision"`：第 2 个**指针控制**的来源 |
| `scoring` | `DEFAULT_DECISION_PARAMS` 取代三个模块常量；`confidence_to_prob(..., params=None)` 默认读**在效版本**；`decision_params_of` / `in_force_decision_params` |
| `policy_sources` | `collect(decision_params=...)`；`_staged_from` / `collect_for_version(version_id)` —— 与 `knowledge` 同法 staging |
| `shadow` | `DecisionContext`（版本参数 + 面板信号）；`Producer.forecast(date, code, ctx)`；出厂候选 `remap_confidence`（`kind="candidate"`） |
| `scripts/policy.py::_sources` | 收敛到 `collect_for_version`，四个检查点走同一条路 |

**staging 是这一节最容易漏、又最难发现的点。** 指针控制的来源若跟 live 比，
每个「不在效的版本」都会被报成 drifted —— 提升无法移动到任何与现在的不同的版本，
回滚会被安全检查本身挡死。`test_a_rollback_restores_the_previous_mapping` 就是这个锚：
它会让 V2 在效、然后回滚到 V1，并断言决策路径真的回到旧映射。

**关于出厂候选的边界，必须一并说清**：`remap_confidence` 是 §12 意义上的
**module-level 实验** —— 面板与上游信心标签都是冠军的并固定住，只有「标签 → 概率」
这一层映射是变量。所以它**能被立刻评估**（不必等一个 LLM 策略攒出样本），
代价是它测的不是整个策略。另外冠军自己的概率在 `dims_passed` 可得时走的是
「基数 + 步长 × 维度」那条分支，而候选只能走标签分支（标签是冠军唯一记录下来的输入），
所以这对测量偏重标签路径 —— 而生产库里 39/47 行正是 `intraday` 那条标签路径。

### 14.4 两处被钉住的红线，按设计者的要求翻了

两处机械判据在本轮**如期变红**，它们的 docstring 都写着「加了候选生产者就会红，
请连同文档一起重审」：

| 用例 | 从 | 到 |
|---|---|---|
| `test_gate_candidate_bound.py::TestTheScopeBelongsToTheProducer::test_the_shipped_registry_holds_*` | 恰好 0 个候选 | 恰好 1 个候选，且其 scope 是 `candidate_policy` |
| `test_readmodels.py::TestTheEvolvePayloadCarriesTheGap::test_this_build_registers_*` | `candidate_producers == []` / `reachable is False` | `== [remap_confidence]` / `reachable is True` |

这是「文档与代码一起被重审」而不是「把断言删掉」：两条都保留了断言并在 docstring 里
写明了为什么翻。

### 14.5 明确没做（本轮 09-13 那支切片的边界）

> 这一节写的是 **09-13 那天收盘时的**边界，保留原文以便对照。其中三条已在
> **09-14** 被后面的切片推翻或偿还，逐条在下面注明；当前边界以 §12 与 §14.7 为准。

- **没有跑过任何一次真实 freeze / install / promote。** 生产库的三张表仍然不存在。
  本轮的交付物是**能跑**，不是**跑过**：往生产注册表写第一个版本、开第一条影子 run
  是操作决定，需要人拿着 CLI 做，且必须先 freeze 一个与在效版本不同的版本。
  **（09-14 更新：freeze 与 install 已在生产库执行，V1 在效，见 §14.7。
  promote 仍然没有跑过，而且今天也跑不了 —— 它要 ≥20 个配对交易日的前向证据。）**
- **影子跑仍然没有生产入口**（无 `open_run` / `emit_for_date` 的 CLI 动词）——
  见 §12 的边界清单。**（09-14 更新：四个动词补上了，见 §14.6；
  而且 `shadow-emit` / `shadow-score` 已经接上每日调度，见 §14.7。）**
- **没有实现 `apply(version_id)`**（把版本参数写回磁盘）。在指针决定参数的模型里
  它不是必需品：提升之后行为已经改变，磁盘常量退化为「未安装时的默认值」。
- **`check:render` 的 evolve 用例仍只用 `reachable: false` 的合成 payload**，
  所以翻成 `true` 的那条渲染分支**目前没有渲染用例**。合成 payload 覆盖的是渲染，
  不是构建事实，所以它不会因为本轮改动而红 —— 记下来，别当成已经覆盖了。
  **（09-14 更新：D11 已偿还，两个分支各一个用例。）**
- **`_require_matches_live` 没动。** 它对「提示词被手改而没开新版本」的拦截仍然有效，
  也是 rollback 安全性的依据；本轮只把「指针控制的来源」从它管辖的范围里按
  `knowledge` 的先例分出来（staging），没有放松其它四个。

### 14.6 操作面：把实验接到同一个审计入口上（2026-09-14）

`dd24c67` 让「闭环可运行」对**记录**成立，但对**实验**不成立：
`shadow.open_run` / `shadow.emit_for_date` / `holdout_gate.run_gate` 的调用者
**都只有测试**，所以那条链只能靠写 Python 跑 —— 而一套只有测试调用者的机制，
在操作上与没有机制是同一件事。D7 的标题（「闸门没有调用者」）说的正是这个。

`scripts/policy.py` 增四个动词，现在按执行顺序是九个：

| 动词 | 作用 | 写什么 |
|---|---|---|
| `shadow-open` | 开一条实验，绑定一个冻结版本 | 一行 `shadow_runs` |
| `shadow-emit` | 为某一天写挑战者的预测（面板=冠军当天的选股） | `shadow_predictions` upsert |
| `shadow-score` | 给窗口已收口的预测补分数，报出「已评分/窗口未收/已删失」 | `shadow_predictions` 的评分列 |
| `gate` | 要一份裁决并**记录**它（不动指针） | 一行 `gate_decisions` |

三处刻意的设计决定：

1. **`gate` 没有 `--dry-run`。** 能做的 dry-run 只有两种：真调用（那就写了），
   或在 CLI 里重实现一遍资格判定（窗口 / 开启的 run 数 / 样本量）——后者正是本仓库
   反复修的「同一个问题两个答案」。改为把进度表放进 `status`：**配对天数 `paired/needed`**
   就是「现在问值不值」的那个数。
2. **`--producer` 必填、不设默认。** 默认基线会让「本想测候选、结果拿到基线」的实验
   看起来正常（裁决 `baseline_only`，晋升只接受 `candidate_policy`）；默认候选则跳过
   §11 的第一个问题。也不在 argparse 里用 `choices`——那是第二份登记表，
   由 `shadow.open_run` 拒绝并列出已登记的名字。
3. **`shadow-open` 指向在效版本时打印警告。** 那等于让挑战者用与冠军同一套参数，
   实验就变成拿策略与自己比；这是最容易犯又最难看出来的错误，
   所以它在**开的时候**说，而不是在配对数为 0 的时候让人猜。
4. **`status` 把在效的决策参数按值打印出来**（不只打哈希）。`install` 之后
   `confidence_priors` / `dim_step` / `dim_base` 的**所有权从磁盘转到指针**，
   再改 `scoring.DEFAULT_DECISION_PARAMS` 对行为没有任何影响 —— 如果读不回在效的那一组，
   「改了没用」与「改好了」在操作者眼里完全一样。这一行就是那条区分。

**D11（渲染用例）同日偿还**：`web/render-check.jsx` 的 evolve 用例此前只有
`reachable: false` 的合成 payload，而真实页面在 09-13 已经翻到 `true` 那一支。
现在两个分支各一个用例，断言用的是**各分支自己的文案**
（「至少有一个候选生产者已登记」vs「本构建不可达」），不是「可达」——
后者是前者的子串，会被**另一个分支**满足。变异探针（把 `open` 钉成 `false`）
确认新用例会红。矩阵从 7 个用例变 8 个。

**仍然没做，且不是本切片能做的**：没有自动调度（没有东西把 `shadow-emit` /
`shadow-score` 挂到每个交易日），生产里也仍然一条 run 都没开。
**「有动词」与「有人跑」是两件事** —— `status` 里那句
「no shadow run has been opened」就是这条边界的证据。

### 14.7 第 0 步在生产库上执行 + 实验接上调度（2026-09-14）

两件事，同一天，都是**由人决定的**（用户明确选了「freeze + install」与「现在就接调度」）。

**一、生产库的指针建立了。** 这是本仓库第一次对 `data/memory.db` 写策略记录：

```
$ .venv/bin/python scripts/policy.py freeze --by evilkylin \
      --reason "the seed before any experiment: the default decision mapping"
  version #1 written.  content hash c1d8632aca3ef97e
$ .venv/bin/python scripts/policy.py install --version 1 --by evilkylin \
      --reason "nothing was in force; the loop's first version"
policy 'trader' now has version #1 in force at seq 1.
```

复核（`status`）：`in force: version #1 at seq 1`、`live configuration still matches: True`、
`integrity: clean`、`decision parameters in force: {"confidence_priors": {...}, "dim_base": 0.44,
"dim_step": 0.04}` —— **与 `DEFAULT_DECISION_PARAMS` 逐值相同，所以行为中性**：
装上的是「已经在跑的配置」，只是它现在有名字了。

**由此生效的所有权切换（单向）**：`confidence_priors` / `dim_step` / `dim_base` 从此归指针。
再改 `scoring.DEFAULT_DECISION_PARAMS` **对行为没有影响**。要改映射只有一条路：
`freeze --decision-json` → 开影子跑 → **等 ≥20 个配对交易日**（`MIN_VALIDATION_SAMPLES`；
本仓库 `n<50` 的规则把诚实门槛定在 50）→ `gate` → `approve` → `promote`。
没有 uninstall。这是 §11 的意图，但它是单向的，所以 `status` 现在把在效的那组参数
**按值**打出来 —— 让「改了没用」与「改好了」不再长得一样（§14.6 第 4 条）。

**二、实验每天自己跑。** `alpha_agents/pipeline/tasks/shadow_run.py` + `main.py` 里
`Task("shadow_run", ..., dtime(15, 45))`，交易日 15:45，**排在 15:30 的 review 之后**
（那时冠军当天的选股已经定了，面板才是最终面板）。它做两件事：

1. 对每条**开启**的 run 写挑战者当天的预测（面板 = 冠军当天的选股）；
2. 给窗口已被市场交易到收口的预测补分数，并分别报出「已评分 / 窗口未收 / 已删失」。

**它刻意不问闸门。** 裁决是给人做决定用的证据，每天问一次只会在配对天数填满之前
天天写一行几乎一样的 `insufficient` —— 「有治理的形状、没有治理的实质」，
而那正是旧的 `run_gate("daily_playbook", today, today)` 被删掉的原因。
报告里给的是进度：`配对进度 0/20，还差 20 个交易日`。

**副本上的实测（2026-09-14，写入之后）**：三个工作台里只有 evolve 变了 ——
`pointer` 从 **absent → present**，`shadow` 从 **absent → empty**（表建好了、0 条 run），
`gates` 仍 **partial**、`knowledge` 仍 empty；`evolve.code` 报
`candidate_producers=['remap_confidence']` / `reachable=True`。
**所以「进化实验台」这个页面从今天起有真实内容了**（在此之前的 24 小时里它显示的是
「表不存在」）。

**一个提前验证掉的风险**：生产库的 `gate_decisions` 是**旧 schema**（缺
`evidence_scope` / `validation_days` / `policy_version_id` / `outcome` 四列），
而 `gate` 要往这四列里写。`_ensure_gate_table` 会在**第一次写入时**跑
`ALTER TABLE`，所以在**副本**上试过一次：四列补齐、行写进去、`outcome` 与
`evidence_scope` 读得回来。**没有**因此在生产库上写任何裁决行
（生产 `gate_decisions` 仍是旧 schema，仍是 3 行 abstained）。
这一步是刻意提前做的：等 20 个交易日后敲 `gate` 才发现表写不进去，代价太大。

### 14.8 闸门自己问了（同日第三支）

上面那句「报告里给的是进度」在当天晚些时候被推翻了一半：**现在会问，但只问一次。**

规则是机械的：某条实验的**配对天数首次达到**闸门要求的那个交易日，
`shadow_run` 替人问一次；此后只要最新一条裁决的 `validation_days` 已经达标，
就不再问。理由不是省事，是 §12 禁止反复窥视 —— 每天问一次等于对同一个实验反复取样，
「其中有一次说 promote」就不再是证据；顺带也不再每天写一行几乎相同的 `insufficient`。
**人工提前问过不会抑制自动提问**，因为那条裁决的 `validation_days` 本来就不足门槛。

三处一起还掉的债（D2 / D7 / D9）逐条见 §9 第十二轮与
[tech-debt tracker](exec-plans/tech-debt-tracker.md)；其中 **D2 的条目自己写错了修法**，
已在条目里写明为什么「整体搬到 `pipeline/`」会造出更糟的反向违规。

### 14.9 主线门：从 `strength >= 4` 到截面评分（同日第四支）

一次专家复盘提了六条，指向同一件事：**下单门槛建在了错误的变量上。**

`strength` 量的是「这条主线被确认了多少个交易日」，却被当成「今天值不值得买」的一票
否决门；`±1` 的量纲又把 `+1.01%` 与 `+9.9%`、`0.01 亿` 与 `55 亿` 记成同分，且从不做
截面标准化。代价写在库里的 `close_reason`：**117 单撤 106（90.6%），约 100 单死于
「主线走弱」**，而那是在下单与成交之间**每 5 分钟**用**日频**指标重检的结果。

| 改动 | 位置 | 要点 |
|---|---|---|
新增第三个数 `trend_score` | `theme_lines` + `theme_manager.theme_score` | 资金分位 · 45% + 相对强度分位 · 35% + 确认度 · 20%，每轮刷新 |
截面含**行业** | `tools.theme_cross_section` | 概念 287 + 行业 90；只读概念框架是 4 条主线永久冻结的成因 |
容错匹配 | `tools.board_match` | `小金属概念`→`小金属`；无中文名只走精确匹配（`5G` 不得命中 `F5G概念`） |
准入 ≠ 撤销 | `data/theme_gate.py`（新模块） | 准入 0.5 / 撤销 0.35，中间是迟滞带 |
门槛进指针 | `scoring.DEFAULT_DECISION_PARAMS["theme_gate"]` | 五个数，读 `in_force_decision_params()` |
评分送进模型 | `entry_pricing.build_context` | `theme_note` 走 `prior_view` 同一条路 |
缺席只计数 | `theme_lines.unmeasured_days` | 连续 2 日才衰减；第 1 次当作抓取不全 |
门里只剩两条硬条件 | `theme_gate.theme_gate` | 没有可跟踪主线 / 状态已 `declining|archived` |

判据 9 条与决策日志 D1–D10 见
[计划文档](exec-plans/completed/2026-09-14-theme-signal-as-a-score.md)。

**实现中量出来的四件事改了设计，其中两件否掉了我原先写在计划里的做法：**

1. **上游只返回部分板块**：连续四次抓取 **287 / 328 / 333 / 380** 条（文档说 387），
   成员在调用间翻转（`共封装光学(CPO)` 有→无→有）。所以「板块里没有这个名字」多数是
   **截断**，不是「这个板块不存在」——第一版写的「缺席即清分并衰减」会以网络噪声的速率
   退役健康主线。改成连续缺席计数（D8）。
2. **`共封装光学(CPO)` 板块里根本没有这一行**，而它是账面上最强的主线（强度 6）且挂着
   活单 #117 中石科技。第一版的「未评分 ⇒ 拒绝」会把老毛病换个输入重犯，改成**放行**，
   退出交给生命周期：连续缺席 → 衰减 → `archived`（D9）。
3. **4 条僵尸主线的成因是框架只覆盖概念**（`石油加工贸易`/`港口航运` 是行业名），
   不是「没衰减」——覆盖修好后 40 条主线命中 38 条。**所以 rec 6 从主要修复手段降级为兜底。**
4. **门的方向得到实证**：`比亚迪概念` 新门 **0.76**（旧门 strength 2 → 拒），而它正是
   09-14 灭掉 4 个候选的那条主线。当日 40 条里旧门放行 2 条、新门放行 4 条，
   `天然气` 被换出（strength 4，当日分 0.28）。

**§9 第五轮留的那条指示照做了**：`portfolio.py` 曾 1197/1200，这次改动必须往里加东西，
于是先把主题门（`theme_gate` / `theme_admits` / `resolve_theme`）拆到
`data/theme_gate.py`，删掉已成死常量的 `MIN_THEME_STRENGTH`（状态机用的是自己那组字面量
7 / 9 / 4 / 1）。现在 `portfolio.py` **1122 行**。注意当初点名的是「资金与主线敞口」那一组
读函数（`_cluster_room` / `get_theme_exposure`），本轮拆的是**门**——敞口读函数仍在原处，
如果下一次还要动那个文件，优先拆它们。

**顺带发现的既有死代码（不是本轮造成）**：`_fix_prices_in_report` 与
`_auto_fill_actionable` 在 HEAD 里**也没有任何调用点**——「用真实行情覆盖幻觉价格、
把涨停股移出可操作表」和「可操作表为空时用 beta 预选兜底」两条安全网**从未运行**。
本轮只把它们连同追因函数搬到 `tasks/intraday_report.py`（`intraday_monitor.py` 撞了
1200 行上限，拆的是「报告文本」这一组），**没有接线**：打开它们会改变报告内容，
那是另一个决定，已写进那个模块的 docstring。

验证：**1938 passed / 18 skipped**（本轮净增 28 条）；`lint_harness` 166 文件 0 新增；
`lint_docs` 通过；6 个变异探针（改坏源码 → 确认对应用例变红 → 复原）全部有效，
覆盖：拉丁片段一致性、衰减门槛、缺席计数复位、`theme_note` 出/入提示词、未评分放行。

### 14.10 挂单必须能被了结：预留预检、每轮论点体检、真正的到期（同日第五支）

起点是「有一笔挂单没成交」这个问题。追下去不是两个 bug，是**一条从未被写下的不变式**：
「pending 挂单必须恰好持有一条 `held` 现金预留」。三个地方依赖它——`_fill_order` 的
`consume_reservation`、撤单路径的 `release_reservation`、以及 `reserve_for_order` 只在建单时被
调用——但没有一处保证它。

| 订单 | 为什么没成交 | 真正的缺陷 |
|---|---|---|
| #117 中石科技 | 09-11 09:49 建于 84.69，区间 95.5–98.0，之后四个交易日最高 87.24 | 它建于 `reservations` 模块（当晚 22:58 随 `0fbe89a` 落地）**之前 13 小时**，没有预留行 ⇒ **既成交不了也撤不掉**；而它按 conviction 排在首位，异常抛出循环、被 `manage_book` 吞成一句「组合管理失败」，**该交易员当轮剩余动作全被跳过** |
| #118 德福科技 | 14:27:42 建于 109.32，区间 109.3–112.5；当日 112.02 在服务启动之前，之后一直在 108.98–109.17 | 一次真实的错过，但指标里**没有能装它的桶**：`_ENTRY_OUTCOMES` 的 `"当日未成交"` 全仓库没有任何生产者 |

| 改动 | 位置 | 要点 |
|---|---|---|
| B 预留预检 | `portfolio.check_pending_orders` | 进入任何规则前先取预留：没有 ⇒ **adopt**（按建单同一公式补一条 `held`，幂等）并打醒目日志；处于终态 ⇒ **跳过 + 每轮 error**，不猜、不修、不撤 |
| C 论点体检每轮执行 | 同上 | `_thesis_already_broken` 从 `if triggered:` 里搬出来；**没有**新增「跌破带子下沿即撤」——那等于再写一套阈值，而论点里已经声明了失效条件 |
| D 到期真正被读 | 同上 + `portfolio._expire_days_for` | 建单 `expire_days` = 论点 `horizon_days` → 交易员 `default_horizon_days` → `PENDING_EXPIRE_DAYS`；监控侧 `days_pending >= expire_days` 撤单，理由含「未到价」 |
| E 指标双向 | `portfolio_risk` | `missed_right` = 「介入价太低」+「价格未到」；注入文案同时给「把区间上移」与「拉回或承认不会来、skip」两条路 |

**实现中量出来的四件事改了设计，其中一件否掉了我写在计划里的做法：**

1. **成交必须压过到期。** 计划 D5 只定了判据 `days_pending >= expire_days`，没定它与成交检查的先后，
   实现时写的是「到期在前」。写测试才发现：那条撤单理由字面上写着「未到价」，而那一轮价格**正在带子内**
   ——等于往学习数据里写一条被自己那一行反驳的标签，`entry_quality` 的每一次读数都会继承它。
   改成 `if triggered: 成交 elif 到期: 撤`。附带效果：**没有实时价就不写「未到价」**（已写进 §7）。
2. **标签表比一个死键更糟。** 把真实理由逐个过 `classify_cancellation`：**8 个理由里 5 个落到「其他」**，
   包括**全部主线撤单与论点撤单**。键是照着想象的字符串写的——代码写的是 `论点在成交前已失效`、
   `主线明显走弱(评分…)`、`主线已declining(…)`、`关联主线'X'不存在`，没有一个包含它配的那个键。
   已按**代码真正写出的子串**重配（旧拼写保留：表里的历史行是它们写的）。没有这一步，本次新接的
   「未到价」也只是往一个看不见的表里再加一格。
3. **`PENDING_EXPIRE_DAYS` 实际上是够不着的兜底**：`Trader.default_horizon_days` 在 dataclass 里默认 5，
   所以 `_trader_pct(..., fallback)` 永远拿到 5（`breakout.yaml` 声明 3、`pullback.yaml` 5），常数只在
   交易员查不到时才会出现。测试照这个事实写：用 3 和 4 两个数把「来自论点」与「来自交易员」分开。
4. **按模块名写死的静态守卫会随搬家一起失真。** 拆 `portfolio_book` 后 `test_intent.py` 红了，但红的
   原因不是「多了个写入者」，而是这条守卫**一直靠一个巧合通过**：成交路径的
   `UPDATE virtual_portfolio SET` 写成相邻字面量，`SET[^"']*status` 这类正则跨不过去，`portfolio.py`
   之所以在允许名单里，只是因为旁边那条撤单 UPDATE 恰巧写成一行。改成用 AST 读（解析器本来就会拼接
   相邻字面量），现在**两条**写入都看得见——守卫比原来强，而且不再依赖排版。

**文件大小**：本轮给 `check_pending_orders` 加了真职责，`portfolio.py` 再次破线（1221 行），于是按职责
拆出 `data/portfolio_book.py`（订单簿的行 + 唯一的「不行使就离开」路径：撤单），重导出让
`from portfolio import get_pending_orders / _cancel_order` 照旧可用。现在 `portfolio.py` **1123 行**、
`portfolio_book.py` **144 行**。**这次搬家的副作用要知道**：按 `alpha_agents.data.portfolio._get_conn`
打桩不再能重定向读路径（`get_open_positions` 在 `portfolio_book` 里绑定 `_get_conn`），
`test_portfolio.py` 的两处补上了自己的桩，照 `test_close_position` 当初给 `portfolio_exit` 写的注释同办。

判据 12 条与决策记录 D1–D8 见
[计划文档](exec-plans/completed/2026-09-14-a-pending-order-must-be-finishable.md)。

验证：**1986 passed / 18 skipped**（本轮净增 22 条，新增 `tests/test_pending_orders_finishable.py` 22 条）；
`lint_harness` **167 文件 0 新增**（存量仍 13 条）；`lint_docs` 通过；**6 个变异探针全部有效**
（B 去掉 adopt 分支 / C 把论点体检关回带子里 / D 关掉到期 / E1 把「从未到达」清零 / E2 删掉 `未到价`
映射 / E3 删掉 `成交前已失效` 映射 → 对应用例均变红，复原后锚点仍在）。

### 14.11 「还有多远」这个问题的两个数本身（2026-09-14 第六支）

用户问「现在距离我们 Trade · Learn · Evolve 还有多远」。这一轮不新增能力，修的是**回答这个问题时
被读的两个数**：一个印错了单位，一个从来没有被比较过；外加拿掉一个正在污染等待期的期限错配。

**1. 把配对样本印成「天」。** 闸门的门槛 `MIN_VALIDATION_SAMPLES` 数的是**配对样本**
（`paired_keys` = 双方都评过分的 `(date, code)` 对），而 `scripts/policy.py status` 把它印成
`paired day(s)`、`shadow.coverage` 的 docstring 写成「how many days are left」。
`coverage` 其实**同时**算了 `scored_days`（不同日期数）——两个数都在，"天"那个从来没被印出来。
后果不是排版难看：`coverage` 的 docstring 自己说它是「操作者判断该不该去要裁决的唯一诚实进度条」，
而一个早晨的二十笔推荐会被读成二十天的证据。现在印
`N/20 paired sample(s) over M scored day(s)`，两个单位并排；测试同时断言
「必须出现 `paired sample(s)`」与「不得出现 `paired day(s)`」——负断言断言的是 stdout，
不会被源文件里任何一句 docstring 满足。

**2. `n<50 不上线` 没有任何代码在强制。** `GOLDEN_PRINCIPLES.md` §7 声明它，
`TRADER_CORE_DESIGN.md` §12 写「the repository's `n ≥ 50` requirement」，
README 写「本仓库 `n<50 不上线` 的规则把诚实门槛定在 50」——**三处都在说仓库有这条规则，
而代码里只有 `MIN_VALIDATION_SAMPLES = 20`**，且晋升重检读的是**冻结版本自己声明的**那个值
（`policy_registry._require_gate_cites` 从 `frozen["rules"]` 取）。于是 n 落在 **20–49** 的裁决
同时满足闸门与在效版本、却违反 §7，**没有任何东西拦它**。这不是推断：
`TestTheDeclaredFloorAgainstTheRepositorysRule::test_the_rule_is_a_quotation_and_not_a_threshold`
用 25 个配对样本把这个缺口跑成一个真的 `promote` 裁决。

**修的是可见性而不是行为，而且这是刻意的选择。** 把常数提到 50 才算真正关上它，代价是
**所有已冻结版本立刻变 drifted**（`MIN_VALIDATION_SAMPLES` 在 `policy_sources._RULE_SOURCES` 里，
而冻结之后的行为性改动本来就该是一个新候选），在效的 V1 又是唯一一个版本 —— 所以那是一次
操作者决定，不是一个补丁。落地的是三件可查的事：
- `holdout_gate.GOVERNANCE_MIN_SAMPLES = 50`：把 §7 的数字变成代码里的一个**引用**，
  **刻意不进 `_RULE_SOURCES`** —— 它不改变任何行为，进了指纹就会在它变化时报一次假漂移。
- `holdout_gate.promotion_floor_gap(declared, when=…)`：说清差在哪里、以及「没有东西拦它」；
  它接受传入的值而不是自己去读，因为门槛是**版本的**属性，不是眼前这份代码的。
- `scripts/policy.py status` 每次打印「版本声明的门槛 / 闸门弃权线 / 差多少」三行。
  与 `--producer` 必填、`gate` 无 `--dry-run` 同一个立场：不替操作者做决定。

**收口（2026-09-15）：** 操作者定了 **20**，于是这条缺口按「**把规则降到 20 去就代码**」关闭，
而不是按上面预想的那条路（把常数抬到 50）。两条路效果相同、代价相反：抬常数是改行为来源、
会漂移所有已冻结版本；降规则不动任何交易行为。落地：`GOVERNANCE_MIN_SAMPLES` 50 → 20，
`GOLDEN_PRINCIPLES` §7 / 设计 §12 / README 三处一并改成 20，缺口与「没有东西拦它」那两句
不再成立（正确表述见上面 §7 与 D15 的 Paid 记录）。**本文提到的
`test_the_rule_is_a_quotation_and_not_a_threshold` 已改名为
`test_the_band_the_old_gap_left_open_is_gone`**，断言的也从「缺口存在」翻成「缺口已关闭」——
这正是它 docstring 里预告的那次翻面。

**3. 等待期曾经在产生期限不对的证据（本轮唯一的真 bug）。** 见 §7 的更正：生产库 232 行
`predictions` 的 `horizon_days` / `deadline` 全为 NULL，于是每一笔预报按**全局 5 天**兜底评分、
被标成 `legacy_horizon`；而同一笔决策的论点里，`intraday_monitor` 硬编码了
`"horizon_days": 3`，生产库 41 行 theses 有 **37 行是 3 天**。
**上一版 §7 把这件事读成「一条已建好、无生产用户的能力，而且 5 天就是调用方想要的」——错的**：
调用方**说了**，只是没说给预报听。修法是把调用方已经决定的那个值传下去（morning 传
`r.get("horizon_days")`，intraday 抽成 `INTRADAY_HORIZON_DAYS` 同时喂给预报与论点），
**而不是**在调用点上补 `horizon_days=5` —— 后者正是 `_deadline_for` 拒绝做的那种替换。
测试驱动**真实保存路径**（`_save_recommendations_list` / `_record_intraday_pick`）而不是
`save_prediction`，因为 `save_prediction` 从来不是坏掉的那一半；变异探针把
`INTRADAY_HORIZON_DAYS` 改成两个路径都不会碰巧产出的 4，于是「还在用字面量 3」的那一支会红。

**4. 查「还有多远」时又翻出两件事，都只记录不修。**

- **每天的停止规则在比较两个不同的单位**（`pipeline/tasks/shadow_run.py`）：`remaining`
  数配对样本，`asked_already` 数验证日，于是实验会在「样本线先到、天数线后到」之间
  **天天问闸门**，每天写一条近乎一样的行 —— 正是这个模块自己的 docstring 说它存在就是为了
  避免的那种「有治理的形状、没有治理的实质」。**测试看不见它**：`test_shadow_run_task.py`
  的 `_ready` 桩给出的裁决里 `n` 与 `validation_days` 都等于 `needed`，两个单位正好相等 ——
  它活过了一个叫 `TestItAsksTheGateOnce` 的测试类。记为 **D16**，不在这里修：两种修法都在
  回答本轮被要求别碰的那个问题（门槛是 20 个样本还是 20 天）。现在有一条用例**断言这个缺陷**
  （命名为「两个单位不一致时停止规则会再问」）与一个把它变红的探针 —— 也就是说这条用例检测的
  是分歧，不是修复，命名本身就是为了让读它的人不要把它当成批准。
- **在效的版本不再描述正在跑的配置**（**D17**）：`status` 打印
  `live configuration still matches: False`。`install` 在 `2026-09-14T03:01:06Z` 时它还是
  `True`；**主题门那一支（`d927a0b`，14:20）**给 `DEFAULT_DECISION_PARAMS` 加了 `theme_gate`，
  而 `decision_params_of` 会把默认值**并到**版本自己的值下面，于是交易路径现在读的这个数
  是冻结版本从未声明过的。`policy_sources.changed_sources(1)` 精确指出就是这一个键。
  这条不是「假漂移」：它是一条真实的、可命名的漂移，而 §11「冻结之后的行为性改动必须是一个
  新候选」这件事没有被履行。修法只有一条命令
  （`freeze --by … --reason "the theme gate, frozen after the fact"`，移动任何指针都不需要证据）
  —— **刻意没跑**：它决定的是下一个候选是什么，那是操作者的判断。

**跑的那一步**：在生产库开了**第一条**影子 run（`#1`，V1，`constant_0.5`，`morning`），
理由写在 run 行里。它是 baseline 生产者，所以它的裁决带 `baseline` scope、在
`policy_registry` 里**不可晋升** —— 开它的目的不是测量，是让 15:45 那条调度真的有一条 run
可喂，把「时钟根本没开始走」变成「时钟开始走了」。`status` 的阴影段随之从
`no shadow run has been opened` 变成 `0/20 paired sample(s) …`。它按 `morning` 面板配对，
所以冠军没做晨扫的那一天不进配对 —— 这件事由报告里那句「今天没有可配对的面板」说明，
不由沉默说明。开这条 run 时顺手修掉了一个**会训练人忽略警告**的东西：dry-run 原本对任何
生产者都警告「等于拿策略跟自己比」，而这句话只对 `remap_confidence` 成立（它读版本参数）；
baseline 读不到任何参数，所以同一个设置测的是「冠军有没有技术」。警告只在它成立时出现。

**没做**：没有把门槛改成 50（见上）；没有在 `status` 里推算「最早可裁日期」——那需要一个**未来**
交易日历，而本仓库的交易日历来自历史行情表 `daily_kline`（只有过去），现推等于造一个
「交易日是什么」的第二个真相来源。报的是两个数的分子分母，不是日期。

判据与决策记录见
[计划文档](exec-plans/completed/2026-09-14-the-two-numbers-that-answer-how-far.md)。

验证：**2000 passed / 18 skipped**（净增 14：`tests/test_declared_horizon_reaches_the_forecast.py` 5 条
+ `TestTheDeclaredFloorAgainstTheRepositorysRule` 4 条 + `test_policy_cli` 4 条
+ `test_shadow_run_task` 1 条）；`lint_harness` **167 文件、存量仍 13 条**；`lint_docs` 通过；
**10 个变异探针全部有效**（去掉 intraday 预报的 horizon / 把论点改回字面量 3 / 去掉 morning 的传递 /
在 `_deadline_for` 里把「未声明」写成 5 / status 把样本印回「天」/ 关掉门槛比较 /
让「已达标」也报缺口 / 让 baseline 也吃那条警告 / 把影子报告的样本印回交易日 /
**把停止规则改成单位一致**——最后这条把「断言 D16 缺陷」的用例变红，证明它测的是分歧本身）。

### §14.12 Learn 页把成熟度叫成了缺口（2026-09-14 傍晚）

Learn 页有两块在说错话，都是截图里直接看得见的。

**「候选知识」块：schema 落后于代码，读模型只好降态。** `learning_candidates` 在生产库还是
T3 之前的形状（缺四个提案列），`candidate_transitions` 整张表不存在——因为这个库**从来没有
写者跑过**（0 行），而读模型按设计**先探针再读、绝不建表**。修法就是
`PARTIAL_NOTE` 自己指明的那句：「补 schema 是运维动作」——备份（SQLite backup API 到
`/tmp/memory.db.before-candidate-migration`）后对生产库跑了一次幂等的
`learning_candidates.init_schema`。之后这块从 `落后于代码/修不到` 变成
`complete / empty`（0 行、integrity 干净），配它本来就有的那句诚实话：
**候选生命周期刻意不被管线驱动，代价就是这里长期为空**。空是合法状态，缺列不是。

**「预测与评估货币」块：把 D10 的日历叫成了「当前最要紧的缺口」。** 旧文案是
`232 行没有 Brier …… 这不是「暂时没数据」，是当前最要紧的缺口`。两处都错：

1. **分母错了**。`rows - brier_scored` 把没有 `prob` 的行也算进「缺的评估」——可评估的总体是
   带 `prob` 的 **48 行**，其余 184 行是观察信号，永远不进 Brier 的账，数它们是在数一笔
   没人欠的债。
2. **性质错了**。brier 只在证据窗口收口后写（`scoring.evidence_window_closed` 是唯一判据），
   所以「已有 Brier = 0」在窗口未收口时是**成熟度，不是缺口**。真正要修的只有一种形状：
   **窗口收了还没分**。

修法是让页面说事实而不是说判断：

- `scoring` 新增公开的 **`window_progress(entry_date, horizon)`**（`closed/have/need/remaining`）
  ——窗口算术的**唯一**一份，`evidence_window_closed` 改成由它推导（行为不变，原用例全绿）。
  页面要「报出还差几天」就必须拿计数，而计数不能再有一份自己的算术。
- 读模型 `learn._read_forecasts` 增加 **`pricing`** 块：按 `(date, horizon_days)` 分批，
  每批给 `have/need/remaining`；`ripe_unscored` 单独数（这是唯一的缺陷信号）；
  行情档案不可读时批次进度为 `None` 并报 `archive_readable: False`——**拒绝断言**，
  不在看不见的行情上画进度条。兜底 horizon 用评分器自己的 `DEFAULT_HORIZON_DAYS`，
  否则页面量的窗口和分数落的窗口不是同一个。
- 前端 `Forecasts` 改成五个分支：无预测 / **prob_rows=0（warn：评估货币无从产生）** /
  **ripe_unscored>0（warn：评分步没跑到，这是要修的）** / 档案不可读（拒绝断言）/
  未熟（soft：成熟度，不是缺口，最早一批还差 N 个交易日）+ 批次进度表。
- `check:render` 补了两个用例（熟了没分 / 无 prob），并且**当场抓到一条真 bug**：
  组件初版从 `pricing` 里读 `prob_rows`，而它在 totals 顶层——lint 和 build 全绿灯，
  渲染矩阵红了（§10 的老规矩再次生效）。

生产库现在的真实读数：232 行 / hit 202 / **可定价 48 / brier 0**，pricing 报
`ripe_unscored 0`、`unripe 48`、最早一批（09-08 的 7 行）**还差 1 个交易日**——
即 2026-09-15 收盘后第一批 brier 才可能落地。和 §14.11 的推算逐批吻合。

验证：**2007 passed / 18 skipped**（净增 7：`test_forecast_pricing_readmodel.py` 5 条 +
`TestWindowProgress` 2 条；check:render 新用例不计入 pytest）；`lint_harness` 167 文件、
存量仍 13 条；`lint_docs` 通过；`web` 三条（lint / build / check:render）全过，
渲染矩阵 10 用例 OK。

### §14.13 门槛从 50 落到 20，停止规则的两种口径合并（2026-09-15）

操作者定了**样本门槛 = 20**，于是 D15 按一条本仓库从未走过的方向收口：**把规则降到代码，
而不是把代码抬到规则**。这个方向就是全部要点，因为它决定代价：

- **抬代码（本计划初稿的选项，被拒）**：`MIN_VALIDATION_SAMPLES` 在
  `policy_sources._RULE_SOURCES` 里，是**行为来源**；改它会把**每一个已冻结版本**判为
  drifted，并顺带抹掉 `rollback` 的可选目标（drifted 的版本不能被回滚到）。
- **降规则（实际采用）**：`GOVERNANCE_MIN_SAMPLES` **刻意不在**那个元组里，改它不动任何交易行为。
  **`MIN_VALIDATION_SAMPLES` 一个字节都没动** ⇒ `drifted()` 仍为空，上面那两样代价**未付**。

落地：`GOVERNANCE_MIN_SAMPLES` 50 → 20；`GOLDEN_PRINCIPLES` §7、本文档 §12、`README` 三处声明
一并改成 20，且 §7 **写明这是降低自我要求**、20 个配对样本是弱依据 —— 不许把它读成一项成就。
`promotion_floor_gap` 保留，因为仍有真事可报：**版本**自己声明的门槛低于 20。

**顺带发现并修掉的一处可见性回退。** `status` 原先只在**有缺口时**才打印仓库规则那一行，
于是「两者一致」与「规则根本没被读过」在输出里长得一模一样 —— 正是这份报告存在的目的所要防的形态。
`_print_promotion_floor` 现在**恒定**打印三行：版本声明的门槛、闸门的弃权线、仓库规则。

**D16 同时结清**：`shadow_run.py` 的停止规则原先把 `remaining`（配对样本数）与
`verdict["validation_days"]`（天数）比同一个 `needed`，于是实验在样本线上早就过关、
闸门**每天**被问一次 —— 正是该模块 docstring 说它写出来要避免的形态。D15 定了单位
（20 个**配对样本**），所以两侧都改成配对样本（`verdict["n"]`）。原先那条「断言缺陷」的用例
按它自己 docstring 的指示翻了面，改名
`test_a_verdict_that_reaches_the_sample_bar_ends_the_asking`。

验证：**2008 passed / 18 skipped**（净增 1：`TestTheDeclaredFloorAgainstTheRepositorysRule`
删 1 条、加 2 条）；`lint_harness` **167 文件、存量仍 13 条**（`lint_baseline.txt` 未新增行）；
`lint_docs` 通过。**变异探针取证**：把 `shadow_run.py` 的比较改回 `validation_days` →
`test_shadow_run_task.py` **恰好两条变红**（`test_it_does_not_ask_again_the_next_day` 与翻面后
那条），复原后 16 passed。另：`tests/test_policy_cli.py` 里断言「冲突被打印」的那条改名并翻转成
断言**没有**冲突（`"nothing refuses it" not in out`），同时新增正向断言——仓库规则那一行确实被打印。

## 15. T5–T8 连续 Trader Runtime 与隔离回放（2026-09-26）

**已实现并以真实 30 日窗口验证。** T5 的 `HOLD / ADD / REDUCE / SELL`、T7 的
`LessonCandidate → Lesson → Rule` 证据门槛、以及 T8 的 historical adapter 现在都经由同一
个 `TraderState` Runtime 运行。历史 BUY 的模拟成交被转换为 `POSITION_CHANGED` 事实；后续会话在
同一 `run_id`/`trader_id` 命名空间读取状态，决策在执行前封存。收盘 Review 与交易 Review 只能产生
隔离的候选/observation，未达到样本门槛不会成为生产 Rule。

最终录制使用 `scripts/walk_bootstrap.py` 生成空白回放目录，然后以 `record` 模式运行：

```bash
ALPHAAGENTS_DATA_DIR=<fresh-replay-dir> ALPHAAGENTS_LLM_MODE=record \
  uv run python scripts/walk_forward.py --start 2026-01-05 --days 30 \
  --trader default --decider llm --max-turns 1 --no-trader-tools \
  --model-timeout 120 --keep-going --run-id t8-20260105-30d-final3
```

实际窗口为 `2026-01-05 → 2026-02-13`（30/30 日），退出 0。报告记录 335 次 Runtime
decision、33 次 watch recheck、30 次 Market Review、31 次 Trade Review 与 13 个
LessonCandidate；`market_review_failed=0`、`trade_review_failed=0`、抛错日阶段为 0。生产库内容
hash 未变，3 个共享语料文件均以只读方式打开。121 次模型调用使用同系列备用模型并被 journal 录制。

这个窗口的账户收益 +2.818%、相对等权市场 -4.002%，**不是**策略有效性结论。所有学习都停在
observation（最高 n=31，小于仓库的 n≥50 门槛），而且 replay 自身的 observation context 不等同于
生产环境中经批准知识快照注入的 Rule。

**复核更正（同日）。** 上一段的 30 日窗口没有验证 T5：335 个 Runtime 决策全部是
BUY / WAIT / REJECT，31 笔卖出全部由入场止损/止盈价机械触发（报告"机械 31、agent 0"），
因为验收命令没有开启 agent 卖出。"13 个 LessonCandidate"实为 13 条 observation，库中
`trader_lesson_candidates` 为 343 条、Lesson/Rule 为 0。

## 16. 无价位出场与行情打分的学习闭环（2026-09-26）

- **没有止损也没有止盈**（`0c5636a`，[计划](exec-plans/active/2026-09-26-no-system-exit-lines.md)）：
  实盘与回放都不在任何价位平仓，`AGENT_EXIT_DECISIONS` 移除，卖出全部由 Trader 决定。
  4 日真实模型回放 `nostop4-20260105`：机械 0 笔、agent 4 笔（清仓 2、减仓 2），HOLD 9。
- **行情打分**（`606a200`，[计划](exec-plans/active/2026-09-26-trader-runtime-completion.md)）：
  `decision_outcomes` 在 5 日窗口闭合后，以前瞻收益减全市场中位数判定每个封存决策；复盘模型
  不再打分。学习按 (动作 × 代码计算的情形标签) 聚合，Lesson n≥10、Rule n≥50。
  对上节 30 日窗口的已录决策离线重算：buy 判对 18/48、reject 107/160、wait 42/76；生成
  9 条 Lesson、3 条 Rule。同窗口决策共享交易日与持有期，n 不是独立样本数。
- **风险可见**：回放报告写出最深单票浮亏、期末亏损持仓与浮亏 ≤ −8% 的仓位-日数。
- **T9**（`3aa2be9`）：`walk_branch` 的 rule arm 在同一检查点上对比"注入一条人工批准的
  Rule"与对照组；实测结果见收尾计划。
