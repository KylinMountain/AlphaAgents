# 自改进路线图

来自 2026-09 的两项调研：`docs/strategy_evaluation_2026-09.md`（策略侧）
与 RSI（Recursive Self-Improvement）文献审计。本文只列**要做什么、为什么、
怎么验收**，不含实现细节。

## 出发点

系统当前的"学习"是 (a) 类：把文本累积起来、再注入 prompt。这类机制在
2025-26 已有多份独立证据显示**普遍不产生可测提升**（MemDelta 加控制变量后
多数记忆系统打不过 trivial baseline；SEAGym 显示频繁更新常在留出集上零提升；
EvoAgentBench 结论是"没有任何现有自动方法能在所有设置下维持正增益"）。

分界线不是"有没有梯度"，而是**提交更新前有没有一个 agent 无法自己写的数字
在把关**。目标是把关键环节从 (a) 迁到 (b)。

## G1 — 把 reward 从累计收益换成 Brier + 因子残差

**为什么**：`SE(SR) ≈ √[(K + SR²/2)/T]`。年化 Sharpe 1.0 要达到 t=2 需约
**4 年**实盘。**累计收益永远无法在几个月内评估一次 playbook 改动**；Brier
和归因残差可以，几百个交易日就有功效。

另一半理由来自 KTD-Fin（CSI300，548 个交易日，10 个前沿模型）：全部模型
从被动市场+风格拿到 +11%~+29%，而 **selection alpha 只有 1 个接近 0，其余
9 个全负**。不做因子中性化，学到的就是 beta。

**做什么**
1. 预测时**强制输出概率** —— "5 日相对基准超额为正的概率 p"，而非二元看多/看空
2. 核验时算两个数：
   - **Brier / log score**（proper scoring rule，有界）
   - **因子残差 alpha** —— 对动量/波动/流动性/反转/市值/行业做日度截面回归取残差

**验收**：`predictions` 表有 `prob` 与 `brier`、`residual_alpha` 三列并被
填充；命中率报表改为报 Brier 与残差，而非绝对涨跌。

## G2 — playbook 变更加留出集 keep-better 门

**为什么**：这是把 (a) 变成 (b) 的最短路径，也是单点收益最大的改动。
对照证据：Dynamic Cheatsheet 在 ALFWorld 拿 70.7%，却在 WebShop 崩到 0.14
（ReAct 0.43）——**无门控的上下文进化是高方差且不安全的**。

**做什么**
1. 历史交易日切成 train / **frozen validation**，validation 严格晚于 train
   且**永不参与反思**
2. 每次 playbook 变更是一个**候选版本**，只有在 validation 上 Brier
   **不退化**（配对检验，不是比均值）才提交，否则回滚
3. champion/challenger 影子模式：challenger 照常出预测并记录，但不驱动任何
   决策，直到通过门

**验收**：存在 `playbook_versions` 记录与门控结果；被拒的候选有留痕。

## G3 — 禁止 LLM 给自己的记忆打分

**为什么**：Echo Gap 量化——agent 把**自己错误的**记忆判为正确的概率：
Claude Haiku 4.5 **31%**、GPT-5.4 **41%**、mini **54%**。BIRD 上自评分吃掉
了记忆收益的约 2.9 个点。理论结论：修正需满足 Error-Independence，
**换更强的裁判模型不满足**（残差误差相关 ≥+0.30），只有检索/执行接地的验证
满足（+0.05）。

当前 `lessons.py` 正是用第二次 LLM 调用来 create/reinforce/weaken principle
——教科书式的 Echo Gap。

**做什么**
1. 记忆条目的效用分**只能来自事后市场核验**（Brier / 残差 alpha），不能来自
   任何 LLM 判断
2. 条目带 provenance + 时间戳 + 命中次数 + 累计得分
3. 得分低于阈值**自动退休**；得分随时间指数衰减（对付非平稳）

**验收**：`trading_principles.win_rate` 由数据填充而非恒为 NULL；weaken 由
数据触发。

## G4 — 条目级增量更新 + 容量上限

**为什么**：ACE 的核心是**增量 delta 而非整体重写**，专治 brevity bias 与
context collapse。另有形式化结果（arXiv:2510.04399）：自改进保持 PAC 可学习
**当且仅当策略可达的模型容量一致有界**——playbook 无限增长是其反例。

**做什么**：playbook 用 bullet delta 追加/失效，禁止整段重写；设硬条数上限，
超限时按 G3 的得分淘汰。

**验收**：playbook 条数有上限且触发淘汰；无整体重写路径。

## G5 — judge 必须先独立作答再评分

**为什么**：self-play 实验里 judge 通过率 0.72→0.94 而真实准确率恒定 0.20；
三家 judge 集成仍接受 55% 的作弊答案。唯一有效修复是**强制 judge 先独立给出
自己的答案再评分**，FPR 0.72 → **0.012**。

**做什么**：所有 LLM-as-judge 调用点改为两段式。

**验收**：无裸评分调用。

## G6 — 信号存档（前置依赖）

**为什么**：`docs/strategy_evaluation_2026-09.md` 的最大空白——入场端无法
评估，因为历史推荐没有存档。G1/G2 都依赖它。

**做什么**：预测落库时带齐可回测的上下文（时点、可见信息窗、特征、基准）。
新闻侧已由 `news_ingest` + `news_items` 解决，个股侧待补。

**验收**：能对任意历史日重放出"当时可见的信息 → 当时的推荐"。

## 明确暂缓

- **权重更新 / SEAL / 在线 RL** —— 样本量差 1-2 个数量级；且边跑边改 reward
  会破坏 off-policy 平稳性假设并污染 replay buffer（arXiv:2607.04470）
- **多 agent 辩论投票** —— DPO 微调后 agent 间误差相关 ρ=0.70，
  **10 个对齐 agent ≈ 1.4 个独立预测者**，10-agent 市场准确率 67.6% 反而低于
  单 agent 70.2%
- **Voyager 式技能库** —— 交易域没有可执行 pass/fail 的等价物

## 不作为设计依据的数字

FinMem / TradingAgents / FinCon / QuantAgent / FinAgent 的公开收益全部不可
采信。FINSABER（KDD 2026）把评测期**仅延长两个月**，FinMem 的 MSFT 从
CR +23.261% / Sharpe 1.440 变成 **−22.036% / −1.247**。The Alpha Illusion
重跑 TradingAgents：毛 +2.31% → **净 −1.74%**，55 个 ticker 里 44 个
buy-and-hold 净收益更高。Agentic Trading 综述筛 77 篇后：仅 2/19 有时间一致
的训练/测试划分，15/19 完全不可复现。
