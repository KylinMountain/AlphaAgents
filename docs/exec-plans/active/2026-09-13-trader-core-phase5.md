# Trade Learn Evolve：第五阶段 产品整合

状态：**实施中**。负责人：本次开发会话。创建：2026-09-13。

> 范围由设计 §14 界定：Trader / episode / experiment 三个读模型贯通 API 与现有的
> [`web/src/`](../../../web/src/) 前端。完成含义原文是
> **"Trade workspace, Learn journal, and Evolve laboratory expose the same facts."**
> 逐项已实现 / 未实现仍以 `docs/TRADER_CORE_IMPLEMENTATION.md` 为准。

## 目标

Phase 1–4 让四件事依次成立：账算对了（1）、交易路径统一了（2）、学习可归因了（3）、
策略变更被证据管住了（4）。**它们全部停在代码与脚本里。**

现在的问题是**读者**：

- 归因链（`thesis → order → exits`）存在，但只有 `/api/portfolio` 手工拼了一半；
- `episodes` / `outcomes` / `learning_candidates` 三张表有完整的读函数，
  但唯一的消费者是运维脚本 `scripts/episode_coverage.py`；
- 策略注册表、影子 run、闸门裁决只有 `scripts/policy.py status` 一个出口。

于是「这个交易员今天怎么样」这句话，在任何界面上都答不出来。本阶段让三个工作台
**读同一批事实**，并且让「读不到」本身也是可见的事实。

## 起点：先量，再设计

本计划的形状不是从设计文档抄的，是从**生产库**（`data/memory.db`，2026-09-13 实测）
量出来的。这一步决定了三个工作台各自能诚实地显示什么。

| 工作台 | 事实来源 | 生产库实测 | 写入者 |
|---|---|---|---|
| **Trade** | `virtual_portfolio` | **117 行**（cancelled 106 / expired 8 / open 1 / pending 1 / stopped 1） | `pipeline/tasks/*` → `portfolio` 兼容包装 → `submit_intent` |
| | `theses` | **40 行**（active 29 / invalidated 9 / blind_spot 2） | `thesis.py`，由 morning / intraday 任务写入 |
| | `position_exits` | **0 行** | `portfolio_exit._close_position_impl` |
| | `intents` | **0 行** | 四条写入路径唯一入口 `intent.submit_intent` |
| | `reservations` / `settlement_lots` | **各 0 行** | `portfolio_risk` / `portfolio` 结算 |
| **Learn** | `episodes` / `episode_events` | **各 0 行** | 门在 `submit_intent`，钩子在 `portfolio` |
| | `outcomes` | **0 行** | `pipeline/tasks/review.py::_label_outcomes` |
| | `learning_candidates` | **0 行**（`candidate_transitions` 表**不存在**） | `save_candidate` / `advance_candidate`，刻意不被管线调用 |
| | `predictions` | **202 行**；`brier` **202 行全空**，`hit` 已评 170 行 | `pipeline` + 评分任务 |
| **Evolve** | `policy_versions` / `active_policy` / `policy_approvals` | **表不存在** | `policy_registry.init_schema`（首次读写时） |
| | `shadow_runs` / `shadow_predictions` | **表不存在** | `shadow.init_schema`（同上） |
| | `knowledge_snapshots` / `_items` | **表不存在** | `knowledge_snapshots.approve` |
| | `gate_decisions` | **3 行，全部 `abstained`、`n=0`**（09-08 / 09-09 / 09-11）；且**该表没有 `evidence_scope` 列**，是 Phase 4 之前的旧 schema | `holdout_gate.record_gate_decision`，**无生产调用者** |

三条结论，直接决定本阶段的切法：

1. **三个工作台的 schema 状态各不相同，所以「一个空态」是谎。**
   Trade 有数据；Learn 的表齐全但 0 行；Evolve 的表**根本不存在**，
   而 `gate_decisions` 是**存在的旧 schema**（少一列）。
   把三者渲染成同一个「暂无数据」，会把「还没跑过」和「表都没建」和
   「schema 落后于代码」混成一句话 —— 这正是本仓库反复抓的形态。
2. **生产库落后于代码是可测的，且当前没人能看见。** `knowledge_snapshots` 与
   `candidate_transitions` 在当前 `_get_conn()._SCHEMA` 里会建，但生产库里没有：
   说明最后一次打开过这个库的进程跑的是**更早的代码**。`/api/version` 已经用
   「running vs disk」表达过同一件事，读模型要把这个状态在**业务层**再说一次。
3. **读模型一旦「顺手建表」，它就变成了迁移工具。** 每个数据模块的读函数进入时
   都 `init_schema`（`episode_coverage.py` 的第七轮更正记过同一件事）。
   如果新页面直接调它们，**打开一个页面就会改生产 schema**。本阶段必须把
   「读」与「建」分开，否则 Phase 5 的交付物本身是一个副作用不明的写者。

## 设计决定

### D1 读模型不建 schema

`alpha_agents/server/readmodels/` 下的三个模块**只读**：任何 section 在调用
某个数据模块的读函数之前，先在 `sqlite_master` / `PRAGMA table_info` 上做一次
**只读探针**；探针不通过就**不调用**，并把缺失的东西**点名报出来**。

理由与 §14「先建立并对账新投影，再改读者，不得留下长期并存的第二真相来源」
同构：一个会建表的读模型同时是迁移工具，而「谁改了生产 schema」必须只有一个答案。

### D2 三态，且正交

每个 section 报两个字段，互相独立：

| 字段 | 取值 | 含义 |
|---|---|---|
| `schema` | `complete` / `partial` / `absent` | 这个 section 依赖的表与列，在本库里是否齐 |
| `state` | `unavailable` / `empty` / `present` | `schema != complete` → `unavailable`；齐了看行数 |

`partial` 不是理论情形：`gate_decisions` 今天就命中它（表在、`evidence_scope` 列不在）。
每个 section 的 `source` 声明它读的**表与列**，探针按声明检查 —— 于是
「schema 落后于代码」从一句传闻变成一个可渲染、可断言的状态。

### D3 一个事实一个来源

读模型是**投影**，不是重算：它不重新计算收益、不重新推导归属、不合并两套记录。
`trading_principles` 的两条边界（design §14「reports are rebuildable read models,
never accounting inputs」）在这里落成一条可检查的规矩：每个 section 必须声明
`source`，且 `source` 里出现的表名必须是它真的查过的表。

## 切片

| 切片 | 内容 | 交付判据 |
|---|---|---|
| **V1 三个读模型 + API** | `alpha_agents/server/readmodels/{__init__,trade,learn,evolve}.py`；三个只读端点 `/api/trade-workspace`、`/api/learn-journal`、`/api/evolve-lab` | 三个端点在空库上返回三态正确的 payload；**读它们不会新建任何表** |
| **V2 三个前端工作台** | `web/src/views/` 新增 Learn / Evolve 两个视图，`PortfolioView` 接归因链；导航加分组 | 每个视图对 `unavailable` / `empty` / `present` 三态各有一处不同的渲染 |
| **V3 对账与口径** | README / ARCHITECTURE / 实现文档写「读模型读到的是什么」，并把「生产库落后于代码」列为可查事实 | 文档里出现该口径；`lint_docs` 通过 |

本阶段先做 V1。V2 需要浏览器，V1 不需要 —— 先让**事实可读**，再让**界面好看**。

## 非目标

- **不补候选生产者、不驱动 `advance_candidate`、不 `apply_snapshot`。** 三件事都在
  Phase 4 / T3 / T4 被**刻意**划到阶段外（「验证不等于授权」的代价）。读模型只报状态。
- **不改任何写入路径。** 三个读模型全部只读；本阶段不新增写者。
- **不做费用引擎、部分成交模拟器、global 事件日志**（Phase 2 非目标，不因做界面而复活）。
- **不迁移生产库。** 打开页面不建表（D1）；补 schema 是运维动作，要单独授权。
- **不引入前端框架 / 状态库。** 沿用现有 React + `useDashboard` 轮询。

## 验收（机器可检查）

### A. V1 代码交付即可检查

1. **三个读模型名与设计一致**：`readmodels.WORKSPACES == {"trade", "learn", "evolve"}`，
   且每个模块的 `snapshot()` 返回 `{"workspace", "generated_at", "sections"}`。
2. **读模型不建表**（D1，本阶段最关键的一条）：在一个**全空**的库里依次调用三个
   `snapshot()`，前后对比 `sqlite_master` 的表集合，**必须完全相同**。
3. **三态正确**（D2）：空库上 Evolve 的 `pointer` / `shadow` / `knowledge` section 为
   `schema=absent, state=unavailable`；预置 `gate_decisions`（旧 schema、无
   `evidence_scope`）后 `gates` 为 `schema=partial, state=unavailable`；
   补上该列后为 `schema=complete`，行数 > 0 时 `state=present`。
4. **`source` 不撒谎**（D3）：每个 section 的 `source` 里声明的表名，必须在
   `sqlite_master` 里查得到（或由 `absent` 状态显式解释）。
5. **端点形状**：三条路径注册在 `app.routes` 上，且直接调用处理器后
   `json.loads(response.body)` 与对应读模型的 `states` 一致。
   **不用 `TestClient`**：本仓库此前没有用它测过这个 app，而跨线程连接会撞上
   `conftest` 的「连接必须在属主线程关闭」守卫；路由注册 + 处理器返回值两件事
   分开断言，覆盖到的是同一批事实，且不引入线程语义。
6. **生产库形状的真实读数**：对 `data/memory.db` 的**一份副本**跑三个 `snapshot()`，
   `gates` 必须是 `partial`、`policy_*` / `shadow_*` 必须是 `absent`。
   **用副本而不是原库**：`memory_store._get_conn()` 首次连接时会执行自己的 `_SCHEMA`
   （`CREATE TABLE IF NOT EXISTS`），所以「打开这个文件」本身就会补上
   `knowledge_snapshots` 与 `_items`。那是共享连接的行为、不是读模型的决定，
   但它确实是一次对生产文件的写 —— 本阶段没有这项授权。副本给出同样的证据。
7. 不扩充 lint baseline；单文件不超过 1200 行。

### B. 依赖真实前向样本，本阶段只能报状态

8. **不得把 `unavailable` / `empty` 渲染成「正常」**。验收方式与 Phase 4 的 B15 同构：
   文档与本文件必须写明三个工作台当前各自的真实状态，且**不得**出现任何
   「学习日志已上线 / 进化实验台已可用」这类与 `episodes=0`、`policy_versions` 不存在
   相矛盾的说法。

## 实测结果（2026-09-13，V1）

| 检查 | 命令 | 结果 |
|---|---|---|
| 全量回归 | `TMPDIR=… .venv/bin/python -m pytest tests/ -q`（不加 `--ignore`） | **1828 passed, 18 skipped**（115s；本轮 +28 用例） |
| 架构不变量 | `scripts/lint_harness.py` | 通过，**157 个文件**，存量 47 条（未扩充 baseline） |
| 文档新鲜度 | `scripts/lint_docs.py` | 通过 |
| 变异探针 | 5 处，见下 | **5 / 5 被对应用例捕获** |

生产库副本（`cp data/memory.db` → `/tmp`，不碰原库）上的真实读数：

| 工作台 | section | schema | state |
|---|---|---|---|
| trade | `book` | complete | **present（52 行）** |
| trade | `attribution` / `intents` / `settlement` | complete | empty |
| learn | `episodes` / `outcomes` | complete | empty |
| learn | `candidates` | **partial** | unavailable |
| learn | `forecasts` | complete | **present（202 行）** |
| evolve | `pointer` | **absent** | unavailable |
| evolve | `shadow` | **absent** | unavailable |
| evolve | `gates` | **partial** | unavailable |
| evolve | `knowledge` | complete | empty |

- `candidates` 缺的是 `learning_candidates.evidence_episode_ids`（T3 加的四列之一，
  生产库还没迁移）与整张 `candidate_transitions`。
- `gates` 缺的是 `gate_decisions.validation_days` 与 `evidence_scope` —— Phase 4 的两个
  `ALTER TABLE` 尚未在这个库上跑过。
- `evolve.code` 报出：`producers=['constant_0.5']`、`candidate_producers=[]`、
  `reachable=False`。**「这个构建产不出可晋升的裁决」因此是一个能被页面读到的事实，
  而不是一句文档里的自述。**
- **读完之后新增的表：无。** 验收 2 在真实库形状上也成立。
- 一个必须写下来的细节：副本上 `knowledge` 是 `complete / empty`，而生产文件里这两张表
  **不存在**。差别的来源是 `memory_store._get_conn()` 的 `_SCHEMA` —— 它不是读模型做的，
  但它意味着「打开一次库」就会补上这两张表。所以本文件「起点」表里写的
  「`knowledge_snapshots` 表不存在」是对**当前文件**的描述，一旦有当前代码的进程连上，
  它就会变成「表在、0 行」。两个状态都是真的，区别在谁先连过。

### 变异探针

| 探针 | 让什么失效 | 变红的用例 |
|---|---|---|
| P1 | 去掉 `section()` 的 schema 闸门 | `test_the_tables_a_reader_would_have_created_are_still_absent` |
| P2 | `_schema_state` 的 `absent` 退化成 `complete` | `test_absent_tables_report_unavailable_and_name_them` |
| P3 | `_missing()` 不再检查列 | `test_a_missing_column_is_partial_not_absent` |
| P4 | `/api/portfolio` 自己拼一份持仓 | `test_the_route_is_a_delegation_not_a_second_assembly` |
| P5 | `shadow.PRODUCERS` 登记一个候选生产者 | `test_this_build_registers_no_candidate_producer` |

P5 是**刻意的**：它的作用不是防回归，而是当有人补上候选生产者时让本文件的一句
「本构建不可达」变红，逼着改文档而不是留下两份互相矛盾的说法。

## 风险

- **最大的风险是把「有页面」当成「已整合」。** 三个工作台里有两个今天没有任何事实可显示，
  照抄设计文档的字面实现会得到三个**样式漂亮、永远空的页面** —— D7 原样再升一层：
  「一个永远空的页面比没有页面更糟，因为它看起来像产品」。**对策**：先把三态做成
  可断言的状态（D2），且验收 8 与 5 分开写 —— 「端点有形状」不许冒充「有事实」。
- **第二个风险是读模型变成第二个真相来源。** 如果它顺手中文化、重算收益、把
  `outcomes` 与 `predictions.hit` 合并，它就制造了 §14 禁止的并存真相。**对策**：D3，
  每个 section 声明唯一 `source`；重算类需求一律回到下层模块。
- **第三个风险是打开页面就改生产 schema。** 每个数据模块的读函数都会 `init_schema`。
  **对策**：D1 的探针必须先于调用，并由验收 2 机械钉住「调用前后表集合不变」。
- **第四个风险是 `partial` 被当成错误。** `gate_decisions` 缺列是**事实**，
  不是 bug：Phase 4 的 `ALTER TABLE` 会在下一次写入前由代码自己补上。
  读模型只报状态，不修 —— 修它是运维动作。**对策**：把这条写进 section 的 `note`。

## 决策日志

- 2026-09-13：确认 Phase 5 的起点是**读者**，不是**写者**；三个工作台不做任何写入。
- 2026-09-13：确定 `server/readmodels/`（§13 把 projections 划给 `server/` 层），
  不新建层、不新增 lint 豁免。
- 2026-09-13：确定 D1（不建 schema）、D2（三态正交）、D3（一个事实一个来源）。
- 2026-09-13：V1 先于 V2 —— 事实可读先于界面好看。
