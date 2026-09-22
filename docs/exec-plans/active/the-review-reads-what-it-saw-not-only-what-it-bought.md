# 复盘要读它看见的全部，不只是它买到的那几笔

## 现状

2026-01-05 起 20 天 `--autonomous` 回放（sector_first_v0，pullback）：

```
证据面在盘里      theme_opportunity_items 7,800   opportunity_items 799
                  episodes 17   episode_events 83   position_exits 10
Learn 实际读到    3 行（_closed_trades: close_date IS NOT NULL AND return_pct IS NOT NULL）
产出              learning_candidates 0   daily_lessons 0   observations 0
```

`evidence.MIN_TRADES = 4`，成交 3 笔，于是 `analyse` 返回 None，一条观察
都没写。这个拒绝本身是对的——3 笔说不了任何话。错的是**它只肯问一个
问题**：「T-1 涨幅高的那批，平仓收益是不是更好」。

同一窗口里没被问的问题，每一个都有比 3 大得多的样本：

| 问题 | 样本 |
|---|---|
| 选中的主线，在当天所有候选主线里排第几 | 20 天 × 每天 ~390 条主线 |
| 选中的票，在**同一条主线内**的其它候选里排第几 | 799 行候选 |
| 挂单要的折价，对得上当天的开盘分布吗 | 17 单 |
| 卖出价落在持有区间的什么位置 | 10 次出场 |
| 空仓那天市场在做什么 | 10 个空仓日 |

## 目标

把 Learn 从「只看成交后的盈亏」扩成一次**复盘**：选主线、选股、买入、
卖出、不作为，五段各出一个可核验的数，样本量各自申报。

## 不可越过的约束

**没有 LLM 给自己的产出打分**（`AGENTS.md` 第 2 条）。分工是死的：

- **数**由代码从行情算。分位数、折价、区间位置，全部来自 `daily_kline`
  与已落盘的 opportunity 表。
- **话**由 agent 写，且必须引用那些数。它可以说「我在弱主线上减太早」，
  但「早了多少」只能是代码算出来的百分位。
- 写入状态恒为 `observation`。晋升仍是人审的动作，`advance_candidate`
  仍然没有管线调用方。

另外两条既有规矩照旧：中位对中位（A 股截面右偏）；样本不够就申报
`too_thin`，不猜。

## 验收标准

1. `alpha_agents/evolution/review.py` 暴露 `review(as_of, ...) -> list[Finding]`，
   五个维度各一个 `Finding`，每个带 `n`、`value`、`too_thin`。
2. 在本仓库这次 20 天回放的产物上运行，`direction`、`stock`、`entry`
   三个维度 `too_thin=False`（n 分别 ≥ 20、≥ 100、≥ 15）。
3. `exit` 维度在 n=10 时按自己的下限申报，申报什么由测试钉住，不由
   实现临时决定。
4. 每个 `Finding.detail` 能列出支撑它的具体条目（日期、代码、数值），
   使 agent 写出的每一句都能被回溯到行。
5. 没有任何一个维度的 `value` 由模型输出决定——测试断言
   `review()` 全程不发起模型调用。
6. `_learn` 调用 review；回放报告新增「复盘」段，五行。
7. `uv run pytest tests/ -q`、`lint_harness.py`、`lint_docs.py` 全绿。

## 决策记录

- **为什么不直接让模型读原始表自己总结**：那正是第 2 条禁止的。模型
  读 7,800 行然后说「我主线选得不错」，这句话没有任何东西能反驳它。
  先把数算出来，模型才有可被证伪的对象。
- **为什么分位数而不是绝对收益**：当天截面就是天然的对照组。「选中的
  主线涨了 2%」没有信息，「在 390 条里排第 31%」有。
- **为什么 `exit` 维度大概率 too_thin 还要做**：成交量上来之后它是最
  重要的一段，而且 `too_thin` 的申报本身就是给下一轮的信息——它把
  「没学到」和「没发生」分开，这是 `MIN_TRADES` 注释里已经写下的原则。
