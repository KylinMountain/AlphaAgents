# 渲染出来的知识要能被指认：`rendered_knowledge_hash` 逐决策落盘

状态：active
创建：2026-09-21
来源：remaining-seven ⑤（GPT-5.6 Sol review P2：「causal trace 还不能证明
具体哪条经验被读取」）。

## 问题

`causal_trace` 的 `note` 自己写着这句话：

> A decision records which policy version it ran under, not which rule text it
> read: the rendered knowledge block is not persisted per decision. So "traced"
> means the chain episode → candidate → variant → decision is complete, and it
> does not mean the decision quoted the candidate.

`decision_snapshots.policy_ref` 已经能回答「哪个版本」，缺的是「读到了哪段文本」。
没有它，`causal_trace_rate` 只是**血缘**（lineage）——一次决策跑在候选提出的
版本上，不等于它读了那条候选。`causal_trace` 自己也把这两件事分开命名了
（`lineage_rate` vs `counterfactual_change_rate`），但前者仍容易被读成后者。

## 要买到什么

每个决策旁边落一个 `rendered_knowledge_hash`：把当时真正渲染进提示词的知识块
取 sha256。之后「这条经验被引用过」从**推断**变成**可核对的事实**——
给定决策和候选，重算 hash 即可。

## 改动

### 1. `alpha_agents/data/decision_context.py`

新增 `knowledge_hash(text)`：对渲染结果取规范化 sha256，空块返回 `None`
（「没有知识在生效」是一个事实，不该和「读了一段空文本」共用一个值）。

`build_decision_context` 增加可选 `knowledge` 参数，把 hash 放进
`ctx["knowledge_hash"]`。**只放 hash，不放正文**——理由与模块文档里
「刻意排除新闻正文」相同：正文已在库里，复制进每行只会让表变胖。

### 2. 生产决策路径

`build_morning_context` / `inject_principles` 渲染出的块要能被取到。
不改变渲染本身，只把它**返回**给调用方，让 `build_decision_context` 能算 hash。

### 3. 读取侧

`causal_trace` 的 `note` 改写：现在可以说「hash 已落盘，可核对」，
并给出核对方式。不再是一句免责声明。

## 验收（机器可查）

1. 同样的知识块 → 同样的 hash；差一个字符 → 不同的 hash；
2. 空块 → `None`（不是空串的 hash，两者可区分）；
3. `build_decision_context(knowledge=...)` 在返回的 ctx 里带 `knowledge_hash`；
4. 不传 `knowledge` 时键不存在（向后兼容，旧调用方行为不变）；
5. `tests/` 全绿；`lint_harness` / `lint_docs` 通过。

## 实施结果（2026-09-21）

- `decision_context.knowledge_hash(text)`：去空白后取 sha256；`None`/空串/纯空白
  都返回 `None`。
- `build_decision_context(..., knowledge=...)` 把 hash 放进 `ctx["knowledge_hash"]`；
  **不传该参数时键不存在**（旧调用方行为不变），传空串时键存在且为 `None`
  （「渲染了，但没东西在生效」）。
- `context_builder.knowledge_in_force()`：**唯一**组装知识块的地方
  （`inject_principles` + `inject_playbooks`）；检索失败降级为空块，不抛。
- `build_morning_context(..., knowledge=...)` 接受调用方传入的块并原样使用，
  让「hash 覆盖的就是 agent 读到的那段」成为**结构事实**而非假设。
- 生产两条决策路径（`morning_scan`、`intraday_monitor`）都传入该块。
  morning 路径在 `_scan_for` 里渲染**一次**，同一个字符串既进上下文、
  又经 `_save_recommendations_list(..., knowledge_block=...)` 落进决策记录。
- `causal_trace` 的 `note` 改写：不再说「没有落盘」，而是说明 hash 已落盘、
  如何核对（按 `decided_at` 重渲染后比对），并保留「traced 只是血缘」这条边界。

**一个在实现中改掉的设计**：初版让记录侧**重新渲染**一次知识块再取 hash，
理由是「渲染是已批准行的纯函数，同一次决策内字节相同」。这个理由不成立——
渲染（第 363 行）和记录（第 666 行）之间隔着一次 LLM 调用，期间若有原则被批准，
hash 覆盖的就不是 agent 读到的那段。改成渲染一次、同一个字符串传下去。

**验证**：`pytest tests/ -q` → **2987 passed, 18 skipped**；
`lint_harness` 222 文件通过；`lint_docs` 通过。

实测（生产库当前状态，没有已批准快照）：`knowledge_in_force()` 返回 65 字符的
「未生效」提示，hash 稳定可复现。

## 决策记录

- 2026-09-21：**只落 hash，不落正文**。与 `decision_context` 已有的
  「新闻正文不进 features」同一条理由；hash 足以回答「是不是这一段」，
  而正文可以按需从 `learning_candidates` 重算。
- 2026-09-21：排在 ② 之后是原计划，但 ② 现在卡在**等真实交易日**（run #4 已开、
  生产评分链已疏通），⑤ 不依赖它——它依赖的是「有决策可追」，而生产每天
  都在写 `decision_snapshots`。所以现在做它不违反原排序的**理由**。