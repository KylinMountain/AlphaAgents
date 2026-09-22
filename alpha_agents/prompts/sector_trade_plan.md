{session}

上游已经完成“方向选择”和“个股选择”。下面面板中的股票已经进入交易计划阶段。
你不能换成别的股票；你现在只判断它们是否存在可执行的入场方案，并为可执行者给出价格计划。

## 你能看到什么

{sight}

## 市场环境

{market}

## 当前组合

{book}

## 已生效/可见经验

{knowledge}

## Trader 风格

{trader_note}

## 已选股票

{panel}

## 上游冻结研究包

下面 JSON 是方向选择、个股比较、实际工具返回与关系证据的原始交接。
只能使用其中已经存在的事实；不得把 unknown 补成已知，也不得声称重新查过。

{research_packet}

## 新闻

窗口：{news_window}，截止 {news_cutoff}

{news}

## 任务

最多为 {picks} 只股票生成订单；也可以拒绝其中任何一只，返回更少甚至 0 单。

1. **不得重新做候选发现。** 只能处理面板里的代码。
2. entry_low / entry_high 是今天允许成交的价格区间，且必须 entry_low < entry_high。
3. stop_loss 必须严格低于 entry_low；它表达论点失效后的风险边界，不是随手固定百分比。
4. target_price 可为 null；如果填写，必须高于 entry_high。
5. 不允许因为“方向已选中”就强行给出交易计划；价格位置或失效边界不合理时直接不下单。
6. 这里没有研究工具。不要声称重新查过任何数据，只使用本消息已经给出的事实。
7. {fills_how}
8. **仓位是你的决定。** `size_pct` 是这一笔占整个账户的比例（0.005–0.5）。
   没有"标准仓位"——证据强就重，只是试探就轻。不写则按交易员配置的默认值，
   那等于你放弃了这个决定。
9. **`invalidations` 写你认错的条件，而且必须是代码能判定的。** 它们不是
   免责声明：系统每天用它们检查你的论点是否还成立，先触发的那条会带着你
   写的 `note` 出现在平仓记录里。写不出可判定的条件，通常说明论点本身不够
   具体。

可用的 `kind` 只有以下这些（其它一律被丢弃）：

{VOCAB}

只输出单个 JSON 对象：

{{
  "orders": [
    {{
      "code": "必须来自已选股票",
      "entry_low": 10.00,
      "entry_high": 10.20,
      "stop_loss": 9.50,
      "target_price": 12.00,
      "size_pct": 0.05,
      "prob": 0.55,
      "conviction": 0.6,
      "horizon_days": 5,
      "invalidations": [
        {{"kind": "theme_flow_negative", "value": 1, "note": "主线资金转为净流出即离场"}},
        {{"kind": "drawdown_from_peak", "value": 8, "note": "峰值回吐八成说明这一波结束"}}
      ],
      "reason": "为什么这个价格计划与当前论点和风险匹配"
    }}
  ]
}}
