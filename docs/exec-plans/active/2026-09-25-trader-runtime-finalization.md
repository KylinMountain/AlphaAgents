# Trader Runtime 最终闭环与 30 天回放验收

状态：active
创建：2026-09-25

## 目标

将已经分开的 T5（持仓动作）、T7（证据分层学习）和 T8（历史回放）收敛到
同一条连续 Trader Runtime 路径。历史回放中每个策略决策必须在执行前封存，模拟成交
必须回写为同一 TraderState 的持仓事实，收盘复盘只产生隔离候选，后续会话只读取此前
已物化且范围匹配的学习。完成后，以隔离目录运行连续 30 个交易日的 replay，不写生产
账本或语料库，并保留可复核报告。

## 验收

- [x] T5、T7 均以合入后的 `main` 为基线；`uv run pytest tests/ -q`、
  `uv run python scripts/lint_harness.py` 和 `uv run python scripts/lint_docs.py` 均退出 0。
- [x] T8 测试证明：历史 BUY 的模拟成交产生 `POSITION_CHANGED` 或等价不可变事实，
  同一 `run_id` 的下一会话能据此作 HOLD / REDUCE / SELL，且决策在执行前已经封存。
- [x] T8 测试证明：收盘 review 产生 LessonCandidate；只有此前已物化、时间尺度、
  horizon、evidence scope 与 run/trader 都匹配的重复证据可在之后注入 Lesson / Rule。
- [x] 最终 T8 分支只包含 T8 的提交，不携带旧 T6/T7 实现；不 force push，采用基于
  合入后 `main` 的新分支与替代 PR。
- [x] 使用 `scripts/walk_bootstrap.py` 创建隔离目录后，运行连续 30 个交易日的
  `scripts/walk_forward.py` 并以退出 0、`corpus_read_only: true`、
  `production_db_unchanged: true` 和报告文件存在为成功判据。
- [x] `TRADER_CORE_IMPLEMENTATION.md` 与本计划更新实际完成状态、运行命令和结果；
  不将未执行的生产晋升或无样本的策略有效性写成已验证。

## 决策记录

- 2026-09-25：#47 以旧 T7 `c769099` 为 base，不能直接视为已兼容当前 T4/T6 或 #49。
  选择从合入 T5/T7 后的 `main` 新建 T8 分支，只移植 7 个 T8 提交；拒绝 rebase 后
  force push，符合仓库禁令并避免旧实现混入最终 PR。
- 2026-09-25：不另造 replay 持仓模型；优先把现有 T5 `POSITION_CHANGED` 事实接口接入
  historical adapter。这样 live 与 replay 保持同一状态机与决策词汇。
- 2026-09-25：把执行账本到 Runtime 的转换放在 `evolution/trader_positions.py`。它消费
  storage 边界已经整形的持仓字典，复用 live `exit_decision` 的同一事实转换，避免让纯
  `trader/` 层依赖 portfolio 的存储形状。
- 2026-09-25：`--no-trader-tools` 时，Planner 提示词不得保留工具清单；否则模型会输出
  不能执行的工具文本而不是 JSON。修复是收紧输入契约，不把推理文本宽松解析为交易。
- 2026-09-25：CLI 的 `--model-timeout` 必须约束 Planner 与三类日终 Review 的整次
  Agent 运行；网络客户端超时本身不足以防止 Runner 挂住。
- 2026-09-26：一次真实 30 日录制证明 Planner 的 JSON 是局部而非全有或全无的契约：一条
  无法执行的 WAIT（`net_flow`）或面板外 REJECT 不得抹掉同一回复中有效的 BUY / WAIT /
  REJECT。坏项会成为带原因的 `refused` 记录；整个集合不是列表时仍硬失败。同步移除了
  prompt 示例中与实际 WAIT metric 白名单矛盾的 `net_flow`。
- 2026-09-26：第二次隔离录制在 2026-01-20 发现收盘 Market Review 的长文本中出现未转义
  英文引号，使 JSON 严格解码失败。使用项目已声明的 `json-repair` 仅修复已定位的 JSON
  对象，再沿用原有对象/字段白名单过滤；普通文本、数组和未知字段仍不能写入复盘。提示词
  同时明确要求转义英文引号或使用中文引号。
- 2026-09-26：第三次录制在 2026-01-13 发现模型偶尔会先写工作草稿/说明、再给出最终 JSON。
  解析器改为扫描完整的顶层对象，并选取最后一个具有 `boards` 列表的对象；不会再把多段对象
  从第一个花括号到最后一个花括号拼接。每个候选对象仍先严格 JSON 解码，必要时才在该对象内
  修复，随后经过既有字段白名单。

## 本次验证记录

- 已通过（修复前基线）：隔离目录
  `/private/var/folders/x5/cdm2lfb11p9_vkm_3z_thlj00000gn/T/alphaagents-t8-30d.EVvXemeyDi/replay`
  实跑 `2026-01-05 → 2026-02-13` 共 30 日，退出 0；30 次 market review、34 次 trade
  review、17 条 observation，`trader_runtime_decisions=294`、`watch_rechecks=22`。报告确认
  生产库 hash 未变、回放语料只读、抛错日阶段为 0。
- 该基线**不作为最终验收**：它记录到两次 `decider_unreadable`。真实原因为一条使用
  `net_flow` 的 WAIT 和一条面板外占位 REJECT 会令整个有效回复被丢弃。2026-09-26 已修复为
  局部拒绝并由新增单元测试覆盖；该行为变更后必须重新录制完整窗口。
- 当前代码验证：`uv run pytest tests/ -q` 为 `3718 passed, 18 skipped`；
  `lint_harness`、`lint_docs`、`git diff --check` 均通过。
- 第二次录制不是最终验收：它在 2026-01-20 的 Market Review 上发现上述 JSON 转义边界，
  已在当天后主动停止，避免把含已知失败的窗口误报为通过。此前各日 Planner 均
  `parse_error=None`，包括前一轮失败的 2026-01-16；修复后须从新的隔离目录重跑完整窗口。
- 第三次录制也不是最终验收：它已连续通过 2026-01-05 至 2026-01-12 的 6 个交易日和
  7 次 Market Review，但在 2026-01-13 暴露多对象输出边界后主动停止。修复后须再次从新的
  隔离目录完整重跑 30 日。
- **最终验收（2026-09-26）：** 全新的隔离目录
  `/private/var/folders/x5/cdm2lfb11p9_vkm_3z_thlj00000gn/T/alphaagents-t8-final.XXXXXX.Y7GEu4fZVl/replay`
  以 `record` 模式完整运行 `2026-01-05 → 2026-02-13` 的 30/30 个交易日并退出 0。报告位于
  `walk-reports/t8-20260105-30d-final3`：30 次 Market Review、31 次 Trade Review、335 次
  Runtime decision、33 次 watch recheck、13 个 LessonCandidate；
  `market_review_failed=0`、`trade_review_failed=0`、抛错日阶段为 0、生产库内容 hash 未变且
  3 个共享语料库均只读。121 次模型调用走同系列备用模型，均被本轮 journal 录制。账户
  +2.818%、相对等权市场 -4.002%，只作为这一个窗口的结果，不作为策略有效性结论；所有学习
  均停留在 observation（最大 n=31，小于 n≥50 的晋升门槛）。

最终重新录制命令：

```bash
task_replay_root=$(mktemp -d -t alphaagents-t8)
uv run python scripts/walk_bootstrap.py --target "$task_replay_root/replay"
ALPHAAGENTS_DATA_DIR="$task_replay_root/replay" ALPHAAGENTS_LLM_MODE=record \
  uv run python scripts/walk_forward.py --start 2026-01-05 --days 30 \
  --trader default --decider llm --no-trader-tools --model-timeout 120 \
  --keep-going --run-id t8-20260105-30d
```

## 风险

- 30 天模型回放可能因无可用凭据、速率限制或历史语料不足而停止；报告必须区分环境
  不可用和 Trader Runtime 失败，不能以静态单元测试代替一次实际窗口运行。
- 将模拟成交回写状态时，必须使用 replay 时钟与 run/trader 隔离；错误的全局数据库
  绑定或当日收盘数据进入开盘决策都会使验收失败。
