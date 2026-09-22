# 把方向盘整个交给 agent：仓位、出场、晋升

2026-09-22。承接 `docs/TRADER_CORE_DESIGN.md` 的「交易员」定位。

## 为什么

两轮 20 日回放（见 `synth-sector-flow-from-stock-flow.md`）之后，报告里
还剩这两行：

```
出场归因：机械 1 笔；agent 3 笔
· no theses and no predictions: exits are stop/target only
```

买入侧已经像交易员了，剩下三件不在 agent 手里：**买多少**、**是否被强制
卖出**、**学到的东西能不能自己生效**。

不是"再像一点"的问题。**机械止损会污染学习信号**：一笔的结局如果是止损
线打出来的，那这笔衡量的是那条线，不是 agent 的判断。AGENTS.md 开篇写着
"a system that learns from results it cannot account for learns its own
bugs" —— 一条 agent 无法否决的规则，会让它的每一次学习都混进别人的决定。

## 真正的缺口：回放从不创建 Thesis

`portfolio_sizing._wanted_pct` 读的是 **Thesis 的 `size_pct`**，不是订单
字段；读不到才退回 `traders/*.yaml` 的 `default_size_pct`。而
`scripts/walk_forward.py` 从头到尾没有创建过 Thesis —— 报告里那句
"no theses and no predictions" 说的就是这件事。

没有 Thesis，三件事同时塌：

1. 仓位永远是 yaml 里的常数，**agent 说了不算**；
2. 出场时没有「买入逻辑」这个对象可以宣告证伪，只能看价格；
3. 失效条件无处存放，于是 `thesis.evaluate()` 这套**代码判定**的机制在
   回放里完全没被使用。

`alpha_agents/data/thesis.py` 已经把这件事设计好了：`claim`、`size_pct`、
`conditions`（11 种可判定类型，含 `theme_flow_negative` 与
`theme_rank_worse_than`，正好接本仓库刚回填的概念资金流）、
`prompt_vocabulary()` 从同一张表生成 agent 说明，保证提示词与校验器不会
漂移。晨扫 agent 已经在用它，回放这条路没有接上。

## 做什么

1. **交易计划输出 Thesis 的字段**：每个 order 追加 `size_pct`、`prob`、
   `conviction`、`horizon_days`、`invalidations: [{kind, value, note}]`。
   提示词里的可用类型由 `prompt_vocabulary()` 生成，不手写。
2. **下单即建 Thesis**，成交后 `attach_position`。仓位随之自动跟随
   （`portfolio_sizing` 本来就读它）。
3. **拆掉机械导轨**：新增 `--autonomous`，含义是
   `--agent-exits` + 关闭机械止损/止盈 + 回放内不执行 `HARD_STOP_PCT`。
   旧行为保持默认，实验要显式开。
4. **Evolve 自动晋升**：管线在**市场数据闸**通过时调用
   `advance_candidate`，不再等人工审批。

## 关于第 4 条的边界（先说清，因为两种理解会做出不同的东西）

「不要人工审批」与「不要统计闸」不是一回事。
`docs/GOLDEN_PRINCIPLES.md` 的「No LLM grades its own output — utility
comes from market data only」管的是**谁来评判**；人工审批是评判之外**另
加**的一道手闸。

因此：**去掉人工审批，保留 `holdout_gate`**。晋升由前向验证的市场数据
触发，全程无人，这已经满足"自动运行"；而如果连统计闸一起去掉，n=4 的
观察也会变成规则，系统就开始学自己的噪声——那正是上面那句警告的事。

## 验收标准

1. 一次 `--autonomous` 回放里，`出场归因` 的机械笔数为 **0**，agent 笔数
   > 0；报告的限制清单里不再出现 "no theses"。
2. 至少一笔仓位的 `size_pct` 与 `traders/pullback.yaml` 的
   `default_size_pct` **不同**，且该数出现在对应 Thesis 上。
3. 至少一条 Thesis 带 ≥1 个通过 `validate_condition` 的 `conditions`，
   并且其中至少一条是 `theme_*` 类（证明资金流进了失效判定，而不只是
   进了选股理由）。
4. 跑一段足够长的窗口（回填后有 175 个交易日可用），使已平仓笔数越过
   `holdout_gate.MIN_VALIDATION_SAMPLES`，观察 `advance_candidate` 是否
   在无人参与下被调用；结果如实记录，**包括它没被调用的情况**。
5. `uv run pytest tests/ -q` 全绿；两个 lint 通过。

## 决策日志

- 2026-09-22 保留 `holdout_gate` 的统计闸，只去掉人工审批。理由见上。
- 2026-09-22 机械导轨做成显式开关而非改默认：旧回放的可比性依赖默认值
  不动，而这次要的是一个新臂，不是把旧臂改掉。
