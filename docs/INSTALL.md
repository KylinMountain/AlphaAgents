# 安装、配置与自定义交易员

这份文档讲怎么把 AlphaAgents 在自己的机器上跑起来：装什么、配什么、需要哪些环境变量、
怎么写一个自己的交易员，以及改完之后怎么用历史回放验证它。

> 本项目只做研究和虚拟盘实验：不接券商，不下真单，不构成投资建议。

---

## 1. 它会跑什么

装好以后，一个进程按 A 股交易日的节奏跑下面这些任务（时间都是北京时间）：

| 时间 | 任务 | 做什么 |
|---|---|---|
| 全天每 5 分钟 | `news_ingest` | 拉取快讯、入库，并增量写入语义索引（不调用 LLM） |
| 09:00 | `morning_scan` | 晨扫：读新闻、资金、持仓、守则和昨日复盘，决定买什么、最多付多少、止损和失效条件 |
| 09:25 | `opening_reminder` | 开盘提醒 |
| 09:30–15:00 每 5 分钟 | `intraday_monitor` | 代码检查失效条件；只在出现异常时唤醒 agent |
| 15:30 | `review` | 校验与评分 |
| 15:45 | `shadow_run` | 挑战者策略的前向影子预测 |
| 17:30 | `daily_archive` | Tushare 盘后数据归档 |
| **19:00** | **`close_review`** | **收盘复盘：逐笔复盘 → 当天决策诊断（错过 / 踩坑 / 转向 / 做对）→ 改写交易守则** |
| 20:00 | `night_scan` | 夜扫外盘 |
| 周六 10:00 | `weekly_report` | 周报 |

另外有一个网页看板（持仓、主线、学习记录），它是独立进程，只读数据库。

---

## 2. 环境要求

| 项 | 要求 |
|---|---|
| 系统 | macOS 或 Linux（Windows 用 WSL） |
| Python | 3.11 及以上，用 [uv](https://docs.astral.sh/uv/) 管理 |
| Node.js | 22（只有构建网页看板时需要） |
| 磁盘 | 约 2–5 GB：全市场日线库、概念库、30 天新闻向量（约 300 MB） |
| 网络 | 国内行情和快讯源需要**直连**（代码会主动绕开系统代理）；海外新闻源可以走 Cloudflare Worker（见 §4.5） |
| LLM | 任意兼容 OpenAI API 的服务：DashScope、DeepSeek、SiliconFlow、OpenRouter、本地 Ollama 等 |

---

## 3. 安装

```bash
git clone https://github.com/KylinMountain/AlphaAgents.git
cd AlphaAgents

uv sync                  # 安装 Python 依赖到 .venv
cp .env.example .env     # 然后按 §4 填写

# 网页看板（可选）
cd web && npm install && npm run build && cd ..
```

`npm run build` 会生成 `web/dist/`，`main.py web` 直接从这个目录提供页面。
不构建也能跑交易，只是没有看板。

---

## 4. 配置（`.env`）

**最简单的方式是在网页上配置**：启动看板（§6）后打开「系统设置」，页面会直接改写 `.env`，
保留原有的注释和顺序，并把修改前的文件备份为 `.env.bak-settings`。每个模型都有「测试连接」按钮，
会真实调用一次并显示模型回复的原文；推送渠道可以发送测试消息。「交易员」页可以编辑 `traders/*.yaml`，
也可以查看、修改交易守则，并和历史版本对比。还没有配置决策模型时，打开看板会直接进入设置页。

几点要知道：

- 设置在进程**启动时**读取，保存后要重启调度器和看板才生效，页面上会提示。
- 密钥只写入、不回显，页面上只显示 `sk-…a3f2` 这样的掩码。但设置接口**没有访问控制**，
  看板不要暴露到公网。
- Docker 部署时，容器里默认没有 `.env` 文件（docker 的 `env_file` 只注入变量）。要用设置页，
  需要把宿主机的 `.env` 挂载进两个容器，并设置 `ALPHAAGENTS_ENV_FILE=<挂载路径>`，
  让调度器和看板读写同一个文件。

下面是手动编辑 `.env` 的说明，内容和设置页一致。

所有配置都在项目根目录的 `.env` 里，启动时自动加载。`.env.example` 里有完整示例和注释。
**`.env` 里是密钥，不要提交到仓库。**

### 4.1 LLM：最少只配一组

每个用途一套凭证，命名规则统一为 `<ROLE>_API_KEY / <ROLE>_BASE_URL / <ROLE>_MODEL`：

| 角色 | 用在哪 | 建议 | 没配时 |
|---|---|---|---|
| `AGENT` | 晨扫、选方向、选股、交易计划、卖出决策、收盘复盘、守则改写 | **要聪明**，这里决定交易质量 | 用 `SILICONFLOW_API_KEY` + DashScope 地址（多半跑不通，**请显式配置**） |
| `DIGEST` | 新闻过滤、事件链接 | 可以便宜 | 落到 SiliconFlow 免费档 |
| `SUMMARY` | 周报、上下文压缩 | 可以便宜 | 继承 `DIGEST` |
| `EMBEDDING` | 概念和新闻的语义向量 | 默认 `BAAI/bge-m3` | 用 `SILICONFLOW_API_KEY` |

最小可用配置，一个 SiliconFlow key 加一个决策模型：

```env
SILICONFLOW_API_KEY=sk-xxx                  # 兜底 embedding / digest，https://siliconflow.cn 免费注册

AGENT_API_KEY=sk-xxx
AGENT_BASE_URL=https://api.deepseek.com/v1
AGENT_MODEL=deepseek-chat
```

填完之后，检查每个用途实际用的是哪个模型：

```bash
uv run python main.py llm-roles
```

**关于免费模型。** OpenRouter 的 `:free` 模型每个账号每天只有 50 次请求，额度按账号算，
换哪个 `:free` 模型都共用这 50 次，UTC 00:00 重置。一次晨扫就要约 10 次请求，
所以免费档跑不完一整天的调度。遇到 429 时换模型没有用，只能等重置或者换付费服务。

### 4.2 交易开关与风控兜底

| 变量 | 默认 | 说明 |
|---|---|---|
| `AGENT_ENTRY_PRICING` | 开 | 由 agent 自己定挂单价 |
| `HARD_STOP_PCT` | `8.0` | 仅供风险预算定仓估算单笔风险；**不触发卖出**。没有止损也没有止盈，卖出全部由 agent 决定 |
| `TOTAL_CAPITAL` | `1000000` | 默认交易员的本金（每个交易员可以在 YAML 里单独设置） |
| `DEFAULT_POSITION_PCT` | `0.03` | agent 没写仓位时用的默认值 |
| `MAX_POSITION_PCT` | `0.10` | 单只股票的仓位上限（兜底，不是目标） |
| `TRADABLE_PREFIXES` | `60,000,001,002,003,300,301` | 可交易的代码前缀，默认是沪深主板加创业板，不含科创板、北交所和 B 股 |

### 4.3 超时（一般不用改）

| 变量 | 默认（秒） | 说明 |
|---|---|---|
| `MORNING_TIMEOUT` | 420 | 一次晨扫 agent 的总时长 |
| `TOOL_TIMEOUT` | 25 | 单次工具调用 |
| `EXIT_DECISION_TIMEOUT` | 120 | 一次卖出决策 |
| `ENTRY_PRICING_TIMEOUT` | 240 | 一次挂单定价 |
| `ORDER_REVIEW_TIMEOUT` | 120 | 挂单复核（主线转弱时唤醒 agent 决定留还是撤） |
| `DIGEST_TIMEOUT` | 240 | 一次新闻过滤 |

模型比较慢（比如推理模型）时，先调大 `MORNING_TIMEOUT`。

### 4.4 数据源

| 变量 | 说明 |
|---|---|
| `TUSHARE_TOKEN` | 龙虎榜、北向、两融等 7 张表。不配时这些表是空的，不影响主流程。`TUSHARE_API_KEY` 也认 |
| `TUSHARE_HTTP_URL` | 只有需要走代理时才设，默认是官方端点 |
| `MONITOR_INTERVAL_SECONDS` | 新闻轮询间隔，默认 300 |
| `NEWS_FETCH_LIMIT` | 每个源每次最多拉取的条数，默认 50 |
| `NEWS_INDEX_RETENTION_DAYS` | 新闻向量保留天数，默认 30 |
| `FUTURES_NIGHT_SESSION` | `1` 表示在期货夜盘时段也分析，默认关 |

### 4.5 推送与代理（可选）

| 变量 | 说明 |
|---|---|
| `NOTIFY_DINGTALK_WEBHOOK` / `NOTIFY_WECOM_WEBHOOK` / `NOTIFY_FEISHU_WEBHOOK` | 钉钉、企业微信、飞书机器人；配了几个就推几个 |
| `NOTIFY_TELEGRAM_BOT_TOKEN` + `NOTIFY_TELEGRAM_CHAT_ID` | Telegram |
| `CF_WORKER_URL` + `CF_WORKER_AUTH_TOKEN` | 海外新闻源经 Cloudflare Worker 转发（部署见 `deploy/cloudflare/`）。不配时海外源会失败，国内源照常工作 |

### 4.6 目录

| 变量 | 说明 |
|---|---|
| `ALPHAAGENTS_ENV_FILE` | 配置文件路径，默认项目根目录的 `.env`。Docker 下指向挂载进来的文件 |
| `ALPHAAGENTS_DATA_DIR` | 数据目录，默认 `./data`。**回放一定要指向单独的沙箱目录**（见 §7），否则回放会写进你的真实账本，还会读到“未来”的经验 |

---

## 5. 初始化数据

`data/` 下有四个库：

| 文件 | 内容 | 怎么来 |
|---|---|---|
| `stocks.db` | 股票、概念、行业与成分股 | `uv run python main.py build-index`（约 5 分钟；首次启动时如果缺失会自动构建） |
| `chroma/` | 概念与新闻的语义向量 | `uv run python main.py build-embeddings`（首次启动时如果缺失会自动构建） |
| `market_history.db` | 全市场日线 | `uv run python main.py init-history --months 6`，逐只拉约 5000 只股票，**要几个小时**，支持断点续传。之后每天会自动增量更新 |
| `memory.db` | 账本、主线、订单、持仓、复盘、学习记录 | 首次运行时自动创建。**这是唯一无法重建的库，请定期备份** |

建议第一次按顺序执行：

```bash
uv run python main.py build-index
uv run python main.py build-embeddings
uv run python main.py init-history --months 6
```

---

## 6. 启动

```bash
# 交易日调度器（主进程）
uv run python main.py run-v2

# 网页看板，另开一个终端。--no-monitor --no-ingest 表示只读，不重复调用 LLM 和拉取新闻
uv run python main.py web --port 8000 --no-monitor --no-ingest
# 然后打开 http://localhost:8000
```

调试时可以只跑一个任务然后退出：

```bash
uv run python main.py run-v2 --task morning     # 可选 morning / intraday / review / shadow / night / weekly / opening / archive
uv run python main.py run-v2 --now              # 启动后立刻跑一次晨扫
```

**验证要看内容，不要只看有没有报错。** 跑完一次晨扫后，打开看板或日志，读一下 agent 实际写了什么。
这个项目最常见的故障是：流程全部走通，状态码都是 200，但正文是一条错误信息，
比如 `Unsupported model`。

Docker 部署（镜像、数据目录挂载、升级）见 [`deploy/README.md`](../deploy/README.md)。

---

## 7. 交易员：默认的，以及你自己的

### 7.1 默认交易员

`traders/` 目录下没有启用的 YAML 时，系统只跑内置的 `default` 交易员：它没有人设，也没有额外规矩，
每天读市场、交易、复盘，自己积累经验。这也是目前推荐的跑法。

它的经验写在 `data/traders/default/MEMORY.md`：

- 每天 19:00 收盘复盘之后，它会**自己改写**这份交易守则，最多 10 条。每条注明来自哪几笔复盘。
- 每条守则下面的「证据」行由系统按日线计算：来源交易的到手收益和吐回幅度，
  以及写下这条守则之后遵守和违反它的交易各自的中位收益。**模型不能给自己打分。**
- 每次决策（晨扫、选方向、卖出）都会自动加载这份守则，以及最近的逐笔复盘和昨日复盘。
- 这是一个普通的 Markdown 文件，你可以直接打开阅读或修改。你改过的内容会被它读到，
  它下一次改写时也会以你的版本为起点。历史版本在 `data/traders/default/memory_history/`。

### 7.2 写一个自己的交易员

一个交易员就是 `traders/` 下的一个 YAML 文件，不需要改代码。可以从示例复制一份开始：

```bash
cp traders/pullback.yaml.example traders/my_trader.yaml
```

```yaml
id: my_trader            # 唯一标识，写进它的每一笔持仓和论点；不能用 default
name: 我的交易员
capital: 500000          # 自己的本金，和其他交易员分开记账
prompt_file: morning_scan.md   # alpha_agents/prompts/ 下的提示词模板
default_size_pct: 0.05   # agent 没写仓位时的默认值
max_position_pct: 0.10   # 单只股票上限（兜底）
default_horizon_days: 5  # 论点没写期限时的默认持有期
enabled: true            # false 表示暂停，历史数据保留
tags: [experiment]
note: 给人看的备注

extra_prompt: |
  你是谁、看什么、偏好什么、什么时候不出手——都写在这里。
  晨扫和卖出决策都会读到这段话。
```

| 字段 | 默认 | 说明 |
|---|---|---|
| `id` | 文件名 | 唯一标识，不能是 `default` |
| `name` | 同 id | 报告和通知里显示的名字 |
| `capital` | 1000000 | 本金 |
| `prompt_file` | `morning_scan.md` | 使用的提示词模板 |
| `default_size_pct` | 0.03 | 默认仓位 |
| `max_position_pct` | 0.10 | 单票上限 |
| `default_horizon_days` | 5 | 默认持有期 |
| `enabled` | true | 是否启用 |
| `extra_prompt` | 空 | **策略之间的差异全部写在这里** |
| `note` / `tags` | 空 | 备注 |

几点说明：

- **策略写在 `extra_prompt` 里，不写在配置字段里。** 介入价、止损、仓位由 agent 自己调用
  `get_price_levels`（均线、区间、支撑阻力、日均波幅）来计算，配置里只放它算不出来的东西：
  本金、读哪个提示词、不能突破的上限。
- 每个交易员的资金、持仓、挂单、论点、复盘和守则都**分开**存放（`data/traders/<id>/MEMORY.md`）；
  行情、主线、快讯和工具是共用的。
- 把一个交易员改成 `enabled: false` 之后，如果它手上还有持仓，系统会继续替它管理这些旧仓
  （显示为「仅清理旧仓」），但不再开新仓，直到清空。
- 格式有错的 YAML 会被跳过并打日志，不会被当成默认交易员运行。
- 仓库里的 `pullback.yaml.example`（回调派）和 `breakout.yaml.example`（突破派）是两个完整示例，
  可以对照着写。

更多设计上的原因见 [`traders/README.md`](../traders/README.md) 和 [`multi_trader.md`](multi_trader.md)。

---

## 8. 用历史回放验证改动

改了交易员或提示词之后，不必等上几周线上结果，可以在历史行情上回放一段。回放在**单独的沙箱**里运行，
不会碰你的真实账本；每个交易日只能看到当天之前的数据和经验。

```bash
# 1. 建一个沙箱（真实库以只读方式链接进来）
uv run python scripts/walk_bootstrap.py --target /tmp/wf-test

# 2. 从 2026-01-05 起回放 30 个交易日，用真实模型决策
ALPHAAGENTS_DATA_DIR=/tmp/wf-test ALPHAAGENTS_LLM_MODE=record \
  uv run python scripts/walk_forward.py --target /tmp/wf-test \
    --start 2026-01-05 --days 30 --trader default \
    --autonomous --decider llm --selection-architecture sector_first_v0 \
    --allow-current-membership --keep-going
```

- 报告写在 `<沙箱>/walk-reports/<起始日>-<天数>d-<交易员>/`，其中 `summary.txt` 是摘要，
  另有 `equity.csv`、`fills.csv` 等明细。
- `ALPHAAGENTS_LLM_MODE=record` 会把每一次模型调用记录下来；之后用 `replay-recorded`
  可以不花 token 原样重放。
- 30 天回放大约要 4 个小时，主要花在模型调用上。
- **模型出问题时**：超时、限流、普通 5xx 会自动重试。模型不可用或费用不足
  （`no_healthy_account`、402、额度不足）时，会自动切换到网关 `/models` 列表里
  同一系列的模型（比如 `global:deepseek-v4.1-flash-sg` → `global:deepseek-v4.1-flash`
  → `cn:deepseek-v4.1-flash`）。也可以用 `AGENT_FALLBACK_MODELS=a,b` 手动指定顺序。
- **全部模型都不可用，或者连续 3 天没能做出决策**时，回放会停下（退出码 3），
  而不是把剩下的交易日都空跑完。恢复后续跑：

  ```bash
  uv run python scripts/walk_resume.py --target /tmp/wf-test
  ```

  续跑会从第一天重新开始：已经记录的调用直接用记录里的回答，并逐条核对请求，
  不花 token，几分钟就能走到断点；之后再切回真实调用，继续往下记录。
  如果中间改过代码或数据，导致请求对不上，续跑会报错并指出哪里不同，
  不会把两次不同的运行拼在一起。
- 报告开头有一段「本轮是否完整」：有交易日没做成决策、复盘或守则改写失败，
  或者有调用是由备用模型回答的，都会写在这里。标注为不完整的轮次，
  不要拿来和其他轮比较。
- 结果要和**等权大盘**比较超额，而不是只看绝对收益；样本少于 50 的结论不要当真。
  更多判断规则见 [`AGENTS.md`](../AGENTS.md) 的「Evidence rules」。
- 回放有已知局限：概念成分股用的是现在的名单，不是当时的；模型的训练截止时间未知。
  这些都会写在报告末尾的「本次运行不能说明什么」里。

---

## 9. 常见问题

**晨扫报 `rate limit exceeded` 或 429。**
多半是免费档额度用完了，见 §4.1。换一个同平台的免费模型没有用。

**日志里行情或快讯全是超时。**
国内数据源要直连。如果本机开了全局代理，把这些域名设为直连；代码已经在请求时绕开系统代理，
但 TUN 模式或透明代理仍然可能拦截。

**15:30 复盘时拿不到当天的 K 线。**
这是正常的：baostock 的日线大约 17:30 才入库。所以逐笔复盘、盘面复盘和守则改写都放在 19:00 的 `close_review`。

**看板上的数和日志对不上。**
先确认看板进程是在最近一次代码更新之后启动的：进程启动时间早于最新提交时，跑的还是旧代码。

**想从零开始。**
先备份 `data/memory.db`，再删除它和 `data/traders/`。行情库（`market_history.db`、`stocks.db`）不用删。

---

接下来可以读：[`ARCHITECTURE.md`](../ARCHITECTURE.md)（分层与数据库地图）、
[`GOLDEN_PRINCIPLES.md`](GOLDEN_PRINCIPLES.md)（不可越过的规矩）、
[`docs/README.md`](README.md)（哪份文档对哪个问题有权威）。
