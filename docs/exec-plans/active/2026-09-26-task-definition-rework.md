# 任务定义重修：面板多来源、市场状态注入、打分尺度分格、仓位意识

状态：active
创建：2026-09-26
上游：[horizon 唤醒](2026-09-26-thesis-horizon-wakes-the-agent.md) · [Runtime 收尾](2026-09-26-trader-runtime-completion.md)

## 为什么（两条独立轨迹的归因结论）

两次 30 天回放（horizon30 / tle30，同窗口 2026-01-05→02-13，独立模型采样）给出
一致的结构性归因，**瓶颈在喂给 agent 的任务定义，不在 agent**：

| 证据 | horizon30 | tle30 | 复现 |
|---|---|---|---|
| 买入 T-1 涨幅≥7% 占比 | 65% | 65% | ✓ |
| buy 判对率 | 44% | 48% | ✓ |
| 持仓中位 | 2 天 | 1 天 | ✓ |
| 资金利用率均值 | 6.6% | 3.0% | ✓ |
| 超额 | −7.0pp | −7.6pp | ✓ |

四个缺口，按预期影响排序：

1. **面板结构上只有追涨菜。** `dual_rank_v0` 的池 = T-1 涨幅榜 ∪ 换手榜
   （`_legacy_pool_rows` 注释自认"the ranking, not the model, chose the
   strategy"）。菜单里没有"跌了但企稳""资金持续流入""横盘突破"的票。
   它 65% 买 T-1≥7% 不是它的选择，是菜单的选择。
2. **无市场状态输入。** capability matrix：`market_regime 0.0%`——决策时
   不知道今天大盘/情绪在什么位置，于是普涨日 +6.8% 里 reject 205 次（判对
   64%，拒对了个体，错杀了行情）。
3. **打分尺度 vs 持仓尺度错配。** 持仓 1-2 天的超短交易者被 5 日前瞻打分。
   它卖出的票在"卖日至后 5 日"区间中位位置 34.1 百分位（卖在低位），但用
   它自己的 1 日动量逻辑看多数卖对了。sell 判对率 42-56% 漂移，无法归因。
4. **仓位无意识。** 利用率 3-6%，复盘维度里没有"利用率"这个概念，agent
   不知道自己 100 万本金每天只用 3 万。收益杠杆在仓位不在选股。

## 交付

### C1 面板多来源（replay `dual_rank_v0` + 新 `multi_source_v1`）

- `_build_panel` 增加第三、四条 lane：**跌幅企稳**（T-1 跌 2-6%、收盘价
  站上日内均价、换手>3%）与**资金流入**（fund_flow 净流入为正、非涨停、
  非暴跌），与现有 change/turnover lane 轮转混选。
- capability：fund_flow 100% PIT 已可用；跌停池/ST 过滤沿用现有 eligibility。
- 计数器：`panel_lane:{change,turnover,stabilizer,inflow}` 各 lane 实际入选数。
- 保留 `dual_rank_v0` 不动（preregistered 对照的 A 臂）。

### C2 市场状态注入决策面板

- 每日一行市场事实进 prompt：上证/深成/创业板涨跌、涨跌家数比、涨停/跌停数、
  5 日市场宽度趋势。全部来自日线/limit_pool，PIT 可回放。
- `market_regime` capability 从 0% 变为有值。

### C3 打分按 decision_horizon 分格

- `decision_outcomes` 的格子键已含 `decision_horizon`（1d/3-5d/position）。
  补齐：agent 写的 `decision_horizon` 必须真实透传（抽查 tle30 里是否全是
  默认值）；1d 格用次一日收盘结算，5d 格不变。
- 复盘的 exit 段按对应 horizon 的区间位置报告，不再只报 5 日窗口位置。

### C4 仓位意识进复盘

- 复盘报告加一行：本周平均资金利用率、最高利用率、空仓日占比、
  "利用率 <10% 的天数"。
- 晨间决策上下文里，让 agent 看到自己昨日的利用率与可用资金。
- 不改任何代码强制的仓位线（sizing is the agent's）。

## 验收（机器可检查）

- [ ] `multi_source_v1` 回放 30 天：`panel_lane:stabilizer` 与
  `panel_lane:inflow` 计数 > 0，买入中 T-1 涨幅≥7% 占比 < 40%（对照 65%）。
- [ ] `market_regime` capability ≥ 有值；普涨日（宽度>70%）买入数显著高于
  普跌日（对照：现在无差异）。
- [ ] `decision_horizon=1d` 的决策在 outcomes 中存在且 verdict 与 5d 格分离。
- [ ] 复盘含利用率行；30 天回放的"利用率 <10% 天数"被报告。
- [ ] 全量 pytest、lint_harness、lint_docs 通过。
- [ ] 对照验收：`dual_rank_v0`（A 臂）与 `multi_source_v1`（B 臂）同窗口
  各跑 30 天，比较 buy 判对率、超额、利用率。**n=1 窗口，只作方向性证据。**

## 决策记录

- 2026-09-26：先修任务定义，不动策略逻辑本身——两次轨迹的归因都指向菜单
  与信息集，不是推理能力。改菜单让学习有新东西可学。
- 2026-09-26：打分分格是前提不是优化——不同尺度的决策混在一个格子里，
  Lesson/Rule 的证据是脏的。

## 重要更正（2026-09-26 晚，用户指出选股架构历史）

初版 C1 写"面板只有追涨菜"并把多来源面板当成新设计——这是误诊。核对
git 历史（`e7efe1d`）与会话记录后的事实：

1. **change/turnover 交替抽取（`_legacy_lane_mix`）确实是用户 2026-09-21
   明确要求删除的旧策略**（"The owner asked for the concept-sector selection
   logic and for change/turnover to go"）。它已从所有交易路径删除，只在
   walk_forward 里内联保留为 **preregistered 对照臂（A 臂）的冻结基线**，
   防止对照漂移。这不是回退，是刻意保留的控制组。
2. **新的选股架构早已建成并比较过**：`sector_first_v0`（PIT 板块快照 →
   方向卡 → sector selector 选 0-3 个方向 → 方向内多因子选股 → 交易计划），
   加上新闻窗口。上个会话（67ace1a4）7 轮实验全部跑在 sector_first_v0 上
   （92 次调用），包括赚钱的 news 轮（+4.66%）。
3. **这两天我的 tle30/horizon30 跑的是默认值 `dual_rank_v0`**——因为我没传
   `--selection-architecture`。这才是"为什么又是 change/turnover"的真正原因：
   是我跑错了臂，不是代码回退。

### 修正后的计划

> C1 已由 `2026-09-26-sector-first-only.md` 落地：非 sector 路径删除，不带参数即 sector_first_v0。


- **C1 改为**：不再发明"多来源面板"。改跑 `--selection-architecture
  sector_first_v0 --allow-current-membership`（或补建 PIT membership archive
  以去掉 allow-current），把新代码的打分/学习闭环接到真正的目标策略上。
  追涨 65% 的画像极可能是 dual_rank 对照臂的特征，不是 sector-first 的。
- **C1b（新增）**：核对 sector_first 路径的 news 流是否已接（`_news_window`
  存在，selector 接收 news 参数），确认 tle30 同参数下新闻确实进了选股上下文。
- C2/C3/C4 不变（市场状态、打分分格、仓位意识对任何架构都成立）。
- A/B 验收改为：**sector_first_v0（B 臂，目标策略）vs dual_rank_v0（A 臂，
  冻结对照）**——这正是 PR-04..RP-10 已经预注册过的比较，不是新实验。
