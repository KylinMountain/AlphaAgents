# 预报成熟度：日历到期 ≠ 证据窗口收口（D10）

状态：**已交付（2026-09-13）**。负责人：本次开发会话。创建：2026-09-13。

> 范围：只改「一条预报什么时候可以被判为 mature / censored」这一件事。
> 不改 `horizon_days` / `deadline` 的语义，不改评分本身，不改闸门。

**收口要点**：判据 1–7 全部落地；`tests/test_score_due_predictions.py` 是新增的
6 个用例（该函数此前 0 个测试），用**真实**行情表跑真实 `score_prediction`，无桩；
六个变异探针全部被捕获（见 `docs/TRADER_CORE_IMPLEMENTATION.md` §14.1）。
新增 `evidence_window_closed` 的适用面不只在预报评分：`shadow.score_due` 用同一刀
把 `deferred` 与 `unscorable` 分开。**仍成立的外部约束**：`brier` 依然只在窗口
真正收口后才可能出现 —— 09-08 那批等 2026-09-15 收盘，09-11 那批等 09-18。
逐项状态以 `docs/TRADER_CORE_IMPLEMENTATION.md` §7 / §14.1 为准。

## 目标

`predictions.deadline` 是**日历日**（`date + horizon_days`），而
`scoring._forward_return` 要的是 `horizon + 1` 根**交易日** K 线。
两者在任何一个含非交易日的日历窗口里都对不上：6 个连续日历日最多含 5 个交易日。

后果不是「评分晚一天」那么轻。`get_predictions_due_for_scoring` 用日历做过滤器，
把还没到期的行交出去；`_score_due_predictions` 拿不到分数，
于是 `outcome_labels.label_forecast(scored=None)` 把它写成 **`censored`**
（"期限到了，证据没来"）—— 一句关于世界的话，而它担保的事实是「我们还没等到」。

本切片把**时钟到达**与**证据窗口收口**分开：

- 窗口收口 + 无分数 ⇒ `censored`（原样保留，这是 §9 要的诚实）；
- 窗口未收口 + 无分数 ⇒ **保持 `pending`**，不写任何终态标签。

## 起点：生产库实测（只读，2026-09-13）

| 事实 | 实测 |
|---|---|
| `predictions` 总行 / 带 `prob` / 已评分 | 202 / **47** / **0** |
| 带 `prob` 且未评分的日期分布 | 09-08 **7** 行、09-09 **29** 行、09-10 10 行、09-11 1 行 |
| `horizon_days` / `deadline` 非空行 | **0 / 0** ⇒ 全部走 5 日兜底，`legacy_horizon=1` |
| `outcomes` 表 | **存在，0 行** |
| `daily_kline` 最大日期 / 交易日数(≥08-25) | 2026-09-11 / 14 |
| `policy_versions` `/ `active_policy` / `shadow_runs` | **表不存在** |

`daily_kline` 的 09-08 之后只有 4 个交易日（09-08/09/10/11），而 5 日窗要 6 根。
所以：

- 09-08 那批的**日历截止日是今天（09-13）**，证据窗口要到 **09-15** 才收口；
- 按当前代码，今天的日评会写下 **7 条 `censored`**，两天后再逐条 `revised` 回来。

也就是说这个 bug 还没在生产里发生过（`outcomes` 0 行），**但今天就会发生**。
这就是本切片排在今天做的原因。

## 判据（机器可验收）

1. `scoring.evidence_window_closed(entry_date, horizon)` 存在，且返回
   「`daily_kline` 中 `date >= entry_date` 的不同日期数 ≥ `horizon + 1`」。
   它是唯一的判据来源，不接受调用方传入的判断。
2. `outcome_labels.label_forecast(scored=None)` 在窗口未收口时**不改变任何状态**
   （返回 `changed=False`、`deferred=True`），标签停在 `pending`。
3. `label_forecast(scored=None)` 在窗口收口时仍然写 `censored`；
   原 `censored → revised` 的修正路径不变。
4. `review._score_due_predictions` 对本次 due 集合分别报出
   「已评分 / 窗口未收 / 已删失」三个数，日志里三个数都在。
5. `shadow.score_due` 同样分开：窗口未收的记 `deferred`，**不**记 `unscorable`。
   语义是「窗口收口了但拿不到价格」才是 unscorable。
6. `_score_due_predictions` 至少有 1 个用例（此前 **0 个**），
   且至少 1 个用例走真实 `evidence_window_closed` + 真实 `score_prediction`
   （用临时 `daily_kline` 造窗口），不是全桩。
7. 全量 `pytest` 通过；`scripts/lint_harness.py` / `lint_docs.py` 通过。

## 决策日志

**D1 —— 为什么是「保持 pending」而不是「把 deadline 改成交易日」。**
改 deadline 要引入交易日历作为写入期依赖，还会篡改历史行的声明
（`deadline` 是作者的**声明**，不是系统的推断）。而缺的那件事本来就只是一次
判断：窗口收口了没有。所以选项 2：不动 `deadline`，只把「收口没有」这件事问清楚。

**D2 —— 判据放在 `scoring`，不在调用方。**
`label_forecast` 自己问 `scoring.evidence_window_closed`。
把 `window_closed: bool` 当参数传进来，等于让调用方**声明**一条裁决依据 ——
本仓库在 Phase 4 U5 抓过同一形态（`evidence_scope` 曾由标签字符串决定）。
代价是单测要造行情（或桩掉这个谓词），可接受：谓词本身另有真实行情用例。

**D3 —— 行情库缺失 / 无 `daily_kline` 时判「未收口」。**
理由是不对称的：写 `censored` 是对世界下断言（"等过了，证据没来"），
而归档缺失是**我们读世界**的故障，该由 source health 报告，不该伪装成
逐条预报的裁决。缺失分支取「拒绝下断言」那一支（保持 `pending`），
且因为行会被每轮重新选中，它是自愈的。代价是「归档长期缺失」表现为
`deferred` 越堆越高——所以判据 4 要求这个数被报出来，而不是静默。

**D4 —— 为什么必须给 `_score_due_predictions` 补测试。**
它是 D10 的发生地，此前完全没有测试，所以这个 bug 是靠推理发现的，
不是靠红线发现的。测试不出来 = 契约不存在。

## 不做的事（本切片不做，别当待办）

- 不引入交易日历/节假日表。窗口靠 `daily_kline` 自己回答，它已经是权威。
- 不改 `predictions.deadline` 的写入语义（D8 的「声明由谁产出」仍是独立问题）。
- 不为 `_score_due_predictions` 补「评分失败也写标签」的路径（现有
  `except Exception: continue` 不动，属于另一个切片）。

## 风险

- **判据 6 需要一个假行情库**：`scoring._connect()` 走 `scoring.DB_PATH`，
  测试里既没这个文件也没 `daily_kline` 表。用例要临时建库并 patch
  `scoring.DB_PATH`，并在结束时复原。
- **`test_outcomes.py` 有 3 个用例直接断言 `scored=None ⇒ censored`。**
  它们要改成「给定窗口已收口」（桩掉谓词），并新增一个「窗口未收口 ⇒ pending」
  的用例，否则改完是把一个真事实删掉，而不是把它说准。
