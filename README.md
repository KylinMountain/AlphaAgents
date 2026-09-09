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

---

## How it works

![AlphaAgents Trading Loop](docs/images/alpha_agents_loop.png)

核心闭环只有六步：

**市场扫描 → 写下论点 → 挂单 / 成交 → 代码监控 → 复盘学习 → 记忆进化**

每一次交易都会留下经验，并重新进入下一次判断。

---

## What makes it different?

|                  | AlphaAgents                   |
| ---------------- | ----------------------------- |
| 🔍 **先看资金，再找原因** | 先发现真正发生资金异动的板块，再从快讯中寻找催化      |
| 📝 **持仓的单位是论点**  | 每次买入都必须声明概率、期限，以及「什么发生就说明我错了」 |
| ⚙️ **代码负责盯盘**    | 失效条件每 5 分钟由代码检查，平静时无需反复调用 LLM |
| 🧠 **交易会形成记忆**   | 校准、盲点、教训、Playbook 会重新注入下一次决策  |

---

## Memory → Evolution

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

**交易记录 → 校准 / 盲点 → 教训 → Playbook → 原则 → 新策略**

最终重新反馈给下一次决策。

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

## Current status

| 能力                    |  状态 |
| --------------------- | :-: |
| Agent 自动选股            |  ✅  |
| 论点 / 概率 / 期限 / 失效条件   |  ✅  |
| 挂单 / 成交 / 持仓 / 平仓     |  ✅  |
| 盘中自动监控                |  ✅  |
| 资金异动 → 快讯追因           |  ✅  |
| 校准曲线 / 盲点率            |  🧪 |
| Playbook / 教训 / 原则    |  🧪 |
| Champion / Challenger |  🧪 |

> 系统骨架已经完成，但进化能力需要真实交易样本逐渐“养”出来。

---

## Architecture

```text
Market Data
    ↓
Pipeline
    ↓
Agents
    ↓
Thesis + Portfolio
    ↓
Memory Store
    ↓
Evolution
    └────────────→ Agents
```

主要目录：

```text
alpha_agents/
├── agents/       # 晨扫 / 分析 / 复盘
├── pipeline/     # 调度与盘中循环
├── data/         # thesis / portfolio / memory
├── evolution/    # 校准 / 盲点 / playbook / 原则
├── tools/        # 市场分析工具
└── prompts/
```

更完整的设计推导见：

[`docs/thesis_design.md`](docs/thesis_design.md)

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
Trade → Learn → Remember → Evolve
```

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
