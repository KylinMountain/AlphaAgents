# 概念成分表重建：从「每概念前 10 只」到完整成分

状态：active
创建：2026-09-21
触发：owner 让探索同花顺本地数据，翻到 `block_conception.ini` 后与仓库对比

## 缺陷（实测，不是推测）

`index_builder.py:84` 的 `_fetch_concept_constituents_ths` 只抓同花顺概念详情页
**第一页**，它自己的 docstring 就写着：

> Returns top stocks from the first page (usually 10-20).
> THS blocks ajax pagination but the first page with auth cookie works.

后果实测：

| | 仓库 `concept_stocks` | 同花顺本地完整 |
|---|---|---|
| 概念数 | 375 | 390（**375 全覆盖**）|
| 成分行数 | 3,657 | **70,382** |
| 中位数/概念 | **10** | 91 |
| 恰好停在 10 | **303 / 375** | — |
| 最大概念 | 10 | 3,870 |

**这不是小误差，是系统性截断。** 303 个概念恰好等于 10，这是分页边界，不是
市场事实。而第一页按同花顺的默认排序返回，**排序本身带偏差**。

### 它污染了哪条链

这是刚统一的那条选股链的**输入端**：

- `sector_scoring.score_members` 的候选池只有 10 只；
- `beta_calculator._get_concept_stocks` 算板块 beta 时用的是「前 10 只」，
  而不是真板块——**板块基准本身就是偏的**；
- live 的 `get_sector_best_stocks(sector, top_n=5)` 从 10 只里挑 5 只；
- 今天实测的 `panel=13/16/19` 同样受这个上限约束。

实测几个常用概念的完整规模：`5G` 454、`存储芯片` 203、`人工智能` 1085、
`国企改革` 1473。**我们一直只在它们的前 10 只里选股。**

## 数据来源

同花顺 macOS 客户端本地文件（需 macOS 完全磁盘访问权限）：

```
~/Library/Containers/cn.com.10jqka.macstockPro/Data/Documents/BlockUpdate/
    block_conception.ini   720KB  [BLOCK_NAME_MAP_TABLE] + [BLOCK_STOCK_CONTEXT]
    block_industry.ini      64KB  行业，同格式
    block_every_day.ini    352KB  昨日涨停/资金前十等
    block_tree.ini                板块层级，3586 行
~/Library/Containers/.../Documents/stockname/{32,64,128}_0.ini   7.9MB  code=名称
~/Library/Containers/.../Documents/JYCurrentYearAllTradeDate/     全年交易日
~/Library/Containers/.../Documents/StockLink/stocklink.ini  908KB  A/H 股映射
```

**格式**：`.ini`，GBK 编码。`[BLOCK_NAME_MAP_TABLE]` 是 `板块码=名称`；
`[BLOCK_STOCK_CONTEXT]` 是 `板块码=市场:代码,...`，市场码 `33`=深、`17`=沪、
`-105`=北交所。`ConfigVer=20260921.160002` 说明是当天 16:00 拉的。

**限制**：只有**当前快照**，每天覆盖，**无历史**。所以它修不了 PIT 问题，
只能修「池子太浅」。（PIT 那条仍按 2026-09-21 的读法 A 处理。）

## 做法

新增一个**离线导入器** `alpha_agents/data/ths_local.py`：

1. 定位并解析 `block_conception.ini`（GBK，两段式）；
2. 返回 `{概念名: [股票代码]} `，纯函数、不写库；
3. 一个 `import_membership(db_path, ...)` 把它写进 `concepts` / `concept_stocks`，
   **只增不删已有概念**，并返回可核对的统计。

**为什么不改 `build_index` 去爬分页**：同花顺明确挡 ajax 分页（docstring 记录），
而本地文件已经是权威完整数据，零请求、零风控。爬虫那条路应该是 fallback，
不是主路径。

**为什么独立模块而不是塞进 `index_builder`**：`index_builder` 是「联网构建」，
这是「离线导入」，失败模式不同（前者怕风控，后者怕文件格式变更）。

## 硬约束

1. **只写 `stocks` 表里已有的代码**。实测 5,575 个 THS 代码中 5,215 个在
   `stocks` 表里，360 个不在（多半是港美股/退市）。沿用 `index_builder` 的
   同一规则，否则外键悬空；
2. **保留现有 375 个概念名**，新增的 15 个（`AIGC概念`/`CRO概念`/`Web3.0` 等）
   一并写入——它们是真概念，且 `block_conception.ini` 是权威源；
3. **可回滚**：导入前把 `stocks.db` 备份到 `data/backup-<ts>/`；
4. **不改 schema**，不加时点列（那是另一件事，见 PIT 计划）。

## 验收

1. `concept_stocks` 行数 ≈ 70,000（实际=5,215 个可匹配代码的交集）；
2. 375 个原概念名**全部保留**，且每个的成分数 = 其完整规模；
3. 抽查 `F5G概念`=36、`6G概念`=99（实测值），不再是 10；
4. 无悬空行：`SELECT COUNT(*) FROM concept_stocks cs LEFT JOIN stocks s ON
   s.code=cs.stock_code WHERE s.code IS NULL` = 0；
5. `pytest tests/` 全绿（`test_index_builder` 的 mock 路径不受影响）；
6. `lint_harness` / `lint_docs` 通过；
7. 在**同一天**重跑 replay 的 sector 面板，候选数应从 10 上限放开
   （panel 从 13/16/19 变成受 `panel_size` 约束）。

## 落地结果

### 导入（已执行）

```
concepts_in_file      390
concepts_importable   390
concepts_new           15   (AIGC概念 / CRO概念 / Web3.0 / 光伏建筑一体化 ...)
members_in_file     70382
members_kept        67406
codes_dropped_unknown 2976   (港美股/北交所 920xxx/未上市，stocks 表没有)
rows_before          3657
rows_after          67417
concepts_total        390
```

备份在 `data/backup-20260921-180031/stocks.db`。

### 验收

```
1. PRAGMA integrity_check      : ok
2. PRAGMA foreign_key_check    : clean
3. concept_stocks 行数          : 67417
4. 概念数                       : 390
5. 悬空行 (LEFT JOIN stocks)    : 0
6. 中位数/概念                  : 86   (原 10)
   恰好停在 10                  : 2 of 390   (原 303 of 375)
7. 抽查 F5G概念=36 / 6G概念=94 / 存储芯片=199 / 中芯国际概念=88 / 汽车芯片=142
   —— 与「完整值减去 stocks 表未知代码」逐一吻合
8. 原 375 个概念名全部保留
```

**那几处 MISMATCH 不是 bug**：`6G概念` 完整 99、入库 94，差的 5 个是
`920021/920438/920725/920961/301689` —— 北交所与次新代码，`stocks` 表里没有。
沿用 `index_builder` 同一规则剔除，与计划硬约束 1 一致。

### 池子放开后，选出来的股票变了

同一天、同一份代码、唯一变量是成分表：

```
                    修复前                     修复后
2026-09-09  panel=19                   panel=40 (受 panel_size 约束)
            selected=[F5G概念,5G,6G概念]  selected=[国企改革,金属铜,小金属概念]
2026-09-16  panel=12                   panel=40
            selected=[PET铜箔,PCB,华为]   selected=[存储芯片,汽车芯片,中芯国际概念]
2026-08-18  panel=13                   panel=40
            selected=[F5G概念,5G,6G概念]  selected=[玻璃基板,共封装光学(CPO),光刻机]
```

**注意 09-09 和 08-18**：修复前选中的 `F5G概念/5G/6G概念`，其真实规模是
36/454/99，而我们一直只在各前 10 只里选股。修复后选中的 `国企改革` 有
**1440 个成员**——之前是前 10 只。

**这不是「数字变大了」，是候选域变了。** 之前 5G 的 10 只（页面第一页，
按同花顺默认排序，带排序偏差）现在换成了完整 454 只里的评分前 N。

### 测试与 lint

`pytest tests/` → **3009 passed, 18 skipped**；
`lint_harness` → 通过（221 个文件）；`lint_docs` → 待跑。

## 决策记录

- 2026-09-21：确认截断是 `index_builder` 抓第一页所致，**不是数据源缺失**。
- 2026-09-21：选择离线导入本地文件，而非修爬虫分页——后者被同花顺明确阻挡，
  且本地文件已是完整权威数据。
- 2026-09-21：**本计划只修池子深度，不动 PIT 语义**。成分仍是「当前快照」，
  lookahead 那句标注继续有效（`replay_capabilities` / `_limitations` 会照旧说出）。
