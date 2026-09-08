# AlphaAgents

模拟顶级量化分析师工作日的自主投资分析系统。通过多模式 Agent 按交易日节奏运行——晨扫预判、盘中异动追因、收盘复盘、周末总结，持续积累市场认知和投资主线跟踪。

> 股票筛选结果可交给 [TradingAgents-AShare](https://github.com/KylinMountain/TradingAgents-AShare) 做单股深度分析。

## 核心理念

**不是数据管道，而是模拟人类分析师的思维方式：**

- 资金行为优先于新闻叙事 — 先看"谁在买卖"，再看"发生了什么"
- 持续记忆积累经验 — 跟踪投资主线生命周期，回测预测命中率，越用越准
- 不做散户技术分析 — 不看 MACD/KDJ，看机构资金流、北向资金、龙虎榜、相对强弱
- 多模式思维 — 晨扫预判、盘中追因、复盘验证、交叉确认，不同场景不同思路

## 架构

```
┌─────────────────────────────────────────────────────────────────┐
│                      AlphaAgents 2.0                            │
│                                                                 │
│  ┌──────────────┐    ┌────────────────────────────────────┐     │
│  │ 调度器        │    │ 交易日任务                          │     │
│  │ scheduler.py │───▶│ 06:30 晨扫 → 晨报                  │     │
│  │              │    │ 09:30-15:00 盘中监控 → 异动提醒     │     │
│  │ 交易日感知    │    │ 15:30 复盘 → 复盘报告              │     │
│  │ 节假日跳过    │    │ 20:00 夜扫 → 夜报                  │     │
│  └──────────────┘    │ 周末 周报 → 周报                    │     │
│                      └──────────┬─────────────────────────┘     │
│                                 │                               │
│  ┌──────────────────────────────▼──────────────────────────┐    │
│  │  多模式 Agent                                            │    │
│  │                                                          │    │
│  │  晨扫分析师          盘中分析师          复盘分析师        │    │
│  │  (morning.py)       (intraday.py)      (review.py)       │    │
│  │  隔夜外盘+新闻      异动检测→追因       预测验证+主线更新  │    │
│  │  → 预判今日方向      → 判断持续性        → 经验总结        │    │
│  │                                                          │    │
│  │  交叉验证器 (cross_validate.py)                           │    │
│  │  资金面 × 基本面 × 位置面 × 情绪面 → 信心评级             │    │
│  └──────────────────────┬───────────────────────────────────┘    │
│                         │                                       │
│  ┌──────────────────────▼───────────────────────────────────┐    │
│  │  记忆系统 (memory_store.py)                               │    │
│  │                                                          │    │
│  │  主线池              预测记录              市场认知        │    │
│  │  AI算力 (8/10)      300308 看多→+3.2% ✅  半导体: 高位    │    │
│  │  军工 (5/10)        601872 看多→-2.1% ❌  白酒: 低位震荡  │    │
│  │  消费复苏 (3/10)    命中率: 73%           银行: 资金流入   │    │
│  │                                                          │    │
│  │  主线自动发现 → 强度追踪 → 衰退退出（最多8条活跃主线）     │    │
│  └──────────────────────────────────────────────────────────┘    │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │  数据层 (21个股票工具 + 7个期货工具)                       │    │
│  │                                                          │    │
│  │  资金行为 (核心)           行情数据            全球市场    │    │
│  │  ├ 龙虎榜(机构/游资)      ├ 个股实时行情       ├ 美股三大指数│   │
│  │  ├ 大宗交易(折溢价)       ├ 基本面(ROE/负债)  ├ 中美国债收益率│  │
│  │  ├ 北向资金(外资方向)     ├ 市场情绪(涨跌比)  └ 利差信号    │    │
│  │  ├ 融资融券(杠杆方向)     ├ 业绩预告(排雷)                 │    │
│  │  └ 个股资金流(主力vs散户)  └ 涨停/炸板检测                  │    │
│  │                                                          │    │
│  │  板块分析               新闻源(12个)       期货工具        │    │
│  │  ├ 行业板块排名          ├ 东方财富/财联社   ├ 主力合约行情  │    │
│  │  └ 概念板块检索          ├ 华尔街见闻/金十   ├ 库存/基差     │    │
│  │                         ├ 白宫/美联储/SEC   └ CFTC持仓     │    │
│  │  通用: 搜索/抓取         └ 国际RSS(14个)                   │    │
│  └──────────────────────────────────────────────────────────┘    │
│                                                                 │
│  存储: SQLite (记忆+报告) + ChromaDB (语义检索)                  │
│  代理: Cloudflare Worker (海外源反爬)                             │
│  推送: 钉钉 / 企微 / Telegram                                    │
└─────────────────────────────────────────────────────────────────┘
```

### 分析师思维链路

不同于传统"新闻→推荐"管道，系统模拟真人分析师的多种思维模式：

**异动追因（盘中）：** 观察异动 → 追溯原因 → 判断持续性 → 推荐/回避

**预判验证（晨扫→开盘）：** 隔夜外盘+新闻 → 预判方向 → 开盘验证 → 调整

**主线评估（复盘）：** 验证预测 → 更新主线强度 → 总结经验 → 调整策略

**交叉验证（推荐前）：** 资金面+基本面+位置面+情绪面 → 4维通过才推荐

### 投资主线生命周期

系统自动发现和管理投资主线（如"AI算力"、"军工"、"消费复苏"）：

```
信号出现 → [萌芽] → 资金确认 → [活跃] → 持续强化 → [主升] → 资金撤退 → [衰退] → 自动归档
```

- 发现条件：板块连续资金流入 + 跑赢大盘 + 新闻催化 + 龙头涨停（满足2+条）
- 淘汰条件：资金连续流出 + 龙头破位 + 涨停板消失（满足2+条）
- 同时最多跟踪 **8条活跃主线**，每条关联 10-20 只标的

## 快速开始

### 1. 安装

```bash
git clone git@github.com:KylinMountain/AlphaAgents.git
cd AlphaAgents
uv sync
```

### 2. 配置

复制 `.env.example` 为 `.env`，填入API Key：

```bash
cp .env.example .env
```

**最小配置**（只需两个key）：

```env
# 硅基流动（免费，用于embedding和新闻摘要）
SILICONFLOW_API_KEY=sk-xxx

# Agent模型（任何OpenAI兼容的provider）
AGENT_API_KEY=sk-xxx
AGENT_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
AGENT_MODEL=qwen-plus
```

支持的Agent模型Provider：

| Provider | BASE_URL | 推荐模型 |
|----------|----------|---------|
| 阿里DashScope | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` / `qwen-max` |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| 硅基流动 | `https://api.siliconflow.cn/v1` | `Qwen/Qwen2.5-72B-Instruct` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o` |

**可选配置：**

```env
# Cloudflare Worker代理（海外新闻源反爬）
CF_WORKER_URL=https://fetch.yourdomain.com

# 推送通知
NOTIFY_DINGTALK_WEBHOOK=https://oapi.dingtalk.com/robot/send?access_token=xxx
NOTIFY_WECOM_WEBHOOK=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
NOTIFY_TELEGRAM_BOT_TOKEN=123456:ABC-DEF
NOTIFY_TELEGRAM_CHAT_ID=123456789
```

### 3. 构建索引

首次运行需要构建股票概念板块索引（约5分钟）：

```bash
uv run python main.py build-index
```

### 4. 运行

```bash
# AlphaAgents 2.0 — 交易日调度模式（推荐）
# 自动按时间运行：晨扫/盘中监控/复盘/夜扫/周报
uv run python main.py run-v2

# 手动分析特定事件
uv run python main.py run --event "特朗普宣布对中国加征50%关税"

# V1模式 — 持续新闻监控（兼容旧版）
uv run python main.py run

# 启动Web界面
uv run python main.py web
```

## 输出示例

### 晨报（06:30，30秒可读完）

```
=== AlphaAgents 晨报 | 2026-04-08 ===

【隔夜外盘】
• 美股: 标普+0.8%，纳指+1.2% | 原油: WTI 72.3 | 美债10Y: 4.33%

【市场情绪】
• 涨跌比: 10.62 (极度乐观) | 涨停: 87家 | 跌停: 5家

【活跃主线预判】
1. AI算力（强度 8/10）— 博通TPU财报超预期，关注中际旭创、长电科技
2. 军工（强度 5/10）— 中东局势趋缓，主线降温中

【推荐关注】
| 代码 | 名称 | 主线 | 理由 | 信心 |
|------|------|------|------|------|
| 300308 | 中际旭创 | AI算力 | TPU光互联龙头，机构连续买入 | 高 |
| 600584 | 长电科技 | AI算力 | 先进封装需求验证 | 中 |

命中率(近7天): 73% (8/11)
```

### 盘中提醒（仅异动时推送）

```
=== 盘中提醒 | 10:23 ===

【异动】半导体板块突然放量拉升 +2.8%
• 龙头: 中际旭创涨停封板，封单12亿
• 资金: 板块15分钟净流入8.3亿
• 催化: 华为发布昇腾910C芯片
• 判断: 主线加强（AI算力强度 8→9）
• 建议: 关注长电科技、寒武纪（未涨停，可能是介入机会）
```

### 复盘报告（15:30）

```
=== AlphaAgents 复盘 | 2026-04-08 ===

【今日推荐验证】
| 代码 | 名称 | 方向 | 推荐价 | 收盘价 | 涨跌 | 结果 |
|------|------|------|-------|-------|------|------|
| 300308 | 中际旭创 | 看多 | 152.3 | 161.5 | +6.0% | 命中 |
| 600584 | 长电科技 | 看多 | 38.7 | 39.9 | +3.1% | 命中 |
命中率: 2/2 (100%)

【主线状态更新】
• AI算力: 8→9 (龙头涨停+机构买入+板块资金净流入15亿)
• 中东地缘: 3→2 (航运板块续跌，主线即将退出)

【龙虎榜关键信号】
• 中际旭创: 机构买入3.2亿 → 持续性较好
• 招商轮船: 机构净卖出2.1亿 → 印证地缘主线衰退
```

## 项目结构

```
AlphaAgents/
├── main.py                          CLI入口 (run / run-v2 / web / review)
├── alpha_agents/
│   ├── agents/                      Agent层（4个分析师 + 1个验证器）
│   │   ├── morning.py               晨扫分析师Agent
│   │   ├── intraday.py              盘中异动分析师Agent
│   │   ├── review_agent.py          复盘分析师Agent
│   │   ├── cross_validate.py        交叉验证Agent（4维验证）
│   │   ├── strategist.py            V1股票策略师Agent（兼容）
│   │   ├── futures.py               V1期货策略师Agent（兼容）
│   │   └── geopolitical.py          局势分析子Agent（TACO方法论）
│   ├── pipeline/                    管线层
│   │   ├── scheduler.py             交易日调度器（V2核心）
│   │   ├── theme_manager.py         主线生命周期管理
│   │   ├── tasks/                   调度任务
│   │   │   ├── morning_scan.py      06:30 晨扫
│   │   │   ├── intraday_monitor.py  09:30-15:00 盘中监控
│   │   │   ├── review.py            15:30 复盘
│   │   │   └── weekly_report.py     周末 周报
│   │   ├── monitor.py               V1新闻监控循环（兼容）
│   │   ├── digest.py                LLM新闻摘要
│   │   ├── event_linker.py          事件因果关系分析
│   │   └── source_health.py         数据源健康检测
│   ├── sources/                     新闻数据源（12个）
│   ├── tools/                       Agent分析工具（28个）
│   │   ├── registry.py              工具注册 (STOCK_TOOLS/FUTURES_TOOLS)
│   │   ├── fund_flow.py             龙虎榜/大宗交易/北向/融资融券/个股资金流
│   │   ├── global_market.py         美股指数/国债收益率/利差信号
│   │   ├── sector_ranking.py        行业板块资金排名
│   │   ├── anomaly_detect.py        涨停/跌停/炸板检测
│   │   ├── stock_quotes.py          个股实时行情
│   │   ├── financial_data.py        基本面数据(ROE/负债/利润)
│   │   ├── market_breadth.py        市场情绪(涨跌比)
│   │   ├── earnings_calendar.py     业绩预告排雷
│   │   ├── stock_search.py          概念板块检索（语义+关键词）
│   │   ├── sector.py                概念板块资金流
│   │   ├── futures_quotes.py        期货行情/库存/基差/CFTC
│   │   ├── web_search.py            DuckDuckGo搜索
│   │   └── web_fetch.py             网页内容抓取
│   ├── data/                        存储层
│   │   ├── memory_store.py          记忆系统（主线池/预测/市场认知）
│   │   ├── report_store.py          分析报告持久化
│   │   ├── db.py                    SQLite schema
│   │   ├── index_builder.py         股票概念索引构建
│   │   └── embeddings.py            ChromaDB向量搜索
│   ├── prompts/                     Agent提示词
│   │   ├── morning_scan.md          晨扫分析师
│   │   ├── intraday.md              盘中分析师
│   │   ├── review.md                复盘分析师
│   │   ├── cross_validate.md        交叉验证
│   │   ├── weekly_report.md         周报
│   │   ├── strategist.md            V1股票策略师
│   │   ├── futures.md               V1期货策略师
│   │   └── geopolitical.md          局势分析（TACO方法论）
│   ├── notify.py                    推送通知（钉钉/企微/Telegram）
│   ├── config.py                    配置管理
│   └── http_client.py               统一HTTP客户端（CF Worker代理）
├── web/                             React前端
└── docs/superpowers/                设计文档和实施计划
```

## API接口

| 接口 | 说明 |
|------|------|
| `GET /api/snapshot` | 管线当前状态 |
| `GET /api/reports` | 历史分析报告 |
| `GET /api/sources` | 新闻源列表（含健康状态） |
| `GET /api/source-health` | 数据源详细健康指标 |
| `GET /api/event-graph` | 事件因果关系图 |
| `POST /api/trigger` | 手动触发一轮分析 |
| `POST /api/review` | 手动触发每日复盘 |
| `WS /ws` | WebSocket实时事件推送 |

## License

[AGPL-3.0](LICENSE)

选它而不是 Apache-2.0 是因为两者方向相反：Apache 允许任何人拿去改、
闭源商用，而这里要的是**改动必须回流**。AGPL 与 GPL 的区别则在触发
条件——GPL 只在"分发副本"时要求公开源码，把服务架成 SaaS 就绕过了；
AGPL 第 13 条把"通过网络提供服务"也算作触发，对这种自托管看板才有
约束力。专利授权条款两者都有。

实际含义：

- 自己用、内部部署：随意，没有任何义务
- 修改后仅自用：同样没有义务
- **修改后对外提供服务（含内部跨组织、SaaS、嵌入其他产品）**：必须以
  AGPL-3.0 公开你修改后的完整源码，并向使用者提供获取入口
- 引入本项目代码的衍生作品：整体须以 AGPL-3.0 发布

界面页脚有源码链接，这是第 13 条要求的获取入口；自建部署请把它指向
你自己的仓库。
