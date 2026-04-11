# 板块 Beta 选股引擎

## 问题

系统发现"板块异动"后，用语义搜索找概念相关的票，LLM 凭感觉挑。结果很多票只是名义上属于该概念，板块涨的时候它不动。需要用历史数据量化"谁跟涨弹性最大"，替代 LLM 拍脑袋选股。

## 方案

新建工具 `get_sector_best_stocks(concept_name)`，基于多周期 beta + 多因子打分，返回板块内 Top 10 标的。

## 数据流

```
输入: "电池" (概念板块名)
  ↓
1. 从 stocks.db 拉板块内所有成分股代码
  ↓
2. 从 beta 缓存表读取预算好的 beta（周末批量计算）
  ↓
3. 获取当日实时数据（新浪）+ 机构数据（北向/龙虎榜）
  ↓
4. 多因子打分：beta 40% + 涨幅位置 25% + 机构认可 20% + 流动性 15%
  ↓
5. 排序，过滤已涨停/ST/停牌，返回 Top 10
  ↓
输出: [{code, name, score, beta, 当日涨幅, 机构信号, 建议}]
```

## Beta 计算

### 公式

对每只股票，计算它和板块指数的收益率相关性：

```
beta = Cov(stock_returns, sector_returns) / Var(sector_returns)
```

### 多周期加权

```
final_beta = 20日beta × 50% + 60日beta × 30% + 120日beta × 20%
```

近期权重大（适应 A 股主线轮动快），长期权重小（保证稳定性）。

### 板块基准

用 akshare 的概念板块涨跌幅作为 sector_returns。如果概念板块无历史数据，用板块内所有成分股的等权平均涨跌幅近似。

## 多因子打分

| 因子 | 权重 | 计算方式 | 说明 |
|------|------|---------|------|
| 板块 beta | 40% | 多周期加权 beta | 跟涨弹性，核心因子 |
| 当日涨幅位置 | 25% | 归一化：0%=未启动(满分) → 涨停=0分 | 已涨停买不到，未启动的补涨概率大 |
| 机构认可度 | 20% | 北向持仓+龙虎榜机构买入+融资净买入 | 有机构背书持续性好 |
| 流动性 | 15% | 20日日均成交额归一化 | 日均成交额太小的排除 |

### 涨幅位置评分规则

```
涨幅 <= 0%  → 100 分（未启动，补涨空间大）
涨幅 0-3%   → 80 分（刚启动）
涨幅 3-6%   → 50 分（已有涨幅）
涨幅 6-9%   → 20 分（涨幅较大）
涨幅 >= 9.8%→ 0 分（涨停，买不到）
```

### 机构认可度评分

```
北向持仓 > 1%     → +30 分
北向今日增持       → +20 分
龙虎榜机构净买入   → +30 分
融资净买入 > 0     → +20 分
最高 100 分
```

### 流动性评分

```
日均成交额 < 5000万  → 0 分（排除）
5000万-1亿          → 40 分
1亿-5亿             → 70 分
> 5亿               → 100 分
```

## 缓存策略

| 数据 | 更新频率 | 存储 |
|------|---------|------|
| beta 系数 | 周末批量计算 | `sector_betas` 表 in memory.db |
| 机构认可度 | 每日复盘后（daily_archive 里已有） | daily_snapshots 表 |
| 当日涨幅 | 实时查（新浪 API） | 不缓存 |
| 流动性（日均成交额） | 周末批量计算 | `sector_betas` 表一起存 |

### sector_betas 表

```sql
CREATE TABLE IF NOT EXISTS sector_betas (
    id INTEGER PRIMARY KEY,
    concept TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    beta_20d REAL,
    beta_60d REAL,
    beta_120d REAL,
    beta_weighted REAL,
    avg_daily_amount REAL,
    updated_at TEXT,
    UNIQUE(concept, code)
);
```

### 周末批量计算流程

在周报任务后运行（周六 10:00 之后）：

```
1. 遍历所有活跃主线（theme_lines 表 status != 'archived'）
2. 对每个主线，拉成分股列表
3. 对每只成分股，用 baostock 拉 120 日 K 线
4. 计算 20/60/120 日 beta
5. 拉 20 日日均成交额
6. 写入 sector_betas 表（upsert）
```

## 使用场景

### 晨扫

```python
# 对每个活跃主线，获取 Top 10
for theme in active_themes:
    best = get_sector_best_stocks(theme["name"])
    # 传给 Agent 作为候选池，替代现在的 search_stocks → filter_stocks
```

### 盘中异动

```python
# 板块异动检测到"电池+4%"
best = get_sector_best_stocks("电池")
# 从 Top 10 里选可操作标的（未涨停 + institutional_position 验证）
```

### Chat

```
你: 电池板块买哪只最好？
Agent: [调用 get_sector_best_stocks("电池")]
  Top 10 按综合评分排序...
```

## 工具接口

```python
def get_sector_best_stocks_fn(concept_name: str, top_n: int = 10) -> str:
    """获取板块内综合评分最高的标的。

    基于多周期 beta（跟涨弹性）+ 当日涨幅位置 + 机构认可度 + 流动性
    综合打分排序。beta 数据每周末预算，实时数据即时查询。

    Args:
        concept_name: 概念板块名，如 "电池"、"芯片概念"
        top_n: 返回前N只，默认10
    """
```

返回 JSON：
```json
{
  "concept": "电池",
  "total_in_sector": 103,
  "scored": 85,
  "top": [
    {
      "rank": 1,
      "code": "300750",
      "name": "宁德时代",
      "score": 87.5,
      "beta_weighted": 1.35,
      "today_change_pct": 2.1,
      "institutional": "北向增持+融资净买入",
      "avg_amount_yi": 45.2,
      "note": "高beta+低涨幅+机构认可"
    }
  ]
}
```

## 不做的事

- 不做实时 beta 更新（周级别够用）
- 不做因子权重自动优化（等数据积累后再做）
- 不替换 institutional_position 工具（两者互补：sector_best_stocks 选候选池，institutional_position 对个股深度分析）
- 不做跨板块比较（只在单个板块内排名）

## 文件清单

### 新文件

| 文件 | 说明 |
|------|------|
| `alpha_agents/tools/sector_beta.py` | beta 计算 + 多因子评分 + 工具函数 |
| `alpha_agents/data/beta_calculator.py` | 周末批量计算 beta 的独立模块 |

### 修改文件

| 文件 | 改动 |
|------|------|
| `alpha_agents/data/memory_store.py` | 新增 sector_betas 表 schema |
| `alpha_agents/tools/registry.py` | 注册 get_sector_best_stocks 工具 |
| `alpha_agents/agents/morning.py` | 晨扫工具列表加入 |
| `alpha_agents/agents/intraday.py` | 盘中工具列表加入 |
| `alpha_agents/agents/chat.py` | chat 工具列表加入 |
| `alpha_agents/pipeline/tasks/weekly_report.py` | 周报后触发 beta 批量计算 |
| `main.py` | 新增 `build-beta` 命令用于手动触发计算 |
