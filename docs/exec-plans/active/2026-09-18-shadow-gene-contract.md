# Shadow 的证据必须绑定它评价的基因位点

状态：active
创建：2026-09-18
来源：GPT-5.6 Sol review Commit 1（Shadow Gene ↔ Evaluator Contract），
诊断已被本仓库的生产数据独立证实。

## 问题

**一条 shadow run 指着 policy version N，不等于它的 producer 真的执行了
version N 改变的那组基因。**

生产里的真实案例（已核实）：

- version #1 的 decision 块里**没有** `theme_gate` 子块（早期收集）；
  #2 相对参照引入了完整的 theme_gate 块（w_rel=0.35），#3 在 #2 的基础上
  把 `w_rel 0.35 → 0.40`——这是实验真正想测的位点；
- producer `remap_confidence` 的整条执行路径是
  `scoring.confidence_to_prob(label, params)`，只读
  `decision.confidence_priors`；
- 所以 run #3（version #3）在 2026-09-18 发出的 3 条 forecast 与 run #2
  （version #2）**逐值相同**（002290/300684/301511，全部 0.53）。

这不是巧合，是结构性的：即使窗口收口、样本填满，两臂的 Brier 将完全相等，
配对检验得到的是一个**关于零差异的确定结论**——证据的血缘合法
（行、run、版本、producer 都对），但**因果上无效**：被评价的基因
（`w_rel`）从未被执行。这正是"学习机器评价自己的Bug"的另一种形态，
评审的框架判断（缺证明不是缺功能）在此再次成立：机制全在，
缺的是"证据确实关于它声称的东西"。

**不变式（candidate 级证据，只约束 decision 块）**：

```
PolicyVersion 的 changed decision genes ⊆ Producer 的 observed genes
（observed 基因可按块粒度声明，覆盖其下全部叶子）
```

- producer 执行的只有 `ctx.params`，即 decision 块——prompts/model 等
  指纹是版本事实（drift 检查的职责），不是任何 producer 会重映射的参数；
- changed genes 是**叶子级且带值比较**的：同一路径 0.35→0.40 必须算变化
  （首版实现只比路径集合，被本计划新增的测试当场抓住）；
- 覆盖判定是**前缀感知**的：`decision.confidence_priors` 声明覆盖
  `decision.confidence_priors.high`——精确相等会让任何真实叶子变化都
  "未覆盖"，一个永远无法满足的契约只会被删除。

baseline 豁免：它只产 `baseline_only` 证据，本来就不支持 promotion。

## 改动

### `alpha_agents/evolution/shadow.py`

1. `Producer` 增加 `observed_genes: frozenset[str]`
   （形如 `"decision.confidence_priors"`）。
2. `constant_0.5` → 空集（baseline）；`remap_confidence` →
   `{"decision.confidence_priors"}`。
3. 基因工具：
   - `flatten_sources(sources)`：嵌套 dict 拍平成 `路径 → 值`；
   - `changed_genes(target, base)`：值不相等或单侧存在的叶子集合；
   - `reference_version_for(version_id)`：显式 `parent_id`，没有则回溯
     `policy_transitions` 找**冻结时**的在效版本，再没有则取同 key 最新
     且更早的版本——手动 freeze 的 candidate 没写 parent，这是记录里
     能找到的最诚实的参照。
4. `compatibility_report(version_id, producer_name)`：只读事实
   （changed / decision_changed / observed / uncovered / compatible /
   reference 规则），供 dry-run 与状态页展示；
   `assert_producer_compatible(...)` 在其上 fail-closed。
5. 在 `open_run()`（candidate 级 run 的第一道门）、
   `emit_for_date()`（老 run 每次发射前复检）、`integrity()`
   （事后审计，报告不修复）三处执行。

### `alpha_agents/evolution/holdout_gate.py`

`run_gate()` 在取用 run 前调用同一断言：老的、不兼容的 run 会在
gate 处被 `GateError` 拒绝——陈旧实验不能靠"攒够了样本"变成可提升。

### `main.py`（两处顺手修，来源同评审）

- 更新 15:45 shadow task 的过时注释（"从不问 gate"→ 在预登记样本点
  恰好问一次，approve/promote 仍是人的动作）；
- `--task` 报告落盘映射补 `shadow → shadow_run`、`archive →
  daily_archive`——现在缺键，手动补跑这两个任务会 KeyError。

### 生产库的两个失效 run（run #2、#3）

修复后二者不再合格（`integrity()` 已当场列出，见决策记录）。**用
`shadow-close` 关闭并写明原因**，证据保留可读。version #2 没有引入任何
producer 执行的基因，version #3 的实质变化 `w_rel 0.35→0.40` 也从未被
执行——两个实验都是"血缘合法、因果无效"。之后若要测 `w_rel`，需要
一个真正执行该位点的 producer（或扩展现有 producer 的观察面），那是
另一个决定。

## 验收（机器可查）

1. `remap_confidence` + 只改 `theme_gate.w_rel` 的版本 →
   `open_run` 抛 `ShadowError`（拒绝信息含位点路径）；
2. `remap_confidence` + 改 `confidence_priors.high` 的版本 →
   `open_run` 成功；
3. 手工注入一条不兼容的 legacy run → `emit_for_date` 拒绝、
   `run_gate` 抛 `GateError`、`integrity()` 列出它；
4. parent 缺失时参照回溯可用：`reference_version_for` 能从 transitions
   找到冻结时的在效版本；
5. `tests/` 全绿；`lint_harness` / `lint_docs` 通过。

## 决策记录

- 2026-09-18：本计划插队到 ④（concept 前视消融）之前——④ 是"量化一个
  已知偏差"，本计划是"阻止继续积累无效证据"，且生产库此刻还有两个
  open run 在每天写入。
- 2026-09-18：参照版本优先显式 `parent_id`，回溯次序
  transitions → 最早的更早版本，全部找不到才拒绝——参照是**记录里的
  事实**，不是猜出来的对手。
- 2026-09-18：对两份 GPT-5.6 文档的取舍：Commit 1 全盘采纳（含 main.py
  两处小修）；Dream Agent（Commit 2）是设计稿，登记为后续计划候选，
  本轮不实现——它的 Phase 1 依赖本计划建立的"changed genes"词汇。
- 2026-09-19：契约只约束 **decision 块**。生产首跑把
  `prompts.morning_scan.md` 指纹差异也判成"未覆盖基因"，测试立刻暴露：
  按 producer 的实际读取面（`ctx.params`）收敛范围，但报告仍列出全部
  changed 叶子，让 scoping 可见而非埋没。
- 2026-09-19：覆盖判定改为**前缀感知**。observed 声明在块粒度、changed
  是叶子粒度，精确相等会让真实变化全部未覆盖。
- 2026-09-19：`changed_genes` 比较**值**而非仅路径。首版把
  `w_rel 0.35→0.40` 判成"无变化"——恰好是要防的那个缺陷在守卫自身里
  复活，被新增测试当场抓住。
- 2026-09-19：参照回溯实测：无 install 轨迹时回退"同 key 最新且更早"；
  生产库 #2/#3 的参照分别由 transitions / parent_id 提供。
- 2026-09-19：`integrity()` 在生产库列出 run #2、#3 不兼容
  （theme_gate 叶子全部未覆盖）——修复生效的实测证据；随后以
  `shadow-close` 关闭二者，理由写明"gene contract: producer 不执行该
  version 的 changed decision genes"。
