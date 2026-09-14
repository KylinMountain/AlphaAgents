# playbook 聚类的第三维：把一个常量 NULL 换成活的字段（D5）

状态：**已交付（2026-09-14）**。负责人：本次开发会话。创建：2026-09-14。

**收口要点**：判据 1–7 全部落地。`change_band` 是唯一边界定义，写入方
（`intraday_monitor` + `replay_evolution.py`，两份副本都改了）与聚类、matcher 读同一个键。
判据 4 是**行为判据**：只差档位的 6 行形成 **2** 个簇（退化成一维时会是 1 个）。
两个变异探针被捕获：维度改回 `vpa_verdict`（少一个维度）、matcher 把缺失字段当通配（fail-open）。
全量 **1930 passed / 18 skipped**。**已知代价已写进 tech-debt**：加一维让自动创建更慢。

> 范围：只改「按什么分组」与「组变成 pattern 时写哪个条件」。
> 不改自动创建的阈值、不改容量与淘汰规则、不改匹配的排序。
>
> **这会改行为**（哪些候选能匹配上哪个 playbook），所以先写这份文档。

## 起点：先量，再改

D5 的原文有两条断言，第一条成立、**第二条实测不成立**：

| 断言 | 实测（生产库副本，2026-09-14） |
|---|---|
| 聚类从三维塌成两维（`theme` × `institutional`） | **成立**。`_query_hit_clusters` 的 `GROUP BY` 里有第三项 `json_extract(features_json,'$.vpa_verdict')`，而**没有任何写入方记录这个键**（`intraday_monitor` 与 `morning_scan` 的 features 都没有它）→ 该表达式恒为 NULL → 第三维是一个常量 |
| 每条带 `vpa_verdict` 条件的历史 playbook 再也匹配不上 | **不成立**。生产库 `playbooks` **总共 1 条**，条件是 `(institutional, theme)`，带 `vpa_verdict` 的**一条都没有** |

第二条不成立的原因正是第一条：那一维是常量，所以它从来没有真正参与过分组，
也就从来没有产出过带 `vpa_verdict` 条件的 pattern。**能力存在，数据不存在** ——
这与本仓库其余几处同形，但结论不同：这里没有「坏掉的历史」，只有**一个死的维度**。

代价因此要重写：不是「历史规则失效」，而是**簇比设计意图更粗**
（theme × institutional 两个条件盖住的人和事太多），于是自动创建出来的 pattern
描述的不是「哪一类机会」，而是「什么主题 + 有没有机构」。

## 判据（机器可验收）

1. 存在 `playbook.change_band(change_pct) -> str`，边界只有一处定义，
   返回 `down`（<0）/ `flat`（0–2）/ `mid`（2–5）/ `strong`（≥5）/ `unknown`（非数字）。
2. 生产写入方 `intraday_monitor` 与回放 `scripts/replay_evolution.py` 都记录
   `features["change_pct_band"]`；**两者与聚类、matcher 读的是同一个键**。
3. `_query_hit_clusters` 的 `GROUP BY` 有**三个**维度，其中第三个活着的键是
   `change_pct_band`；`vpa_verdict` 不再出现在这个模块里。
4. 行为判据（不是静态断言）：**只差 `change_pct_band` 的两笔预测落进两个不同的簇**。
5. `_pattern_from_cluster` 生成的 pattern 里，第三个条件指的是 `change_pct_band`；
   `lessons.py` 与 `playbook.py` 自动生成的名字里也带上这一维（同一个簇在别处改名会
   让人觉得是另一个簇）。
6. 没有该键的历史行**仍然能形成簇**，只是生成的 pattern 少一个条件（不丢证据）。
7. 全量 `pytest` / `lint_harness` / `lint_docs` 通过。

## 决策日志

**D1 —— 存下来，不在两处各推一次。**
「涨幅分档」可以在 SQL 里用 `CASE` 现算、也可以在 Python 里现算 —— 两处各写一遍阈值，
就是本仓库反复抓的「同一个问题两个答案」。一旦两边漂移，聚类会按一套边界分组、
matcher 按另一套匹配，于是**自动创建出一个永远匹配不上的 pattern**（而这次是真的会发生，
不像原来那条断言）。所以：**在决策时记下 `change_pct_band`**，
聚类（SQL）、pattern 条件、matcher 读的是同一条已存的值；边界的唯一定义在
`evolution/playbook.py::change_band`，写入方 import 它（`pipeline → evolution` 方向合法，
`intraday_monitor` 本来就已经 import `match_playbook`）。

**D2 —— 为什么选「涨幅分档」而不是 `score` 分档或异常类别。**
丢掉的 `vpa_verdict` 描述的是**入场时的形态**（量价确认过的多头结构）。三个候选里：
`score` 是选择器自己的打分，与已有的 `confidence`/`dims_passed` 高度同源，
分档等于把一个维度数两遍；异常类别（涨跌停池）与 `rec_type='signal'` 重合，
而聚类本来就为「signal 行会淹没有用模式」把 `report_type='intraday_signal'` 排除在外；
`change_pct` 是**市场状态**、与选择器的判断独立，也正是「这只股票是不是已经涨过头了」
这一维度。**边界取 0 / 2 / 5**：与 `intraday_monitor` 里已经存在的
「`change_pct > 1.5` 才算候选板块」同一量级，不引入新的经验常数。

**D3 —— `morning_scan` 不改。**
它不记 `change_pct`（早盘没有当日涨跌幅），而 `match_playbook` 的**唯一调用者是
`intraday_monitor`**，聚类也只读 `report_type='intraday'`。所以给它补一个
`change_pct_band` 只会写下一个假值（`unknown`），且没有任何读者。
**不写一个没有读者的字段** —— 这正是 D8 那条「列存在但从未被写入」的镜像。

**D4 —— 不退役任何东西。**
生产库里没有带 `vpa_verdict` 条件的 playbook，所以没有需要清理的对象；
改这一维不会让任何现存规则失效（那条唯一的 playbook 的签名是
`(institutional, theme)`，两个条件都还在）。**先量再改**避免了「顺手做一次迁移」。

## 不做的事

- 不动 `scripts/backtest_vpa.py`（离线研究：它的 `vpa_verdict` 是自己的中间结果，
  与 `features_json` 无关）。
- 不改自动创建阈值（`_AUTO_CREATE_MIN_WINS` / `_AUTO_CREATE_MIN_TOTAL`）。
  加一维会让簇变细、达标更难，**这是这条改动的已知代价**，要靠调阈值来抵消的话
  得先有数据说明该调多少 —— 现在没有。
- 不回填历史行。旧行没有这个键，它们各自落进一个 NULL 档；`_pattern_from_cluster`
  对空值不生成条件，所以旧簇的 pattern 仍然只描述它真正钉住的那几维。

## 风险

- **簇变细 ⇒ 自动创建更慢。** 生产 `predictions` 里带 `prob` 的只有 47 行、
  带 `hit` 的更少，而自动创建要求 `hits >= _AUTO_CREATE_MIN_WINS`。加第三维之后
  短期内**大概率不会**再自动创建出新的 playbook。这是「更具体 vs 更快积累证据」的
  取舍，选择更具体是因为原来那两维的 pattern 太宽 —— 但这条要写下来，
  免得下次看到「一个月没新建 playbook」以为是坏了。
- **旧行与新行不同档。** 旧行 `change_pct_band` 为 NULL，新行有值；
  在过渡期里同一个 `theme × institutional` 会裂成「NULL 档」与「有值档」两组，
  两组的 `hits` 各自计数。这会**延后**达标，不会造假。
