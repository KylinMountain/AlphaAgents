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

- [ ] T5、T7 均以合入后的 `main` 为基线；`uv run pytest tests/ -q`、
  `uv run python scripts/lint_harness.py` 和 `uv run python scripts/lint_docs.py` 均退出 0。
- [ ] T8 测试证明：历史 BUY 的模拟成交产生 `POSITION_CHANGED` 或等价不可变事实，
  同一 `run_id` 的下一会话能据此作 HOLD / REDUCE / SELL，且决策在执行前已经封存。
- [ ] T8 测试证明：收盘 review 产生 LessonCandidate；只有此前已物化、时间尺度、
  horizon、evidence scope 与 run/trader 都匹配的重复证据可在之后注入 Lesson / Rule。
- [ ] 最终 T8 分支只包含 T8 的提交，不携带旧 T6/T7 实现；不 force push，采用基于
  合入后 `main` 的新分支与替代 PR。
- [ ] 使用 `scripts/walk_bootstrap.py` 创建隔离目录后，运行连续 30 个交易日的
  `scripts/walk_forward.py` 并以退出 0、`corpus_read_only: true`、
  `production_db_unchanged: true` 和报告文件存在为成功判据。
- [ ] `TRADER_CORE_IMPLEMENTATION.md` 与本计划更新实际完成状态、运行命令和结果；
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

## 本次验证记录

- 已通过：完整测试集 `3716 passed, 18 skipped`；T8 Runtime / position /
  replay-learning、T5 position、T6 review、T7 learning、bootstrap、Planner 与 tools
  wiring 的定向回归均覆盖；`lint_harness`、`lint_docs`、`git diff --check` 均通过。
- 隔离单日实跑：`2026-01-05` 在新 bootstrap 目录创建了空的 Runtime 与机会日志 schema，
  证明原先的首日 `theme_opportunity_items` / `opportunity_items` 缺表已修复。当前网关在
  15 秒内未返回模型响应，Runner 如配置抛出 `TimeoutError`；日终语义索引同样等待该外部
  embedding 服务。因此它是可复现的环境阻塞，不能把该单日写成通过的策略或学习验收。

待外部服务恢复后执行：

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
