# 论点到期不平仓：到期与失效一样，只唤醒交易员

状态：active（horizon 部分已随 #54 合入；打分修复见追加节）
创建：2026-09-26
上游：[无止损止盈](2026-09-26-no-system-exit-lines.md) · [Runtime 收尾](2026-09-26-trader-runtime-completion.md)

## 为什么

用户 2026-09-26，验证交接文档 V3 判据评审时指出：`thesis_monitor.check_all`
在 `holding_days >= horizon_days` 时仍由代码平仓（"论点兑现/到期未兑现"），
不受 `hard_only` 门控。这与已定的方向冲突：

- [无止损止盈](2026-09-26-no-system-exit-lines.md)："持有期上限、失效条件照常计算，
  **只作为信号**唤醒交易员，不平仓"。失效条件在 `invalidation-wakes-the-agent`
  里已改为唤醒（`7236a0c` 之前的 `606a200` 系列），但**到期**这一条漏了——
  它是同一个论点的另一半：失效是"提前知道自己错了"，到期是"承认到期前没对"。
  两者都该由交易员回答，而不是由到期日替它答。
- 实盘 9 个持仓中 8 个绑着 2026-09-23/24 创建的 horizon=3 天活跃 thesis。周一
  （09-28）持仓天数即为 4 ≥ 3，8 笔会被代码平仓，理由串是"论点兑现（3天期限）"，
  不经过交易员。V3 判据（每笔平仓都来自交易员决策）会直接失败。
- 到期平仓的结果衡量的是那个 horizon 数字，不是交易员的判断；学习不可归因——
  与"agent 自挂止损照单执行"被纠正的理由完全相同。

设计意图辨析：`test_a_fired_condition_defers_the_horizon_close` 钉住了
"失效触发则同轮不得平仓"，却仍平"到期"的仓。本计划把到期与失效对齐。

## 交付

- `thesis_monitor.check_all`：到期不再调用 `close_position`。改为发 `signal`
  （`type="signal"`，带 `horizon_due` 标记、thesis id、当前浮盈与持仓天数），
  走 book_manager 已有的 `pos_alerts` → `exit_decision.run` 管道，交易员被问，
  答什么封什么。thesis 本身**保持 active**，不加 checkpoint、不改状态——它是否
  兑现由复盘与行情判定去记，不由平仓动作记。
- `exit_decision` 的候选逻辑：带 `horizon_due` 信号的位置必须被问到模型，即使
  它有 active thesis（现状是 `signal_codes and not get_active(...)` 才进候选——
  有 thesis 的位置此前靠失效唤醒；到期唤醒必须同样能穿过这个条件）。
- 报告计数：`thesis_closed_on_horizon` 保留但恒为 0 也可删；新增
  `thesis_horizon_wakes`（发出过的到期唤醒数）。`walk_forward` 出场归因里
  到期平仓不再是机械来源。
- 文档：`TRADER_CORE_DESIGN` 与 IMPLEMENTATION 相应句子（若有"到期平仓"表述）
  同步；交接文档 V3 判据补一句"thesis 到期唤醒后交易员 HOLD 是正确结果"。

## 验收（机器可检查）

- [ ] `tests/test_thesis_monitor.py`：到期且无失效触发 → 不平仓、`signals` 含
  `horizon_due` 唤醒、thesis 仍 active；有失效触发 → 仍只发失效唤醒（不叠加到期）。
- [ ] `tests/test_thesis_monitor.py` 原"到期平仓"三个测试
  （`played_out_is_validated`、`went_nowhere_expires`、`fired_condition_defers`）
  改写为"到期唤醒"断言；`close_position` 在 `check_all` 全路径断言不被调用。
- [ ] `tests/test_exit_decision*.py`：`horizon_due` 信号使带 active thesis 的
  位置进入模型候选；无信号、有 thesis 的位置不进（不回归现状）。
- [ ] `tests/test_no_system_exit_lines.py` 增一条：到期当天，持有不被卖出，
  交易员被询问（或其上下文含到期信号）。
- [ ] 全量 `pytest`、`lint_harness`、`lint_docs` 通过。
- [ ] 4 日回放（`--decider llm`）报告：出场归因"机械 0 笔"；若期间有到期，则
  `thesis_horizon_wakes` > 0 且对应持仓仍由 agent 决定（HOLD 或卖出都算正确）。

## 决策记录

- 2026-09-26：thesis 到期与失效同权——都是"该交易员回答的问题"，不是"代码替它
  执行的价位或日历"。用户的原话方向："让 agent 自己学"；到期平仓让结果衡量的是
  horizon 常数。
- 2026-09-26：不改 thesis 状态、不加自动 checkpoint。到期是否"兑现"由行情判定
  （C2 的 5 日前瞻）与复盘记账，不由平仓事件定义——否则又造出一个由代码写的结论。
- 2026-09-26：`settle_orphans` 的 blind_spot 语义不受影响（它只认"仓位没了"）。


## 追加：回放行情打分为零的根因与修复（2026-09-26）

horizon30 回放（30/30 完整）的"行情判定"栏为空：505 个可判决策一条未写。
根因不在数据：离线对同一库跑 `decision_outcomes.grade()` 505 条全部成功。
真正根因是 `grade()` 的行情辅助函数按列名读行（`row["date"]`），需要
`sqlite3.Row`；而两个调用方（实盘 `close_review.py:198` 与回放
`walk_forward.py:1483`）都是裸 `sqlite3.connect`，默认元组工厂——第一次
`_sessions_from` 抛 TypeError，被 `close_day` 的 except 吃掉，计为 pending，
当天学习归零，日志只有一行 warning。

**这不止是回放的 bug**：实盘第一次 `close_review`（周一 19:00）也会同样静默归零。

修复：`grade()` 自己设 `hist.row_factory = sqlite3.Row`，不再信任开连接的一方。
回归测试 `test_grading_works_with_a_bare_connection`（已验证：去掉修复行该测试失败）。

### horizon30-20260105 打分结果（修复后离线重算，505 判定 / 60 未闭合）

| 动作 | right | wrong | flat | 判对率 | 中位超额 |
|---|---|---|---|---|---|
| buy | 17 | 22 | 0 | 44% | −1.22pp |
| hold | 59 | 64 | 8 | 48% | −0.17pp |
| reduce | 27 | 22 | 2 | 55% | −1.11pp |
| reject | 99 | 71 | 10 | 58% | −3.11pp |
| sell | 18 | 14 | 2 | 56% | −0.97pp |
| wait | 35 | 33 | 2 | 51% | −0.47pp |

读法：买入判对率 <50% 且 reject 的中位超额 −3.11pp（拒掉的票后来跑赢大盘 3.1pp）
——在 +6.8% 的窗口里大量拒绝/等待就是超额 −7pp 的直接来源，学习信号清晰。
格子已聚合（如 reject·off_5d_high+run_up_5d n=36），满足 Lesson n≥10 门槛。
**限定**：同窗口决策共享交易日，n 不是独立样本；单窗口，不是前向验证。
