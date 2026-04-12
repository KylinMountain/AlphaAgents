# 情绪周期模块

## 问题

当前系统只看当天涨跌比（静态快照）判断市场情绪，不看趋势。连续 3 天涨停家数增加和连续 3 天减少，交易策略应该完全不同，但系统无法区分。

## 方案

基于 4 个指标的 3 日趋势，判断市场处于 6 个情绪阶段之一，动态调整仓位上限、选股策略和卖出策略。

## 情绪指标

| 指标 | 数据来源 | 判断什么 |
|------|---------|---------|
| 涨停家数趋势（3日） | daily_snapshots.limit_up_pool + 实时 get_anomaly_stocks | 赚钱效应在扩散还是收缩 |
| 炸板率 | 涨停后开板数 / 涨停总数 | 分歧程度——炸板多说明追高被套 |
| 连板最高板 | 连板股中的最高连板数 | 情绪天花板——从 5 板降到 3 板说明退潮 |
| 涨跌比趋势（3日） | daily_snapshots.market_breadth + 实时 get_market_breadth | 验证信号，避免误判 |

## 6 个情绪阶段

```
冰点 → 修复 → 升温 → 狂热 → 分歧 → 退潮 → 冰点
```

### 判断规则

| 阶段 | 涨停家数趋势 | 炸板率 | 连板最高板 | 涨跌比 |
|------|------------|--------|----------|--------|
| 冰点 | 连续 2 天 < 20 家 | — | ≤ 2 板 | < 0.5 |
| 修复 | 从冰点回升，涨停 20-50 | < 20% | 2-3 板 | 0.5-1.5 |
| 升温 | 连续 2 天增加且 > 50 | < 15% | 3-5 板 | > 1.5 |
| 狂热 | > 100 家 | < 10% | ≥ 5 板 | > 3 |
| 分歧 | 仍多但炸板率升高 | > 25% | 最高板断板 | 1-3 |
| 退潮 | 连续 2 天减少 | > 30% | 最高板持续下降 | < 1 |

判断优先级：先看涨停家数趋势（主信号），再用其他指标验证和修正。

### 阶段转换逻辑

不需要严格按顺序转换。每次计算独立判断当前阶段。但加入"惯性"——如果昨天是"升温"，今天指标模糊时优先保持"升温"而不是跳到"分歧"。

## 策略影响

| 情绪阶段 | 仓位上限 | 选股策略 | 卖出策略 |
|---------|---------|---------|---------|
| 冰点 | 20% | 只低吸强主线龙头 | 不急卖，主线没死就拿着 |
| 修复 | 40% | 低吸为主，开始关注新方向 | 正常止损 |
| 升温 | 60% | 可追强势，高 beta 优先 | 放宽移动止损（给空间跑） |
| 狂热 | 50%（反而收） | 只持有不新开仓 | 收紧移动止损（5%→3%，锁利润） |
| 分歧 | 30% | 不追高，只保留强主线 | 弱持仓主动减仓（主线强度 < 7 清掉） |
| 退潮 | 15% | 几乎不买 | 全面收紧（主线强度 < 8 都走） |

### 仓位上限

替换现有的 `get_sentiment_exposure_limit()`，从基于涨跌比的静态判断升级为基于情绪周期的动态判断。

### 选股策略

通过在 Agent 的 system prompt 或工具调用的上下文中注入当前情绪阶段和对应策略建议。Agent 根据这些建议调整推荐行为。

### 卖出策略

在 `check_positions()` 中根据当前情绪阶段动态调整：
- 移动止损的 trailing_pct：升温时 5%（宽松），狂热/分歧时 3%（收紧）
- 主线强度阈值：分歧时 < 7 清仓，退潮时 < 8 清仓（比默认的 ≤ 3 更激进）

## 数据需求

### 历史数据（从 daily_snapshots 读取）

```python
# 过去 3 天的涨停池数据
for date in last_3_days:
    snapshot = get_snapshot(date, "limit_up_pool")
    # 提取：涨停家数、炸板家数、连板最高板
```

### 实时数据（盘中调用）

```python
# 当天实时
breadth = get_market_breadth_fn()  # 涨跌比
anomaly = get_anomaly_stocks_fn()  # 涨停/炸板/连板
```

### 数据不足时的降级

系统刚启动时 daily_snapshots 可能不足 3 天。降级策略：
- 1 天数据：只用当天数据，不判断趋势，默认"中性"
- 2 天数据：用 2 天趋势，精度降低但可用
- 3 天及以上：正常判断

## 工具接口

```python
def get_sentiment_cycle() -> dict:
    """计算当前情绪周期阶段。

    Returns:
        {
            "phase": "升温",
            "phase_en": "warming",
            "confidence": 0.8,
            "indicators": {
                "limit_up_trend": [45, 62, 78],  # 过去3天涨停家数
                "broken_rate": 0.12,              # 今日炸板率
                "max_consecutive": 5,             # 最高连板数
                "ad_ratio_trend": [1.2, 1.8, 2.5] # 过去3天涨跌比
            },
            "strategy": {
                "max_exposure_pct": 60,
                "trailing_stop_pct": 5,
                "theme_exit_threshold": 3,
                "buy_style": "可追强势，高beta优先",
                "sell_style": "放宽移动止损（给空间跑）"
            }
        }
    """
```

## 集成点

### 1. portfolio.py — 替换 get_sentiment_exposure_limit

现有的 `get_sentiment_exposure_limit()` 改为调用 `get_sentiment_cycle()`，从返回的 `strategy.max_exposure_pct` 读仓位上限。

### 2. portfolio.py — check_positions 中的移动止损

`trailing_pct` 不再固定，从 `get_sentiment_cycle().strategy.trailing_stop_pct` 读取。

### 3. portfolio.py — check_positions 中的主线退出阈值

分歧/退潮阶段，主线强度退出阈值从默认的 ≤ 3 提高到 < 7 或 < 8。

### 4. intraday_monitor.py — 注入情绪上下文

在传给 Agent 的 context 里加入当前情绪阶段，让 Agent 知道应该进攻还是防守。

### 5. morning_scan.py — 晨扫策略建议

晨报中加入当前情绪阶段判断，影响推荐的激进程度。

### 6. chat.py — 快捷命令

`情绪` / `sentiment` 快捷命令直接显示当前情绪周期和策略建议。

## 文件清单

### 新文件

| 文件 | 说明 |
|------|------|
| `alpha_agents/data/sentiment_cycle.py` | 情绪周期计算核心模块 |

### 修改文件

| 文件 | 改动 |
|------|------|
| `alpha_agents/data/portfolio.py` | 替换仓位限制 + 动态止损 + 动态退出阈值 |
| `alpha_agents/pipeline/tasks/intraday_monitor.py` | 注入情绪上下文 |
| `alpha_agents/pipeline/tasks/morning_scan.py` | 注入情绪上下文 |
| `alpha_agents/agents/chat.py` | 加快捷命令 + 注册工具 |
| `alpha_agents/tools/registry.py` | 注册 get_sentiment_cycle 工具 |

## 不做的事

- 不做情绪预测（只判断当前阶段，不预测下一阶段）
- 不做自动全面清仓（退潮时收紧策略但不强制一键清仓）
- 不做历史回测（等数据积累后再做）
- 不影响挂单逻辑（挂单由主线状态管理，不受情绪周期影响）
