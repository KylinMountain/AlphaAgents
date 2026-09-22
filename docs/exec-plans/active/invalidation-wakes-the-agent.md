# 失效条件唤醒 agent，而不是替它平仓

2026-09-22。用户原话：「这个什么 down from peak 8% 有点无稽之谈，不应该
是 agent 自己看这只股票买入后的情况吗？然后再看这个股票是否应该继续持有
吗？」

## 他是对的，而且比阈值本身的问题更根本

`thesis_monitor.py:117`：

```python
fired = T.evaluate(th.conditions, mv)
if fired:
    reason = f"论点失效: {T.describe(fired)}"
    if close_position(pos["id"], close_price=price, close_reason=reason):
```

条件触发**直接平仓**。agent 在买入当天写了一个数，第五天代码拿这个数把
仓位砍掉，中间它没有再看过这只股票。这与 `autonomous-trader-no-mechanical
-rails.md` 拆掉的机械止损是同一个东西——只是常数的选择权从人换成了 agent，
执行依然是机械的。

## 但不能因此删掉失效条件

如果 agent 每天重新看一遍再决定，中间没有任何事先承诺，就没有任何东西可
以被证伪：它可以一直给亏损仓位找理由，复盘时无法区分判断对了还是运气好。
`docs/GOLDEN_PRINCIPLES.md` 的可归因要求靠的正是事先声明。

**两件事被我混成了一件：**

| | 职责 | 正确用法 |
|---|---|---|
| thesis 的 `invalidations` | 事先声明什么算我错了，供学习归因 | 触发时**唤醒 agent**，带上证据 |
| `exit_decision` | 现在看这只票该不该继续持有 | agent 决定 sell/trim/hold，写理由 |

## 要接的东西已经存在

`exit_decision.build_context` 给 agent 的每一行本来就是：

```
● 002916 深南电路 1000股 成本45.20 现价48.30 浮动+6.86%（峰值+15.2%，回撤8.3%） 持仓5天
  买入理由: ...
  主线: ...
  规则信号: 无          ← 触发该落在这里
```

峰值、回撤、持仓天数、买入理由、主线状态——正是用户说的「买入后的情况」。
L1 设计也写明「其余触发变成 `type="signal"` 递给 agent」。是
`thesis_monitor` 绕过了它。

## 做什么

1. `check_all` 触发时**不平仓**：写一条 `add_checkpoint(verdict=TRIGGERED)`
   并产出 `{"type": "signal", ...}`，理由里带上 agent 自己当初的原话
   （`Condition.note`）与当前证据。
2. 重复触发**不压制**。第 3 次越过自己写的线、前两次都选择持有，这个事实
   本身就该摆在 agent 面前，所以信号里带上已触发次数与上次的裁决。
3. 只有 agent 决定卖出时才 `T.close(..., INVALIDATED, close_kind=<fired>)`。
   选择持有则 thesis 保持 active，checkpoint 留下这次覆盖。

## 学习信号因此变强，不是变弱

现在拿不到、改完能拿到的三个维度：

- agent 当初定的阈值好不好；
- 触发时它**遵守还是推翻**了自己的承诺；
- 推翻之后结果如何。

「它说 8% 认错，真到 8% 时选择继续持有，随后又跌 6%」——这是能学的东西。
当前设计做不出来，因为仓位在 agent 开口前就没了。

顺带卸掉阈值的压力：阈值定得差，代价是多一次唤醒，不是一次被迫平仓。

## 验收标准

1. 一次 `--autonomous` 回放中，`close_kind` 非空的 thesis **全部**有一条
   agent 的 `close_reason`；不存在「条件平仓、agent 无发言」的记录。
2. 至少一条 thesis 出现 `verdict=triggered` 后 agent 选择持有，且该
   checkpoint 可从库中查到（证明覆盖被记录，而不是被吞掉）。
3. `规则信号:` 一行在 agent 的上下文中出现过真实内容，不恒为「无」。
4. `uv run pytest tests/ -q` 全绿；两个 lint 通过。

## 决策日志

- 2026-09-22 保留失效条件，只改它的作用：从执行改为唤醒。删掉它会同时
  删掉事先承诺，而那是学习可归因的来源。
- 2026-09-22 不压制重复触发：重复本身是信号，压制它等于对 agent 隐瞒它
  自己的历史。
