# Trader Core Implementation: Phase 1–5 实际状态

- 记录日期：2026-09-13（补记第九轮：Phase 4 的 U1–U5）。
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
  它剩下两个边界：
  - **快照不进任何生效路径。** 模块里没有 `apply_snapshot`，没有 prompt、检索权重、
    或任何 `_get_*` 的返回值读它。`approve()` 的唯一调用者是运维脚本
    `scripts/approve_knowledge.py`（即「人的动作」），所以「已批准」现在是
    **可查的事实**，不是**已生效的行为** —— 这正是 §10 要的形状，不是遗漏。
  - **读侧只有运维脚本与测试在读。** `candidate_is_approved` / `entity_is_approved` /
    `drifted` 的消费者是 `scripts/episode_coverage.py` 与测试，没有任何决策路径读它。
    与候选区一样：是**有能力**，不是**有数据**。
- **策略版本注册与前向影子实验**（Phase 4）—— 机制已交付，且**2026-09-13 起闭环可运行**。
  U1–U5 的机制存在（冻结策略注册表、前向影子评估、候选绑定闸门、人工晋升与回滚、检索闸门），
  同日补上了两个此前缺失的部件：`shadow.PRODUCERS` 现在有一条 `kind="candidate"` 的
  生产者（`remap_confidence`，用**它绑定版本**的参数重映射冠军记录的信心标签），
  `scripts/policy.py` 现在有 `freeze` / `install` 两个子命令（此前四个动词没有一个能
  创建第一行记录）。**「可运行」不等于「跑过」**：生产库的
  `policy_versions` / `active_policy` / `shadow_runs` 三张表仍然不存在、一次真实晋升
  也没发生过，因为缺的是样本不是代码（`predictions.brier` 仍 0 行）。
  逐项见 §12 的边界清单与 §14；tech-debt 记为 D6 / D11。
- **产品整合**（Phase 5）—— **2026-09-13 已交付 V1 + V2 + V3，见 §13**。
  三个读模型、三个端点、三个前端工作台都已存在，`/api/portfolio` 已改为委托同一个投影。
  但**「页面能打开」不是「页面有事实」**：三个工作台在生产库上处于**三种不同的状态**
  （`policy_*` / `shadow_*` 表不存在、`gate_decisions` 与 `learning_candidates` 缺列、
  `episodes` / `outcomes` 0 行），这些状态由读模型自己报出来、由页面自己渲染出来，
  不靠人去比对文档。**Learn 与 Evolve 两个页面今天显示的主要是「当前状态说明」，不是数据。**
- **奖励信号（G1）的机制已在跑，缺的是样本不是代码。** `predictions` 表有
  `prob` / `log_score` / `brier` / `excess_return` / `residual_alpha` 五列；
  `review` 任务里的 `_score_due_predictions`（其 docstring 自述 *"This is the G1 signal"*）
  调 `scoring.score_prediction`，后者做因子残差回归取残差再算 Brier —— 这条链
  **已接、已在每日 15:30 的 review 里跑**。生产库实测：`prob` 已填 **47 / 202** 行
  （最早 2026-09-08），而 `brier` / `residual_alpha` 都是 **0 行**，原因不是没接，
  是**还没有一笔预测的窗口走完**。所以 Learn 页面上那句「还没有评估货币」说的是
  **样本还没成熟**，读成「评分没实现」是错的。
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
- **但 `horizon_days` / `deadline` 这两个列，生产里没有任何写入者。**
  这是 G1 之外的另一件事，且是本仓库第四类缺陷（「列存在但从未被写入」，S6 抓到过
  `intents.order_id` 同型）。T2 的「每笔预测自己声明多久到期」机制是完整的：
  `_deadline_for()` 在未声明时**拒绝**替调用方假设（返回 `None` 而不是套用全局默认），
  `save_prediction` 的 INSERT/UPDATE 都写这两列，`get_predictions_due_for_scoring`
  用 `COALESCE(deadline, date(date, '+5 days'))` 兜底并把 `deadline IS NULL` 报成
  `legacy_horizon`，`outcome_labels` 也照着这个区分打标签。**问题在调用方**：
  `morning_scan.py` 与 `intraday_monitor.py` 两处生产调用都传了 `prob` 却**都没传
  `horizon_days`**，所以 202 行的 `deadline` 与 `horizon_days` 全为 NULL，
  **每一笔预测都在用全局 5 天兜底、并被标成 `legacy_horizon`**。
  这不是 bug（5 天就是这两个调用方想要的），是**一条已建好、当前无生产用户在用的能力** ——
  与 Phase 4 的三个「没人调」同形。**不要顺手在调用点上补 `horizon_days=5`**：
  那正是 `_deadline_for` 拒绝做的事（把「调用方没说」写成「调用方说了五天」）。
  要用它，得先有第二个真的用不同期限的交易员。

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
| `.venv/bin/python -m pytest tests/ -q`（**全量，不 ignore**） | **1894 passed, 18 skipped**（168s；本轮 +13 用例） |
| `.venv/bin/python scripts/lint_harness.py` | 通过（157 个文件），存量 47 条待偿还（**未扩充**） |
| `.venv/bin/python scripts/lint_docs.py` | 知识库校验通过 |
| `cd web && npm run check:render` | **8 个用例全过**（新增 evolve 的 `reachable` 分支） |

`tests/test_policy_cli.py` 新增 `TestTheExperimentIsDrivable`（13 个用例），
驱动真 `main(argv)` 跑完 `shadow-open → shadow-emit → shadow-score → gate`
并用 `status` 读进度。**两个变异探针**：把 `Reachability` 的 `open` 钉成 `false`
（渲染用例变红：`缺少「至少有一个候选生产者已登记」`）、
把 `shadow-emit` 的面板换成空列表（面板用例变红）。两处复原后锚点均在。

**一条口径上的自我更正**：`status` 从「不改变任何东西」改成
**「不动指针、不留记录」** —— 它现在会 `init_schema` 出 shadow 两张表
（此前只建 policy 四张），严格说不是「什么都没发生」。建表是
`CREATE TABLE IF NOT EXISTS`，与交易路径第一次落 intent 时触发的同一段 DDL；
但把那句话写准比保留一句好听的强。

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
- **`evolve.candidates` 与 `evolve.gates` 会显示 `partial`。** 这要求运维动作
  （让当前代码的进程连一次库，或显式迁移），不是前端能修的。页面只报状态。
- **本阶段没有任何写入路径被修改。** 三个读模型全部只读，不新增写者。
- **`check:render` 不进 CI。** `harness.yml` 里没有 node 环境，本仓库的前端逻辑测试
  （`tests/test_report_markdown.mjs`）同样是本地命令。给前端单开 CI 是独立的一件事。
- **G1（奖励改成 Brier + 因子残差 alpha）的机制已在跑，见 §7 对应条目。**
  页面上的「还没有评估货币」说的是**样本还没成熟**，不是代码没接；
  而「什么时候算成熟」这件事在 2026-09-13 修过一次（D10，日历天 vs 交易日），见 §14。

## 14. 第 0 步与路线 B（Phase 4 闭环可运行，2026-09-13）

记录日期：2026-09-13（§14.6 于次日补上）。计划见
[成熟度计划](exec-plans/completed/2026-09-13-forecast-maturity-trading-days.md)、
[闭环计划](exec-plans/completed/2026-09-13-evolution-loop-runnable.md) 与
[操作面计划](exec-plans/completed/2026-09-14-evolution-operator-surface.md)。

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

### 14.5 明确没做（本轮的边界）

- **没有跑过任何一次真实 freeze / install / promote。** 生产库的三张表仍然不存在。
  本轮的交付物是**能跑**，不是**跑过**：往生产注册表写第一个版本、开第一条影子 run
  是操作决定，需要人拿着 CLI 做，且必须先 freeze 一个与在效版本不同的版本。
- **影子跑仍然没有生产入口**（无 `open_run` / `emit_for_date` 的 CLI 动词）——
  见 §12 的边界清单。**（本行已被 §14.6 推翻：2026-09-14 补上了四个动词，
  但生产里仍然一条 run 都没开。）**
- **没有实现 `apply(version_id)`**（把版本参数写回磁盘）。在指针决定参数的模型里
  它不是必需品：提升之后行为已经改变，磁盘常量退化为「未安装时的默认值」。
- **`check:render` 的 evolve 用例仍只用 `reachable: false` 的合成 payload**，
  所以翻成 `true` 的那条渲染分支**目前没有渲染用例**。合成 payload 覆盖的是渲染，
  不是构建事实，所以它不会因为本轮改动而红 —— 记下来，别当成已经覆盖了。
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


