# Trader Runtime 验证任务（交接）

状态：active · 创建：2026-09-26 · 交接基线：`main@97bb696`（PR #50 #51 #52 已合并）

这份文档交给接手验证的人。它回答四件事：**设计是什么、实现在哪、已经证明了什么、
还要验证什么**。每一项验证都给出命令和"通过"的判据；判据是读出来的内容，不是退出码
（本仓库反复出现"管道通了、内容是错的"，见 `AGENTS.md` 证据规则）。

---

## 0. 一句话现状

一个持续存在的模拟 A 股交易员：同一个 `TraderState` 贯穿实盘与历史回放；买入、持有、
卖出全部由交易员（LLM）决定，**没有任何止损或止盈价位**；每个决策在 5 个交易日后由行情
打分（对比全市场中位数），经验按"动作 × 情形标签"聚合成 Lesson/Rule，再作为证据回到
决策上下文——采不采纳由交易员自己决定。代码 T1–T9 完成；**实盘在新代码上还没有经历过
一个交易日**（2026-09-26 周六重启），这是最需要验证的部分。

---

## 1. 先读什么（按顺序）

| 问题 | 文档 | 读哪部分 |
|---|---|---|
| 仓库规矩、证据规则、不可违反的约束 | `AGENTS.md` | 全文（短） |
| 目标架构（权威设计） | `docs/TRADER_CORE_DESIGN.md` | §2 不变量、§14 阶段、§15 验收契约 |
| Trader Runtime 的 T0–T9 定义 | `docs/exec-plans/active/2026-09-25-trader-runtime.md` | §4 领域模型、§8 各阶段状态、§9 验收场景 |
| 回放接入 Runtime（T8）及其更正 | `docs/exec-plans/active/2026-09-25-trader-runtime-finalization.md` | "追加验收"一节 |
| 为什么没有止损止盈 | `docs/exec-plans/active/2026-09-26-no-system-exit-lines.md` | 目标、决策记录 |
| 行情打分、学习重做、T9 结果 | `docs/exec-plans/active/2026-09-26-trader-runtime-completion.md` | 全文 |
| 实际做成了什么、实测数字 | `docs/TRADER_CORE_IMPLEMENTATION.md` | §15、§16 |

**用户定下的三条方向，验证时不要"修"掉它们：**

1. 没有硬止损、没有硬止盈，交易员自己挂的价位也不执行——"让 agent 自己学"。
2. 学到的 Rule 不做代码拦截、不强制回应；只作带行情计数的证据。
3. 模型不给自己打分；对错只由行情决定（`AGENTS.md` 不变量 2）。

---

## 2. 实现地图

| 职责 | 位置 |
|---|---|
| 领域核心：Observation / TraderState / TraderDecision / `TraderRuntime.step()`（纯函数，无存储） | `alpha_agents/trader/` |
| TraderState 持久化（append-only、CAS、按 `run_id` 隔离；实盘 `run_id=live`） | `alpha_agents/data/trader_state_store.py` |
| 晨扫 → Runtime（晨扫只产出候选，`place_orders=False`，不直接下单） | `alpha_agents/pipeline/tasks/morning_scan.py`、`alpha_agents/agents/trader_runtime.py` |
| 盘中事件唤醒同一个 Trader | `alpha_agents/pipeline/tasks/intraday_monitor.py` |
| 持仓与卖出（HOLD/ADD/REDUCE/SELL，先封存后执行；注入 `position` 期经验） | `alpha_agents/pipeline/tasks/exit_decision.py`、`book_manager.py` |
| 规则层（移动止损、主线走弱、持有上限）——**只发 signal，不平仓** | `alpha_agents/data/position_monitor.py` |
| 收盘编排（逐笔复盘 → 决策复盘 → 市场复盘 → 行情打分 → 学习） | `alpha_agents/evolution/close_day.py`，实盘由 19:00 `close_review` 任务调用 |
| 决策复盘（模型只写理由与候选文字，不打分） | `alpha_agents/evolution/trader_review.py` |
| **行情打分**（5 日前瞻收益 − 全市场中位数；right/wrong/flat；情形标签） | `alpha_agents/evolution/decision_outcomes.py` → 表 `trader_decision_outcomes` |
| **学习**（格子 = 动作×标签×时间尺度×horizon×证据域；Lesson n≥10、Rule n≥50、偏离 60/40） | `alpha_agents/evolution/trader_learning.py` → `trader_lessons` / `trader_rules` / `trader_rule_events` |
| 历史回放（同一 Runtime；LLM 回放一律 agent 卖出、无价位出场） | `scripts/walk_forward.py`（`walk_bootstrap.py` 建隔离目录） |
| 检查点 / 分支对照（T9：注入一条人工批准的 Rule） | `scripts/walk_checkpoint.py`、`scripts/walk_branch.py` |

---

## 3. 已经证明了什么（证据，均在隔离回放目录）

| 结论 | 证据 | 说明 |
|---|---|---|
| 回放决策走同一 Runtime、状态连续 | 30 日回放 `t8-20260105-30d-final3`：335 个决策封存、生产库无写入 | **不证明**持仓管理——该窗口 31 笔卖出全是旧的机械止损/止盈 |
| 无价位出场后卖出由交易员决定 | 4 日回放 `nostop4-20260105`：机械 0、agent 4（清仓 2、减仓 2），HOLD 9 | n 极小，只证明接通 |
| 行情打分有区分度 | 对上述 30 日已录决策离线重算：buy 判对 18/48、reject 107/160、wait 42/76；9 Lesson / 3 Rule | 旧自评为 319/326 good；同窗口决策不独立 |
| Rule 能送达决策上下文 | T9：实验组 5/5 天上下文含 Rule | — |
| 文字 Rule 基本不改变行为 | T9：run_up_5d 情形买入 实验 6/7、对照 8/9 | 1 个 trial，噪声内；用户决定不加强制 |
| 全量测试 | `uv run pytest tests/ -q`：3745 passed | CI 同 |

---

## 4. 验证任务

每项标 **[阻塞]** 的不通过就不能说"Trader Runtime 在实盘生效"。

### V0 基线（任何时候，~3 分钟）

```bash
uv run pytest tests/ -q
uv run python scripts/lint_harness.py
uv run python scripts/lint_docs.py
```
通过：三者退出 0；pytest 无 failed。

### V1 实盘跑的是最新代码 [阻塞]

实盘调度跑在**本机**（不是 tradingagents-box）：`uv run python main.py run-v2`。

```bash
pgrep -f "main.py run-v2" | xargs -I{} ps -o pid=,lstart= -p {}
git -C ~/Projects/AlphaAgents log -1 --format='%h %ci'
grep -cE "ERROR|Traceback" logs/run-v2-20260926b.log
```
通过：进程启动时间晚于 `main` 最新影响交易行为的合并；日志 `Registered task` 10 条、无
Traceback。**每次合并影响交易的 PR 后都要重启**（见 §6）。

### V2 第一个交易日的买入侧（2026-09-28 周一 09:00 晨扫后）[阻塞]

```bash
sqlite3 "file:data/memory.db?mode=ro" "
  SELECT version, as_of, length(payload_json) FROM trader_state_snapshots
  WHERE run_id='live' ORDER BY version DESC LIMIT 5;"
```
通过：
- 表存在且有 `run_id='live'` 的新快照，`as_of` 为当天。
- 读最新 `payload_json` 的 `recent_decisions`：当天有 BUY/WAIT/REJECT，每条有 `reasoning`、
  `made_at`、`evidence_scope=live_daily`；**`stop_loss` 与 `target_price` 均为 null**。
- 当天新挂单（`virtual_portfolio` 新行）都能对应到一条封存的 BUY 决策；晨扫本身没有绕过
  Runtime 直接下单。
- 读晨报/日志**正文**：不是以 `[` 开头的错误串，不是 `Unsupported model`。

### V3 持仓与卖出（周一盘中 09:30 起）[阻塞]

现有 9 个持仓是旧代码开的；新代码下它们只能被交易员卖出。

```bash
sqlite3 "file:data/memory.db?mode=ro" "
  SELECT code, status, close_date, substr(close_reason,1,60) FROM virtual_portfolio
  WHERE close_date >= '2026-09-28' ORDER BY close_date;"
grep -E "signal|Exit decisions|agent" logs/run-v2-20260926b.log | tail -40
```
通过：
- 每一笔平仓的 `close_reason` 都来自交易员决策（`agent卖出` / `agent减仓` 或 Runtime 封存的
  SELL/REDUCE），**没有任何一笔是"止损触发 / 止盈触发 / 硬止损 / 主线归档"**。
- 规则层（移动止损、主线走弱等）只以 `type=signal` 出现，并在交易员的决策上下文里被看到。
- 持仓决策（HOLD/REDUCE/SELL）在 `recent_decisions` 中先于执行封存。
- 如果当天没有任何卖出也可能正确——那就检查 HOLD 决策确实存在并带理由。

### V4 收盘与学习（周一 19:00 `close_review` 后）

```bash
sqlite3 "file:data/memory.db?mode=ro" "
  SELECT decision_quality, count(*) FROM trader_decision_reviews
  WHERE run_id='live' GROUP BY 1;
  SELECT count(*) FROM trader_decision_outcomes WHERE run_id='live';
  SELECT count(*) FROM trader_lessons WHERE run_id='live';"
```
通过：
- `trader_decision_reviews` 的质量列全部是 `market_pending`（模型不再打分）；`reason` 是引用
  事实的文字。
- `trader_decision_outcomes` 在**前 5 个交易日为 0 是正确的**（窗口未闭合）；约第 6 个交易日
  起开始出现，`verdict` 有 right 也有 wrong，基准是市场中位数。
- Lesson 需要同一格子 ≥10 个已判定决策，实盘大约要数周；在此之前为 0 是正确的。

### V5 回放回归（任何时候，约 15 次模型调用）

```bash
root=$(mktemp -d -t alphaagents-verify)
uv run python scripts/walk_bootstrap.py --target "$root/replay"
ALPHAAGENTS_DATA_DIR="$root/replay" ALPHAAGENTS_LLM_MODE=record \
  uv run python scripts/walk_forward.py --start 2026-01-05 --days 4 \
  --trader default --decider llm --no-trader-tools --model-timeout 120 \
  --keep-going --run-id verify-4d
cat "$root/replay/walk-reports/verify-4d/summary.txt"
```
通过（读 `summary.txt` 正文）：
- `出场归因：机械 0 笔`；所有卖出 `reason` 以 `agent` 开头。
- `本次运行未写生产库  是`（`生产库内容 hash` 那一行只是诊断，实盘在写时为"否"是正常的）。
- 有"风险暴露"段（最深单票浮亏、期末亏损持仓、≤−8% 仓位-日）和"行情判定"段。
- 限制说明写的是 "the agent decides every sell and there is no stop or target"。

注意 T+1：第 1 天买的票第 2 天才能卖，持仓决策至少要 3 天才看得到。

### V6 隔离与不变量（代码审阅 + 测试即可）

- 回放/分支不写生产库：`tests/test_walk_forward.py::TestTheProductionVerdictIsAboutThisRun`。
- 行情打分只读 as-of 之前的 bar、基准是中位数：`tests/test_decision_outcomes.py`。
- 模型写的等级与计数被丢弃：`tests/test_trader_review_runtime.py::test_the_models_grades_and_counts_are_dropped`。
- 卖出决策只读 `position` 期经验：`tests/test_sell_side_learning.py`。
- 没有任何价位出场：`tests/test_no_system_exit_lines.py`、`TestAnLlmRunHasNoPriceExits`。

通过：上述测试存在并通过；审阅时确认没有新引入任何"按价格自动平仓"的路径。

### V7 T9 复现（可选，花 token；想得到比 1 个 trial 更可靠的结论时做）

```bash
# 1) 封存前缀（期间不要改仓库文件，否则检查点拒绝封存）
root=$(mktemp -d -t alphaagents-t9)
uv run python scripts/walk_bootstrap.py --target "$root/replay"
ALPHAAGENTS_DATA_DIR="$root/replay" ALPHAAGENTS_LLM_MODE=record \
  uv run python scripts/walk_forward.py --start 2026-02-16 --days 2 --trader default \
  --decider llm --no-trader-tools --model-timeout 120 --run-id t9-prefix \
  --checkpoint-out "$root/checkpoint"
# 2) arms.json：[{"name":"rule_x","rule":{"claim":"…","action":"buy",
#    "applicable_context":"run_up_5d","approved_by":"<人名>"}}]
uv run python scripts/walk_branch.py run --checkpoint "$root/checkpoint" \
  --output "$root/t9" --days 5 --trials 3 --max-branch-sessions 30 \
  --timeout 10800 --jobs 2 --live --arms arms.json
```
读 `summary.json` 的每个分支 `decisions`（动作分布、`tagged_buys`）与
`intervention_evidence.decision_contexts_with_rule`。结论只按中位数比较，trial 数写清楚。

---

## 5. 已知限制与开放问题（不是 bug，但要知道）

1. **文字 Rule 基本不改变行为**（T9，1 trial）。用户决定不加强制；要不要在更多 trial 上确认，
   是 V7 的问题。
2. **n 不是独立样本**：同一窗口的决策共享交易日、持有期重叠。Lesson/Rule 的计数是描述，
   不是显著性检验。
3. **情形标签只有 6 个价格类标签**（`decision_outcomes.TAGS`）。持有/卖出类决策没有
   "浮盈/浮亏、峰值回吐"这类持仓标签，卖出侧格子可能长期学不出东西。这是最值得补的地方。
4. **模型不可用时没有任何东西会卖出持仓**——这是"无止损"的直接后果，用户接受。报告的
   "风险暴露"段是唯一的可见性。
5. **30 日回放数字产生于旧代码**（有机械止损、自评打分），只能作历史对照，不能当新代码的证据。
6. 回放模型可能回退到同系列备用模型（报告里有计数）；比较两次回放时要确认模型一致。
7. `HARD_STOP_PCT` 还存在，但只用于默认关闭的风险预算定仓，不触发卖出。

---

## 6. 运维要点

- **实盘在本机**：`nohup uv run python main.py run-v2 > logs/run-v2-<date>.log 2>&1 &`。
  当前日志 `logs/run-v2-20260926b.log`；应用日志 `data/logs/alphaagents.log`。
- **重启流程**（只在非交易时段）：确认日志里没有进行中的任务 → `kill -TERM` 两个进程 →
  `sqlite3 data/memory.db ".backup 'data/memory.db.bak-<date>-<why>'"` → `git pull --ff-only` →
  `uv sync` → nohup 启动 → 看 10 条 `Registered task`、无 Traceback。
- **备份**：`data/memory.db.bak-20260926-before-runtime`（旧代码最后状态，9 个持仓）、
  `data/memory.db.bak-20260926-before-pr51`。
- **回放只在 `mktemp` 隔离目录里跑**；`walk_forward` 拒绝把生产目录当回放目录。
- GitHub：remote 是 `git@home:KylinMountain/AlphaAgents.git`，`gh` 用 `KylinMountain` 账号。

## 7. 不要做的事

- 不要加回任何止损/止盈/硬线，也不要以"安全网"为名让代码替交易员卖。
- 不要让模型给决策、经验或复盘打分或填计数。
- 不要用均值当市场基准；不要用 n<50 的结果上线行为变化；不要在蒸馏规则的同一窗口上验证它。
- 不要在回放/检查点运行期间修改仓库文件（检查点校验源码哈希，会拒绝封存）。
