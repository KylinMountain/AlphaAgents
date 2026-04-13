# 历史日线数据库 (market_history)

## 问题

1. baostock 每次查询都是远程 TCP 调用，串行慢（login/query/logout），无缓存
2. akshare 涨停池只有最近 1 个月数据，情绪周期无法回测
3. beta 计算每次都要远程拉 120 天数据，周末批量算 72 只股票要 2 分钟

## 方案

独立 SQLite 数据库 `data/market_history.db`，存储全市场日 K 线。一次性初始化 6 个月历史，之后每日增量更新。所有读历史数据的地方改为先查本地。

## 数据库

### 文件

`data/market_history.db`（独立于 memory.db，数据量大约 60 万行）

### 表

```sql
CREATE TABLE IF NOT EXISTS daily_kline (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume INTEGER,
    turnover_rate REAL,
    change_pct REAL,
    PRIMARY KEY (code, date)
);
CREATE INDEX IF NOT EXISTS idx_kline_date ON daily_kline(date);
```

### 数据规模

5000 只股票 × 120 个交易日 = 60 万行，约 50-100MB。

## 对外接口

```python
# alpha_agents/data/market_history.py

def get_local_history(code: str, days: int = 5) -> list[dict] | None
    """从本地数据库读历史K线。没有返回 None。"""

def init_history(months: int = 6, batch_size: int = 500) -> int
    """一次性拉全市场历史日线。支持断点续传。返回总行数。"""

def update_daily() -> int
    """增量更新当天收盘数据（复盘时调用）。返回更新行数。"""

def get_all_codes() -> list[str]
    """获取全市场股票代码列表（从 baostock）。"""

def compute_limit_up_stats(date: str) -> dict
    """从本地日线数据计算指定日期的涨停/炸板/连板/涨跌比。"""

def backfill_sentiment_snapshots(days: int = 120) -> int
    """用本地数据回填 daily_snapshots 的涨停池和涨跌比。"""
```

## 初始化流程

`uv run python main.py init-history`

```
1. 调 baostock query_stock_basic() 获取全市场股票代码列表
2. 分批处理（每批 500 只）:
   a. login baostock
   b. 对每只股票拉 6 个月日 K 线
   c. 批量写入 SQLite
   d. logout
   e. 记录进度到 data/init_progress.json
3. 断点续传: 下次运行时跳过已完成的批次
```

预估时间：5000 只 / 500 每批 = 10 批，每批约 5-10 分钟，总计 50-100 分钟。

### 断点续传

```json
// data/init_progress.json
{
  "total_codes": 5000,
  "completed_batches": 3,
  "batch_size": 500,
  "last_batch_end": 1500,
  "start_date": "2025-10-13",
  "end_date": "2026-04-13"
}
```

## 每日增量更新

在复盘任务（15:30）的 daily_archive 之后运行：

```python
# review.py
await asyncio.to_thread(update_daily)
```

只拉当天数据（5000 只 × 1 天），比初始化快得多（几分钟）。

## 涨停/炸板计算

```python
def compute_limit_up_stats(date: str) -> dict:
    """从日线数据计算涨停统计。"""
    # A股涨跌停规则:
    # - 普通股: ±10% (实际判断 >= 9.8%)
    # - 创业板/科创板 (300xxx/688xxx): ±20% (>= 19.8%)
    # - ST股: ±5% (>= 4.8%)
    
    # 涨停: close 涨幅 >= 阈值
    # 炸板: high 触及涨停价 但 close < 涨停价
    # 连板: 连续多天涨停
    # 涨跌比: 上涨家数 / 下跌家数
```

## 集成点

### 1. market_data.get_stock_history — 改为先查本地

```python
def get_stock_history(code: str, days: int = 5):
    # 先查本地
    from alpha_agents.data.market_history import get_local_history
    local = get_local_history(code, days)
    if local and len(local) >= days:
        return local
    # 本地不够，查 baostock
    ...原有逻辑...
```

### 2. sentiment_cycle — 用本地数据回填

初始化历史数据后，运行 `backfill_sentiment_snapshots(120)` 回填 120 天的涨停/炸板/涨跌比到 daily_snapshots，然后情绪周期可以做 120 天回测。

### 3. beta_calculator — 自动加速

因为 `get_stock_history` 改了，beta_calculator 不用改代码就自动走本地查询。

### 4. review.py — 每日增量

复盘时在 daily_archive 后调用 `update_daily()`。

## CLI 命令

```bash
# 一次性初始化（支持断点续传）
uv run python main.py init-history

# 查看初始化进度
uv run python main.py init-history --status
```

## 文件清单

### 新文件

| 文件 | 说明 |
|------|------|
| `alpha_agents/data/market_history.py` | 历史日线数据库核心模块 |

### 修改文件

| 文件 | 改动 |
|------|------|
| `alpha_agents/data/market_data.py` | `get_stock_history` 先查本地 |
| `alpha_agents/data/sentiment_cycle.py` | `backfill_snapshots` 改用本地数据 |
| `alpha_agents/pipeline/tasks/review.py` | 复盘后调 `update_daily()` |
| `main.py` | 新增 `init-history` 命令 |

## 不做的事

- 不存分钟级数据（只存日线）
- 不存指数数据（用成分股平均代替）
- 不做实时更新（盘中还是用新浪 API）
- 不替换新浪实时接口（本地只存历史，盘中实时还是走新浪）
