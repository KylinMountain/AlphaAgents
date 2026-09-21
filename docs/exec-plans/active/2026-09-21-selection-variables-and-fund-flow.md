# 选股链上的变量：现状、可行窗口，以及合伙人建议

状态：active
创建：2026-09-21
来源：owner 三问——「1 补上资金异动、2 修 beta、3 你作为合伙人觉得该怎么做？」

## 三件事的处置

### 2. beta 因子（已修，d68550c）

`sector_scoring` 把 beta 权重定在 **0.40**（最大的一项），但 replay 从没
喂过它：`normalize_beta_scores` 拿到空列表 → 每个名字都拿默认 50 →
**最大的因子变成常数，四因子分数实际是三因子在跑**。

不能读 live 的 `sector_betas`：它是 `UNIQUE(concept, code)` 的**当前**快照，
周更覆盖，用在 replay 里会把今天的 beta 排到历史每一天。

新增 `Corpus.sector_beta(code, peers, before)`，从 `daily_kline` 按 T-1 重算，
**调用 `beta_calculator` 同一批函数**（`compute_beta` / `weighted_beta` /
`_returns_from_history`），不写第二份公式。

实测（2026-08-18，F5G/5G/6G 板块）：

```
beta values: [0.8702, 1.0647, 1.0572, 0.7351, 0.9033, 1.1353]
distinct: 6   any None: False
```

修复前这 6 个值全是同一个默认 50。

### 1. 资金异动的接口（已验证可行，但有硬窗口）

**接口存在且可用**：`data/snapshot_store.read_sector_flow(scope, as_of)`
按 `captured_at <= as_of` 读最近一次概念级快照，返回的形状**刻意与 akshare
实时输出一致**（`行业`/`行业-涨跌幅`/`净额`/`领涨股`），所以
`sector_ranking` 那个消费者可以原样复用。

实测 2026-09-16 的重建结果：

```
columns: [行业, 行业-涨跌幅, 净额, 领涨股, 领涨股-涨跌幅, 公司家数]
top5 by net flow:
  融资融券        chg=+1.43%  flow=628.4亿
  芯片概念        chg=+3.01%  flow=574.6亿
  数据中心(AIDC)  chg=+1.89%  flow=348.0亿
  深股通          chg=+1.47%  flow=339.8亿
  共封装光学(CPO) chg=+4.04%  flow=326.5亿

Case A (chg>1.0 and flow>3) 会命中: [融资融券, 芯片概念, 数据中心(AIDC), 深股通, CPO]
```

**注意**：`read_sector_flow` 目前**零调用者**——只有 `market_data.py:340`
一个包装函数转发它。接口齐全、没人接，又是 D31 那个形状。

**硬窗口**：

| 源 | 覆盖 | 天数 |
|---|---|---|
| sector_flow_snapshots（概念级） | 2026-09-08 → 2026-09-21 | **13** |
| stock_fund_flow_daily（个股级，可按成分聚合） | 2026-08-18 → 2026-09-16 | 约 21 |
| daily_kline（replay 语料） | 2020-01-02 → 2026-09-18 | 1629 |

**与 kline 的重叠只有 9 天**（2026-09-08 → 2026-09-18）。所以：

- 「20 天小批量验证」用资金异动**勉强够**（9 个交易日）；
- 任何更长窗口**没有资金流**，replay 只能用价格/宽度代替——那正是现在
  两边不一致的根源。

### 3. 变量：选股链上一个基因都没有

现在仓库里只剩 8 个决策基因，**全都不管选股**：

```
confidence_priors.{high,medium,low}   # 概率映射
dim_base, dim_step                    # 证据数 → 概率
theme_gate.{w_flow,w_rel,w_confirm,admit_score,cancel_score}
```

而选股链上的变量随手就能数出一堆，**全部硬编码**：

| 位置 | 变量 | 现值 |
|---|---|---|
| 概念异动 | Case A/B/C/D/E 五个阈值 | 1.0/3、2.0/-1、1.0/5、-1.5/-3、-2.0/2 |
| 概念选择 | 取前几个板块 | 3 |
| 概念排序 | 三列权重（现在等权平均） | 1/3 each |
| 板块内 | 四因子权重 | .40/.25/.20/.15 |
| 板块内 | 涨幅分档 | 9.8/6/3/1/0/-2 |
| 板块内 | 流动性门槛 | 5e7 |
| 全局 | 取前几个 | 5 |

**所以「如何选择」目前完全由代码常量决定，没有任何一个能被证据驱动。**
这就是 `_SUPPORTED_DELTAS` 为空的根本原因——不是忘了填，是目标路径上
确实没有可动的变量。

## 合伙人建议：从资金面着手，但要按顺序

### 判断：是，从资金面着手，而且这是最诚实的第一步

三条理由：

1. **它已经是 live 的决策依据**。owner 的意图是「从概念板块选股」，
   而 live 的板块选择**就是**资金流异动（`_detect_anomalies` 主触发）。
   给它建基因不需要发明新语义，只需把已经在下游生效的阈值变成可动
   参数——这满足 DREAM §6 第 1 条「live 路径真的读它」。
2. **它有可重建的历史**（9 天）。这是目前**唯一**一个有 PIT 历史、又能
   被 replay 重建的候选发现通道。相比之下 beta 只能靠重算、成分只能靠
   当前快照。
3. **它的变量语义最清楚**。「净流入超过 N 亿才算异动」是一个能解释、
   能预注册、能用前向证据检验的命题。而「三列权重各占多少」很难说清
   方向。

### 建议按三步走，且第三步现在不该做

**第一步（便宜、纯减法）：把异动阈值从硬编码抽成命名常量，写进
`DEFAULT_DECISION_PARAMS`，但先不给变体映射。**

现在五个阈值散在 `anomaly_scan.py` 的 if 里，连「有哪些变量」都数不清。
抽出来之后 replay 和 live 读同一份，且能回答「今天为什么没触发」——
**先让它可观测，再让它可动**。

**第二步：让 replay 读资金流（`read_sector_flow`），把异动检测抽成
live/replay 共用的纯函数。**

这是 #1 的完成。做完之后 replay 的板块选择和 live 同源，代价是只有 9 天窗口。

**第三步（要 owner 决策）：给阈值定变体映射。**

这是唯一需要拍幅度的一步，也是最危险的一步。`variant.py` 的注释已经警告：
「Inventing a magnitude here would freeze a guess that later reads as a
measurement」。所以：

- **只给一个**变量建映射，不要一次上五个；
- 幅度从**观测分布**来，不是拍的：先统计 9 天里 Case A 在 flow 阈值 3.0
  附近的边际变化率，用它决定第一步长；
- 预注册样本数**至少 50**，而 9 天窗口产不出 50 个独立样本。

### 结论

**第一、二步现在做；第三步等数据。**

§6 要求 n≥50 且验证必须前向，而资金流历史只有 13 天、与 kline 重叠 9 天。
在 9 天上调任何阈值然后宣称它更好，正是这个仓库最反对的事
（「n < 50 does not ship」）。

这也顺便回答了「能不能从资金面着手」——能，而且它是唯一有 PIT 历史的
通道，但它的历史**还不够长到支持调参**。

## 第二步已完成：replay 与 live 同源读资金流

### 发现：管道早就铺好了，只是没人接

`market_data.get_concept_fund_flow()` **本来就是 replay 感知的**——它读
`get_replay_as_of()`，在 replay 里走 `_replay_sector_flow` →
`snapshot_store.read_sector_flow`。而 `walk_forward` 也早就在
`replay_as_of(f"{day} 09:00")` 块里跑决策。

**缺的只有一步：replay 从不调用概念异动检测。**

### 做法：抽出两个纯函数，两边共用

1. `data/sector_scoring.rank_concepts_by_flow(rows)` —— 排名成形（涨/跌榜、
   按净额排序、名字平手）。放在 `data/` 是因为 `tools/` 也要用，而
   `tools` 不能 import `pipeline`（分层方向），lint 会拦。
2. `pipeline/tasks/anomaly_scan.concept_anomaly_signals(ranking)` —— Case A–E
   逻辑，返回 `(概念名, 文案)` 对。

**返回结构化对而不是只返回文案**：replay 之前要从渲染好的句子里把概念名
再解析出来，那让消息格式变成承重结构，措辞一改就断。

### 一处实测纠正

我最初的过滤条件用「价格 shortlist」交集，实测三次（09-09/09-16/09-18）
交集分别是 **0 / 1 / 0**——资金异动板块基本不在价格前 8 里，**这正是资金
信号的意义所在**（它本就该与价格排名不一致）。改成只要求**在成分表里**
（面板能解析出成员），否则这个分支会退化成它要取代的价格分支。

### 实测

```
=== 资金流窗口内（>= 2026-09-08）===
2026-09-09: method=concept_fund_flow_anomaly
             selected=[国企改革, 金属铜, 小金属概念] panel=19
2026-09-16: method=concept_fund_flow_anomaly
             selected=[存储芯片, 汽车芯片, 中芯国际概念] panel=16

=== 窗口外（回退，并说明）===
2026-08-18: method=transparent_price_breadth_top3
             selected=[F5G概念, 5G, 6G概念] panel=13
```

`direction_trace.method` 会写进 journal，所以**读者能分辨**这次选板块用的是
资金流还是价格——不能只从代码判断。

### 新增测试（`tests/test_shared_concept_flow.py`，10 条）

成形/异动的算术，加上两条**防第二份实现**的：`inspect` 断言 live 的
`get_concept_ranking_fn` 调用共享函数且不再有 `gainers.sort`；
`_detect_anomalies` 不再内联任何阈值字面量。

## 验收

1. `Corpus.sector_beta` 返回非 None，且面板内**至少 5 个不同值**（已实测 6 个）；
2. `sector_scoring.FACTOR_WEIGHTS["beta"]` 的输入不再是常数默认 50；
3. `read_sector_flow("concept", D)` 对 2026-09-08..2026-09-18 返回非空；
4. 抽出的异动阈值可在 `DEFAULT_DECISION_PARAMS` 里 `grep` 到（第一步完成后）；
5. `tests/` 全绿；`lint_harness` / `lint_docs` 通过。

## 网络通道探测：CF worker 可用，东财历史不可得

### CF worker 是好的（实测）

`deploy/cloudflare/worker.js` 是一个通用 HTTP 转发（`POST {url,...}` → `{status, body}`），
`CF_WORKER_URL` 已配置。实测：

| 目标 | worker | 上游 | 说明 |
|---|---|---|---|
| `httpbin.org/get` | 200 | **200** | 477 bytes 真实响应，cf colo=`SJC` |
| `data.10jqka.com.cn/funds/gnzjl/` | 200 | **401** | 同花顺反爬页（未带 `hexin-v` 签名）|
| `push2his.eastmoney.com/...` | 200 | **520** | Cloudflare「未知错误」|
| `push2his.eastmoney.com.cn/...` | 200 | **526** | 无效 SSL 证书 |
| `push2.eastmoney.com/api/qt/clist` | 200 | **None** | 超时/拦截 |

**结论**：worker 完全正常（能取 httpbin，也真实转发了同花顺）。**是东财按来源 IP
拒绝了 Cloudflare 数据中心。** 520/526 是 CF 边缘错误码，不是东财的应用层响应。

### 本地代理的现状（2026-09-21 实测）

```
DNS: push2his.eastmoney.com -> 198.18.0.34   (Clash fake-IP)
     data.10jqka.com.cn    -> 198.18.0.33
监听端口: clash-ver 在 33331，**7890 没有监听**
系统代理设置: http/https/socks 都指向 http://127.0.0.1:7890
```

域名全部解析到 `198.18.0.x`（Clash 的 fake-IP 段），说明**流量必须经过代理**，
但配置指向的 7890 端口没有进程监听（clash-verge 实际在 33331）。所以本地直连
和走系统代理都失败。**这解释了上一轮东财请求 `ConnectionError`。**

### 可用的路，与不通的路

| 源 | 通道 | 状态 |
|---|---|---|
| 同花顺网页 `gnzjl` | 本地生成 `hexin-v` + 直连 | ✅ 已成功一次（33KB / 11 列）|
| 同花顺网页 `gnzjl` | 同上，改走 CF worker | ✅ 可行（worker 能到）|
| 同花顺客户端本地 | 离线读容器文件 | ✅ **已用于重建成分表** |
| 东财 `push2his` 历史 | 本地 / CF worker | ❌ 520 / 526 |
| 本地 `sector_flow_snapshots` | SQLite | ✅ 13 天（2026-09-08 起）|
| Tushare `concept`/`concept_cons` | API | ⚠️ 限速 1 次/小时 |

### 对「资金异动 replay」的影响

**没有好转，但也不是死路。** 长历史资金流仍只有东财一个候选，而它当前被边缘拒。
不过：

1. **同花顺网页接口走 worker 是通的**——若哪天找到一个带日期的 THS 接口，
   这条路立刻可用；
2. **CF worker 本身是可复用资产**：以后遇到「域名被本地网络挡」或「需要换出口
   IP」时，它已在位、已验证；
3. **换个思路：不需要东财。** 用新重建的**完整成分表**聚合个股资金流——
   `stock_fund_flow_daily` 有 2026-08-18 起约 21 天、每日约 5,500 行个股净额，
   按概念成分聚合即可得到概念级资金流。**窗口比 `sector_flow_snapshots` 的
   13 天更长，且不依赖任何被墙的外部接口。**

第 3 点是这一轮探索最有价值的副产品：**我们可能根本不需要东财。**

## 决策记录

- 2026-09-21：**beta 修复已交付**（d68550c）。原注释声称 as-of 重算、实际
  没传字段；这是真 bug，不是风格问题——0.40 的权重变成常数。
- 2026-09-21：**验证 `read_sector_flow` 可用但零调用者**，重建窗口与 kline
  只重叠 **9 天**。
- 2026-09-21：**建议按「可观测 → 同源 → 调参」三步走**，并明确第三步在当前
  数据下**无法通过 §6 的 n≥50 门槛**，因此不该现在做。
