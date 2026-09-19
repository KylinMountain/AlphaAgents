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

只输出单个 JSON 对象：

{{
  "orders": [
    {{
      "code": "必须来自已选股票",
      "entry_low": 10.00,
      "entry_high": 10.20,
      "stop_loss": 9.50,
      "target_price": 12.00,
      "reason": "为什么这个价格计划与当前论点和风险匹配"
    }}
  ]
}}
