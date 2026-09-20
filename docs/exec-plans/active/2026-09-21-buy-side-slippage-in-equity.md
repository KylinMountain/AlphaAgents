# 建仓当日的净值要把买入腿滑点扣掉

状态：active
创建：2026-09-21
来源：remaining-seven ⑦。原计划「与 ② 同期做」，触发条件是
「当比较两臂的收益差（而不是行为差）时」。**该条件现已满足**：
`sector_experiment_compare.py:105` 与 `selection_experiment_compare.py:202`
都通过 `performance.equity_metrics` 读 `equity.csv` 的**日收益**来比较两臂，
5bps 会在配对样本里累积。

## 问题（已实测）

买入腿滑点被**记录**了，但从未被**扣款**。

实测（`/tmp` 副本，真成交）：30,000 元名义金额、5bps 买入滑点：

```
shares=3000 open_price=10.0
notional              = 30000.00
notional*(1+5bps)     = 30015.00   <- 预留与 reconciliation 都按这个数
cash that left        = 30000.00   <- 实际只扣了 30000
```

机制：`_fill_order` 用 `actual_cost = shares * fill_price * (1 + SLIPPAGE_RATE)`
去 `consume_reservation`，`reconciliation._check_consumed_amount` 也按同一个
公式校验 `consumed_amount`（`reconciliation.py:278`）——**两者都认为花了 30,015**。
但 `get_available_capital` 的恒等式读的是 `get_invested_capital()`，
而后者是 `SUM(open_price * shares)`（`portfolio.py:270`），**不含滑点**；
`consumed` 行在 `unconsumed_total` 里又计 0。于是这 15 元在恒等式里凭空消失。

后果：**持仓未平期间净值被高估**恰好一个买入腿滑点。平仓后总收益是对的
（`_estimate_net_close_result` 的 `cost_basis` 含买入滑点），所以这不是
「总账错了」，而是「持仓期间错了」——而两臂比较读的正是逐日净值。

## 验收（机器可查）

1. 一笔 10.00 × 1000 股的成交，`get_available_capital` 相对本金减少
   `1000 × 10.00 × (1 + SLIPPAGE_RATE)`（含滑点），不是 `1000 × 10.00`；
2. `get_total_capital == available + invested + unconsumed` 恒等式仍成立；
3. 同一笔仓位在**平仓前后**的 `equity` 连续：平仓不产生额外的跳跃，
   即「持仓期已扣的滑点」与「平仓时 realized 里的滑点」不重复计一次；
4. 一个完整往返（10.00 买、10.00 卖）的期末净值与修复前**相同**
   （总账不变），只有持仓期间的每日净值改变；
5. `tests/` 全绿；`lint_harness` / `lint_docs` 通过。

## 实施结果（2026-09-21）

修了**三处**，缺一处都不成立——这是实现中最重要的发现：

1. `portfolio.position_cost_basis(open_price, shares)`：新的单一成本基础定义
   （`shares × price × (1 + slip)`）。`open_price` 继续存**原始成交价**，
   因为 `reconciliation` 与 `settlement` 都按「原始价 + 独立滑点」解释它，
   改存储值会让那两处再乘一次。
2. `get_invested_capital` 改读成本基础（SQL 里乘 `1 + SLIPPAGE_RATE`）。
3. **`walk_forward._value` 的 equity 公式**：从 `total + unrealized` 改成
   `cash + market_value`，且 `unrealized` 改用成本基础。

**第 3 处是只改 `invested` 不会发现的那处。** 实测（`/tmp`，3000 股 @10.00）：

```
真实：cash 969,985.00  equity@平价 999,985.00
只改 invested ：available=969,985.00  equity=1,000,000.00  <- 仍错
两处都改     ：available=969,985.00  equity=999,985.00    <- 对
```

原因：`total = available + invested + reservations` 已经**扣掉**了滑点，
而 `unrealized = (price − open_price) × shares` 又把同一笔**加回来**，两者抵消，
仓位看起来是免费的。`cash + market_value` 形式不可能抵消，因为它从不减去
一个已经扣过的成本基础。`portfolio_risk.record_equity_mark` 用的是同一个
`cash + market_value` 形式，本来就没这个问题——加注释说明为什么。

**改掉的旧契约（两处测试原本钉住缺陷）**：

- `test_reservations.py` 的注释原文写着「``invested`` reads ``open_price * shares``
  without the slippage leg」——那正是缺陷本身，已改写为成本基础；
- `test_trader_ledger.py` 断言 `trader_capital() - 30000`，同样改为基础。

**新增回归测试**（`test_cash_settlement_semantics.py`，3 条）：
成本基础定义、恒等式仍成立、以及「平价持仓必须低于开仓前本金」——
后者在修复前会失败，因为开仓被算成免费。**已证明红**：把 `invested` 的
滑点因子换成 `1.0`，3 条中 2 条失败（第 3 条测的是纯函数，不受影响），恢复后全绿。

**验证**：`pytest tests/ -q` → **2990 passed, 18 skipped**；
`lint_harness` 222 文件通过；`lint_docs` 通过。

## 决策记录

- 2026-09-21：**改成本基础，不改 `open_price` 的存储值**。`open_price` 是
  成交价，`reconciliation` 与 `settlement` 都按「原始价 + 独立滑点」解释它；
  把它改成含滑点的价会让那两处**再乘一次**滑点。所以新增一个
  `position_cost_basis(open_price, shares)`，让 `invested` 与浮盈读同一个函数，
  原始价继续存原始值。