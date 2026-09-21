# 删掉 change/turnover 双通道，让 replay 走概念板块那套

状态：active
创建：2026-09-21
来源：owner 指令——「我要的就是从概念板块选股的逻辑，你直接去掉 change
turnover 这个，这不是我写的，AI 写的。replay 也要是概念板块那套东西。」

## 为什么这不是风格问题，是证据链错误

尽调中发现一处**声明与行为矛盾**，比「replay 和实盘不一致」更严重：

```
sector_forward.PRODUCER        = "sector_rank_price_v1"
sector_forward.producer_genes  = ["decision.selection_rank.change_share"]
```

而 `sector_rank_price_v1` 这条路径**从不执行该基因**（实测）：

```
_price_sector_stage (PRODUCER 路径)
   calls selection_policy : False
   calls _build_sector_panel: True
_build_sector_panel
   calls selection_policy : False
```

也就是说：**晋升级的 sector producer 声明它执行 `change_share`，但它读都不读。**
`gene_registry.assert_exact_coverage` 只校验「变化的基因 ⊆ 声明的基因」，
**从不校验声明的基因是否真的被读取**——所以这个矛盾能一路通过所有门禁。

这与之前记录的「生产不读该基因」是两个不同的缺陷：
- 之前：实盘路径不读它（基因是惰性的）；
- 现在：**连声明执行它的 producer 也不读它**（声明是假的）。

## 实盘与 replay 的真实差异（实测）

| | 实盘盘中 | replay |
|---|---|---|
| 触发 | 概念资金流异动（5 情形，实时） | 无 |
| 板块 | 概念涨幅榜 + 异动 + 主线强度，取前 3 | `_sector_cards`：5d 相对收益 + 上涨占比 + 宽度改善，平均排名 |
| 板块内 | `sector_beta` 四因子加权（beta .40 / 位置 .25 / 机构 .20 / 流动性 .15） | `sector_panel._within_sector`：`(change_rank + turnover_rank)/2` |
| 全局 | 按 `score` 取 5 | 无（`dual_rank_v0` 走全市场 change/turnover 交替） |
| 默认 | — | `dual_rank_v0`（`walk_forward.py:4376`） |

**根因（仓库自己的记录）**：`2026-09-19-sector-first-opportunity-selection.md`
开头写着「**根本偏差：回放脚手架变成了默认策略假设。**」，并在 L37 声明
`change_share`「保留为旧策略参数，不把它当作已经验证的 alpha 来源」。
计划记录了偏差，但代码从未按它执行——默认值、基因注册表、晋升 producer
三处都还挂着这条路径。

## 要做的（三件，按依赖排序）

### 1. 摘掉基因与它的注册（可独立完成）

- `gene_registry.SELECTION_RANK_GENES` 移除 `decision.selection_rank.change_share`；
- `scoring.DEFAULT_DECISION_PARAMS` 移除 `selection_rank` 块；
- `variant._SUPPORTED_DELTAS` 的 `t1_change_rank` 两条映射改为拒绝
  （该证据目前无处可去，应明确报错而非静默改别的东西）；
- `variant._apply` 里 `change_share` 的 [0,1] 特判随之删除；
- `sector_forward` 不再声明 `SELECTION_RANK_GENES`（它本来就不执行）。

### 2. 让 replay 走概念板块路径

- `walk_forward` 默认架构从 `dual_rank_v0` 改为 sector 路径；
- 板块内排序改用与实盘同源的逻辑（`sector_beta` 四因子，或至少让
  `sector_panel` 与实盘共用一份实现）；
- 删掉 `_build_panel`（全市场 change/turnover）或降级为显式 legacy 选项。

### 3. 样本源（② 的收口）

- `_build_sector_panel` 不再把 `last_panel_candidate_pool` 清空后丢弃
  `last_sector_candidate_pool`；把它写进 journal 的 `candidate_pool`。

## 阻断（必须先解决，否则第 2 件做不了）

**PIT 成员档案不存在，且无法从现有数据构造。** 实测：

1. `find /tmp /private/tmp data -name '*membership*.json'` → **除 pytest 临时目录外无**；
2. `concept_stocks` 只有 `(concept_id, stock_code)` 两列，**无任何时点列**
   （`has any date/as-of column: False`）；
3. replay 的 sector 路径**强制要求** `--sector-membership`，缺失直接
   `SystemExit`（`walk_forward.py:3321`）。

所以「让 replay 走概念板块」当前**跑不起来**——不是代码没写，是
**输入不存在**。`docs/reviews/2026-09-20-sector-first-partner-review.md` 也确认：
「membership archive、hash、来源和 capability 审计」仍是待补的硬边界。

**这是 owner 决策点**：要么先补 PIT 成员档案（数据工程），要么接受
「用当前成员快照做 replay」并**明确标注为 lookahead**（这会污染任何证据）。

## 实施结果（第 1 件，2026-09-21）

**删除的文件（5 个）**：

路径不再加反引号：它们已不存在，而 `lint_docs` 会把任何反引号包裹的
`alpha_agents/*.py` 当作现役模块引用（`check_freshness`）。本文件是删除记录，
不是导航，所以用普通文本写路径。

- alpha_agents/data/selection_policy.py（178 行）+ tests/test_selection_policy.py
- alpha_agents/evolution/selection_shadow.py（425 行）
- alpha_agents/evolution/selection_gate.py（184 行）
- scripts/selection_shadow.py（90 行）
- `tests/test_selection_shadow.py`、`tests/test_selection_gate.py`

**修改的源文件（6 个）**：

- `gene_registry.py`：`SELECTION_RANK_GENES` 删除，`KNOWN_POLICY_GENES` 同步；
- `scoring.py`：`DEFAULT_DECISION_PARAMS` 移除 `selection_rank` 块；
- `variant.py`：`_SUPPORTED_DELTAS` 置空、删除 `change_share` 的 [0,1] 特判；
- `dream_selection.py`：删除 `panel_policy_counterfactual` + `_policy_panel` + `_adv20`；
- `sector_forward.py`：新增 `SECTOR_FORWARD_GENES = frozenset()`（**空集**）；
- `walk_forward.py`：`selection_policy` 调用替换为自包含的 `_legacy_pool_rows` /
  `_legacy_lane_mix`，journal context 的 `selection_rank` 置 `None`。

**一处关键决定**：`walk_forward._build_panel` 的双通道逻辑**没有删**，而是
内联成 `_legacy_*` 两个函数并标注为「legacy whole-market control, not the
target strategy」。理由：A 臂（`dual_rank_v0`）需要冻结的 incumbent 基线，
而一个会随目标策略漂移的对照组**测不出任何东西**。

**一处设计后果，需要 owner 知晓**：`_SUPPORTED_DELTAS` 置空意味着
**当前没有任何候选能变成变体**——因为 `t1_change_rank` 是唯一的映射。
这是诚实的（没有基因可动），但意味着「证据 → 挑战者」这条链整体停摆，
直到给 sector 路径定义真正的基因。测试用注入合成映射的方式继续覆盖机制本身。

## 验收（已达成）

1. `grep -rn 'change_share' alpha_agents/ scripts/` → 仅剩 5 处**历史注释**，
   无任何代码引用；
2. `grep -rn 'SELECTION_RANK_GENES' alpha_agents/` → 仅剩 1 处历史注释；
3. `'selection_rank' in scoring.DEFAULT_DECISION_PARAMS` → **`False`**；
4. `V._delta_to_change({"field":"t1_change_rank","direction":"down"})` →
   抛 `VariantError`，消息含 `Known: []`；
5. 四个被删模块 `os.path.exists` 全部 `False`；
6. `sector_forward.SECTOR_FORWARD_GENES` → `[]`（空集，诚实）；
7. `pytest tests/ -q` → **2976 passed, 18 skipped**；
   `lint_harness` 219 文件通过；`lint_docs` 通过。

## 决策记录

- 2026-09-21：**确认 owner 判断正确。** `change/turnover` 双通道由 AI 引入
  （Kylin `765cd97`），仓库自己的 RP-05 计划早已把它标为「旧策略参数，
  不当作已验证的 alpha 来源」，但代码从未按该结论执行。
- 2026-09-21：**发现声明与行为矛盾。** 晋升级 producer
  `sector_rank_price_v1` 声明执行 `selection_rank.change_share`，
  实测其路径从不调用 `selection_policy`。`assert_exact_coverage` 只校验
  方向（变化 ⊆ 声明），不校验声明的基因是否被读取。
- 2026-09-21：**第 2 件被数据阻断。** PIT 成员档案不存在，
  `concept_stocks` 无时点列。先做第 1 件（可独立完成、且是纯减法），
  第 2 件等 owner 决定「补档案」还是「接受 lookahead 并标注」。
