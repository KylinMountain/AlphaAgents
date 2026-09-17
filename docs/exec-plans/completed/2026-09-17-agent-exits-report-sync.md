# 三个"数字说的是另一本账"的缺陷（agent-exits 实验暴露）

状态：completed（2026-09-17）
创建：2026-09-17

## 目标

第二轮 agent-exits 实验（20 天、止损与止盈全关、agent 自主）跑完后，用
`/tmp/walkagent/memory.db` 核对报告与账本，发现三处**交易真实发生、但描述它的
数字错误或不完整**——与上一轮修掉的三个缺陷（agent 卖出没并进 fill_rows、
股数写死 None、计数器双前缀）同一条缝。本计划把它们各钉一个测试并修复。

## 三个缺陷

### 缺陷 1（P0）：减仓 13 笔全部从报告蒸发

`position_exits` 里本窗口共 **18 条腿**：全清 6 + 减仓 12（agent_trim）。
`_agent_exits` 把 trim 类 alert 计入计数器后**丢弃**，只有
`type == "agent_exit"` 进 `fill_rows`。于是：

- 报告「卖出 5 笔 66,904 元」——真实是 18 笔腿、约 20 万 元卖出额；
- 「agent 5 笔（agent 判断 5）」——agent 实际做了 18 次卖出动作；
- 减仓的股数、金额、理由**无一处落在报告文件里**。

验收：`fill_rows` 含每条 trim 腿（`side=sell`、股数、金额、`agent减仓:` 理由）；
`_summary` 的卖出笔数与 `position_exits` 行数一致；`_exit_attribution` 区分
`agent卖出` / `agent减仓`。测试：`tests/test_walk_forward.py` 增加 trim 腿
进报告的断言。

### 缺陷 2（P0）：exit 提示词的「峰值/回撤/持仓天数」全是死数据

`build_context` 渲染 `峰值{peak}%, 回撤{max(0,peak-ret)}%, 持仓{n}天`，
但回放不跑 `position_monitor.check_positions`，三列**恒为 0**。agent 拿着
"峰值 0%"做卖出决策——它声称"回撤 X%"的理由全部是它自己用浮亏推的，不是
系统喂的。这是把`--no-stop-loss`实验（用户下一轮要看"它会不会更早卖"）的地基。

验收：`decide_for_replay` 前由调用方（walk_forward）按 as-of 价格序列计算
`peak_return_pct` / `holding_days` 并写回 position dict；测试断言
`build_context` 输出含非零峰值与真实持仓天数。

### 缺陷 3（P1）：挂单预留 100,050 元成交后不归还，可用资金被永久锁死

`consume_reservation` 返回 `released_amount`（超预留额），调用方
`_fill_order` 忽略返回值；`unconsumed_total` 把 consumed 行的
`amount − consumed_amount` 永久计入。本窗口 8 笔 consumed 共锁死 **647k**，
提示词印出「可用 -41,624 元」。agent 是**按错误资产负债表决策的**。

模块注释说「超预留部分成交后归还可用资金」；测试
`test_filling_converts_the_hold_to_actual_cost` 说「仍计入、由
reconciliation 释放」——两处互相矛盾，且 reconciliation 对 reservations
**只读**，承诺从未兑现。

收口方向：**以模块注释（归还）为准**。`_fill_order` 消费预留后把超预留部分
转为 `released`（或直接按 `actual_cost` 收缩 `amount`），`unconsumed_total`
公式随之简化为 held 行全额；更新 `test_filling_converts_the_hold_to_actual_cost`
的注释与断言、`unconsumed_total` docstring、reconciliation 注释。同步修
提示词层的分母：`get_open_positions_summary` 的「已投」在 reservations
语义下包含被锁现金，口径行内说明或改用 `total - available`。

验收：一次 create→fill 后 `get_available_capital` 恢复到"持仓市值从现金里
扣除"的预期值（不再残留 100,050−actual 的锁）；`test_reservations.py`
的 fill 转换测试改为断言超额部分不占用 `unconsumed_total`。

## 决策记录

- 2026-09-17：三个缺陷由第二轮 agent-exits 实验的账本核对发现；
  缺陷 3 的口径以模块注释（成交即归还超预留）为准，测试注释是后来的、
  与实现一起漂移了。
- 2026-09-17：缺陷 1 与缺陷 2 都属于"交易/状态真实发生、描述层缺失"，
  与上一轮三缺陷同根因：**报告与提示词层没有和账本同步演进**。

## 验收结果（2026-09-17）

- 缺陷 1：`fill_rows` 收下全部卖出腿（全清 + 减仓）；同日同仓两条腿
  由 `_shares_for_exit` 的 leg-id 认领机制区分，不会重读同一条；
  `_exit_attribution` 区分 `agent 清仓` / `agent 减仓` /
  `agent 判断`。本窗口在旧代码下会印「卖出 5 笔 66,904 元」，新代码
  应为 18 笔腿、约 177,090 元。
- 缺陷 2：`_fill_replay_peak_fields` 按 as-of 收盘序列回填
  `peak_return_pct` / `holding_days`（开盘相位截止 T-1、收盘相位含当日；
  成交日自身收盘计入——T+1 正是它卖不掉的原因）。提示词的峰值/回撤/
  持仓天数不再是 0。
- 缺陷 3：`consume_reservation` 把 consumed 行收缩到实际成本，
  `unconsumed_total` 只计 held 行；一次 create→fill 后可用资金 =
  本金 − 持仓成本，不再残留每单 ~10 万的幽灵占用。
  `reservation_consumed_remaining` 在新账本上恒为 0，字段保留以对照
  旧库。
- `pytest tests/ -q`：2519 passed, 18 skipped；`lint_harness.py`、
  `lint_docs.py` 通过。
