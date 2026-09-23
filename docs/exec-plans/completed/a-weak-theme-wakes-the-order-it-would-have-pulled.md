# 主线变弱时，唤醒 agent，而不是替它撤单

## 现状

`check_pending_orders` 每 5 分钟问一次 `theme_gate(theme, "cancel")`：主线
`declining` / `archived`，或者当天评分低于 `cancel_score`，就直接
`_cancel_order`。agent 从头到尾没有机会开口。

本机线上 2026-09-10 → 09-23，回调派 + 突破派：

```
组合回撤触及上限           20   ← 1141626 已修（净值把预留当亏损）
主线已archived / 明显走弱   ~25  ← 本计划
论点在成交前已失效          ~10  ← agent 自己写的条件，保留
价格已涨走                   2
```

有的单子下了几分钟就被撤了：sector_first 选方向的时候看的是资金流，生命周期
表却已经把同名主线归档了。两套系统意见不一致，结果是没人问过的那一套赢了。

回放里没有这条规则（`t1_settlement` 不看生命周期），所以回放和线上本来就是两
套行为。

## 目标

对**带论点**（`thesis_id`）的挂单，主线规则由撤单降级为证据：生成
`order_signal`，由 agent 回答 keep / cancel，并写明理由。回调派的人设本来就要求
失效条件里**必须**有一条是关于主线的，这条已经在 `_thesis_already_broken`
里按它自己的话执行了。

没有论点的旧单保持原样。`AGENT_EXIT_DECISIONS` 关闭时保持原样。

## 验收标准

1. 带 `thesis_id` 的挂单 + 主线 archived + `wake_agent=True` → 订单仍是
   `pending`，返回一条 `order_signal`。
2. 同样的情况下 `wake_agent=False`，或者没有 `thesis_id` → 照旧撤单，原因不变。
3. agent 回答 `cancel` → 撤单，原因以 `agent撤单:` 开头；回答 `keep` → 仍然
   `pending`，论点上多一条 checkpoint。
4. 同一张单、同一种主线原因，每个交易日最多唤醒一次（模型调用有成本，主线状态
   一天之内不会反复变化）。
5. 模型超时、报错或者回复读不出来 → 保留挂单（agent 自己的失效条件每轮照常执行；
   成交后硬止损照旧兜底）。
6. pytest、lint_harness、lint_docs 全部通过。

## 决策记录

- **为什么不删掉这条规则**：生命周期归档是真实的信息，只是不该由它来做决定。
  把它交给 agent，agent 拿到的证据比以前多了，而不是少了。
- **为什么失败时保留而不是撤单**：这和 `exit_decision` 的做法一样。挂单不花钱，
  而 agent 自己的失效条件仍然每一轮都在跑。
- **为什么每天只问一次**：`评分0.30<0.35` 这一段每个周期都会变，所以去重只
  看原因的种类（`主线已archived` / `主线明显走弱` / `主线不存在`），不看后面
  的数字。

## 第二步：下单时的准入闸门（2026-09-23，用户确认"一起改"）

`create_pending_order` 里的 `theme_admits` 在主线已归档或低于 `admit_score`
时直接拒绝建单。这和撤单那一侧是同一条规则，所以改法也一样：带论点的单照下，
下单时主线系统的判断以 `theme_gate` checkpoint 的形式记在论点上；如果主线
已经跌破撤单线，第一个盘中周期就会生成 `order_signal` 去问 agent。
`wake_agent` 从 intent 一路传到 impl；早盘和盘中两个下单点都从
`exit_decision.enabled()` 取值。

顺手修了一处：早盘下单失败原本只记 debug 日志，一个 NameError 就能让当天
所有早盘单消失，日志却看起来像是平静的一天。已提到 warning，下单点的接线
也用测试钉住了。
