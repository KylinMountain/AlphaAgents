# Learn 页把成熟度叫成了缺口

日期：2026-09-14。上一轮（[两个数字](2026-09-14-the-two-numbers-that-answer-how-far.md)）
把「距离」的口径修准了；这一轮处理的是**页面上还在说错话的两块**——都是用户截图指认的。

## 问题陈述

Learn 页两块，两句话错：

| 块 | 页面在说 | 实际是什么 |
|---|---|---|
| 候选知识（隔离区） | 「表在，但这个库的 schema 落后于代码 / 修不到」 | 生产库从没有写者跑过，schema 停在 T3 之前；修法是页面自己指明的「补 schema 是运维动作」 |
| 预测与评估货币 | 「232 行没有 Brier …… 这不是『暂时没数据』，是当前最要紧的缺口」 | 分母错（可定价只有 48 行）+ 性质错（窗口未收口 = D10 的成熟度，不是缺口） |

## 验收标准

1. 生产库 candidates section 读出 `complete / empty`，不再显示「落后于代码」。
2. 评估货币的分母是**带 prob 的行数**；没有 prob 的行不再被数成「缺的评估」。
3. 页面能回答「最早哪批能出 brier、还差几个交易日」，且窗口算术与评分器**同一份**
   （`scoring.window_progress`），不允许第二份算术。
4. 「窗口收了还没分」单独成一类并渲染成警告——这是唯一真实的缺陷形状。
5. 行情档案不可读时拒绝断言（进度为 null），不在看不见的行情上画进度条。
6. 渲染矩阵覆盖每个新分支（成熟度 / 熟了没分 / 无 prob / 档案不可读），成对断言。

## 决策记录

- **D1：迁移走运维动作，不改读模型。** 读模型不建表是 §10 的不变量；`PARTIAL_NOTE`
  自己写着「补 schema 是运维动作」。备份（SQLite backup API →
  `/tmp/memory.db.before-candidate-migration`）→ 幂等 `learning_candidates.init_schema` → 验证。
  加了 4 列、建了 `candidate_transitions`（含 append-only 触发器），0 行不变。
- **D2：窗口算术收进 `scoring.window_progress`。** 页面要报「还差几天」就需要计数；
  让计数住在判据旁边、判据由它推导，是「同一句话不会有两个答案」的落法。
- **D3：兜底 horizon 用评分器的 `DEFAULT_HORIZON_DAYS`。** 存量 48 行都没声明 horizon；
  页面若自选一个数，量的窗口和分数落的窗口就不是同一个。
- **D4：`prob_rows` 放 totals 顶层，不放 `pricing` 里。** 它是分母（全体），pricing 是
  未评分部分的分解。这个决定立刻被 check:render 证明有价值：组件初版从 `pricing` 读它，
  lint/build 全绿、渲染矩阵红——当场抓到一条真 bug。
- **D5：五个分支的判词分级。** warn 只留给两种「 somebody 能修」的形状
  （无 prob / 熟了没分）；成熟度与档案不可读用 soft。判断由数据出（`pricing`），
  文案不含日期硬编码。

## 交付记录

| 文件 | 改了什么 |
|---|---|
| `alpha_agents/data/scoring.py` | 新增 `window_progress()`（closed/have/need/remaining/last_market_date，None=拒绝断言）；`evidence_window_closed` 改由它推导，行为不变 |
| `alpha_agents/server/readmodels/learn.py` | `_read_forecasts` 增加 `prob_rows` 与 `pricing` 块（`_pricing`）；模块 docstring 与 forecasts note 改为「成熟度，不是缺口」 |
| `web/src/views/LearnView.jsx` | `Forecasts` 五分支判词 + 批次进度表 + 「可定价（带 prob）」行；`Row` 支持 note |
| `web/render-check.jsx` | learn 夹具更新为含 pricing；新增「熟了没分」「无 prob」两个用例；成对断言 |
| `tests/test_scoring.py` | `TestWindowProgress` 2 条（计数与布尔一致；档案缺席= None 不是 0） |
| `tests/test_forecast_pricing_readmodel.py` | 新增 5 条：分母、未熟、熟了没分、legacy 兜底与评分器一致、档案缺席拒绝断言 |

**在生产库跑的那一步**：备份 → `learning_candidates.init_schema` → 验证。
candidates：`complete / empty`（0 行，integrity 干净）。
forecasts：232 行 / hit 202 / **可定价 48 / brier 0**；pricing：`ripe_unscored 0`、
`unripe 48`、最早一批（09-08，7 行）**还差 1 个交易日**——与 §14.11 的推算逐批吻合
（09-08→1、09-09→2、09-10→3、09-11→4、09-14→5）。

## 验证

```bash
.venv/bin/python -m pytest tests/ -q          # 2007 passed, 18 skipped（净增 7）
.venv/bin/python scripts/lint_harness.py      # 167 文件，0 新增（13 条存量豁免）
.venv/bin/python scripts/lint_docs.py
cd web && npm run lint && npm run build && npm run check:render   # 10 用例 OK
```
