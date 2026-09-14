<div align="center">

# AlphaAgents

### 会因为亏过钱，而改变下一次判断的 AI 交易员

**Trade · Learn · Evolve**

[![Harness](https://github.com/KylinMountain/AlphaAgents/actions/workflows/harness.yml/badge.svg)](https://github.com/KylinMountain/AlphaAgents/actions/workflows/harness.yml)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)

A 股短线 · 虚拟盘 · 不接券商接口

</div>

---

<img src="docs/images/hero.png" alt="AlphaAgents" width="820">

大多数 AI 交易系统在给出 **Buy / Sell** 的那一刻就结束了。

AlphaAgents 不一样。

它会：

**观察市场 → 写下论点 → 交易 → 判断自己哪里错了 → 形成记忆 → 改变下一次判断**

目标不是做一个更聪明的选股器。

> **而是养出一个会从真实交易中持续进化的 AI 交易员。**

而且顺序不是可选的：

> **可信的交易事实 → 可归因的学习 → 可验证的进化。**
>
> 一个从自己算不清的结果里学习的系统，学到的是自己的 bug。
> 更多的报告、更多的 agent，都替代不了这个顺序。

---

## How it works

![AlphaAgents Trading Loop](docs/images/alpha_agents_loop.png)

核心闭环只有六步：

**市场扫描 → 写下论点 → 挂单 / 成交 → 代码监控 → 复盘学习 → 过闸门才进化**

每一次交易都会留下经验；但经验要过闸门，才会变成下一次判断的依据。

---

## What makes it different?

|                  | AlphaAgents                   |
| ---------------- | ----------------------------- |
| 🔍 **先看资金，再找原因** | 先发现真正发生资金异动的板块，再从快讯中寻找催化      |
| 📝 **持仓的单位是论点**  | 每次买入都必须声明概率、期限，以及「什么发生就说明我错了」 |
| ⚙️ **代码负责盯盘**    | 失效条件每 5 分钟由代码检查，平静时无需反复调用 LLM |
| 🧠 **经验要过闸门才生效** | 校准、盲点、过程质量回注决策；而「教训 → 原则」必须先过前向评估，再由人授权 |

---

## Learn：区分三类结果

AlphaAgents 不只记录盈亏。

它试图区分：

* **判断没错，只是没赌赢**
* **论点确实失效了**
* **时间到了，但行情没发生**
* **被风控平仓，但自己根本没想到这种失败方式**

最后一种叫：

### `blind_spot`

它代表的不是一次亏损，而是：

> **AI 的认知里缺了一块。**

这些结果会逐渐形成：

**交易记录 → 校准 / 盲点 → 教训 → 候选知识 → ？（能不能生效）**

最后那一步是**刻意留空的**，也是这个项目最较真的一步。见下一节。

---

## Learn → Evolve：用数字把关

一个会从自己交易里学习的系统，最容易骗的是自己。

所以这里有一条硬规矩：

> **提交一次改动之前，必须有一个「agent 自己写不出来的数字」在把关。**

不是「模型觉得这条经验有用」，而是留出样本上的 **Brier 分数**与**因子残差** ——
两个都由市场数据算出，模型碰不到。

于是「学到的经验」和「可以生效的知识」被分开：

```text
交易 → 三类结果各自独立存储 → 候选知识（只进不出，没有自动晋升）
                                      │
                                      ▼
                         冻结候选版本 → 前向影子账户
                                      │
                                      ▼
                          独立评估：不退化才提交
                                      │
                                      ▼
                          人授权 → 切换 active policy
                                      │
                                      ▼
                              回滚 / 退休（同样留痕）
```

三件**明确不做**的事，因为它们在别处被证伪过：

- **不让模型给自己的记忆打分。** 模型把自己**错误的**记忆判为正确的概率是 31–54%；
  换一个更强的裁判模型不解决，因为误差仍然相关。
- **不按累计收益判断一次改动。** 年化 Sharpe 1.0 要跑约 **4 年**才能达到 t=2；
  Brier 与残差几百个交易日就有功效。
- **不把历史按比例切分来做验证。** 候选是从最近几天蒸馏出来的，最近那段正是它拟合过的。
  验证只认「候选诞生**之后**」的日子。

> **LLM 负责思考，代码负责守纪律，市场负责打分。**

判断依据与外部证据见 [`docs/self_improvement_roadmap.md`](docs/self_improvement_roadmap.md)
与 [`docs/GOLDEN_PRINCIPLES.md`](docs/GOLDEN_PRINCIPLES.md)。

---

## Thesis, not ticker

AlphaAgents 买入的不是一只股票，而是一条**可以被证伪的论点**。

```json
{
  "claim": "小金属主线持续走强，个股存在补涨机会",
  "prob": 0.60,
  "horizon_days": 5,
  "size_pct": 0.03,
  "invalidations": [
    {"kind": "price_below",             "value": 49.5, "note": "跌破止损位"},
    {"kind": "theme_daily_score_below", "value": 0,    "note": "主线当天转弱"},
    {"kind": "no_progress_by_day",      "value": 5,    "note": "短线逻辑，不动就是错了"}
  ]
}
```

`kind` 只能取自封闭词汇表，所以每一条都能被代码零成本判定。
写表外的名字会被丢弃并告警——**一条读不懂的条件，比没有条件更糟。**

> **LLM 负责思考，代码负责守纪律。**

这不是示意图——下面是真实持仓页上的一条，三个失效条件由服务端从判定引擎
读的同一张表渲染，**页面不可能把一个条件描述成跟触发它的代码不一样**：

<img src="docs/images/thesis-card.png" alt="真实持仓与它的论点" width="760">

<details>
<summary>完整界面</summary>

| 今日总览 | 虚拟持仓 |
|:--:|:--:|
| <img src="docs/images/dashboard.png" width="420"> | <img src="docs/images/portfolio.png" width="420"> |

</details>

---

## 可信与可审计

一个会从自己的交易里学习的系统，最容易骗的是自己。

所以每一条「我学到了」都能被机器复核，而且「学到的经验」与「可以生效的知识」之间有一道闸门：

| 它防的是 | 做法 |
| --- | --- |
| 事后把决策依据改成「我早就看好」 | 建单即冻结 `decision_snapshots`（介入区间 / 止损 / 目标 / 理由 / `information_cutoff`）；`BEFORE UPDATE` / `BEFORE DELETE` 触发器直接拒绝改写，哈希可独立复算 |
| 同股双交易员，结果记到别人头上 | 归属**存成列**（`thesis → order → exits`），不靠「按代码就近匹配」重推 |
| 「预测对了」和「这笔赚了」被压成一个标签 | 三类结果各自独立存储、互不覆盖；平仓永远不会重写预测标签 |
| 把「没交易」当成「打平」，冒充成技能证据 | 未成交报 `fills=0` 且 `return_pct=None`，不是一个 0 收益 |
| 让模型给自己打分 | 记忆 / 原则 / Playbook 的效用只来自市场数据；提示词里不含胜率与战绩 |
| 一次走运被自动写成一条原则 | 未验证经验只进候选区，**没有自动晋升路径**；生效要人显式批准并留下不可变快照 |
| 「我们批准的是哪一版」说不清 | 批准落成不可变快照（谁 / 何时 / 因为什么 / 哪一版）；版本哈希**不含**会自己变的计数器 |
| 修正被原地覆盖，看不出改过什么 | 修正是**追加**一行并指名被替代的那行，旧行始终可读 |
| 「有流程在把关晋升」其实是空转 | Phase 1 **主动断开**了 `evolution/holdout_gate.py` 的每日调用：它拿到的曾是零长度留出窗口，三次运行全部弃权（`gate_decisions` 三行都是 `validation_days: 0`）——**一个永远不会触发的闸门比没有闸门更糟，因为它看起来像治理**。Phase 4 把它改成候选绑定，并让裁决带上**证据等级**（只有候选策略的比较才能晋升）；但它目前**仍没有生产调用者**，构建里也**还没有候选生产者**，所以生产里一次晋升都没发生过 |

> **LLM 负责思考，代码负责守纪律，市场负责打分。**

两处如实说明：

- 已批准知识快照**已接管检索闸门**（Phase 4 的 U5，2026-09-13）：`inject_playbooks` /
  `inject_principles` 现在只渲染**当前生效策略所指向的已批准快照**里的规则行 ——
  一条 `active` 但从未被批准过的行不再进入提示词，规则被改过或快照损坏时**明确阻断**
  而不是静默降级。范围限定：这关掉的是这两条路径，检索权重 / 校准器 / 退役决定
  仍能改变模型看到什么，仍属后续工作。
- 上表最后一行是同一个道理的另一面。这类「承诺超过代码」的地方在本仓库都要求写下来，
  清单在 [`docs/TRADER_CORE_IMPLEMENTATION.md`](docs/TRADER_CORE_IMPLEMENTATION.md) §7。

---

## Current status

进度锚定在 [`docs/TRADER_CORE_DESIGN.md`](docs/TRADER_CORE_DESIGN.md) §14 的五个阶段上 ——
那是唯一定义「阶段」的地方。

| 阶段 | 状态 | 交付了什么 |
| --- | :-: | --- |
| 1 可信的最小切片 | ✅ 2026-09-11 | 现金含已实现损益、逐笔退出记录、显式的交易员/来源绑定、交易结果与预测标签分离、候选知识边界 |
| 2 完整交易内核 | ✅ 2026-09-11 | 订单状态机、资金预留、T+1 批次结算、统一意图路径、内核时钟、对账、重放 |
| 3 可归因的学习 | ✅ 2026-09-12 | 决策 episode、三类结果的生命周期、候选知识的提案与五态生命周期、已批准知识快照 |
| **4 受控演化** | ✅ 2026-09-13 | 冻结策略注册表、前向影子账户、公平评估、授权晋升与回滚、检索闸门；2026-09-13 补第 0 步（`freeze` / `install`）与出厂候选生产者，2026-09-14 在生产库建立指针（V1 在效、行为中性）并把实验接上每日调度。**但一次真实晋升都没发生过**——它要 ≥20 个配对交易日的前向证据，而实验今天才刚开始跑 |
| **5 产品整合** | ✅ 2026-09-13 | 交易 / 学习 / 实验三个读模型贯通 API 与前端。**但「页面能打开」不是「页面有事实」：Learn 与 Evolve 两个页面今天显示的主要是「当前状态说明」，不是数据** |

逐项明细、每条边界、以及每一轮跑了什么命令得到什么结果，在
[`docs/TRADER_CORE_IMPLEMENTATION.md`](docs/TRADER_CORE_IMPLEMENTATION.md)。

### 机制已交付 ≠ 已经在跑

Phase 1–5 的代码都有测试钉住（全量 **1930 passed, 18 skipped**），
但**生产库还是空的**：`position_exits` / `intents` / `decision_snapshots` /
`episodes` / `outcomes` / `learning_candidates` 全是 0 行 —— 这些表落地之后还没跑过生产。
`predictions` 202 行里 `brier` 全为 NULL，所以校准曲线仍然是空的。

**卡点是样本，不是代码** —— 这是这个项目当前最诚实的一句话，而且它有确切日期。
G1 的评分链路（`_score_due_predictions` → `scoring.score_prediction`，做因子残差回归
取残差再算 Brier）**已经在每日 15:30 的 review 里跑**，`prob` 也已经在 47 行上填了
（最早 2026-09-08）；`brier` 为空的原因不是没接，是**还没有一笔预测的窗口走完**。
所以「还没有评估货币」是一句关于日历的话，不是关于代码的话 —— 而**日历要按交易日算**：
到期判定原本用 `date + 5 天`（日历天），而 `_forward_return` 要 6 个**交易日**的收盘，
于是每一笔预测在它「到期」那天都必然拿不到数据。这个错 2026-09-13 修掉了（D10）：
`scoring.evidence_window_closed` 成为唯一的判据，窗口未收口的预报**保持 `pending`
而不是被写成 `censored`**（「我们还没等到」与「我们等了、没等到」是两件事）。
最早那批（09-08）要等 **2026-09-15** 的收盘进库才可评，09-11 那批等 **09-18**。

Phase 4 的另一半也在 2026-09-13 补上了：`scripts/policy.py` 此前四个动词
**没有一个能创建第一行记录**（approve / promote 都要引用一个必须已存在的版本），
现在有了 `freeze` / `install`；`shadow.PRODUCERS` 也第一次有了 `kind="candidate"`
的生产者（`remap_confidence`，用**它绑定版本**的参数重映射冠军记录的信心标签）。
于是「提升真的会改变行为」不再是靠约定：`confidence_to_prob` 读的是**在效版本**里的参数，
所以指针移动的那一刻行为才改变。

2026-09-14 那两步也做了，都是操作决定：**生产库的指针建立了**（V1 在效，
参数与代码默认逐值相同，所以行为中性），实验的两个日常动作
（`shadow-emit` / `shadow-score`）**接上了每日 15:45 的调度**；
`scripts/policy.py` 另有四个手动动词（`shadow-open` / `gate` 等）。
但**一次真实晋升仍然没有发生过，而且今天也跑不了**：晋升要 ≥20 个配对交易日的
前向证据（本仓库 `n<50 不上线` 的规则把诚实门槛定在 50），而那要等实验先跑起来。
**可运行 ≠ 跑过**，而**「可达」不被读成「在跑」**正是这几轮刻意保留的一句话。

**还有一条单向的后果要记住**：`install` 之后概率映射的所有权从磁盘转到指针，
再改 `scoring.DEFAULT_DECISION_PARAMS` 对行为**没有影响**。要改映射就得走完整条证据路径。
`scripts/policy.py status` 会把在效的那组参数按**值**打出来，就是为了让「改了没用」
与「改好了」不是一个样子。

同一形状的第四件：`predictions.horizon_days` / `deadline`（「这笔预测自己声明多久到期」）
机制完整、有测试，但 `morning_scan` 与 `intraday_monitor` 两处生产调用**都没传
`horizon_days`**，所以 202 行全靠全局 5 天兜底并被标成 `legacy_horizon`。
这是**有能力、无生产用户**，不是 bug —— 也**不要顺手在调用点补 `horizon_days=5`**：
那正是 `_deadline_for` 明确拒绝做的事（把「调用方没说」写成「调用方说了五天」）。


### 三个工作台：`unavailable` / `empty` / `present`

Phase 5 的读模型（`alpha_agents/server/readmodels/`）把上面这句话做成了可读的状态，
而不是留给人去比对文档。每个 section 声明它读的**表与列**，读之前先做一次只读探针：

| 状态 | 含义 | 今天的例子（2026-09-14） |
|---|---|---|
| `unavailable` / `schema=absent` | 表在本库不存在，模块从未跑过 | `candidate_transitions` |
| `unavailable` / `schema=partial` | 表在，但缺列 —— **本库 schema 落后于代码** | `gate_decisions` 缺 `evidence_scope` 等四列（第一次写裁决时会自动 `ALTER`）；`learning_candidates` 缺 `evidence_episode_ids` |
| `empty` | 表在、0 行，**这是合法状态** | `episodes`、`outcomes`、`position_exits`、`shadow_runs` |
| `present` | 有行 | `virtual_portfolio`（52 行）、`predictions`（202 行）、`policy_versions`（1 个版本在效） |

**这一列会随运维动作变，所以它带日期。** 09-13 实测 evolve 的 `pointer` / `shadow` 是
`absent`；09-14 执行第 0 步（`freeze` + `install`）之后分别变成 `present` 与 `empty`。
「进化实验台」这个页面因此从 09-14 起有真实内容，而「学习日志」仍然主要是状态说明。

这条设计的要点是**读模型不建表**：每个数据模块的读函数进入时都会 `init_schema`，
所以一个顺手调用的页面会把「打开一次网页」变成一次 schema 变更。
探针不通过就不调用，并把缺的东西**点名报出来** —— 「表没建」和「暂时没数据」
从此不是同一句话。三个端点是 `/api/trade-workspace`、`/api/learn-journal`、
`/api/evolve-lab`；`/api/portfolio` 改为委托同一个投影，不再自己拼一份。

前端三个工作台把上面四种状态渲染成**四种不同的东西**：`absent` 与 `partial` 各有自己的
说明文案（「这些表在这个库里不存在」vs「表在，但这个库的 schema 落后于代码」），
因为两者指向完全不同的修复动作；payload 整个没到是第四态，**不得**打印
`0 有数据 · 0 空 · 0 读不到` —— 那正是健康空页的样子。这条性质由
`cd web && npm run check:render` 机械检查（3 状态 × 3 视图，每个用例都声明
「不得出现什么」）。它不进 CI：`harness.yml` 里没有 node，仓库的前端逻辑测试
（`tests/test_report_markdown.mjs`）同样是本地命令。

**它抓到过什么**（`npm run lint` 与 `npm run build` 对这三件事全是绿灯）：
两个新视图在 payload 为空时崩溃（`sec.value` 少一个 `?`）；`trade.book` 把
「表读不到」渲染成「你没有持仓」；「整个读模型没到」原本与「四节都读不到」同一句话。


---

## Architecture

依赖只朝一个方向走，反向 import 会被 `scripts/lint_harness.py` 在 CI 里拦下来：

```text
data → sources → tools → evolution → pipeline → agents → server
```

| 层 | 负责 | 不负责 |
| --- | --- | --- |
| `data/` | SQLite schema 与读写、评分、决策上下文、交易账本（订单 / 退出 / 论点 / 决策快照）、归属 | 不访问网络 |
| `sources/` | 快讯源，各自归一成同一种形状 | 不做判断 |
| `tools/` | Agent 可调用的行情查询：报价、资金流、涨跌家数、退出信号 | 不持有状态 |
| `evolution/` | 记忆效用、原则、Playbook、前向闸门 | 不靠调 LLM 来决策 |
| `pipeline/` | 调度器与它的任务、快讯摄入循环 | 不含策略规则 |
| `agents/` | LLM 提示词，以及包在它们外面的 runner | 不直接写存储 |
| `server/` | FastAPI、WebSocket、看板，以及三个工作台的读模型（只做投影） | 不重算下层已经算过的数 |

一天是这样跑的：

```text
00:00–23:59  news_ingest       每 5 分钟，全周 —— 让库一直是热的
09:00        morning_scan      回看窗口到昨天收盘
09:25        opening_reminder
09:30–15:00  intraday_monitor  每 5 分钟；有异动才调用 LLM
15:30        review            校验 → 评分 → 学习 → 闸门
20:00        night_scan
周六 10:00    weekly_report
```

摄入是连续的（因为信息源是连续的），消费是开窗的（因为任务是开窗的），两者在 `news_items` 相遇。

主要目录：

```text
alpha_agents/
├── data/         # 交易账本、归属、决策快照、SQLite schema
├── sources/      # 快讯源，各自归一
├── tools/        # Agent 可调用的市场查询
├── evolution/    # 校准 / 盲点 / 教训 / Playbook / 原则 / 前向闸门
├── pipeline/     # 调度器与各任务
├── agents/       # 提示词 runner
├── prompts/      # 提示词模板（.md）
└── server/       # FastAPI + 看板
web/              # React + Vite 前端
```

更完整的推导见：

- [`docs/README.md`](docs/README.md) —— **哪份文档对哪个问题有权威**，先看这个
- [`docs/TRADER_CORE_DESIGN.md`](docs/TRADER_CORE_DESIGN.md) —— 目标态与五个阶段的定义（规范，不是进度表）
- [`docs/TRADER_CORE_IMPLEMENTATION.md`](docs/TRADER_CORE_IMPLEMENTATION.md) —— **代码里已经存在什么**，以及什么还没做
- [`ARCHITECTURE.md`](ARCHITECTURE.md) —— 分层、数据库地图、一天的调度
- [`docs/thesis_design.md`](docs/thesis_design.md) —— 为什么持仓的单位是论点

---

<details>
<summary><b>Quick Start</b></summary>

### 1. 安装

```bash
git clone git@github.com:KylinMountain/AlphaAgents.git
cd AlphaAgents

uv sync
cp .env.example .env
```

### 2. 配置模型

```env
SILICONFLOW_API_KEY=sk-xxx

AGENT_API_KEY=sk-xxx
AGENT_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
AGENT_MODEL=qwen-plus
```

兼容 OpenAI API 风格的模型服务。

### 3. 构建新闻索引

```bash
uv run python main.py build-index
```

然后启动系统即可。

</details>

---

## Philosophy

传统量化在优化规则。

LLM Agent 在生成判断。

AlphaAgents 想做的是第三种东西：

> **让 Agent 的判断经过市场验证，再把经验变成下一次判断的一部分。**

不是：

```text
Model → Recommendation
```

而是：

```text
Trade → Learn → Evolve
```

三个词各自有一个不能退让的条件：

| | 条件 |
| --- | --- |
| **Trade** | 成交与账本是唯一事实来源；报告是可以重建的读模型，永远不是账务输入 |
| **Learn** | 三类结果分开存储，互不覆盖；未验证的经验只进候选区，没有自动晋升 |
| **Evolve** | 换掉一条冻结的策略，必须经过独立的前向评估与人的授权；证据不足就保留现任 |

> **证据不足时，保留现任。** 这不是保守，这是这个项目对「进化」的定义。

---

## Disclaimer

本项目仅用于研究与虚拟盘实验，不构成任何投资建议。

所有判断均可能出错。

---

## License

AGPL-3.0

详见 [LICENSE](LICENSE)。

---

<div align="center">

### Trade · Learn · Evolve

**让每一次交易，都成为下一次判断的一部分。**

</div>
