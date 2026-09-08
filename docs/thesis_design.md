# 论点（Thesis）：持仓的单位

2026-09-08 设计。取代了同一天早些时候写的「每 5 分钟问 agent 一次要不要卖」。

## 为什么推翻上一版

上一版 `exit_decision.py` 是一个**无状态的逐轮分类器**：每 5 分钟把持仓
状态摆给 agent，问 sell / trim / hold。跑通了，产出的理由也确实可读。但抽象
是错的，三个理由：

**它没有自己推理的记忆。** 09:35 说 hold，09:40 冷启动重问，不知道自己
刚说过什么。可以反复横跳而毫无察觉，也永远说不出那句最要紧的话——
「我说过 X 发生就走，X 刚刚发生了」。

**成本与信噪比。** 48 轮 × N 个持仓 × 完整推理，而其中 46 轮的答案是
hold。实测单次决策十几秒、数千 token。更糟的是**不一致**：同样的市场，
模型这一轮多看重一句话就可能给出相反答案。

**真人不这么想。** 交易员持有的不是一个每五分钟重新评估的意见，而是一个
**计划**：「我拿着，除非主线资金转负，或者跌破 18.20。」

所以持久化的单位应该是那个计划。

## 模型

```
Thesis {
  claim            我认为会发生什么（一句话，可事后验证）
  horizon_days     给自己多久证明
  prob             agent 自己给的 0-1（不是代码从 dims_passed 推的）
  conviction       决定仓位大小
  invalidations    [结构化条件，代码每轮可判定]
  checkpoints      [每次复核的观察与结论]
  status           active → invalidated | validated | expired | blind_spot
}
```

表在 `memory_store._SCHEMA` 的 `theses`，代码在 `alpha_agents/data/thesis.py`。

### 状态的含义

| status | 何时进入 | 说明 |
|---|---|---|
| `invalidated` | 某条列出的失效条件触发 | **推理正确**，只是没赌赢 |
| `validated` | 到期且浮动 ≥ +1% | 论点兑现 |
| `expired` | 到期但浮动在 ±1% 内 | 没走出方向，不算对也不算错 |
| `blind_spot` | 仓位被风控硬线平掉，**一条列出的条件都没响** | **推理不完整**——唯一值得学的失败 |

`blind_spot` 是整个设计的产出物。它回答的是「我有多少次是因为**自己从没
想到过的原因**亏钱的」，这是唯一能泛化的失败统计。按盈亏强化学不到东西
——市场里「决策对但亏钱」和「决策错但赚钱」都太常见，20 个样本上按 P&L
强化，学到的必定是噪声。

## 失效条件必须能被代码判定

这是整个设计的承重墙。如果失效条件是自由文本，那么每轮还是得问 LLM，
只是多了一层自欺——看起来风险被考虑过了，实际没有任何东西会去检查它。

所以 `kind` 只能从一个**封闭词汇表**里选，每一条都是盘中循环已经拿到
数据、可以零成本判定的：

| kind | 判定依据 | 单位 |
|---|---|---|
| `price_below` / `price_above` | 实时价 | 元 |
| `drawdown_from_peak` | 峰值收益 − 当前收益 | % |
| `loss_exceeds` | 当前收益 | % |
| `theme_strength_below` | `theme_lines.strength` | — |
| `theme_daily_score_below` | `theme_lines.daily_score` | — |
| `theme_flow_negative` | 板块净流入 | 亿 |
| `theme_rank_worse_than` | 概念板块排名 | 名 |
| `breadth_below` | 市场涨跌比 | — |
| `no_progress_by_day` | 持仓天数 + 浮动 | 天 |
| `narrative` | **不机械判定**，见下 | — |

**词汇表和 prompt 是同一份数据。** `prompt_vocabulary()` 从 `_CONDITIONS`
渲染出给 agent 看的清单，`validate_condition()` 读同一张表做校验。加一个
kind，agent 立刻能用；agent 发明一个 kind，会被丢弃并 warn。两边不可能
漂移——有测试盯着（`test_generated_from_the_evaluator`）。

### 三条防线

1. **未知 kind 直接丢弃。** 一条读不懂的条件比没有条件更糟——它让风险
   看起来被考虑过，而实际上永远不会被检查。
2. **明显反向的阈值丢弃。** `theme_strength_below: 11` 对任何主线恒真。
   `_SANITY` 表给出每个 kind 的合理区间。
3. **数据缺失时不触发。** 板块接口这一轮挂了 → 需要它的条件跳过。
   「我没法检查」和「论点破了」是两回事，让一次抓取失败去平仓，会让
   这些检查变得看天吃饭。

### narrative 这个逃生口

有些条件真的没法机械化：「若关税豁免未续期」。硬要塞进词汇表只会逼 agent
把它编码成一个假的价格条件。所以留一个 `narrative` kind：

- 它**永远不会自己触发**
- 它把这条论点标记为「需要模型复核」，每个交易日 14:00 安排一次
- 复核写入 checkpoint，可能改变结论，但不会绕过硬线

这是诚实的做法：承认有一部分判断必须回到模型，而不是假装全都能规则化。

## 一天的流转

```
晨扫 agent ──▶ Thesis（claim / prob / horizon / invalidations）
                    │
                    ▼
              挂单（介入区间 + 止损）
                    │  价格进区间
                    ▼
              成交 ──▶ attach_position()，论点绑到仓位
                    │
                    ▼
   ┌─── 每 5 分钟：thesis_monitor.check_all() ───┐   ← 零 LLM
   │  条件触发 → 平仓，status=invalidated        │
   │  到期     → validated / expired            │
   │  narrative 到点 → 排队给模型                │
   └────────────────────────────────────────────┘
                    │
        风控硬线先动手（−8% 或主线归档）
                    │
                    ▼
        settle_orphans() → status=blind_spot
                    │
                    ▼
      复盘：校准曲线 + 盲点率 + 条件有效性 ──▶ 注回晨扫 prompt
```

**LLM 调用从 48 次/天降到 3–5 次/天**，而且每次都有一个明确的问题要答。

模型只在两种情况被叫回来（`exit_decision.run`）：

1. 某条论点有 `narrative` 条件且到了复核时段
2. 规则信号触发在一个**没有论点**的仓位上——历史仓位，或者推荐时漏写了
   invalidations

其余全部在代码里结清了，免费且每次一致。

## 校准：样本效率最高的学习信号

`alpha_agents/evolution/calibration.py`。

发现这条之前，项目里的 Brier 机制是**给错误的对象打分**的：
`confidence_to_prob()` 由代码从「交叉验证通过了几维」算出概率，agent 从头
到尾没说过一个概率，也从没看过自己的校准曲线。所以那个分数衡量的是交叉
验证器的校准度，跟 agent 的行为没有任何回路。

现在 agent 每条论点自报 `prob`。这让校准成为系统里样本效率最高的信号，
原因值得说清楚：**每一条论点都贡献一个样本，不只是赚钱平仓的那些。**
P&L 学习要几百笔才能把噪声磨掉；校准曲线在 20 条上就可读，因为「你说
70% 的那 11 条里中了 5 条」是关于这 11 条的事实，不是关于市场的推断。

注回 prompt 的三块（`inject_calibration()`）：

- **概率校准**：按 ≤55% / 55–70% / >70% 分桶（宽桶——问题是「你是不是
  系统性高估」，三个桶答得了，十个桶只会每桶两条样本）。不足 3 条的桶
  不报，一条样本上说「你说 70% 中了 0%」会教坏它。
- **盲点率**：多少笔亏损是「没有任何列出的条件触发就被硬线平掉」，附
  具体例子。分母**排除盈利单**——问的是失败的构成。
- **从未触发的条件**：写了 ≥5 次却一次没响的 kind。那不是谨慎，是阈值
  设在了价格根本不会去的地方，看着有防守其实没有。

## 仓位由 conviction 决定

`portfolio._conviction_factor()`。原来每笔都是 `TOTAL_CAPITAL * 15%`，
把交易员一半的价值扔掉了——「对的次数多」不如「对的时候仓位大」。

`conviction` 从 agent 自报的 `prob` 推导（`(prob - 0.5) * 2`），映射到
per-stock 上限的 `[0.45, 1.0]`。下限不是 0：一个值得开仓的想法，仓位小到
产出读不出结论，学习层就白学了。

没有论点的仓位返回 1.0，行为与改造前完全一致。

## 尚未做的

- **trim 只能全平。** `close_position` 没有部分平仓路径，agent 说减仓时
  按全平记录，理由保留。要真做需要在 `close_position` 里拆 shares。
- **累计强度没有衰减。** 新的每日累加规则下，+2/+2/+2 之后连续持平两周，
  强度还停在 6。持平不是确认。旧版因为瞬间饱和看不出来。
- **「今天不出手」还不是一个选项。** 晨扫每天都必须产出推荐。不能拒绝
  出手的交易员，在组合层面没有风险管理。
- **闭环还没转过一整圈。** 见 README 的状态表——`theses` 表刚建，样本 0。

## 相关文件

| 文件 | 作用 |
|---|---|
| `alpha_agents/data/thesis.py` | 模型、词汇表、判定引擎、持久化 |
| `alpha_agents/pipeline/tasks/thesis_monitor.py` | 每轮判定、到期结算、盲点识别 |
| `alpha_agents/pipeline/tasks/exit_decision.py` | 只处理需要判断的两类 |
| `alpha_agents/evolution/calibration.py` | 校准曲线、盲点率、条件有效性 |
| `alpha_agents/prompts/morning_scan.md` | 论点字段的写作要求 |
| `tests/test_thesis.py` | 词汇表与判定的边界，尤其是各种「该被丢弃」 |
| `tests/test_thesis_monitor.py` | 触发、到期、盲点 |
| `tests/test_calibration.py` | 分桶、样本下限、盲点分母 |
