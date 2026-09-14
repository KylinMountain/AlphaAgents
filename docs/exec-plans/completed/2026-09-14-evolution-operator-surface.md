# 闭环的操作面：把实验也接到那一个审计入口上

状态：**已交付（2026-09-14）**。负责人：本次开发会话。创建：2026-09-14。

> 范围：只加**操作入口**，不改任何决策路径、不改评分、不改闸门判据。
> 交付物是「一个人能按顺序把这套东西跑起来」，不是「跑过一次」。

**收口要点**：判据 1–9 全部落地。`tests/test_policy_cli.py::TestTheExperimentIsDrivable`
13 个用例驱动真 `main(argv)`；渲染矩阵 7 → 8 个用例（D11 偿还）；
两个变异探针被捕获（`Reachability` 的 `open` 钉死、`shadow-emit` 的面板置空）。
全量 **1895 passed, 18 skipped**。**边界照旧**：没有自动调度，生产里一条 run 都没开 ——
`status` 里的「no shadow run has been opened」就是这条边界的证据。
逐项状态以 `docs/TRADER_CORE_IMPLEMENTATION.md` §12 / §14.6 为准。

## 目标

上一支切片（`dd24c67`）补了第 0 步（`freeze` / `install`）与出厂候选，于是
「闭环可运行」这句话第一次成立。但它只对**记录**成立，不对**实验**成立：

- `shadow.open_run` 与 `shadow.emit_for_date` 的调用者**只有测试**；
- `holdout_gate.run_gate`（D7 的标题）同样只有测试调用，`pipeline/` 与 `server/`
  都不引用它；
- 于是「开一条影子 run → 按日喂它冠军的选股 → 评分 → 问闸门要裁决」这条链
  **只能靠写 Python 跑**。一套只有测试调用者的机制，与「没有机制」在操作上是同一件事。

本切片把四个动词接到 `scripts/policy.py` 上，让这条链是一条命令序列：

```
freeze → install → freeze --decision-json → shadow-open
       → shadow-emit（每日）→ shadow-score（每日）→ gate → approve → promote
```

## 判据（机器可验收）

1. `scripts/policy.py` 有 `shadow-open` / `shadow-emit` / `shadow-score` / `gate`
   四个子命令，且都由 `main(argv)` 驱动（`tests/test_policy_cli.py::
   TestTheExperimentIsDrivable`，13 个用例）。
2. `shadow-open` 指向**在效版本**时打印警告（影子会与冠军用同一套参数，等于拿策略
   跟自己比）；`--dry-run` 一个字节都不写。
3. `shadow-emit` 的面板是冠军当天的选股；面板为空时**报出「面板为空」并返回 0**，
   而不是把「什么都没写」伪装成安静的一天。
4. `shadow-score` 分别报出「已评分 / 窗口未收 / 已删失」，且窗口未收的行**不**记为
   `unscorable`（与 `dd24c67` 的 D10 修法同刀）。
5. `gate` **记录**裁决并**不动指针**；冻结日当天提问被拒绝且**不留下裁决形状的行**。
6. `gate` 没有 `--dry-run`：在闸门之外再实现一遍资格判定就是第二个真相来源。
7. `status` 打印每条实验的 `paired/needed` 进度（闸门要的是配对天数，所以这就是
   「现在问值不值」的那个数）。
8. `web/render-check.jsx` 的 evolve 用例覆盖 `reachable` 的两个分支（D11）。
9. 全量 `pytest` / `lint_harness` / `lint_docs` / `npm run check:render` 全绿。

## 决策日志

**D1 —— 为什么加在 `scripts/policy.py`，而不是新开 `scripts/shadow.py`。**
一次受控变更是一条链：建记录 → 做实验 → 拿裁决 → 移动指针。拆成两个脚本会让
「一个工作流有两个入口」，而这两个入口共享同一张表、同一个指针、同一套拒绝规则。
`policy.py` 的定位从「移动在效策略」扩成「受控演化的操作面」，docstring 里按执行顺序
列出九个动词，而不是按字母。

**D2 —— `gate` 故意没有 `--dry-run`。**
能做出来的 dry-run 只有两种：真的调用 `run_gate`（那就写了），或者在 CLI 里
重新实现一遍资格判定（窗口、开启的 run 数、样本量）——后者正是本仓库反复修的
「同一个问题两个答案」。所以不给：先 `status`（看配对进度），再 `gate`。
`shadow-score` 同理没有 dry-run，理由写在它的输出里（它只给已存在的预测补分数、
幂等、不碰记录与账本）。

**D3 —— `--producer` 必填，不做默认值。**
默认基线会让「本来想要候选、拿到手却是基线」的实验看起来正常（裁决会
`baseline_only`，而晋升只接受 `candidate_policy`）；默认候选则把「冠军有没有技能」
这个 §11 的第一个问题跳过。两种默认都在替操作者做判断，所以不默认。
也不在 argparse 里用 `choices`（那是第二份登记表）——由 `shadow.open_run` 拒绝并列出
已登记的名字。

**D4 —— `status` 带上进度表。**
进度表本来可以单开一个 `coverage` 动词，但那个数字的**唯一用途**是回答「现在问闸门值不值」，
而闸门就在同一个脚本里。放在 `status` 里，操作者用一个命令就能决定下一步。

**D5 —— 关于 `status` 的「不改变任何东西」。**
它现在会 `init_schema` 出 shadow 两张表（此前只建 policy 四张）。严格说这不是
「什么都没发生」，所以 docstring 里改成准确的说法：**不动指针、不留记录**；
建表是 `CREATE TABLE IF NOT EXISTS`，与交易路径第一次落 intent 时触发的同一段 DDL。

## 不做的事（本切片不做，别当待办）

- **不跑真实实验。** 开一条影子 run 会开始累积一条不可回退的实验记录，
  且 `install` 只能成功一次。这是操作决定，需要人拿着这个 CLI 做。
- **不加自动调度。** 没有 cron / 任务把 `shadow-emit` 与 `shadow-score` 挂到每个交易日；
  这属于「让它在生产里跑」，与「让它能跑」是两件事，且需要先回答「第一个候选测什么」。
- **不改 `run_gate` 的任何判据**（窗口、样本量、scope）。本切片只把它的调用者从零变成一。
- **不改 D7 的成立条件**：加了 `gate` 动词之后 `run_gate` 仍然**没有生产调用者** ——
  有动词与有人跑是两件事，`status` 里那条「no shadow run has been opened」就是证据。

## 风险

- **四个动词都能写库**，其中 `gate` 会写一条 append-only 的裁决行。它们与
  `promote` 共用一个入口，所以**任何一条都可能在开发者本地 `data/memory.db` 上生效** ——
  与 `approve_knowledge.py` 同型。本切片沿用既有姿势：命令先打印目标库路径，
  危险动作有 `--dry-run`，验证全程把 `MEMORY_DB_PATH` 指向临时目录。
- **`shadow-emit` 的幂等键是 (run, date, code)**：同一天重复喂会**更新**而不是追加，
  这是刻意的（两条行 = 两次预测，配对检验会把同一支股票算两次）。
