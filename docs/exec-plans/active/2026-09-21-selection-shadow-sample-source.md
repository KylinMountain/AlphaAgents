# selection shadow 的样本源缺失要能诊断，而不是崩在 sqlite 上

状态：active
创建：2026-09-21
来源：remaining-seven ②。目标里写着「为 ② 补上缺失的样本源」。
**实施前先做了尽调，结论是：不能照字面补，因为生产根本不跑那个架构。**

## 尽调（都是实测）

1. **生产没有 `opportunity_sets` 表。** 全仓库唯一写入点是
   `scripts/walk_forward.py:2035`（回放）；`alpha_agents/pipeline/` 无任何引用。
2. **生产不跑 `dual_rank_v0`。** 生产 `predictions` 里带
   `selection_architecture` 的行数 = **0**；回放的 `opportunity_contexts` 里
   是 `dual_rank_v0`。生产选股走 `get_sector_best_stocks_fn` + 按 `score` 排序
   （`intraday_monitor.py:510`），从不调用 `selection_policy`（生产零引用）。
3. **生产的候选行缺字段。** 实测 `get_sector_best_stocks_fn("天然气")` 返回的行
   `turnover_rate` 全为 `None`、`change_pct` 也是 `None`（只有
   `today_change_pct`）。把它喂进 `selection_policy.candidate_pool_rows` 得到
   5 行、**可用 0 行**，`materialize_codes` 返回 `[]`。

所以「往生产管线里加一个机会集写入者」会产出一批**结构上不可评分**的集合：
`sample_count` 永远停在 0，而表里有行、日志里有「已写入」——
**这正是这个仓库最反对的那种假修复**。

**基因本身是可观测的**（所以不是「没意义」）：在回放的真实池上，
`change_share` 0.5→0.6 在 15 个名字的池上都能改变选中集合。
缺的不是灵敏度，是**生产侧那条池子不存在**。

## 因此这次修的是「诊断」，不是「造数据」

### 1. `process()` 不再把 sqlite 错误漏出去

实测：在无 journal 的部署里 `process()` 抛
`OperationalError: no such table: opportunity_sets`。一个诊断函数崩在
底层错误上，等于把「这个实验没有样本源」报告成「代码坏了」。改成抛
`SelectionShadowError`，消息点名真实情况与前置条件。

### 2. `summary()` 报告样本源是否存在

新增 `sample_source_present` / `sample_source`。一个永远攒不到样本的 run
必须自己说出来，而不是让操作者从 `sample_count=0` 反推。

### 3. 改掉两处**不实**的文档声明

- `dream_selection.panel_policy_counterfactual` 的 docstring 写着
  「the **live** panel, the Dream replay and the policy variant all execute
  `selection_rank.change_share`」——**live 不执行**。改成 replay，并写明
  生产尚未接入。
- `selection_shadow` 模块 docstring 说它「watches future Opportunity Journal
  sets produced under the parent policy」，读起来像样本源在生产里存在。
  写明唯一生产者是回放。

## 验收（机器可查）

1. 无 journal 时 `process()` 抛 `SelectionShadowError`（不是 `OperationalError`），
   消息含 `opportunity_sets`；
2. 无 journal 时 `summary()["sample_source_present"] is False`；
3. 有 journal 时 `sample_source_present is True`；
4. 两处 docstring 不再声称 live 路径执行该基因（grep 可查）；
5. `tests/` 全绿；`lint_harness` / `lint_docs` 通过。

## 实施结果（2026-09-21）

- `selection_shadow.sample_source_present(conn)`：查 `sqlite_master`，
  回答「这个部署有没有机会集日志」。
- `_require_sample_source(conn)`：无日志时抛 `SelectionShadowError`，消息点名
  `opportunity_sets`、唯一生产者、以及前置条件。**放在使用点**（查询之前），
  不是函数开头——上面的不变量是关于 run 自己账本的，无论环境有没有日志都该报。
- `summary()` 新增 `sample_source_present` / `sample_source`。
- 改掉两处不实 docstring：`dream_selection.panel_policy_counterfactual` 的
  「the **live** panel … all execute」改为 replay 并写明生产未接入；
  `selection_shadow` 模块 docstring 写明唯一生产者是回放。

**实测**（`/tmp` 副本，无日志的部署）：

```
sample_source_present: False
summary: sample_source_present=False source=None
process -> SelectionShadowError: No opportunity_sets table in this deployment,
           so this selection shadow run can never accumulate a sample. ...
```

修复前同一场景抛 `OperationalError: no such table: opportunity_sets`。

**新增测试 4 条**（`test_selection_shadow.py`）：源缺失时报告 False、
`process` 抛对类型且消息含关键词、`summary` 带前置条件、源存在时报告 True。

**验证**：`pytest tests/ -q` → **2994 passed, 18 skipped**；
`lint_harness` 222 文件通过；`lint_docs` 通过。

## 决策记录

- 2026-09-21：**不造生产写入者。** 尽调证明生产既不跑该架构、候选行又缺
  基因需要的字段，任何写入者都会产出不可评分的集合。补样本源的前置条件是
  「把 dual-rank 面板真正接进生产选股」——那是一次策略变更，不是一次接线，
  应按 `docs/DREAM_RSI_SELECTION.md` §6 的四条件先做诊断与确定性基线。
- 2026-09-21：**概率 shadow 那条路已经解锁**（本日另两笔提交）：
  跑通 review 评分后，实测 `paired` 从 **0 → 5**，所以 ② 现在缺的只是
  真实交易日推进，不再缺代码。