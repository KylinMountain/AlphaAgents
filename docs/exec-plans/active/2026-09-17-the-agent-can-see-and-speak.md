# 让 agent 看得见、也说得上话

状态：completed（四阶段落地、6 个缺陷修复、端到端复跑验证通过）
创建：2026-09-17

## 这解决的是什么

2026-09-17 跑了第一次真实 20 天回放（`--decider llm`，mimo-v2.5，47 分钟）。机制全部通过：
下单、开盘撮合、止损、跳空、打标、提炼、引用、回灌提示词，12 个环节 0 抛错。

**但结果是：11 笔买入全部止损出场，0 胜。** 查证后发现原因不是模型笨，也不是"信息不够"
这种笼统的说法，而是**四个具体的接口缺口**。每一条都读到了行号，不是推断。

### 缺口 1：agent 没有卖出决策权

回放每天只调用模型一次，只问"买什么"。`_settle_exits`（`scripts/walk_forward.py:1101`）
是纯机械规则。**agent 从未被问过"要不要卖"。**

生产里**有**这个能力：`pipeline/tasks/exit_decision.py`（445 行）设计完整——成本、现价、
浮动盈亏、峰值、回撤、买入理由、主线、近期快讯、规则信号，还支持 `sell/trim/add/hold`
四种动作和仓位比例。**回放没接它。**

### 缺口 2：持仓信息缺三个决定性字段

agent 实际看到的（回放日志真实记录）：

```
300936 中英科技 300股 @ 85.50 (市值25,650元) | 止损84.0 | 持仓0天
```

1. **「市值」标错了**——`portfolio_report.py:64` 是 `cost = open_price * shares`，
   那是**成本**。字段名在骗人。
2. **没有现价**——所以算不出浮盈浮亏，**agent 不知道自己赚了还是亏了**。
3. **没有目标价**——`create_pending_order` 支持 `target_price`，但回放
   （`walk_forward.py:915`）**不传**。没有目标价 ⇒ 出场判定只剩止损一条路。

**这就是"为什么都是 hard stop loss"的答案**：不是 agent 想止损，是结构上只有止损。

### 缺口 3：Tushare 指向一个已失效的共享端点，7 张表全空

**（2026-09-17 更正。第一版分析说"token 名错配导致 token 为空"，实测证明那句话
错了一半 —— 变量的名字确实不一致，但后果不是"空 token"，而是回落到一个在官方
端点无效的公共 token。）**

`alpha_agents/data/tushare_client.py:21` 的默认端点是：

```python
_DEFAULT_URL = "http://121.40.135.59:8010/"   # 已失效：5 秒超时空返回
```

而 `.env` **没有设 `TUSHARE_HTTP_URL`**，所以每一次调用都走这个死端点。

四组实测（`moneyflow_hsgt`，2026-09-01..15）：

| 组合 | 结果 |
|---|---|
| 用户 token + **官方端点** | ✅ **11 行 / 0.3 秒** |
| 用户 token + 共享端点 | ❌ 0 行 / 5.0 秒 |
| 共享 token + 官方端点 | ❌ `您的token不对，请确认。` |
| 共享 token + 共享端点 | ❌ 0 行 / 5.0 秒 |

**用户的 token 完全有效**，只是被默认端点挡住了。第二个 bug 是变量名：
代码读 `TUSHARE_TOKEN`（`tushare_client.py:32`），`.env:1` 写的是
`TUSHARE_API_KEY`，所以即使端点修好，用户自己的 token 仍然取不到，会回落到
那个公共 token —— 而它在官方端点被拒绝。

每次 `daily_archive` 都打：

```
Tushare lhb_inst failed: RemoteDisconnected
Tushare hm failed: RemoteDisconnected
Tushare north failed: RemoteDisconnected
Tushare margin failed: RemoteDisconnected
```

`lhb_daily`、`lhb_inst_daily`、`hm_daily`、`north_flow_daily`、`margin_daily`、
`kpl_limit_list_daily`、`kpl_concept_daily` —— **7 张表 schema 完整、writer 完整、0 行数据**。

另外 `daily_archive` **在 `scheduler.py` 里没有注册**，所以即使端点修好也可能不会每天跑。

### 缺口 4：候选池只有一个因子

`_build_panel`（`walk_forward.py:563-598`）按 **T-1 涨幅倒序**取前 12：

```python
ranked.sort(key=lambda t: t[0], reverse=True)
panel = ranked[:limit]
```

agent 看到的每只股票只有 **3 个数字**（收盘、涨幅、ADV20）。**候选池已经替它做完决定了**
——它只能在"昨天涨得最多"里挑，所以必然追高、必然吃反转。

而本地**已经有数据却没喂进去**：

| 表 | 行数 |
|---|---|
| `daily_kline` | 7,763,121 |
| `sector_flow_snapshots` | 346,635 |
| `limit_pool_snapshots` | 31,236 |
| `market_breadth_snapshots` | 568 |

## 目标

把上面四个缺口补上，让 agent 在**买入和卖出两侧**都拿得到判断所需的信息，并且**对卖出
有发言权**。分四个阶段，每个阶段独立可验收、独立可提交。

**明确不做：**
- **不碰 `evolution` 的迭代机制。** 用户明确说了不能只想着让它自己迭代。这一轮是**修接口**，
  不是调参数。学习闭环保持现状。
- **不改生产交易路径的行为。** 除缺口 3 的 token 名（纯 bug）外，改动集中在回放 + 提示词 +
  只读数据层。生产的 `exit_decision` 已经在跑，不动它。
- **不发明新数据源。** 缺口 3 修好 7 张表之后，用尽已有的；估值/财务不在本轮。
- **不调候选池的规模上限**（`--panel-size` 保持默认），只改**排序依据**。

## 阶段与验收

### 阶段 1：修 token 名 + 把 `daily_archive` 接进调度（最小、最直接）

改动：
- `alpha_agents/data/tushare_client.py` — 接受 `TUSHARE_TOKEN` **和** `TUSHARE_API_KEY`
  两个名字，后者优先。**不删旧名**：`.env` 已经在用 `TUSHARE_API_KEY`，而
  别的部署可能用 `TUSHARE_TOKEN`，两个都认是唯一不破坏任何一方的做法。
- `.env.example` — 补上 `TUSHARE_TOKEN` 并说明两个名字等价。
- `alpha_agents/pipeline/scheduler.py` — 注册 `daily_archive`。

**验收（机器可查）：**
1. `tests/test_tushare_client.py` 断言两个环境变量名都能取到 token。
2. 断言两个都没设时**抛出清晰错误**，而不是静默用空 token 去请求。
3. `scheduler` 的注册表里能查到 `daily_archive`。
4. 实跑一次 `daily_archive`，4 个原先失败的接口**不再报 `RemoteDisconnected`**
   （report 实际行数，为 0 也如实报告——周一是非交易日，0 不是失败）。

### 阶段 2：补齐持仓字段 + 让 agent 输出目标价

改动：
- `alpha_agents/data/portfolio_report.py` — `get_open_positions_summary` 增加
  `current_price` 参数；持仓行渲染 **现价、浮动盈亏%、峰值回撤**；把错的 `(市值…)`
  改成 **`(成本…)`**，并新增真正的市值。
- `alpha_agents/prompts/t1_decide.md` — 输出 schema 增加 `target_price`（可选）。
- `alpha_agents/agents/t1_decider.py` — 解析并校验 `target_price`
  （必须 `> entry_high`）。
- `scripts/walk_forward.py:915` — 把 `target_price` 传给 `create_pending_order`。

**验收：**
1. `tests/test_portfolio_report.py` 断言持仓行含现价与浮动盈亏；
   断言字段名是 `成本` 而不再是 `市值`（**这条就是防回归那句话的**）。
2. `tests/test_t1_decider.py` 断言 `target_price <= entry_high` 被拒绝，
   `target_price > entry_high` 被接受，缺省为 `None`。
3. 回放 20 天后 `fills.csv` 里出现**至少一笔非止损的卖出**（止盈或 agent 卖出），
   或者报告如实说明本窗口没有触发——**两者都接受，静默不出现不接受**。

### 阶段 3：给 agent 卖出决策权

改动：
- `alpha_agents/pipeline/tasks/exit_decision.py` — 增加一个**回放专用**入口
  `decide_for_replay(positions, price_map, signals, *, day)`，复用既有的
  `build_context` / `decide` / `apply`。**不修改现有 `run()`**，因为它的
  "只在需要时调用模型"启发式依赖 thesis，而回放没有 thesis。
- `scripts/walk_forward.py` — 在 `_settle_exits` **之前**插入 agent 卖出判断，
  一天的模型调用从 1 次变成 2 次（买 + 卖）。

**契约（重要）：** agent 的卖出判断**不能覆盖硬止损**。`_settle_exits` 的机械规则
**先跑还是后跑**决定了这一点，必须明确：
- 顺序 = **先机械止损，后 agent 判断**。理由：硬线是保命的，不能被模型推理绕过；
  这在 `exit_decision` 自己的 docstring 里就是设计意图
  （"风控硬线…你的判断不能覆盖这两条"）。

**验收：**
1. `tests/test_exit_decision_replay.py` 断言：一个已触及硬止损的持仓，
   即使 agent 说 `hold`，**仍然被平掉**。
2. 断言 agent 说 `sell` 而机械规则没触发时，**确实平掉**。
3. 断言模型调用失败时**全部 hold**（失败不导致误卖）。
4. 回放报告新增一行「agent 卖出 N 笔 / 机械止损 M 笔」，两者分开计数。

### 阶段 4：候选池改为多维 + 把已有维度喂进提示词

改动：
- `scripts/walk_forward.py` — `_build_panel` 的排序从「涨幅」单因子改为**可配置的
  复合分**，至少纳入：T-1 涨幅、板块资金流、是否在涨停池、大盘涨跌家数状态。
- `alpha_agents/prompts/t1_decide.md` — 候选池增加列：**板块、换手率、资金净流入**；
  新增一段**大盘状态**（涨跌家数、等权指数当日）。
- 数据读取走**只读**的 `market_snapshots.db`，且必须受 `replay_as_of` 约束
  （见下面的风险）。

**验收：**
1. `tests/test_walk_forward.py` 断言面板行含新列，且**每一列都有非空值或显式 `None`**。
2. 断言面板构造**不读取窗口日之后的任何数据**（快照表按 `captured_at` 过滤）——
   这是防未来函数，必须有测试。
3. 回放报告新增一行「本窗口等权大盘收益 X%，agent 收益 Y%，超额 Z%」。
   **没有这一行，"亏了"永远无法解释。**

## 决策记录

### 2026-09-17：为什么先修接口而不是先调迭代

用户的原话值得原样记下：「我们不能总是想着让它自己迭代，而是要想想这个思维方式什么问题？
股票应该不只是看自己的量价吧？决定它上涨下跌还受到外部因素，世界状态啊，新闻啊，大盘啊」。

这个判断被数据支持。当前 `evolution` 学的唯一参数是 `t1_change_rank`，而它学的
**正是候选池那个被写死的排序**。在一个只有单因子的池子上做 RSI，是在优化一个
**不该存在的选择**。先让池子和信息集成立，迭代才有对象。

### 2026-09-17：为什么「亏了」这件事本身需要基准

我第一轮说"策略是亏的"，但没跟大盘比。补算之后：

| | 区间收益 |
|---|---|
| 等权大盘 | **-2.81%** |
| mimo-v2.5 | -1.71% |
| Qwen-14B | -0.56% |

**两个模型都跑赢大盘。** 所以"亏"主要是市场在跌。但 20 天、单次采样，
**这个超额本身也不足以证明能力**——它只说明"不跟大盘比就没法解释收益"。
阶段 4 的验收 3 就是为此。

### 2026-09-17：模型选择是配置，不是结论

同窗口换模型，行为差异巨大：mimo 买 11 笔全止损，Qwen 只买 3 笔。
**换模型 = 换策略**，这印证了 `model_identity` 把模型写进策略指纹的正确性。
推论：不能用单次结果评价策略，必须 record/replay 固定模型多次采样。

顺带确认（官方文档 + 实测）：mimo 用 `thinking: {"type": "disabled"}` **可以关思考**，
22.7 秒 / 0 推理 token，对比开启时 148 秒 / 2499 token。
`alpha_agents/tools/vpa/llm.py:626` 那句「mimo has no documented disable flag」**已过时**。
本 plan 不含此改动，但记在这里免得再查一遍。

## 风险

1. **未来函数（最高风险）。** 阶段 4 要读 `market_snapshots.db`，而它是**实时快照**，
   时间戳是 `captured_at` 不是交易日。**回放读它必须按 as-of 过滤**，否则就是
   把窗口之后的信息喂给窗口之内的决策——这是本仓库最不能犯的错。
   验收 2 专门钉这一条。
2. **模型调用翻倍。** 阶段 3 把每天 1 次调用变成 2 次。用 mimo 是 100 分钟/20 天，
   不可接受。**阶段 3 的验收必须用一个不思考的模型跑**（Qwen-14B，2 分钟/20 天）。
3. **`exit_decision` 的复用面。** 它的 `run()` 依赖 thesis，回放没有。所以是**新增**入口
   而不是复用 `run()`。如果发现 `build_context` 本身也依赖 thesis，阶段 3 要先把
   `build_context` 拆出纯函数部分。
4. **提示词变长。** 阶段 4 加列会让 prompt 从 5.7KB 涨到可能 8-10KB。
   注意 `build_message` 有"未填占位符即报错"的守卫，改模板必须同步改渲染器。

## 实施记录（2026-09-17）

四个阶段全部落地并提交，各自带测试。

| 阶段 | 提交 | 关键结果 |
|---|---|---|
| 1 Tushare | `82e77a0` | 4 个接口通：lhb +73、lhb_inst +653、north +1、margin +3 |
| 2 持仓/目标价 | `e7adb5c` | 成本不再冒充市值；现价与浮盈入提示词；`target_price` 贯通 |
| 3 agent 卖出 | `5f75e20` | 回放接上 `exit_decision`；硬止损仍先跑 |
| 4 面板/基准 | `9a5eb63` | 面板含换手与概念、池子混合；报告带等权大盘 |

### 我在实施中发现并修掉的自伤

两处是**我自己写错、被度量抓出来的**，记在这里免得重犯：

1. **面板"混合"其实没混。** 第一版把换手排名**接在**涨幅排名后面再取前 N，
   于是涨幅排名仍然填满每一个位置。是运行计数 `panel_offered_liquid: 0`
   暴露的。现在两个排名**交替**取，计数改名为 `panel_offered_beyond_top_change`
   以名副其实。实测 12 行里有 5 行来自涨幅榜之外。
2. **`exit_decision` 绕过 LLM journal。** 它自建 `AsyncOpenAI`，所以卖出调用
   从不被记录，回放无法复现；而且它带**实时工具**，回放里会读到窗口之后的数据。
   现在接受注入的 model，回放传 journaled 的那个且**不传工具**。

另外修了一个**既有**的循环导入：`portfolio_report` 无法先于 `portfolio` 导入
（后者在文件末尾反向 re-export 前者），所以它一直只能经由 `portfolio` 到达。

### 回放暴露出的一个既有严重缺陷：卖出 agent 从未工作过

**`.env` 里 `AGENT_EXIT_DECISIONS=1` 是开着的，但生产日志里一条
`Exit decisions:` 都没有。** 原因是解析器只接受
`<!-- DECISIONS: [...] -->` 这一种形状，而**两个模型都不产出它**——
要求写 marker，mimo 和 Qwen 都回 ```json 围栏数组。实测：20 天回放里
**17 次卖出调用、17 次读不懂、0 条决策**。

也就是说：**这个 agent 从来没有对任何一个仓位做过卖出决定**，它一直
静默地 hold 一切。这正是本仓库反复付出代价的那种失败——**一个看起来
正常的答案**。

已修：解析器现在接受三种形状（marker、围栏数组、裸数组），并且
「散文里先举了个例子、真答案在后面」时取**最后**一个，而不是第一个。
提示词仍然要求 marker（那是无歧义的形式），fallback 存在是因为
**一个只读模型不产出的形式的解析器，等于没有解析器**。

### 资金流：一条链上两个 bug，都让数据看起来"没权限"

目标是「把已有的资金流喂进提示词」。追下去发现两个独立的缺陷：

1. **归档调了一个 token 读不到的接口。** `_tushare_archive` 要的是
   `moneyflow_dc`，实测返回「您没有接口(moneyflow_dc)访问权限」，于是
   `stock_fund_flow_daily` 一直 0 行——而失败长得像 schema 问题。
   普通 `moneyflow` 能返回全市场（单日 5550 行）。
2. **就算接口通了，writer 也会静默写 0 行。** 它无条件读 `net_amount` /
   `net_amount_rate`，而 `moneyflow` 用的是 `net_mf_amount`，且**根本没有
   rate 字段**。全缺失的一帧和空帧长得一模一样。现在列映射按帧自身的列选取，
   接口不给的 rate 写 NULL 而不是 0.0（0.0 等于宣称净额为成交额的 0%）。

回填窗口 21 个交易日、**116,477 行**。面板实测（2026-09-09）：

```
600127 金健米业  9.31%  换手46.62  2板  -6,472万   乳业、养老概念
002436 兴森科技  9.17%  换手18.75  2板 -35,196万   6G概念、低空经济
605577 龙版传媒  9.12%  换手15.05  7板 -16,411万   AI视频、IP经济
```

7 板接力风险、涨停但主力净流出 3.5 亿、46% 换手的派发——**这些在面板里
原本一个都没有**。

### 诚实的缺口

- **概念成分是当前值，不是时点值。** `concept_stocks` 没有日期列。这是真实的
  前视，只是比价格泄漏弱（是标签不是结果）。列头写明「当前成分」。
  覆盖率也只有 **1226 / 5216**，其余显示 `-`。
- **`fund_flow` 与 `hm_detail` 权限不足。** 需要更高 Tushare 积分档，不是代码缺陷，
  已如实记 warning 并跳过。
- **涨停池 / 板块资金流快照只有 2026-09-08 起**，所以 08-18 开的窗口用不上；
  本轮改用 `daily_kline` 自算宽度，覆盖完整。
- **没有估值/财务数据**，本轮不含。
- **资金流已交付**（见下）。此前判断它未交付是对的：`stock_fund_flow_snapshots`
  是按需单只缓存（252 行 / 47 个代码 = 0.9% 覆盖），不是每日全量。但
  `stock_fund_flow_daily` 走的是另一条路，而那条路上有两个 bug（见实施记录）。
- **涨停池只覆盖 2026-09-08 起**（快照表起点），之前的交易日该列为空。

## 这一步之后为真的

- agent 在**买入和卖出两侧**都能拿到判断所需信息
- 出场不再只有止损一条路（有目标价、有 agent 判断）
- 7 张 A 股核心表开始进数据
- 候选池不再是单因子
- 每次回放报告都带大盘基准，收益可解释

## 端到端验证（2026-09-17，20 个交易日，Qwen2.5-14B）

| 项 | 结果 |
|---|---|
| 区间收益 | **+0.606%** |
| 等权大盘 | **-2.807%** |
| 超额 | **+3.413%** |
| 出场归因 | 机械 5 笔（止损 3、止盈 2） |
| agent 决策 | `agent_exit_calls 12`、`agent_trim 2`、T+1 拦下 8 |
| 面板 | `panel_offered_beyond_top_change 98`（池子混合是真的） |
| 连板 | 面板含 5板/3板/2板 与板块名（如「000978 桂林旅游 5板 旅游概念」） |
| 市场宽度 | `market_state_days 20`（每天都有） |
| 抛错 | 0 |

**卖出 agent 第一次真正生效**：它做出 `trim fraction:0.3` 的决策并写了理由，
300624 从 500 股减到 400 股，账面与算术一致（500×0.3=150→取整手 100）。
在本次修复之前，同一路径 **17/17 次调用都读不懂，0 条决策**。

## 这一步之后仍不为真的（别读成已交付）

- **没有任何策略被证明有效。** 20 天、单次采样，n 远低于 50。
- **学习闭环没有改变。** 这一轮不碰 `evolution`。
- **回放仍然不是时点回测。** 模型权重已经读过这些日期，所以强结果
  "至少同样可能是记忆而不是信号"。
- **生产交易路径行为不变**（除 token 名这个纯 bug）。
