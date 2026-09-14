# 挂单必须能被了结：预留预检、每轮论点体检、真正的到期

状态：**已交付**。创建 2026-09-14，同日完成。

## 目标

一句话：**一个 pending 挂单，要么能被成交，要么能被撤掉，要么被告知它不能。**

现在有两笔挂单挂着没成交。查下来不是两个 bug，是**一条从未被写下的不变式**：
「pending 挂单必须恰好持有一条 `held` 现金预留」。三个地方依赖它，没有一个地方保证它。

1. `_fill_order` → `consume_reservation` 要求 `held` 预留，否则抛 `ReservationStateError`。
2. `_cancel_order_unlocked` → `release_reservation` 同样要求 `held`，否则也抛。
3. `reservations.reserve_for_order` 只在 `_create_pending_order_impl` 里被调用。

于是一笔**早于预留机制**的挂单（#117 中石科技，建于 2026-09-11 09:49，而
`reservations` 模块 2026-09-11 22:58 才随 `0fbe89a` 落地）**既成交不了、也撤不掉**。
更糟的是它按 conviction 排在同交易员挂单的第一位，任何一次触发都会把异常抛出
`check_pending_orders` 的循环、被 `manage_book` 的 `except Exception` 吞成一句
「组合管理失败」，并且**跳过该交易员该轮的全部剩余动作**（成交、平仓检查、论点监控）。

同时还有两处「看不见」：

- **论点失效判定只在价格进了带子之后才执行**（`_thesis_already_broken` 嵌在
  `if triggered:` 里）。所以「价格从未进带子、但论点早已失效」永远不被检查。
  #117 的止损是 94.0，现价 85.49 —— 它自己的失效条件已经连着几天成立，没人看。
- **`expire_days` / `days_pending` 算了从不读**（§7 已知）。只要主线还活着，挂单无期限。

第四处是度量：`portfolio_risk._ENTRY_OUTCOMES` 里
`"当日未成交": "价格未到"` **全仓库没有任何生产者**。所以「价格从未到达」这个失败方向
**在指标里不存在**，注入给 agent 的介入质量文案也就只能单向地说「把区间上移」。

## 验收（全部达成，实测数字见括号）

- [x] `.venv/bin/python -m pytest tests/ -q` → 全绿（**1986 passed / 18 skipped**，基线 1964，净增 22）
- [x] `.venv/bin/python scripts/lint_harness.py` → **167 文件、存量 13 条不变、0 新增**
- [x] `.venv/bin/python scripts/lint_docs.py` → 通过
- [x] `tests/test_pending_orders_finishable.py`：无预留的 pending 挂单被 adopt 出一条 `held`
      预留，且 adopt 后**能成交**与**能撤单**（各 1 个用例，另加「只 adopt 一次」一条）
- [x] 同上文件：预留处于 `consumed` 却仍 `pending` 的行被**跳过并告警**，
      **不得**抛异常穿透 `check_pending_orders`（断言队列里后一笔健康挂单仍被成交）
- [x] `tests/test_pending_orders_finishable.py`：价格**从未进入**带子、但论点的 `price_below`
      已成立时，挂单**被撤**（理由来自论点描述），且论点被 `close` 为 `invalidated`、
      `close_kind` 记下是哪条条件
- [x] 同上：价格未进带子、论点**未**失效时，挂单**不得**被撤（成对断言；85.49 与上一条同价，
      唯一差别是论点还活着）
- [x] 同上：`days_pending >= expire_days` 时挂单被撤，理由可被 `classify_cancellation`
      归到「价格未到」
- [x] 同上：`days_pending < expire_days` 时**不得**被撤（成对断言）
- [x] 建单时 `expire_days` 取论点 `horizon_days`（测试用 3），其次交易员
      `default_horizon_days`（测试用 4），两者都没有才回落到 `PENDING_EXPIRE_DAYS`（2）
- [x] `entry_quality` 的 `missed_right` 同时包含「介入价太低」与「价格未到」两个方向，
      注入文案双向
- [x] 变异探针：B/C/D/E 每项改一处唯一锚点 → 对应用例变红 → 复原后锚点仍在
      （实跑 **6** 个，含 E 的「映射键」两条：见 D10）

## 决策记录

**D1. 无预留的挂单：adopt，不是 cancel。**
预留的作用是「别让两笔挂单花同一笔钱」。没有预留 = 这笔挂单的现金**没有被扣住**，
`get_available_capital` 因此**高报**了它的兜底额——正是 `reservations` 这个模块存在的理由被破坏。
adopt 是把这个约束补回去；cancel 是为一笔记账缺失而销毁一个下注。补账是对的。
金额用创建路径的同一公式（`trader_capital * MAX_POSITION_PCT * (1 + SLIPPAGE_RATE)`），
且 `reserve_for_order` 本身幂等（`ON CONFLICT DO NOTHING`），重复预检安全。
**必须打醒目日志**：孤儿是上游的 bug，修好不等于没发生过。

**D2. 不削弱 `consume_reservation` / `release_reservation` 的 fail-loud。**
「缺预留是 bug，不是恢复路径」（docstring 原话）这个判断是对的——静默降级会让真正的双花
变成一条 debug 日志。所以修复放在**预检**：在到达 finish 路径之前把状态修好，
让 finish 路径继续假定「held 一定在」。契约不变，雷拆在前面。

**D3. 预留处于终态却仍 pending：跳过，不猜。**
这是「同一笔被了结两次」，成因无法从数据推断（可能是历史事故）。自动撤单需要一次
`release`，而对终态行 release 会抛——所以不能撤。跳过 + 每轮告警，交给人。
生产库目前 0 例。

**D4. 论点体检搬到每轮，而不是新建一条「跌破带子下沿」规则。**
我原本提的是 C：「价格跌破带子下沿即撤」。想清楚后否掉了——那等于**再写一套阈值**，
而论点里**已经**声明了失效条件（#117 的 `price_below 94.0`，note 写着「跌破则突破失败
买入理由不成立」）。重复实现会产生第二个真相来源。正确做法是让既有判定不再被
`if triggered:` 门住。副作用：`_thesis_already_broken` 会顺手 `close` 论点，所以每个
挂单每轮会多一次论点读；命中一次后论点不再 active，后续轮次自然短路。

**D5. 到期跟「论点自己的期限」走，不跟一个全局常数走。**
`PENDING_EXPIRE_DAYS = 2` 且注释写着「仅用于无主线的挂单」——而无主线的挂单在更早一行
就被撤了，所以它从来没用过，且 2 天对突破派（`default_horizon_days: 3`）和回调派（5）
都是错的数。改为 `expire_days = 论点.horizon_days`，其次交易员 `default_horizon_days`，
最后才回落常数。判据 `days_pending >= expire_days`（「有效 3 天」= 第 0/1/2 天有效，第 3 天起到期）。
这样这个字段终于有了它字面上的意思：**这个想法值得等几天**。

**D6. 到期不新增 `expire` 意图种类。**
`intents.kind` 的 CHECK 约束刻意不含 `'expire'`，理由在 `memory_schema.py`：到期从未被读，
声明这个种类等于承诺一个代码没有的能力。现在到期被读了，但**到期本来就是一撤单**——
`_cancel_order` 已有完整审计（`episodes.note_cancel` + 预留释放）。而且 SQLite 改不了
CHECK 约束，改它要重建表，收益为零。所以：照旧记 `cancel`，理由字符串里带「未到价」。
被推翻的是那句注释里的「then never read」，不是「不新增种类」。

**D7. 「价格未到」要双向进指标。**
`_ENTRY_OUTCOMES` 的 `"当日未成交"` 没有任何生产者，说明这个桶是**照着愿望写的**。
而它是唯一能装「区间挂太远、价格从未来」的桶——正好是突破派最典型的输法。
改法不是新造指标，是：①给真正会写出的理由（含「未到价」）加一条映射；
②`missed_right` 同时计「介入价太低」（涨走）与「价格未到」（从未来）两个方向；
③注入文案改双向。**注意这不等于建议突破派改成回调派**——文案要同时给出
「把区间下移」与「承认这个设定不会来、skip」两条路，选择权仍在 agent，这与
`traders/README.md` 的分界线一致。

**D8. 不碰「带子在现价上方」这件事本身。**
`breakout.yaml` 的 `entry_low >= 现价` 是这本书的**定义**，不是 bug。本次只修
「它永远不能被了结」和「它的失败数不出来」，不改它的挂法。

**D9. 成交压过到期（实现中改的口径，否掉了本轮最初的写法）。**
D5 只定了判据，没定它与成交检查的先后；代码里先写的是「到期在前」。写测试时发现：那条撤单
理由字面上写着「未到价」，而那一轮价格**正在带子内** —— 等于往学习数据里写一条被自己那一行
反驳的标签，`entry_quality` 的每一次读数都会继承它。改成 `if triggered: 成交 elif 到期: 撤`，
`未到价` 于是**只在价格确实没来时**才写。附带效果是「没有实时价就不写未到价」，即停牌股的
挂单会一直挂着——这个洞写进了 `TRADER_CORE_IMPLEMENTATION.md` §7。

**D10. 标签表按代码真正写出的子串重配（实现中发现，范围比 E 大）。**
把真实理由逐个过 `classify_cancellation`：**8 个理由里 5 个落到「其他」**，包括**全部主线撤单
与论点撤单**。键是照着想象的字符串写的：代码写 `论点在成交前已失效`、`主线明显走弱(评分…)`、
`主线已declining(…)`、`关联主线'X'不存在`，没有一个包含它配的那个键。所以 E 不只是「给
`价格未到` 找一个生产者」，而是「让这张表对上代码」。已按真实子串重配（旧拼写保留，因为表里
的历史行是它们写的），并新增 `TestEveryCancelReasonReachesItsLabel`：每个用例**跑一次真实
撤单**，再问它的 `close_reason` 归到哪个桶 —— 措辞改了而表没跟，这组用例就红。

## 交付记录

- 新增 `tests/test_pending_orders_finishable.py`（22 条）；`test_episodes.py` 里那条
  「有效期仍是死代码」按它自己 docstring 的指示**翻面**为「到期产生一次撤单」。
- 体量：本轮给 `check_pending_orders` 加了真职责，`portfolio.py` 再次破线（1221 行），
  于是按职责拆出 `data/portfolio_book.py`（订单簿的行 + 撤单这条唯一的「不行使就离开」路径），
  重导出让既有调用点不变。现 `portfolio.py` **1123 行** / `portfolio_book.py` **144 行**。
- 拆家的两个副作用，都要记住：①按 `alpha_agents.data.portfolio._get_conn` 打桩不再能重定向
  读路径（`get_open_positions` 在 `portfolio_book` 里绑定 `_get_conn`），`test_portfolio.py`
  两处补了自己的桩；②按模块名写死的静态守卫（`test_intent.py` 的允许名单）随搬家失真，
  顺手发现它**本来就在靠一个巧合通过**——成交路径的 `UPDATE virtual_portfolio SET` 写成
  相邻字面量，`SET[^"']*status` 这类正则跨不过去，改成 AST 读后两条写入都看得见。

## 风险（交付后仍要看的）

- **到期会减少挂单存活时间**，而突破派与回调派的假设都建立在「愿意等」上。
  缓解：期限取自各自声明的 horizon（突破 3 天、回调 5 天），不是拍脑袋的 2 天；
  且主线门已经独立处理「主线变弱」，两者不重叠。
  **会在下一次复盘的数据里显形**：撤单理由出现新的「未到价」一类，且占比应与
  `价格未到` 桶对得上。**若 `未到价` 撤单量大到挤掉成交，说明期限定短了，回滚点在
  `expire_days` 的取值，不在这个机制本身。**
- **论点体检每轮执行会提前平掉一批论点**（`_thesis_already_broken` 内部会 `close`）。
  判据：`theses` 里 `close_kind` 分布是否出现异常堆积；预期是新增一批
  `成交前失效：价格 X 低于 Y`，数量应与「挂单挂着但价格早已远离」的笔数量级一致。
- **adopt 会改变 `get_available_capital`**（对 breakout 是 −100050 元）。
  这是修正高报，不是新损失；判据：breakout 可用资金下降恰等于该挂单兜底额。
- 本次**不改** `traders/` 下任何 YAML，因此 `entry_side` 的分布不应变化。
  若变化，说明改动越界了。
