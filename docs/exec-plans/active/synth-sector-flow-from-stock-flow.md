# 概念/行业资金流：从个股资金流合成历史

## 为什么

`scripts/replay_evolution.py:261` 读 `daily_snapshots` 的
`concept_fund_flow_hist` 来选方向。这个 key **一行都没有**：唯一的写入者
`scripts/backfill_sector_flow.py` 要东财，而东财按来源 IP 拒绝 Cloudflare
数据中心（2026-09-21 实测：worker 转发 httpbin 200、同花顺 401 反爬页，
东财 520/526/超时）。所以 `_top_sectors` 一直返回 `[]`，replay 从来没有
真正按资金流选过方向——它静默地退到了兜底池。

`scripts/backfill_daily_snapshots.py` 的 docstring 早就写明了出路：
"concept_fund_flow / industry_fund_flow — synthesized from
stock_individual_fund_flow (**not yet implemented**)"。本计划实现它。

## 已具备的前提（2026-09-21 实测）

- `stock_fund_flow_daily`：**175 个交易日**（20260105–20260921）、920,331 行、
  `net_amount` 无空值。来自 Tushare `moneyflow` 的 `net_mf_amount`。
  （`moneyflow_dc` 无权限；`backfill_tushare.py` 曾指着它，静默回填 0 行，本轮已修。）
- `concept_stocks`：390 概念 / 67,417 条成分，来自同花顺本地客户端
  （`alpha_agents/data/ths_local.py`）。与官方 `company_count` 在 94.3% 的
  概念上完全一致。
- 口径校准：按成分聚合个股 `net_amount` 对同花顺官方概念资金流，
  **10 个交易日 Spearman 中位 0.974、最低 0.942**，其中 3 天为样本外。

## 目标

为 `stock_fund_flow_daily` 覆盖的每个交易日，写出
`concept_fund_flow_hist` 与 `industry_fund_flow_hist` 快照，形状与
`backfill_sector_flow.py` 一致，使 replay 能在 2026 全年任意窗口选方向。

## 验收标准（可机器检查）

1. `daily_snapshots` 中 `concept_fund_flow_hist` 的日期数 ≥ 175，
   且每个快照 `len(sectors) ≥ 350`。
2. 在 09-08..09-21 的 10 个重叠交易日上，合成值与
   `sector_flow_snapshots`（同花顺官方）的横截面 Spearman **中位 ≥ 0.90**。
3. **前视闸**：概念建立日期晚于快照日期的，不得出现在该日 `sectors` 里。
   以 `AI应用`（建立 2026-01-12）为验证点：20260105–20260109 的快照
   **不含**它，20260112 起**含**它。
4. `uv run pytest tests/ -q` 全绿；两个 lint 通过。

## 设计决定

- **建立日期来源**：同花顺「概念解析」页 `q.10jqka.com.cn/gn/`，
  按 addtime 倒序分页。`ajax/1/` 变体要 `hexin-v` 签名（401），
  **非 ajax 的 `/page/N/` 可用**，但约 5 页后按 IP 限频返回 302。
  已取到最新 50 个（覆盖到 2023-11-14）——对 2026 窗口即为完备，
  因为倒序保证未列出的都更早。存为 `concepts.created_date`，
  **NULL 表示"早于已知最早日期"，一律放行**。
- **口径**：`main_net` 单位为元（与东财快照一致），
  由 `net_amount`（万元）× 1e4 得到。
- **覆盖闸**：当日命中成分 < 5 只的概念跳过，避免小样本噪声。
- **涨跌幅**：`stock_fund_flow_daily` 的 `pct_change` 全为 NULL
  （`moneyflow` 不提供），改取 `daily_kline` 当日成分涨跌幅的**中位数**
  ——中位而非均值，见 AGENTS.md 的证据规则。
- **行业**：用 `stocks.industry`（baostock 证监会分类，83 个，5216 只全覆盖）。
  与同花顺 90 行业不同名，replay 侧已有模糊匹配。

## 决策日志

- 2026-09-21 放弃"直接回填东财历史"：被按 IP 拒绝，换域名/加 header 都绕不过。
- 2026-09-21 放弃用 `moneyflow` 的 `lg+elg` 自行合成主力净额：
  个股级与 `net_mf_amount` 只有 Spearman 0.30，聚合后掉到 0.55–0.90。
  直接用 `net_mf_amount`（与旧表逐股 r=1.000）。

## 结果（2026-09-21）

四条验收全部满足：

1. `concept_fund_flow_hist` **175 天**（2026-01-05 … 2026-09-21），
   每日最少 **384** 个概念。
2. 重叠的 10 个交易日横截面 Spearman **中位 0.974、最低 0.942**，
   Pearson 0.963–0.997。
3. 前视闸实测：`AI应用`（建立 2026-01-12）在 20260105–20260109 的快照中
   **不存在**，20260112 起出现；对应概念数 384 → 385。
4. `pytest` 3027 passed / 18 skipped；`lint_harness` 与 `lint_docs` 均通过。

20 个交易日的回放（`walk_forward.py --start 2026-01-05 --days 20`）
`exit=0`，能力矩阵里 `fund_flow` 从 **0.0% (0/20)** 变成 **100.0% (20/20) PIT**。

### 顺带修掉的两个静默缺陷

两处读 `stock_fund_flow_daily` 的代码查的都是一个**从不存在的 `date` 列**
（真实列名 `trade_date`，且格式为 YYYYMMDD 而非 ISO）：

- `alpha_agents/tools/trader_tools.py::_fund_flow_series` —— 六个 trader 工具
  之一。`sqlite3.Error` 被兜成 `{"available": False, "reason": "资金流表不可用:
  no such column: date"}`，与「表真的没有」在调用方看来完全一样。**它从未
  返回过一行数据**，而且没有任何测试覆盖它。
- `alpha_agents/evolution/replay_capabilities.py` —— 同样的错误被记为
  `unreadable`，报告渲染成 0% 覆盖，与「窗口内无数据」不可区分。

`tests/test_fund_flow_columns.py`（5 个用例）从 schema 建表来钉住列名，
并要求模式错误必须写成 `unreadable: <原因>` 而不是静默降级。

### 第三个静默缺陷：`--allow-current-membership` 从未生效

`scripts/walk_forward.py::_decision_world_read_set` 调
`sector_membership.as_of` 时**漏传 `strict_pit`**，取了默认值 `True`，而
`_sector_cards` 在同一次运行里传了 `_membership_is_strict(ctx)`。结果是：
带 `--allow-current-membership` 起的 run 能正常建面板，一进 decider 就抛
`SectorSnapshotError: strict sector replay requires point-in-time membership`
—— 这个开关对所有 sector-first 架构**一次都没生效过**。

`as_of` 的 docstring 本来就写明「the choice is visible at the call site」，
这个调用点是唯一没说话的那个。修复即补上同一个谓词。
`tests/test_world_read_set_membership_flag.py`（3 个用例）钉住三件事：
带开关时接受 current-only、PIT 档案仍读作 strict、`as_of` 的默认拒绝不变。

### 日志：决策进了日志，不再只在 journal 里

跑一次 20 日回放，日志每天只有 `ordered 1` —— 模型选了哪个方向、依据
什么、什么条件算失效，全部只存在 `llm_journal/*.jsonl` 的 response 里，
那是个 response-hash 归档，不是跑的时候能 grep 的东西。

现在每天多打三类行（`_one_line` 压成单行，可 grep）：

```
2026-01-05: direction 军工信息化 — 5日相对中位收益4.15%为表中最高，上涨广度82.41%…
2026-01-05:   invalidated by: 地缘军事事件明显降温且无后续政策/订单催化 | …
2026-01-06:   002230 中国AI 50 — T-1收53.84、+7.06%、主力净额+99,950万（全名单最大量级）…
```

### 一个边界：首日没有资金流

回填从 **20260105** 起，而第一个回放日的排名日是它的前一个交易日
（2026-01-05 → 2026-01-02）。所以窗口首日的方向表没有资金流列，理由退回
价格/广度口径；从第二日起才有。能力矩阵按窗口日统计，看不到这一天的缺口。

### 第四到第七个：同一个家族

修完前三个之后，同样的形状又出现了四次——**调用方式或契约错了，被一层
宽容的处理兜住，日志说得像"数据没有"**。

4. **`_trader_note`（`scripts/walk_forward.py`）**：`load_traders()` 返回
   list，调用方写的是 `.get(ctx.trader)`，必然 `AttributeError`，被宽
   except 记成 "trader note unavailable"。**交易员人设从未进过 prompt**，
   而 `traders/pullback.yaml` 那 501 字规定了「用 `get_price_levels` 找
   真实支撑、ATR 定区间宽度、失效条件必须含主线」。
   见 `tests/test_trader_note.py`。

5. **六处 `max_tokens`**：reasoning 模型把思考和作答算在同一个预算里。
   实测 `cn:deepseek-v4.1-flash`，一个分析类提问在 `max_tokens=800` 下返回
   **HTTP 200、`finish_reason="length"`、800 token 全是 reasoning、
   `content == ""`**；同一个提问不设上限答了 778 字。所有上限已移除，
   空内容改由 `alpha_agents/llm_output.py` 统一判定为失败。
   最扎眼的是 `playbook.annotate_degraded` 的 `max_tokens=80`——必然为空，
   且因为没抛异常，连它自己的兜底值都拿不到。
   `/api/event-graph` 的 links 恒为 0 也是这个（`event_linker` 的 1024）；
   修后实测 8 个事件产出 2 条带理由的关联。
   见 `tests/test_llm_output.py`。

   顺带记一个网关事实：`reasoning_effort: none` 与 `enable_thinking: false`
   **这个网关不认**，静默忽略；只有 `thinking: {"type": "disabled"}` 生效
   （实测 reasoning token 从 800 降到 0）。

6. **`index_builder.build_index`**：仍连着只抓详情页第一页的爬虫，且函数
   开头就 `DELETE FROM concept_stocks`。手动跑一次 `main.py build-index`
   会把 67,417 行打回 3,657，而且**短名单和小概念在数据上长得一样**。
   现在从本地同花顺客户端读，读不到就 `RuntimeError` 拒绝重建，不退回
   爬虫。写入的是名单与本地文件的**并集**（客户端多出 15 个概念）。
   见 `tests/test_index_builder.py` 新增的两例。

7. **概念名里的一个空格（`sector_stock_selector`）**：2026-01-06 实测，
   模型答 `中国AI50`，而同花顺的名字是 `中国AI 50`，精确比对 →
   两只票全被 `invalid_primary_theme` 拒掉 → **当天 `ordered 0`**。
   那一天方向选对、票选对、理由里明确引用了 trader note 的规则，全部作废，
   日志上只剩一行 `ordered 0`。改为忽略空白比对，**存的仍是官方拼写**，
   其它差异照旧拒绝。见 `tests/test_theme_name_whitespace.py`。

### 仍然未解

- LLM 档回放跑不了：局域网网关 `503 no_healthy_account`，SiliconFlow
  `402 余额不足`（第一次调用 200 成功，所以管道本身是通的）。
  `sector_first_v0` 强制要求 `--decider llm`，因此概念资金流方向这条路
  只验证到 `_concept_flow_directions` 能返回正确名单，未验证到成交。
- `index_builder.build_index` 仍连着只抓前 10 只的爬虫，且会
  `DELETE FROM concept_stocks`。手动跑 `main.py build-index` 会把 67,417
  行打回 3,657。`_ensure_index` 只在表为空时触发，所以不会自动引爆。
- 概念建立日期只拿到最新 50 个（覆盖到 2023-11-14），页 6 起按 IP 限频。
  对 2026 窗口已完备。
