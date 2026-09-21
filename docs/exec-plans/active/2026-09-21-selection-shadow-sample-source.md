# Sector 路径没有 selection 基因，它的候选池写了就丢

状态：active
创建：2026-09-21
来源：remaining-seven ②。目标写「为 ② 补上缺失的样本源」。

## 先更正上一轮的方向错误

上一轮我把「补样本源」理解成「把 dual-rank 双通道面板接进生产选股」。
**方向错了**，而且错在把一条并行路径当成了项目目标。查清后：

1. **双通道不是本 session 写的。** 引入提交是 Kylin `765cd97`（2026-09-19，
   "make T1 ranking a real evolvable selection gene"），随 147 提交的 RP-09/RP-10
   合并（`6f9c9ed`）进入本地。本 session 的 7 个提交逐个查过文件列表，
   **没有一个碰过 `selection_policy.py` / `sector_panel.py`**；唯一相关的是
   `d974217`，只动 `selection_shadow.py` / `dream_selection.py` 的诊断。
   旁证：session 开始前的 `backup/pre-pull-20260920-102514` 树里，
   `selection_policy.py` **不存在**。
2. **项目目标是 Sector-First**（见下 B 节），不是双通道。
3. **sector 路径的候选池写了就丢**（见下 C 节）——这才是样本源真正的断点。

## 尽调（都是实测）

### A. 两套「change/turnover 双通道」并存，且都不是生产路径

| | 位置 | 合并方式 | 基因 | 引入 |
|---|---|---|---|---|
| 交替双通道 | ~~`data/selection_policy.py`~~（**2026-09-21 已删除**） | `weighted_merge` 交替，参数 `change_share` | ~~`selection_rank.change_share`~~（已删除） | Kylin `765cd97` 2026-09-19 |
| 板块内平均排名 | `alpha_agents/data/sector_panel.py` | `(change_rank + turnover_rank) / 2` | **无** | Kylin `bbf327e` 2026-09-19 |

两者语义不同，不是同一份代码。**两者都只被 `scripts/walk_forward.py` 调用**
（`738`/`740` 与 `1299`），生产四个入口
（`pipeline/`、`agents/`、`server/`、`main.py`）里 `selection_policy.` 的调用数 = **0**。
生产实际按 `score` 排序（`intraday_monitor.py:510`）。

### B. 项目要的是 sector 路径，而它没有 selection 基因

- `docs/README.md` 开篇：「**2026-09-20 验证前审查结论：REWORK。** 对 Sector-First
  是否具备正式验证资格……」——sector-first 是当前目标，且尚未通过验证前审查。
- `alpha_agents/evolution/sector_experiment.py` 的四臂 A=`dual_rank_v0`、
  B/C/D=`sector_first_*`，在其中 grep `selection_rank` **全部为空**。
- `gene_registry.py` 里 grep `sector` **无结果**；`variant.py` 同样无 sector delta。

所以：**sector 路径目前没有任何 selection 基因**。已经进了 PolicyVersion 的
`selection_rank.change_share` 挂的是那条交替双通道 replay 路径——既不是 sector 的，
生产也不读。

### C. sector 路径的候选池写了就丢（关键，且此前无人记录）

`_build_sector_panel` 末尾（`walk_forward.py:1353`）：

```python
ctx.last_panel_candidate_pool = []          # 显式清空
ctx.last_sector_candidate_pool = [candidates[code] for code in sorted(candidates)]
```

而 journal 写进 `context` 的是（`walk_forward.py:2059`）：

```python
"candidate_pool": getattr(ctx, "last_panel_candidate_pool", []),
```

`last_sector_candidate_pool` 全仓库只有两处出现：写入（`walk_forward.py:1354`）
和测试初始化（`test_minimal_selection_wiring.py:62`）。**没有任何读取者。**

后果链条，逐环可查：

```
sector 架构运行
  -> _build_sector_panel 把 last_panel_candidate_pool 置为 []
  -> journal 的 context.candidate_pool 恒为 []
  -> selection_shadow.process 里 if not context.get("candidate_pool"): continue
  -> 每一条 sector 集合都被跳过
  -> sample_count 永远是 0
```

**所以对 sector 路径而言，「样本源缺失」不是「生产没写 journal」，而是
「写了也会被跳过」。** 这与上一轮记的「生产没有 opportunity_sets 表」是
两个独立的断点，且后者更根本——即使补上写入者，sector 集合仍进不了 shadow。

## 结论：这是新设计，不是接线

要真正买到 ② 的 sector 样本，需要三件**新设计**，缺一不可：

1. 给 sector 路径定义 selection 基因（当前没有）；
2. 把 `last_sector_candidate_pool` 写进 journal 的 `candidate_pool`，
   或让 shadow 读 sector 池——二者必须语义一致；
3. 把该基因接进生产选股，使 journal 成为忠实记录（`DREAM_RSI_SELECTION.md` §6
   第 1 条 "the live/replay trading path actually reads it"）。

这三件都改行为，且第 1 件要先回答「幅度从哪来」——按 §6 的告诫
（"Inventing a magnitude here would freeze a guess that later reads as a
measurement"），不能拍脑袋定。**所以本次没有实施，先记录。**

## 本次实际交付（诊断，已提交 `d974217`）

- `selection_shadow.sample_source_present(conn)`：查 `sqlite_master`，
  回答「这个部署有没有机会集日志」。
- `_require_sample_source(conn)`：无日志时抛 `SelectionShadowError`（原来抛
  `OperationalError: no such table: opportunity_sets`），消息点名唯一生产者与前置条件。
  放在**使用点**（查询之前），因为上面的不变量是关于 run 自己账本的，
  无论环境有没有日志都该报。
- `summary()` 新增 `sample_source_present` / `sample_source`：一个永远攒不到
  样本的 run 必须自己说出来。
- 改掉两处不实 docstring：`dream_selection.panel_policy_counterfactual` 的
  "the **live** panel … all execute" 改为 replay 并写明生产未接入；
  `selection_shadow` 模块 docstring 写明唯一生产者是回放。

## 验收

已达成（本次交付的诊断）：

1. 无 journal 的部署里 `process()` 抛 `SelectionShadowError`（不是
   `OperationalError`），消息含 `opportunity_sets`；
2. 同一部署 `summary()["sample_source_present"] is False`，`sample_source is None`；
3. 有 journal 时 `sample_source_present is True` 且 `sample_source == "opportunity_sets"`；
4. 两处 docstring 不再声称 live 路径执行该基因（`grep -n "the live panel"` 无结果）；
5. `pytest tests/ -q` → **2994 passed, 18 skipped**；
   `lint_harness` 222 文件通过；`lint_docs` 通过。

**尚未达成**（本计划记录的新设计，未实施）：

6. `python scripts/selection_shadow.py process --run <sector-run>` 的
   `sample_count > 0`——需要先有 sector selection 基因 + 池子入 journal；
7. `grep -c last_sector_candidate_pool alpha_agents/ scripts/` 出现**读取者**，
   当前只有 1 处写入、0 处读取。

## 决策记录

- 2026-09-21：**不造生产写入者。** 生产既不跑该架构、候选行又缺基因需要的字段，
  任何写入者都会产出不可评分的集合。
- 2026-09-21：**更正方向。** 上一轮把 dual-rank 当作目标路径；实测表明项目目标是
  Sector-First，而 dual-rank 双通道是 Kylin 在 RP-09/RP-10 引入的并行路径。
  双通道本身不是我的产物，本 session 未改动其任何文件。
- 2026-09-21：**记录 `last_sector_candidate_pool` 是死状态。** sector 路径的候选池
  被算出、赋值、丢弃，从未进入 journal；而 shadow 只认 `candidate_pool`，
  所以 sector 集合即使被 journal 也会被跳过。这是 ② 在 sector 方向上的真正断点。
- 2026-09-21：**概率 shadow 那条路已解锁**（同日另两笔提交）：跑通 review 评分后
  实测 `paired` 从 **0 → 5**，所以 ② 在概率方向只缺真实交易日推进。
