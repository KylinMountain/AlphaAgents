# ④ 概念成分历史前视的消融——先量化，再决定是否修

2026-09-19。对应 `2026-09-18-the-remaining-seven.md` 的 ④。

## 前视是什么（已核实的事实）

`stocks.db` 的 `concept_stocks` 只有两列（`concept_id`, `stock_code`），
没有日期。链路：`stock_meta.concepts_for()` 读它 →
`walk_forward._resolve_concepts()` 放进面板「概念（当前成分）」列 →
`t1_decider.format_panel()` 渲染进 prompt。一只在回放窗口之后才被归入
某概念的票，回放时带着未来的标签——真实前视（标签级，弱于价格泄漏）。

两个边界，先说清：

1. **占位符 decider 不读这一列**（按 T-1 涨幅机械选股）。此前所有
   placeholder 回放与此前视无关。本消融只对 `--decider llm` 有意义。
2. **六个 trader 工具均不读成分表**（已核对 `trader_tools.py` 全部
   函数）。面板列是模型接触概念前视的唯一入口——在面板层消融即
   完全消融。

## 目标

不改生产行为。给回放加 `--no-concepts` 消融开关，双臂各跑一次同窗口
LLM 回放，量化该列对决策的实际影响，用数字决定修不修、怎么修。

## 改动（一个开关 + 三处一致性，都在 `scripts/walk_forward.py`）

1. CLI 加 `--no-concepts`（dest `concepts`，action `store_false`，
   default `True`；`Context` 存同名字段）。
2. `_build_panel`：两臂都调 `_concepts_map`（一次查询、进程内缓存，
   只读不进决策），消融臂面板行 `concepts` 置 `[]`，并计数
   `concepts_ablated_rows`（本会被展示的概念非空行数——先算后清才能数）。
   保留臂行为逐字节不变。
3. 报告：`concepts_ablated: true|false` 字段；limitations 按臂措辞——
   消融臂写「本臂按计划移除概念列，两臂差异只能归因于它」，保留臂
   维持现有「当前成分」警示。
4. prompt 模板与表头**两臂不变**（都写「概念是当前成分」）：消融的是
   数据，不是说明文本，否则差异里混进措辞效应。

## 消融协议

- 窗口：20 天（与 09-17 基线同长度）；`--decider llm`；出场用机械
  止损/止盈（不开 `--agent-exits`，少一路模型调用噪声）。
- 两臂模型必须一致（运行时 config 默认，两份报告的 model 字段
  核对一致才算数）。
- 臂 A（保留）：`ALPHAAGENTS_LLM_MODE=record` 录制。
- 臂 B（消融）：同窗口重跑 `record`。
- 对照口径：两臂 orders CSV 按 `(code, order_date)` 逐日比对；
  收益比较**中位数对中位数**（A 股截面右偏，均值会造假优势）。
- 成本预估：约 2 × 47 分钟（09-17 基线实测 47 分钟/20 天）。
- 不复用 09-17 journal：录制随 /tmp 回放目录清理，已不存在，重录。

## 验收标准（机器可查）

- [ ] 不带旗标时现有行为不变：`pytest tests/ -q` 全绿。
- [ ] 消融臂报告 `concepts_ablated: true`，且 `concepts_ablated_rows`
      等于保留臂同窗口的概念非空面板行数（两边市场面相同）。
- [ ] 两臂报告各含对应 limitation 条目。
- [ ] 差异结论落进本文件决策日志：订单逐日相同率、差异日清单、
      每臂订单数、每臂已平仓收益中位数。
- [ ] ④ 的处置写入 remaining-seven 计划：修（哪种）/不修（数字依据）。
- [ ] `lint_harness.py` 无新增违例。

## 解读规则（跑之前写死，防止跑完再找说法）

- 订单几乎相同（差异日 < 10%）：概念列对行为无实质影响 → 记「惰性」，
  不修，结论补进 `stock_meta` 警示。
- 差异显著、消融臂更差：前视承载了真实板块判断 → 保留现状 + 等
  dated membership 源，不造假快照。
- 差异显著、消融臂更好：前视在污染结论 → 报告与 roadmap 升级优先级；
  但修复仍是数据问题——找带日期的成分源，不在代码里猜历史成分。
- n=20 天、单一模型、单一窗口：全部结论标注为单窗口证据，不外推。

## 决策日志

- 2026-09-19：面板层消融，不动 prompt/表头——数据级单差，一次只变
  一个变量。
- 2026-09-19：两臂都调 concepts map（消融臂只用于计数），换来
  「ablated_rows = 保留臂非空行数」这条可机检的等式。
- 2026-09-19：不新增面板导出；订单比对用两臂已有 orders CSV。
- 2026-09-19：重录 journal 而非复用——旧录制已清理。
