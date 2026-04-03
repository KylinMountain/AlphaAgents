# AlphaAgents

新闻驱动的自主分析系统。监控国内外财经新闻和地缘政治事件，自主分析对A股和期货市场的影响，输出推荐关注的个股和期货品种。

> 股票筛选结果可交给 [TradingAgents-AShare](https://github.com/KylinMountain/TradingAgents-AShare) 做单股深度分析。

## 架构

```
┌──────────────────────────────────────────────────────────────┐
│                        AlphaAgents                           │
│                                                              │
│  ┌────────────┐    ┌─────────────────────────────────────┐   │
│  │ 入口层      │    │ 管线层 (pipeline/)                   │   │
│  │ CLI / Web  │───▶│ monitor → digest → route → agents   │   │
│  └────────────┘    └──────────┬──────────┬───────────────┘   │
│                               │          │                   │
│              ┌────────────────▼┐   ┌─────▼──────────────┐    │
│              │ 股票策略师Agent  │   │ 期货策略师Agent     │    │
│              │ (strategist.py) │   │ (futures.py)       │    │
│              │ handoff→局势分析 │   │ 供需/传导链/多空   │    │
│              └────────┬────────┘   └─────┬──────────────┘    │
│                       │                  │                   │
│  ┌────────────────────▼──────────────────▼──────────────┐    │
│  │  数据源层 (sources/)              工具层 (tools/)      │    │
│  │                                                      │    │
│  │  新闻数据源(12个)                 股票工具             │    │
│  │  ├ 东方财富/7x24快讯              ├ 概念板块检索        │    │
│  │  ├ 财联社电报                     ├ 板块资金流向        │    │
│  │  ├ 华尔街见闻                     ├ 个股过滤            │    │
│  │  ├ 金十数据                       └ 自选股管理          │    │
│  │  ├ 新华社/白宫/美联储/SEC                              │    │
│  │  ├ Trump/Musk社交媒体             期货工具             │    │
│  │  ├ Bloomberg/FT/Al Jazeera        ├ 主力合约行情        │    │
│  │  ├ Middle East Eye/Haaretz        ├ 交割仓库库存        │    │
│  │  ├ France24/DW/RT News            ├ 期现基差数据        │    │
│  │  └ 五角大楼披萨指数(pizzint)       └ CFTC持仓报告       │    │
│  │                                                      │    │
│  │  通用工具: DuckDuckGo搜索 / 网页内容抓取              │    │
│  └──────────────────────────────────────────────────────┘    │
│                                                              │
│  存储: SQLite + ChromaDB    健康检测: 自动跳过/恢复不健康源   │
└──────────────────────────────────────────────────────────────┘
```

### 核心流程

```
新闻源(12个并发) → 去重 → LLM摘要(便宜模型) → target_market路由
                                                ├→ stock events  → 股票策略师Agent → 报告+推送
                                                └→ futures events → 期货策略师Agent → 报告+推送
```

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

### 3. 构建索引

首次运行需要构建股票概念板块索引（约5分钟）：

```bash
uv run python main.py build-index
```

这会从baostock拉取全部A股信息，从同花顺拉取概念板块和成分股，存入本地SQLite。

### 4. 运行

```bash
# 手动分析特定事件
uv run python main.py run --event "特朗普宣布对中国加征50%关税"

# 持续新闻监控（自动拉取新闻 → 判断重要性 → 触发分析）
uv run python main.py run

# 启动Web界面
uv run python main.py web
```

## 输出示例

### 股票分析报告

```
═══════════════════════════════════════════
AlphaAgents 分析报告 | 2026-04-03 14:28
═══════════════════════════════════════════

【核心事件】
[地缘/政策] 特朗普宣布对中国加征50%关税 — 重要性 5/5

【A股板块影响】
利好板块:
• 半导体设备（力度 5/5）— 管制升级→国产设备替代加速
• AI芯片设计（力度 4/5）— 英伟达中国份额归零→国产窗口打开

【推荐关注】
| 代码   | 名称     | 关联概念   | 推荐理由                    |
|--------|---------|-----------|----------------------------|
| 002371 | 北方华创 | 半导体设备 | 国产设备平台龙头              |
| 688041 | 海光信息 | AI芯片    | 直接承接英伟达让出市场空间     |
```

### 期货分析报告

```
═══════════════════════════════════════════
AlphaAgents 期货分析报告 | 2026-04-03 14:28
═══════════════════════════════════════════

【品种影响总览】
• 能源: 看多 — OPEC减产叠加地缘风险
• 贵金属: 看多 — 避险需求+实际利率下行
• 有色金属: 看空 — 关税冲击需求预期

【重点品种分析】
品种: 黄金(AU)
方向: 看多 | 力度: 4/5
逻辑链: 关税升级 → 全球衰退预期 → 避险资金涌入 → 金价上行
驱动类型: 情绪驱动
```

## 项目结构

```
AlphaAgents/
├── main.py                          CLI入口
├── alpha_agents/
│   ├── agents/                      Agent层
│   │   ├── strategist.py            股票策略师Agent
│   │   ├── futures.py               期货策略师Agent
│   │   └── geopolitical.py          局势分析子Agent（TACO方法论）
│   ├── pipeline/                    管线层
│   │   ├── monitor.py               新闻监控循环 + 双路由分发
│   │   ├── digest.py                LLM新闻摘要 + target_market标记
│   │   ├── event_linker.py          事件因果关系分析
│   │   ├── daily_review.py          每日复盘
│   │   └── source_health.py         数据源健康检测
│   ├── sources/                     新闻数据源（12个）
│   │   ├── eastmoney.py             东方财富
│   │   ├── eastmoney_live.py        东方财富7x24快讯
│   │   ├── cls_telegraph.py         财联社电报
│   │   ├── wallstreetcn.py          华尔街见闻
│   │   ├── jin10.py                 金十数据
│   │   ├── xinhua.py                新华社
│   │   ├── whitehouse.py            白宫声明
│   │   ├── fed.py                   美联储
│   │   ├── sec.py                   SEC
│   │   ├── pboc.py                  人民银行
│   │   ├── truthsocial.py           Trump/Musk社交媒体（Google News代理）
│   │   ├── world_news.py            14个国际RSS源
│   │   └── pizzint.py               五角大楼披萨指数
│   ├── tools/                       Agent分析工具
│   │   ├── registry.py              工具注册 + 分组（STOCK/FUTURES/NEWS_TOOLS）
│   │   ├── stock_search.py          概念板块→个股检索（语义+关键词）
│   │   ├── sector.py                板块资金流向
│   │   ├── stock_filter.py          ST/停牌/小市值过滤
│   │   ├── watchlist.py             自选股管理
│   │   ├── futures_quotes.py        期货行情/库存/基差/CFTC持仓
│   │   ├── web_search.py            DuckDuckGo搜索
│   │   └── web_fetch.py             网页内容抓取
│   ├── data/                        存储层
│   │   ├── db.py                    SQLite schema
│   │   ├── report_store.py          分析报告持久化
│   │   ├── index_builder.py         baostock+同花顺数据索引
│   │   └── embeddings.py            ChromaDB概念向量搜索
│   ├── prompts/                     Agent提示词
│   │   ├── strategist.md            股票策略师
│   │   ├── futures.md               期货策略师（含品种映射/传导链）
│   │   └── geopolitical.md          局势分析师（TACO方法论）
│   ├── notify.py                    推送通知（钉钉/企微/Telegram）
│   ├── config.py                    配置管理
│   ├── http_client.py               统一HTTP客户端（重试/代理）
│   └── web/                         Web界面
│       ├── app.py                   FastAPI后端 + WebSocket
│       └── events.py                实时事件总线
├── web/                             React前端（可视化监控面板）
├── cloudflare/                      CF Worker代理（海外RSS加速）
├── config/
│   └── watchlist.json               自选股配置
└── tests/                           145+单元测试
```

## 核心设计

### 双路由分析

新闻摘要阶段为每条事件标记 `target_market`（stock/futures/both），然后分发给对应Agent：

- **股票策略师**：搜索关联概念板块 → 识别利好/利空 → 筛选个股
- **期货策略师**：事件→品种映射 → 产业链传导 → 供需研判 → 多空信号

两个Agent使用不同工具集，并行运行（asyncio.gather）。

### 数据源健康检测

- 连续3次失败自动标记为不健康，暂停抓取
- 10分钟后自动重试恢复
- `/api/source-health` 接口实时查看各源状态

### 概念板块检索

支持两种检索方式：
- **关键词匹配**：SQL LIKE模糊匹配概念名称
- **语义搜索**：ChromaDB + BGE-M3向量搜索（输入整句话，如"特朗普关税利好的国产替代板块"）

### TACO方法论

局势分析子Agent内置特朗普政策分析框架（The Art of the Deal）：
1. **极限施压**：识别恐慌信号，可能是买入机会
2. **谈判空间**：评估让步余地
3. **交易达成**：警惕利好出尽
4. **宣传胜利**：区分真制裁与谈判筹码

## API接口

| 接口 | 说明 |
|------|------|
| `GET /api/snapshot` | 管线当前状态 |
| `GET /api/reports` | 历史分析报告（含股票+期货） |
| `GET /api/sources` | 新闻源列表（含健康状态） |
| `GET /api/source-health` | 数据源详细健康指标 |
| `GET /api/event-graph` | 事件因果关系图 |
| `POST /api/trigger` | 手动触发一轮分析 |
| `POST /api/review` | 手动触发每日复盘 |
| `WS /ws` | WebSocket实时事件推送 |

## 自选股配置

编辑 `config/watchlist.json`：

```json
{
  "stocks": [
    {"code": "600519", "name": "贵州茅台", "concepts": ["白酒", "消费"]},
    {"code": "002371", "name": "北方华创", "concepts": ["半导体设备", "国产替代"]}
  ]
}
```

分析时会自动检查自选股是否受当前事件影响。

## 推送通知（可选）

在 `.env` 中配置：

```env
# 钉钉
NOTIFY_DINGTALK_WEBHOOK=https://oapi.dingtalk.com/robot/send?access_token=xxx

# 企业微信
NOTIFY_WECOM_WEBHOOK=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx

# Telegram
NOTIFY_TELEGRAM_BOT_TOKEN=123456:ABC-DEF
NOTIFY_TELEGRAM_CHAT_ID=123456789
```

## License

MIT
