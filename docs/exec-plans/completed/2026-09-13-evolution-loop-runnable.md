# 演化闭环可以跑起来：第 0 步的 CLI，与「指针决定参数」的第 6 个来源

状态：**已交付（2026-09-13）**。负责人：本次开发会话。创建：2026-09-13。

> 范围：让 Phase 4 的受控闭环**在机制上可运行**——有一个真实的候选生产者，
> 有一个能把配置写进注册表的审计入口，并且让**提升真的改变行为**。
> 本切片不产生前向证据，也不做任何提升；产生证据需要的天数是市场的，不是代码的。

**收口要点**：判据 1–9 全部落地。两处被刻意钉住的红线**如期变红并按设计者的要求
连同文档一起重审**（`test_gate_candidate_bound` 与 `test_readmodels` 各一条）。
六个变异探针全部被捕获（§14.3）。**边界照旧**：生产库的
`policy_versions` / `active_policy` / `shadow_runs` 三张表仍然不存在，
一次真实 freeze / install / promote 都没跑过 —— 交付的是**能跑**，不是**跑过**。
`check:render` 的 evolve 用例仍只用 `reachable: false` 的合成 payload，
所以翻成 `true` 的那条渲染分支目前没有渲染用例（记入 tech-debt D11）。
逐项状态以 `docs/TRADER_CORE_IMPLEMENTATION.md` §12 的边界清单与 §14.2–14.5 为准。

## 目标

Phase 4 交付了机制，但闭环从来没跑过。三条缺口，都在代码里可指认：

1. **没有候选生产者。** `shadow.PRODUCERS` 只登记恒 0.5 的基线，
   于是每份裁决都是 `baseline_only`，而晋升只接受 `candidate_policy`。
2. **`freeze` / `install` 没有命令入口。** `scripts/policy.py` 只有
   status / approve / promote / rollback。生产库的
   `policy_versions` / `active_policy` / `shadow_runs` **三张表都不存在**，
   指针从未建立。（`gate_decisions` 存在，但是缺 `evidence_scope` 列的旧 schema。）
3. **提升不会改变行为。** 这一条最隐蔽，也是本切片真正要修的东西。

### 第 3 条的机理

`promote` 要求 `_require_matches_live`：**live 配置必须仍等于目标版本的哈希**。
而冠军的概率来自 `scoring.confidence_to_prob`，它读的是**代码里的常量**。
于是任何能通过 `promote` 的版本，其参数在提升前就已经是磁盘上的参数了——
「提升版本 N」在行为上是一次空操作，指针只是给已经在跑的东西起了个名字。
后果不止于此：候选生产者即使存在，它与冠军也不是两个策略，而是同一个映射的两份记录。

**修法是让参数有唯一的权威来源。** 本切片把概率映射（`confidence_priors` /
`dim_step` / `dim_base`）变成**指针决定**的参数：决策路径读**在效版本**里的值，
没有版本在效时才读代码默认。于是——

- 提升前：冠军跑 V1 的参数，候选生产者也跑 V2 的参数，两者真的不同；
- 提升后：冠军改读 V2 的参数，**行为在指针移动的那一刻改变**。

这与仓库已有的先例同法：`knowledge` 来源早就是指针决定的
（`feedback.in_force_snapshot_id()` 读指针，不读"最新批准的快照"）。
本切片新增第 6 个声明来源 `decision`，用同一套 staging 机制，
使漂移检查不会因为「版本不在效」而误报（见决策日志 D2）。

## 判据（机器可验收）

1. `SOURCE_NAMES` 含 `decision`；`policy_sources.collect()` 产出的键集与它完全一致。
2. 没有版本在效时，`scoring.confidence_to_prob` 的返回值与改动前**逐个相等**
   （改动对当前生产行为是中性的，直到第 0 步真的执行）。
3. `scripts/policy.py freeze --by --reason` 写出一行 `policy_versions`，
   打印版本号与哈希；同一配置再 freeze 一次返回**同一个**版本号（内容幂等）。
4. `scripts/policy.py install --version N --by --reason` 建立指针、写一条
   `install` 转移记录；已有指针时拒绝并说明原因（"moving it is a promotion"）。
5. 存在 kind = `candidate` 的注册生产者，其预测用**该 run 绑定版本**的参数算出，
   与在效版本的参数无关：同一 (date, code) 在 V2 上 emit 的结果 ≠ 在 V1 上 emit 的结果。
6. 该候选生产者**不读冠军的 `prob`**：把冠军行的 `prob` 改成任意值，
   候选的预测不变；只依赖冠军记录的 `confidence` 标签。
   面板与上游信号固定、只变映射——§12 的 module-level experiment。
7. `verify_live` / `drifted` 不会因为 `decision` 来源而误报漂移：
   一个不在效的版本仍然 `verify_live → True`，且 `drifted()` 不列它。
8. `test_the_shipped_registry_holds_no_candidate` 被改写为新事实
   （恰好一个候选），而不是删掉——原用例的 docstring 明确要求
   「加候选生产者要先在这里失败，好让文档和这条拒绝一起被重新审视」。
9. 全量 `pytest` 通过；`lint_harness` / `lint_docs` 通过。

## 决策日志

**D1 —— 为什么不把参数塞进 `rules`，而是新增第 6 个来源。**
塞进 `rules` 会让 `rules` 里一半的值来自磁盘、一半来自指针，
漂移检查的含义就变成**按键**不同——一个来源里两种真相。
`knowledge` 已经证明「指针决定的来源」可以是一个独立来源，
新增一个比改造 `rules` 的语义更小。代价：所有手写 source 字典的测试 fixture
要补一个键（5 个文件、7 处），机械且一次性。

**D2 —— 验证一个版本时，staging 它自己的 `decision`。**
否则 `promote V2` 之后 `V1` 会显示为 drifted、`rollback` 到 V1 会被拒绝——
安全阀被自己的检查堵死。`knowledge` 的既有做法就是 staging
（`scripts/policy.py::_sources` 的注释写明了理由）。
本切片把这个做法收敛成 `policy_sources.collect_for_version(version_id)`，
让四个检查点（`verify_live` / `drifted` / `changed_sources` / CLI）走同一条路，
免得下一个来源又漏一个调用点。

**D3 —— 候选生产者的 `forecast` 收一个上下文，不是两个字符串。**
`Producer.forecast(date, code)` 拿不到「我绑在哪个版本上」，
而「读自己绑定的版本」正是这个实验的全部内容。
改为 `forecast(date, code, ctx)`，`ctx` 携带 `policy_version_id` /
`report_type` / `params` / `signals`。基线忽略它。
`signals` 在**取写锁之前**一次性解析好，避免在事务里再读库。

**D4 —— 候选读 `confidence`，不读 `prob`。**
读 `prob` 会让比较变成同义反复（拿被测的东西去算候选）。
读 `confidence` 是把**上游信号**固定住——这正是 §12 对 module-level 实验的要求
（"hold upstream dependencies fixed"）。
`shadow.py` 原文说"the probability is a constant that does not look at the
champion's"，本切片把这句话改写为准确的那个版本：**面板与信号固定，映射是变量。

**D5 —— 参数块缺键时的分支：合并到默认值，并在下一次 freeze 时补齐。**
`confidence_to_prob` 用 `{**默认, **版本里的}`。理由：这是**行为定义**，
KeyError 会让交易日的决策直接崩；而缺键不会让行为与记录不符，
因为记录里就是那份不全的块，合并在两处（决策与 freeze）是同一条规则。
版本一旦重新 freeze，写下去的就是合并后的完整块。

## 不做的事（本切片不做，别当待办）

- **不实现 `apply(version_id)`**（把版本的参数写回磁盘/提示词文件）。
  在指针决定参数的模型里它不是必需品：`promote` 之后行为已经改变，
  磁盘常量退化为「未安装时的默认值」。这条留待真要做提示词版本化时再谈。
- **不改 `_require_matches_live`**。它对「提示词被手改而没开新版本」的拦截
  仍然有效，且是 `rollback` 安全性的依据。
- **不跑一次真实的 freeze/install/promote**。本切片交付的是**能跑**；
  往生产注册表写第一个版本、开第一条影子 run 是操作决定，
  要由人拿着 CLI 做（且需要先有前向证据才不会写出一个永远无法晋升的实验）。
- 不给候选生产者接 LLM。它必须是非 LLM、确定性、可复算的，否则闸门的
  「不拿模型的自信当基准」就失效了。

## 风险

- **`confidence_to_prob` 从纯函数变成读指针。** 三个生产调用点
  （`morning_scan` / `thesis` / `intraday_monitor`）会因此间接读
  `active_policy`。`conftest` 每个用例重置存储，所以用例之间不串；
  但 `test_scoring.py` 的断言从此依赖「没有版本在效」，
  新增用例要把「指针决定」这件事单独钉住。
- **`shadow.PRODUCERS` 从 1 个变 2 个**，`scope_for` / `open_run` 的拒绝路径
  多了一条真实分支；`test_gate_candidate_bound.py` 的 `candidate` fixture
  用的是**临时注册**，要注意别与出厂候选混淆（分属两个测试类）。
- **本切片的收益不可当场看见。** 它让闭环可运行，但 n≥50 的配对日要等市场给。
  文档里必须同时说「机制可运行」和「尚无前向证据」，不能只说前者。
