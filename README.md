# AlphaAgents

新闻驱动的自主选股系统。监控国内外财经新闻和地缘政治事件，自主分析对A股板块的多空影响，输出推荐关注的个股列表。

> 本系统只负责"选什么股"，不负责"怎么交易"。筛选出的个股可交给 [TradingAgents](https://github.com/KylinMountain/TradingAgents) 做单股深度分析。

## 架构

```
┌─────────────────────────────────────────────────────┐
│                    AlphaAgents                       │
│                                                     │
│  ┌──────────┐    ┌───────────────────────────────┐  │
│  │ 入口层    │    │  主Agent（策略师）              │  │
│  │ CLI/Web  │───▶│  OpenAI Agents SDK             │  │
│  │ Monitor  │    │  自主决策分析路径               │  │
│  └──────────┘    └──────────┬────────────────────┘  │
│                             │ handoff                │
│                   ┌─────────▼──────────┐            │
│                   │ 子Agent（局势分析师）│            │
│                   │ TACO方法论          │            │
│                   └────────────────────┘            │
│                                                     │
│  ┌──────────────────────────────────────────────┐   │
│  │  工具层（@function_tool）                      │   │
│  │                                              │   │
│  │  新闻源        数据工具       通用能力          │   │
│  │  ├ 财联社电报   ├ 概念板块检索  ├ DuckDuckGo搜索│   │
│  │  ├ 华尔街见闻   ├ 板块资金流向  └ 网页内容抓取  │   │
│  │  ├ 东方财富快讯  ├ 个股过滤                     │   │
│  │  ├ 金十数据     └ 自选股管理                    │   │
│  │  ├ 新华社                                     │   │
│  │  ├ 白宫声明     存储层                         │   │
│  │  ├ 美联储       ├ SQLite（股票/概念索引）       │   │
│  │  ├ SEC         └ ChromaDB（语义向量搜索）      │   │
│  │  ├ Truth Social                               │   │
│  │  └ BBC/CNBC/Google News                       │   │
│  └──────────────────────────────────────────────┘   │
│                                                     │
│  数据源: baostock (TCP) + akshare (同花顺)           │
└─────────────────────────────────────────────────────┘
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
• 稀土永磁  （力度 3/5）— 中国反制筹码

承压板块:
• 消费电子出口链（力度 3/5）— 47.5%关税→代工企业承压

【推荐关注】
| 代码   | 名称     | 关联概念   | 推荐理由                    |
|--------|---------|-----------|----------------------------|
| 002371 | 北方华创 | 半导体设备 | 国产设备平台龙头              |
| 688041 | 海光信息 | AI芯片    | 直接承接英伟达让出市场空间     |
| 688256 | 寒武纪   | AI芯片    | 国产AI推理芯片核心标的        |

【自选股影响】
• 600519 贵州茅台 — 中性（2/5）：内需主导，关税无直接影响

【风险提示】
此为事件驱动初筛，需结合单股深度分析做最终决策。
```

## 项目结构

```
AlphaAgents/
├── main.py                          CLI入口
├── alpha_agents/
│   ├── agents/
│   │   ├── strategist.py            主Agent（OpenAI Agents SDK）
│   │   └── geopolitical.py          局势分析子Agent
│   ├── tools/
│   │   ├── server.py                工具注册（@function_tool）
│   │   ├── news.py                  东方财富新闻
│   │   ├── cls_telegraph.py         财联社电报
│   │   ├── wallstreetcn.py          华尔街见闻
│   │   ├── jin10.py                 金十数据
│   │   ├── xinhua.py                新华社
│   │   ├── whitehouse.py            白宫声明
│   │   ├── fed.py                   美联储
│   │   ├── sec.py                   SEC
│   │   ├── truthsocial.py           Trump/Musk社交媒体
│   │   ├── eastmoney_live.py        东方财富7x24快讯
│   │   ├── world_news.py            BBC/CNBC/Google News
│   │   ├── stock_search.py          概念板块→个股检索
│   │   ├── sector.py                板块资金流向
│   │   ├── stock_filter.py          ST/停牌/小市值过滤
│   │   ├── watchlist.py             自选股管理
│   │   ├── web_search.py            DuckDuckGo搜索
│   │   └── web_fetch.py             网页内容抓取
│   ├── data/
│   │   ├── db.py                    SQLite schema
│   │   ├── index_builder.py         baostock+同花顺数据索引
│   │   └── embeddings.py            ChromaDB概念向量搜索
│   ├── prompts/
│   │   ├── strategist.md            策略师系统提示词
│   │   └── geopolitical.md          局势分析师提示词（含TACO方法论）
│   ├── monitor.py                   新闻监控循环
│   ├── news_digest.py               新闻摘要（LLM过滤）
│   ├── notify.py                    推送通知
│   └── web/                         Web界面
├── config/
│   └── watchlist.json               自选股配置
└── tests/                           136个单元测试
```

## 核心设计

### 自主决策

主Agent不是固定流水线，而是根据新闻内容自主选择分析路径：
- 判断新闻重要性（1-5级），不重要的跳过
- 大事件调用局势分析子Agent深挖
- 自主选择搜索哪些板块、验证哪些数据
- 信息不够时主动搜索更多来源

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
