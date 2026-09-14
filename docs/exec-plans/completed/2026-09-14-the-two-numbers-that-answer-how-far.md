# 「还有多远」这个问题里的两个数本身

- 起于 2026-09-14，交付同日。
- 触发：一个提问 ——「现在距离我们 Trade · Learn · Evolve 还有多远」。
- 这份计划不新增能力。它修的是**回答那个问题时被读的三个数**：一个印错单位、
  一个从来没有被比较过、一个正在污染等待期。

## 为什么要专门做这一轮

问「还有多远」会得到一个数字。如果那个数字的单位是错的，得到的不是「远」，
是「远得不对」。这一轮查出来的三件事都属于这一类：

| # | 症状 | 真身 |
|---|---|---|
| 1 | `status` 印 `N/20 paired day(s)` | 那个 N 是**配对样本数**（`(date, code)` 对），不是天数；同一个函数明明还算了 `scored_days`（不同日期数），从来没印出来 |
| 2 | 三份文档都说仓库要求 `n ≥ 50` | 代码里只有 `MIN_VALIDATION_SAMPLES = 20`，而晋升重检读的是**冻结版本自己声明的**值 —— n 落在 20–49 的裁决会**被放行** |
| 3 | `predictions.horizon_days` 在生产里恒为 NULL | 不是「调用方没说」：`intraday_monitor` 给同一笔决策的**论点**写了 3 天，只是没写给预报 |

第 3 条是本轮唯一的真 bug，而且它的代价正好落在「距离」上：那二十个配对样本
本来就在被按一个**调用方并不打算用的期限**评分。

## 判据

> 每条都要有"现在能查"的判据，不是"以后会做"。

1. **`status` 不再把配对数叫天。** 输出里必须同时出现 `paired sample(s)` 与
   `scored day(s)`；必须**不出现** `paired day(s)`。✅
2. **门槛的三个值同时可见。** `status` 打印：在效版本声明的门槛、闸门的弃权线、
   以及这两个与 `GOVERNANCE_MIN_SAMPLES` 的差。✅
3. **差是有据的，不是修辞。** 一个纯函数处理三个分支（无声明 / 低于规则 / 达到规则），
   并且有一个用例把缺口跑成**真实的 `promote` 裁决**（n = 25）。✅
4. **调用方声明的期限到达预报。** 驱动**真实保存路径**（`_save_recommendations_list` /
   `_record_intraday_pick`），断言预报与**同一笔决策的论点**拿到同一个期限。✅
5. **未声明仍然是未声明。** 模型没说时预报保持 NULL，不得被写成 5。✅
6. **单一是结构性的。** intraday 的 3 只出现在一个具名常量里；把常量改成 4，
   两条路径都跟着变。✅
7. **三处文档不再声称一条不存在的规则。** `GOLDEN_PRINCIPLES.md` §7、
   `TRADER_CORE_DESIGN.md` §12、`README.md` 都说明「声明了、没有代码在强制」。✅
8. **不扩 lint 豁免基线，不新增分层违规。** ✅
9. **探针有效。** 每个新判据都要有一个变异探针把它变红。✅

## 决策记录

**D1 — 不把 `MIN_VALIDATION_SAMPLES` 提到 50，尽管那才是真正关上 §7 的做法。**
`MIN_VALIDATION_SAMPLES` 在 `policy_sources._RULE_SOURCES` 里，是这个指纹覆盖的
rule 源之一。改它 → 在效的 V1 **立刻变 drifted**（`live configuration still matches:
False`），而在效版本是唯一一个版本：没有 uninstall，`install` 只在无指针时可用，
晋升又需要一条引自该版本的裁决。于是「改常数」不是一次补丁，是一次操作者决定，
成本要写在明面上。**本轮交付可见性，把决定留给人。**

**D2 — 把 §7 的数字引进代码，但不进指纹。**
`GOVERNANCE_MIN_SAMPLES = 50` 是一条**引用**，不是阈值：它不改变任何行为。
刻意不加入 `_RULE_SOURCES` —— 进了指纹，它一变就会报一次**假漂移**
（行为没动，指纹动了）。`policy_sources` 的模块 docstring 把这份元组定义为
"which constants are *behaviour*"，而这条不是。

**D3 — `promotion_floor_gap(declared)` 接受传入的值，不自己去读常量。**
门槛是**版本的**属性：晋升重检读的是 `frozen["rules"]` 里那份。一个自己去读常量的
函数会在版本声明的值与代码的值不同时给出相反结论 —— 而那正是要报的那件事。

**D4 — 不替 `status` 推算「最早可裁日期」。**
那需要一个**未来**交易日历。本仓库的交易日历来自 `daily_kline`（只有过去），
现推等于给「交易日是什么」造第二个真相来源。报两个数的分子分母，报日期是要人自己算。

**D5 — 单位错误要成对断言。**
「必须出现 `paired sample(s)`」会被别处恰好正确的文案满足；「不得出现
`paired day(s)`」才是承重的那条，而且断言的是 **stdout**，不会被源文件里
任何一句 docstring 自证。

**D6 — intraday 的 `signal` 行不声明期限。**
`rec_type='signal'` 是涨停观察：没有 `prob`，没有可成熟的预报。给它写 3 天
是给一笔不存在的预报伪造一个窗口。`None` 才是诚实的答案。

**D7 — morning 不镜像论点的 5 天默认。**
`from_recommendation` 在模型没给时用 `_DEFAULT_HORIZON = 5`，那是**它**的决定。
预报跟着写 5 会把「模型没说」变成「模型说了五天」，正是 `_deadline_for` 拒绝做的事。
所以同一笔决策可能记成「论点声明 5 / 预报声明空」—— 这个差异是诚实的，
而且它正是 `legacy_horizon` 这个标签剩下的一点意思。

**D8 — 停止规则的单位不一致只记录，不修（D16）。**
`shadow_run.py` 的 `remaining` 数配对样本、`asked_already` 数验证日，于是闸门会被
**天天问**。两种修法都在回答 D15 那个被要求别碰的问题（门槛是 20 个样本还是 20 天）：
按样本比会让一个早晨的选股结束实验，按天数比则要动闸门自己的常数（漂移所有已冻结版本）。
所以只写下来，并留下一条**断言缺陷**的用例与一个把它变红的探针 —— 用例名与 docstring
都写明它测的是分歧不是契约，免得下一个人读成批准。

**D9 — 不替操作者冻一个 V2 覆盖漂移（D17）。**
`status` 现在打印 `live configuration still matches: False`，原因可命名
（`d927a0b` 给 `DEFAULT_DECISION_PARAMS` 加了 `theme_gate`，而 `install` 在那之前）。
修法是一条不需要证据、不移动指针的命令，但它决定的是「下一个候选是什么」——
本轮已经两次被叫停在同一件事上，所以给出命令而不执行。

**D10 — baseline 不触发「拿策略跟自己比」的警告。**
那句话只对读版本参数的生产者成立。让它在不成立时也出现，代价是训练操作者忽略它 ——
而它真正成立的那一次就没人看了。警告按 `Producer.kind` 分支，并给 baseline 一条
说明它为什么安全（`baseline` scope 不可晋升）的正面提示。

## 交付记录

改动：

| 文件 | 改了什么 |
|---|---|
| `alpha_agents/evolution/holdout_gate.py` | `MIN_VALIDATION_SAMPLES` 注释点明单位是配对样本；新增 `GOVERNANCE_MIN_SAMPLES` 与 `promotion_floor_gap()` |
| `alpha_agents/evolution/shadow.py` | `coverage()` docstring：`paired`/`needed` 是配对样本，`scored_days` 是日期数，并把两者分开说明 |
| `scripts/policy.py` | status 增 `_declared_floor` / `_print_promotion_floor`（三个值 + 差异）；进度条改印 `paired sample(s) over scored day(s)`；模块 docstring 说明单位 |
| `alpha_agents/pipeline/tasks/intraday_monitor.py` | 新增 `INTRADAY_HORIZON_DAYS = 3`，同时喂给 `save_prediction` 与 `from_recommendation` |
| `alpha_agents/pipeline/tasks/morning_scan.py` | `save_prediction(horizon_days=r.get("horizon_days"))` |
| `tests/test_declared_horizon_reaches_the_forecast.py` | 新增 5 条，驱动两条真实保存路径 |
| `tests/test_holdout_gate.py` | 新增 `TestTheDeclaredFloorAgainstTheRepositorysRule` 4 条 |
| `tests/test_policy_cli.py` | 进度条用例改为成对断言；新增门槛与「达标即无缺口」两组 |
| `docs/GOLDEN_PRINCIPLES.md` §7 | 「Enforced by」补上：自动化路径**不在**覆盖内，并说清为什么 |
| `docs/TRADER_CORE_DESIGN.md` §12 | 那句「the repository's `n ≥ 50` requirement」改为「声明了、未强制、差异会被打印」 |
| `docs/TRADER_CORE_IMPLEMENTATION.md` | §7 两处更正（horizon 那条的旧结论是错的）+ 新增 §14.11 |
| `docs/exec-plans/tech-debt-tracker.md` | D8 半付并记下「上一版说错了为什么」；新增 D15（§7 无强制） |
| `docs/QUALITY_SCORE.md` | `holdout_gate` 行重写（已接调度、门槛不是仓库的门槛） |
| `alpha_agents/pipeline/tasks/shadow_run.py` | 进度行改成 `个配对样本（覆盖 N 个交易日）`；`asked_already` 旁边写下 D16 的成因 |
| `scripts/policy.py` | dry-run 的在效警告改为按生产者类型区分（baseline 不触发） |
| `tests/test_shadow_run_task.py` | 进度行改成整行断言；新增一条**断言 D16 缺陷**的用例 |
| `docs/exec-plans/tech-debt-tracker.md` | 新增 D16（停止规则单位不一致）与 D17（在效版本已漂移） |

**在生产库跑的那一步**：开了第一条影子 run（`#1` / V1 / `constant_0.5` / `morning`）。
选 baseline 而不是候选，是因为它可以立刻开、且其裁决在 `policy_registry` 里不可晋升
（`baseline` scope），所以它只做一件事：让 15:45 的调度有一条 run 可喂。

验证：

```bash
.venv/bin/python -m pytest tests/ -q          # 2000 passed, 18 skipped
.venv/bin/python scripts/lint_harness.py      # 167 文件，0 新增（13 条存量豁免）
.venv/bin/python scripts/lint_docs.py
.venv/bin/python .pytest-tmp/probe_declared_horizon.py   # 10/10 effective
```
