# 概率 shadow 缺一道前向门：回填历史能被当成前向证据

状态：active
创建：2026-09-20
来源：20 天小批量验证（`logs/session-20260920/replay-20d.log`）暴露；
与 `selection_shadow` 的既有契约对照后发现。

## 问题

**`shadow.emit_for_date(run, date)` 不检查 `date` 与 run 的 `opened_at` 关系。**
任何早于实验开启日的日期都能被写入，随后 `score_due` 会照常评分——
于是「回填一段已经知道结果的历史」与「积累真实前向样本」在库里长得一模一样。

这正是 §11「验证是前向的」要防的那件事，而且它同时绕过了
`open_run` 里那道「注册时间由写入端控制、调用者不能回填过去时间」的守卫：
守卫挡住了 `opened_at`，却没挡住 `date`。

**实测（`/tmp` 副本，非生产）：**

```
opened run 4  opened_at = 2026-09-21
  date=2026-09-08  panel=5  window_closed=True
     EMITTED 5  for a date BEFORE opened_at -> BACKFILL ACCEPTED
  date=2026-09-16  panel=4  window_closed=False
     EMITTED 4  for a date BEFORE opened_at -> BACKFILL ACCEPTED
--- now score ---
{'graded': 5, 'unscorable': 0, 'deferred': 8}
predictions=9 scored=5
    2026-09-08 000510 brier= 0.1936
    ... 4 more with brier on 2026-09-08
```

开启当天（09-21）就拿到了 5 条 09-08 的**已评分**样本，而这 5 条的
结果在开启时早已确定。

## 对照：selection shadow 是对的

`evolution/selection_shadow.py` 有这道门，而且写在 manifest 里：

```python
"opened_on": opened_on,
"forward_rule": "opportunity_set.day > opened_on",
```

并且 `process()` 的 SQL 真的执行它（`s.day > ?`）。概率 shadow 缺的正是同一条规则。

## 为什么现在做

② 的验收是「`shadow_runs` ≥ 1 且 `shadow_predictions` 有已评分行」。
按当前实现，**这条验收可以用回填满足**——那会给出一个「已积累前向样本」的
假象，比 2026-09-19 那次「因果无效」更糟：那次至少样本是真的前向。
所以先补门，再谈样本。

## 改动

### `alpha_agents/evolution/shadow.py`

1. `emit_for_date` 增加前向校验：`date < run["opened_at"]` 时抛 `ShadowError`，
   消息里写明 run 的开启日与收到的日期。**拒绝而不是跳过**：静默跳过会让
   「这天没样本」与「这天被拒」无法区分，而调用者需要知道自己在做回填。
2. 与 `selection_shadow` 同一条规则的措辞：`forward_rule = "date > opened_at"`，
   在 `manifest_for_run` 的返回里可见，便于审计。

**与 selection shadow 的差异，说清楚**：selection 用 `day > opened_on`，因为它的
run 行只记日期不记盘中时刻，开启当天的机会集先后不明；概率 shadow 的信号来自
当天 09:00 冠军已定稿的标签，15:45 发射是同一个决策日，所以取 `>=`。

**不做的**：不新增 CLI 开关来「允许回填」。一个需要开关才能安全使用的门，
在实际使用中等于没有门。

## 验收（机器可查）

1. `emit_for_date(run, date)` 其中 `date < opened_at` → 抛 `ShadowError`，
   且库中不产生新行；
2. `date == opened_at` → **允许**（这是生产路径：15:45 的任务给当天发射，
   而当天开启的 run `opened_at` 就是当天）。判据是「发射时结果是否已知」，
   不是「日期是否严格靠后」——当天发射的 3 日窗口要到 3 个交易日后才收口；
3. `date > opened_at` → 正常写入（正例，防止把门做成永远拒绝）；
4. `score_due` 在只有被拒日期时 `graded == 0`；
5. `tests/` 全绿；`lint_harness` / `lint_docs` 通过。

## 实施结果（2026-09-20）

门的归属最终放在 `alpha_agents/evolution/experiment_manifest.py`（治理），
而不是 `shadow.py`：后者已经 1198 行、上限 1200，把守卫塞进去会当场触发
file-size 违规。`shadow.emit_for_date` 调用 `experiment_manifest.assert_forward`，
并把 `ManifestError` 翻译成本模块的 `ShadowError`。规则常量 `FORWARD_RULE` 与
`forward_rule` manifest 字段同源。

**顺手修掉的第二处**：`scripts/policy.py shadow-open --at` 是一个**死开关**。
它把 `args.at` 传给 `open_run(opened_at=...)`，而 `open_run` 拒绝一切
调用者提供的 `opened_at`——所以 `--at` 没有任何可达的成功路径，却读起来像
「一种把实验开在过去的方式」。已删除，并加测试钉住「不接受回填」。

**测试**：`tests/test_shadow_runs.py` 新增 `TestAForecastCannotPredateItsExperiment`
（7 条）。先证明红：把 `_assert_forward` 调用换成 `pass` 后，其中 3 条失败
（`DID NOT RAISE ShadowError`）；恢复后 7 条全过。

另有三处既有测试原本依赖「开一条今天的 run、发射几个月前的日期」，它们被
改成用 `_open_at` 把 run 开在那一天（或直接用 `_legacy_emit` 造一条历史行，
因为那些测试要验证的正是**读取端**如何处理一条遗留行——生产库里就有 10 条）。

**验证**：`pytest tests/ -q` → **2976 passed, 18 skipped**；
`lint_harness` 222 文件通过；`lint_docs` 通过。

## 验证跑出的第二处缺陷：容量"已强制"了，报告仍说"尚未强制"

20 天回放跑完后，summary 里同时出现两句互相矛盾的话：

- 「成交量超 ADV20 参与上限的笔数：0（**已计数、尚未强制**）」
- 「capacity is measured from pre-decision ADV20 and enforced as a hard
  share cap on open-time fills」

代码事实：`_settle_entries` 传 `max_shares_by_code=ctx.capacity`，
`_fill_order` 把 `capacity_amount` 并进 `max_amount` 的 `min()`——所以
**已强制**是对的，summary 那句是上一轮改限制条文时漏改的。

**更深的一层**：`capacity_oversize` 用
`shares > cap` 判定，而 `shares` 正是被 `cap` 截断出来的——**它恒为 False**，
是一列读起来像测量、实际是常数的东西。实测：20 天 17 笔成交，全部 False。

改成回答一个可行动的问题：**这次成交是不是被流动性卡住的**。

- `_fill_order` 把其余五个限额（可用资金、单票上限、计划风险、主题额度、
  簇额度）先算成 `other_room`，再与 `capacity_amount` 取小；
  `capacity_bound = capacity_amount < other_room` 随成交返回。
- 一手兜底路径同样重新推导（那里的上限不同）。
- `walk_forward` 把它写进 `capacity_oversize` 列。
- summary 那句改为「已强制」，并指向限制条文。

测试：`test_a_fill_says_when_liquidity_was_what_limited_it`（True）与
`test_a_fill_is_not_called_capacity_bound_when_cash_was_the_limit`（False）
**两个方向都钉住**，所以它不可能再退化成常数。

## 决策记录

- 2026-09-20：由 20 天小批量验证暴露，不是读代码读出来的。该验证同时暴露了
  另一件事——replay 在第 9 天被 provider `ReadTimeout` 打断（`--keep-going`
  才继续），记录在 `replay-20d-crash-evidence.log`；那是运行环境问题，不是
  本计划的缺陷，但它说明 20 天窗口需要一个能容忍单日失败的跑法。
- 2026-09-20：选择「拒绝」而非「跳过」。理由同上：可区分的失败优于静默的缺失。