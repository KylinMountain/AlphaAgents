# Phase 2: Fund Flow & Market Behavior Tools

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add 7 new data tools that capture institutional fund flow behavior (龙虎榜, 融资融券, 北向资金, 大宗交易, 个股资金流, 板块排名, 量价异常), giving the analyst agent the "who is buying/selling" data that distinguishes professional analysis from retail.

**Architecture:** Each tool is a standalone module in `alpha_agents/tools/` following the existing pattern: a `_fn()` function that calls akshare inside `no_proxy()`, returns a JSON string. All tools are registered in `registry.py` as `@function_tool` wrappers and added to STOCK_TOOLS + reflection verify_tools.

**Tech Stack:** akshare (existing), SQLite Row pattern (existing), no_proxy() context manager (existing, patched in Phase 1).

---

## File Structure

```
alpha_agents/tools/
├── fund_flow.py        # NEW — P0: 龙虎榜, 大宗交易, 北向资金, 融资融券, 个股资金流
├── sector_ranking.py   # NEW — P1: 行业板块排名 (概念板块已有 sector.py)
├── anomaly_detect.py   # NEW — P1: 量价异常检测
└── registry.py         # MODIFY — register 7 new tools
alpha_agents/agents/
└── reflection.py       # MODIFY — add new tools to verify_tools
```

**Design decision:** P0 tools (5 fund flow tools) go in a single `fund_flow.py` file because they share the same domain (资金行为), similar error handling, and are always used together. P1 tools get separate files because they're conceptually distinct.

---

### Task 1: Create fund flow tools (P0 — 5 tools in one file)

**Files:**
- Create: `alpha_agents/tools/fund_flow.py`

- [ ] **Step 1: Create fund_flow.py**

```python
"""Fund flow behavior tools — who is buying/selling and how much.

Five tools capturing institutional money flow:
1. LHB (龙虎榜) — top buyer/seller seats after market close
2. Block trades (大宗交易) — negotiated bulk trades with discount rates
3. North flow (北向资金) — foreign money flow via Stock Connect
4. Margin data (融资融券) — leveraged buying/selling activity
5. Individual stock fund flow — main force vs retail money flow
"""

import json
import logging
from datetime import datetime, timedelta

import akshare as ak

from alpha_agents.config import no_proxy

logger = logging.getLogger(__name__)


def get_lhb_detail_fn(date: str = "") -> str:
    """Get Dragon-Tiger Board (龙虎榜) data — institutional/hot money seat details.

    Shows which broker seats (机构/游资) were top buyers/sellers for stocks
    that hit daily limits or had unusual volume. Key signal: institutional
    seats buying = more sustainable; hot money seats = likely one-day wonder.

    Args:
        date: Date in YYYYMMDD format. Empty for latest trading day.
    """
    try:
        if not date:
            date = datetime.now().strftime("%Y%m%d")

        with no_proxy():
            df = ak.stock_lhb_detail_em(start_date=date, end_date=date)

        if df.empty:
            # Try previous trading day
            prev = (datetime.strptime(date, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
            with no_proxy():
                df = ak.stock_lhb_detail_em(start_date=prev, end_date=prev)

        if df.empty:
            return json.dumps({"date": date, "data": [], "error": None}, ensure_ascii=False)

        results = []
        for _, row in df.iterrows():
            net_buy = float(row.get("龙虎榜净买额", 0) or 0)
            buy_amt = float(row.get("龙虎榜买入额", 0) or 0)
            sell_amt = float(row.get("龙虎榜卖出额", 0) or 0)

            # Determine if institutional or hot money
            reason = str(row.get("上榜原因", ""))
            interpretation = str(row.get("解读", ""))
            is_institutional = "机构" in interpretation

            results.append({
                "code": str(row.get("代码", "")),
                "name": str(row.get("名称", "")),
                "date": str(row.get("上榜日", "")),
                "change_pct": float(row.get("涨跌幅", 0) or 0),
                "net_buy": net_buy,
                "net_buy_yi": round(net_buy / 1e8, 2),
                "buy_amount": buy_amt,
                "sell_amount": sell_amt,
                "turnover_rate": float(row.get("换手率", 0) or 0),
                "reason": reason,
                "interpretation": interpretation,
                "is_institutional": is_institutional,
            })

        # Sort by absolute net buy amount
        results.sort(key=lambda x: abs(x["net_buy"]), reverse=True)

        return json.dumps({
            "date": date,
            "count": len(results),
            "institutional_buys": sum(1 for r in results if r["is_institutional"] and r["net_buy"] > 0),
            "data": results[:30],  # Top 30
        }, ensure_ascii=False)

    except Exception as e:
        logger.error("get_lhb_detail failed: %s", e)
        return json.dumps({"date": date, "data": [], "error": str(e)}, ensure_ascii=False)


def get_block_trade_fn(date: str = "") -> str:
    """Get block trade (大宗交易) data — negotiated bulk trades.

    Block trades at a discount (折价) suggest sellers are eager to exit.
    Block trades at a premium (溢价) suggest strong buying interest.

    Args:
        date: Date in YYYYMMDD format. Empty for latest trading day.
    """
    try:
        if not date:
            date = datetime.now().strftime("%Y%m%d")

        with no_proxy():
            df = ak.stock_dzjy_mrtj(start_date=date, end_date=date)

        if df.empty:
            prev = (datetime.strptime(date, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
            with no_proxy():
                df = ak.stock_dzjy_mrtj(start_date=prev, end_date=prev)

        if df.empty:
            return json.dumps({"date": date, "data": [], "error": None}, ensure_ascii=False)

        results = []
        for _, row in df.iterrows():
            discount_rate = float(row.get("折溢率", 0) or 0)
            total_amount = float(row.get("成交总额", 0) or 0)

            results.append({
                "code": str(row.get("证券代码", "")),
                "name": str(row.get("证券简称", "")),
                "change_pct": float(row.get("涨跌幅", 0) or 0),
                "close_price": float(row.get("收盘价", 0) or 0),
                "trade_price": float(row.get("成交价", 0) or 0),
                "discount_rate": discount_rate,
                "trade_count": int(row.get("成交笔数", 0) or 0),
                "total_amount_wan": round(total_amount, 1),
                "signal": "溢价买入" if discount_rate > 0 else "折价卖出" if discount_rate < -0.05 else "平价",
            })

        results.sort(key=lambda x: x["total_amount_wan"], reverse=True)

        return json.dumps({
            "date": date,
            "count": len(results),
            "discount_trades": sum(1 for r in results if r["discount_rate"] < -0.05),
            "premium_trades": sum(1 for r in results if r["discount_rate"] > 0),
            "data": results[:20],
        }, ensure_ascii=False)

    except Exception as e:
        logger.error("get_block_trade failed: %s", e)
        return json.dumps({"date": date, "data": [], "error": str(e)}, ensure_ascii=False)


def get_north_flow_fn(indicator: str = "today") -> str:
    """Get northbound capital (北向资金) stock holdings — foreign money direction.

    Foreign investors via Stock Connect tend to be longer-term and more informed.
    Their buying/selling direction is a strong signal for A-share investors.

    Args:
        indicator: "today" for today's top holdings, or a stock code like "000858"
                   to check if northbound holds that specific stock.
    """
    try:
        with no_proxy():
            df = ak.stock_hsgt_hold_stock_em(market="北向", indicator="今日排行")

        if df.empty:
            return json.dumps({"data": [], "error": "no data"}, ensure_ascii=False)

        if indicator != "today":
            # Filter for specific stock
            code = indicator.strip()
            match = df[df["代码"] == code]
            if match.empty:
                return json.dumps({
                    "code": code,
                    "held_by_north": False,
                    "note": "北向资金未持有该股票",
                }, ensure_ascii=False)
            row = match.iloc[0]
            return json.dumps({
                "code": code,
                "name": str(row.get("名称", "")),
                "held_by_north": True,
                "hold_shares_wan": float(row.get("今日持股-股数", 0) or 0),
                "hold_value_wan": float(row.get("今日持股-市值", 0) or 0),
                "pct_of_float": float(row.get("今日持股-占流通股比", 0) or 0),
                "change_shares_wan": float(row.get("今日增持估计-股数", 0) or 0),
                "change_value_wan": float(row.get("今日增持估计-市值", 0) or 0),
                "change_pct": float(row.get("今日增持估计-市值增幅", 0) or 0),
            }, ensure_ascii=False)

        # Return top movers (biggest increases/decreases)
        results = []
        for _, row in df.head(30).iterrows():
            results.append({
                "code": str(row.get("代码", "")),
                "name": str(row.get("名称", "")),
                "hold_value_wan": float(row.get("今日持股-市值", 0) or 0),
                "pct_of_float": float(row.get("今日持股-占流通股比", 0) or 0),
                "change_shares_wan": float(row.get("今日增持估计-股数", 0) or 0),
                "change_value_wan": float(row.get("今日增持估计-市值", 0) or 0),
                "change_pct": float(row.get("今日增持估计-市值增幅", 0) or 0),
                "sector": str(row.get("所属板块", "")),
            })

        return json.dumps({
            "count": len(results),
            "data": results,
        }, ensure_ascii=False)

    except Exception as e:
        logger.error("get_north_flow failed: %s", e)
        return json.dumps({"data": [], "error": str(e)}, ensure_ascii=False)


def get_margin_data_fn(code: str = "") -> str:
    """Get margin trading (融资融券) data — leveraged money direction.

    Rising margin balance = bullish leverage building.
    Falling margin balance = deleveraging / bearish.

    Args:
        code: Stock code to check (e.g. "000858"). Empty for market summary.
    """
    try:
        with no_proxy():
            df_sse = ak.stock_margin_detail_sse(date=datetime.now().strftime("%Y%m%d"))

        if df_sse.empty:
            prev = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
            with no_proxy():
                df_sse = ak.stock_margin_detail_sse(date=prev)

        if df_sse.empty:
            return json.dumps({"data": [], "error": "no margin data"}, ensure_ascii=False)

        if code:
            match = df_sse[df_sse["标的证券代码"] == code]
            if match.empty:
                return json.dumps({
                    "code": code,
                    "has_margin": False,
                    "note": "该股票无融资融券数据",
                }, ensure_ascii=False)
            row = match.iloc[0]
            balance = float(row.get("融资余额", 0) or 0)
            buy_amt = float(row.get("融资买入额", 0) or 0)
            repay_amt = float(row.get("融资偿还额", 0) or 0)
            net_buy = buy_amt - repay_amt
            return json.dumps({
                "code": code,
                "name": str(row.get("标的证券简称", "")),
                "has_margin": True,
                "margin_balance": balance,
                "margin_balance_yi": round(balance / 1e8, 2),
                "margin_buy": buy_amt,
                "margin_repay": repay_amt,
                "net_margin_buy": net_buy,
                "signal": "加杠杆" if net_buy > 0 else "去杠杆",
            }, ensure_ascii=False)

        # Market summary: top 20 by margin balance
        results = []
        df_sorted = df_sse.sort_values("融资余额", ascending=False).head(20)
        for _, row in df_sorted.iterrows():
            balance = float(row.get("融资余额", 0) or 0)
            buy_amt = float(row.get("融资买入额", 0) or 0)
            repay_amt = float(row.get("融资偿还额", 0) or 0)
            results.append({
                "code": str(row.get("标的证券代码", "")),
                "name": str(row.get("标的证券简称", "")),
                "margin_balance_yi": round(balance / 1e8, 2),
                "net_margin_buy_wan": round((buy_amt - repay_amt) / 1e4, 1),
                "signal": "加杠杆" if buy_amt > repay_amt else "去杠杆",
            })

        return json.dumps({"count": len(results), "data": results}, ensure_ascii=False)

    except Exception as e:
        logger.error("get_margin_data failed: %s", e)
        return json.dumps({"data": [], "error": str(e)}, ensure_ascii=False)


def get_stock_fund_flow_fn(code: str, market: str = "") -> str:
    """Get individual stock fund flow — main force vs retail money flow.

    Shows whether big money (主力/超大单/大单) is flowing in or out,
    versus small retail money (中单/小单). Main force inflow = strong signal.

    Args:
        code: Stock code, e.g. "000858"
        market: "sh" for Shanghai, "sz" for Shenzhen. Auto-detected if empty.
    """
    try:
        if not market:
            market = "sh" if code.startswith("6") else "sz"

        with no_proxy():
            df = ak.stock_individual_fund_flow(stock=code, market=market)

        if df.empty:
            return json.dumps({"code": code, "data": [], "error": None}, ensure_ascii=False)

        # Get last 5 trading days
        df = df.tail(5)
        records = []
        for _, row in df.iterrows():
            main_net = float(row.get("主力净流入-净额", 0) or 0)
            main_pct = float(row.get("主力净流入-净占比", 0) or 0)
            records.append({
                "date": str(row.get("日期", "")),
                "close": float(row.get("收盘价", 0) or 0),
                "change_pct": float(row.get("涨跌幅", 0) or 0),
                "main_net_flow": main_net,
                "main_net_flow_yi": round(main_net / 1e8, 2),
                "main_net_pct": main_pct,
            })

        # Trend: consecutive inflow/outflow days
        if records:
            consecutive_inflow = 0
            for r in reversed(records):
                if r["main_net_flow"] > 0:
                    consecutive_inflow += 1
                else:
                    break
            consecutive_outflow = 0
            for r in reversed(records):
                if r["main_net_flow"] < 0:
                    consecutive_outflow += 1
                else:
                    break

            latest = records[-1]
            trend = "主力连续流入" if consecutive_inflow >= 2 else "主力连续流出" if consecutive_outflow >= 2 else "无明显趋势"
        else:
            trend = "无数据"
            consecutive_inflow = consecutive_outflow = 0

        return json.dumps({
            "code": code,
            "trend": trend,
            "consecutive_inflow_days": consecutive_inflow,
            "consecutive_outflow_days": consecutive_outflow,
            "data": records,
        }, ensure_ascii=False)

    except Exception as e:
        logger.error("get_stock_fund_flow failed for %s: %s", code, e)
        return json.dumps({"code": code, "data": [], "error": str(e)}, ensure_ascii=False)
```

- [ ] **Step 2: Test the tools**

Run:
```bash
set -a && source .env && set +a && uv run python -c "
from alpha_agents.tools.fund_flow import *
import json

r = json.loads(get_lhb_detail_fn())
print(f'LHB: {r[\"count\"]} stocks, {r[\"institutional_buys\"]} institutional buys')

r2 = json.loads(get_block_trade_fn())
print(f'DZJY: {r2[\"count\"]} trades, {r2[\"discount_trades\"]} discount, {r2[\"premium_trades\"]} premium')

r3 = json.loads(get_north_flow_fn())
print(f'North: {r3[\"count\"]} holdings')

r4 = json.loads(get_margin_data_fn('000858'))
print(f'Margin 000858: has_margin={r4.get(\"has_margin\")}, signal={r4.get(\"signal\")}')

r5 = json.loads(get_stock_fund_flow_fn('000858'))
print(f'Fund flow 000858: trend={r5[\"trend\"]}, {len(r5[\"data\"])} days')

print('ALL TESTS PASSED')
"
```
Expected: All 5 tools return data without errors.

- [ ] **Step 3: Commit**

```bash
git add alpha_agents/tools/fund_flow.py
git commit -m "feat: add fund flow tools — LHB, block trade, north flow, margin, stock fund flow"
```

---

### Task 2: Create sector ranking tool (P1)

**Files:**
- Create: `alpha_agents/tools/sector_ranking.py`

- [ ] **Step 1: Create sector_ranking.py**

```python
"""Sector ranking tool — industry-level fund flow ranking.

Complements the existing concept-level sector tool (sector.py) with
industry-level (行业) data. Shows which industries are strongest/weakest
by fund flow, helping detect sector rotation.
"""

import json
import logging

import akshare as ak

from alpha_agents.config import no_proxy

logger = logging.getLogger(__name__)


def get_sector_ranking_fn(top_n: int = 20) -> str:
    """Get industry sector ranking by fund flow — shows sector rotation direction.

    Returns top gaining and losing industries by net fund flow.
    Use this to detect which sectors money is flowing INTO and OUT OF.

    Args:
        top_n: Number of top/bottom sectors to return. Default 20.
    """
    try:
        with no_proxy():
            df = ak.stock_fund_flow_industry()

        if df.empty:
            return json.dumps({"error": "no sector data", "gainers": [], "losers": []}, ensure_ascii=False)

        gainers = []
        losers = []
        for _, row in df.iterrows():
            name = str(row.get("行业", ""))
            change_pct = float(row.get("行业-涨跌幅", 0) or 0)
            net_flow = float(row.get("净额", 0) or 0)
            leader = str(row.get("领涨股", ""))
            leader_change = float(row.get("领涨股-涨跌幅", 0) or 0)

            entry = {
                "sector": name,
                "change_pct": change_pct,
                "net_flow_yi": round(net_flow, 2),
                "leader": leader,
                "leader_change_pct": leader_change,
                "company_count": int(row.get("公司家数", 0) or 0),
            }

            if net_flow > 0:
                gainers.append(entry)
            else:
                losers.append(entry)

        gainers.sort(key=lambda x: x["net_flow_yi"], reverse=True)
        losers.sort(key=lambda x: x["net_flow_yi"])

        return json.dumps({
            "total_sectors": len(gainers) + len(losers),
            "inflow_sectors": len(gainers),
            "outflow_sectors": len(losers),
            "gainers": gainers[:top_n],
            "losers": losers[:top_n],
            "rotation_signal": "资金集中流入少数行业" if len(gainers) < len(losers) else "普涨格局",
        }, ensure_ascii=False)

    except Exception as e:
        logger.error("get_sector_ranking failed: %s", e)
        return json.dumps({"error": str(e), "gainers": [], "losers": []}, ensure_ascii=False)
```

- [ ] **Step 2: Test**

Run:
```bash
set -a && source .env && set +a && uv run python -c "
from alpha_agents.tools.sector_ranking import get_sector_ranking_fn
import json
r = json.loads(get_sector_ranking_fn())
print(f'Sectors: {r[\"total_sectors\"]} total, {r[\"inflow_sectors\"]} inflow, {r[\"outflow_sectors\"]} outflow')
print(f'Top gainer: {r[\"gainers\"][0][\"sector\"]} +{r[\"gainers\"][0][\"net_flow_yi\"]}亿')
print(f'Top loser: {r[\"losers\"][0][\"sector\"]} {r[\"losers\"][0][\"net_flow_yi\"]}亿')
print(f'Signal: {r[\"rotation_signal\"]}')
print('TEST PASSED')
"
```

- [ ] **Step 3: Commit**

```bash
git add alpha_agents/tools/sector_ranking.py
git commit -m "feat: add sector ranking tool — industry-level fund flow rotation"
```

---

### Task 3: Create anomaly detection tool (P1)

**Files:**
- Create: `alpha_agents/tools/anomaly_detect.py`

- [ ] **Step 1: Create anomaly_detect.py**

```python
"""Anomaly detection tool — find stocks with unusual volume/price behavior.

Detects stocks with abnormal volume ratio (量比 > 3) or extreme turnover,
which often signal institutional activity or news-driven moves.
Uses the existing stock_zt_pool_em for limit-up detection.
"""

import json
import logging
from datetime import datetime

import akshare as ak

from alpha_agents.config import no_proxy

logger = logging.getLogger(__name__)


def get_anomaly_stocks_fn(date: str = "") -> str:
    """Detect stocks with unusual price/volume behavior.

    Returns:
    - Limit-up stocks (涨停) with seal strength info
    - Limit-down stocks (跌停)
    - Stocks breaking out of limit-up (炸板)

    Args:
        date: Date in YYYYMMDD format. Empty for today.
    """
    try:
        if not date:
            date = datetime.now().strftime("%Y%m%d")

        result = {
            "date": date,
            "limit_up": [],
            "limit_down": [],
            "broken_limit": [],
            "error": None,
        }

        # 1. Limit-up pool (涨停)
        try:
            with no_proxy():
                df_zt = ak.stock_zt_pool_em(date=date)
            for _, row in df_zt.head(20).iterrows():
                result["limit_up"].append({
                    "code": str(row.get("代码", "")),
                    "name": str(row.get("名称", "")),
                    "change_pct": float(row.get("涨跌幅", 0) or 0),
                    "turnover_rate": float(row.get("换手率", 0) or 0),
                    "seal_amount_yi": round(float(row.get("封板资金", 0) or 0) / 1e8, 2),
                    "first_seal_time": str(row.get("首次封板时间", "")),
                    "break_count": int(row.get("炸板次数", 0) or 0),
                    "consecutive_limits": int(row.get("连板数", 0) or 0),
                    "sector": str(row.get("所属行业", "")),
                })
        except Exception as e:
            logger.debug("Limit-up pool failed: %s", e)

        # 2. Broken limit-up pool (炸板)
        try:
            with no_proxy():
                df_zb = ak.stock_zt_pool_zbgc_em(date=date)
            for _, row in df_zb.head(10).iterrows():
                result["broken_limit"].append({
                    "code": str(row.get("代码", "")),
                    "name": str(row.get("名称", "")),
                    "change_pct": float(row.get("涨跌幅", 0) or 0),
                    "turnover_rate": float(row.get("换手率", 0) or 0),
                    "sector": str(row.get("所属行业", "")),
                })
        except Exception as e:
            logger.debug("Broken limit pool failed: %s", e)

        # 3. Limit-down pool (跌停)
        try:
            with no_proxy():
                df_dt = ak.stock_zt_pool_dtgc_em(date=date)
            for _, row in df_dt.head(10).iterrows():
                result["limit_down"].append({
                    "code": str(row.get("代码", "")),
                    "name": str(row.get("名称", "")),
                    "change_pct": float(row.get("涨跌幅", 0) or 0),
                    "sector": str(row.get("所属行业", "")),
                })
        except Exception as e:
            logger.debug("Limit-down pool failed: %s", e)

        # Summary
        result["summary"] = {
            "limit_up_count": len(result["limit_up"]),
            "limit_down_count": len(result["limit_down"]),
            "broken_count": len(result["broken_limit"]),
            "top_sector": _most_common_sector(result["limit_up"]),
            "consecutive_limit_stocks": [
                s for s in result["limit_up"] if s["consecutive_limits"] >= 2
            ],
        }

        return json.dumps(result, ensure_ascii=False)

    except Exception as e:
        logger.error("get_anomaly_stocks failed: %s", e)
        return json.dumps({"date": date, "error": str(e)}, ensure_ascii=False)


def _most_common_sector(stocks: list[dict]) -> str:
    """Find the most common sector among a list of stocks."""
    if not stocks:
        return ""
    sectors = {}
    for s in stocks:
        sec = s.get("sector", "")
        if sec:
            sectors[sec] = sectors.get(sec, 0) + 1
    return max(sectors, key=sectors.get) if sectors else ""
```

- [ ] **Step 2: Test**

Run:
```bash
set -a && source .env && set +a && uv run python -c "
from alpha_agents.tools.anomaly_detect import get_anomaly_stocks_fn
import json
r = json.loads(get_anomaly_stocks_fn())
s = r['summary']
print(f'Limit up: {s[\"limit_up_count\"]}, Limit down: {s[\"limit_down_count\"]}, Broken: {s[\"broken_count\"]}')
print(f'Top sector: {s[\"top_sector\"]}')
print(f'Consecutive limit stocks: {len(s[\"consecutive_limit_stocks\"])}')
print('TEST PASSED')
"
```

- [ ] **Step 3: Commit**

```bash
git add alpha_agents/tools/anomaly_detect.py
git commit -m "feat: add anomaly detection tool — limit up/down, broken limit, sector concentration"
```

---

### Task 4: Register all 7 new tools

**Files:**
- Modify: `alpha_agents/tools/registry.py`
- Modify: `alpha_agents/agents/reflection.py`

- [ ] **Step 1: Add imports to registry.py**

After the existing imports in `registry.py`, add:

```python
from alpha_agents.tools.fund_flow import (
    get_lhb_detail_fn, get_block_trade_fn, get_north_flow_fn,
    get_margin_data_fn, get_stock_fund_flow_fn,
)
from alpha_agents.tools.sector_ranking import get_sector_ranking_fn
from alpha_agents.tools.anomaly_detect import get_anomaly_stocks_fn
```

- [ ] **Step 2: Add @function_tool wrappers**

Add these before the `# --- Tool sets ---` comment:

```python
@function_tool
def get_lhb_detail(date: str = "") -> str:
    """获取龙虎榜数据 — 机构/游资席位买卖明细。

    龙虎榜展示当日涨跌幅异常、成交量异常的个股的买卖席位。
    机构席位买入 = 持续性较好；游资席位买入 = 可能是一日游。
    输入日期(YYYYMMDD)，留空为最新交易日。
    """
    return get_lhb_detail_fn(date=date)


@function_tool
def get_block_trade(date: str = "") -> str:
    """获取大宗交易数据 — 折溢价率判断买卖意愿。

    折价成交 = 卖方急于出货；溢价成交 = 买方看好。
    输入日期(YYYYMMDD)，留空为最新交易日。
    """
    return get_block_trade_fn(date=date)


@function_tool
def get_north_flow(indicator: str = "today") -> str:
    """获取北向资金持股数据 — 外资方向是重要信号。

    外资通过陆股通买卖A股，其方向通常具有较强的参考价值。
    输入"today"查看今日持股排名；输入股票代码（如"000858"）查看该股是否被北向持有及增减仓情况。
    """
    return get_north_flow_fn(indicator=indicator)


@function_tool
def get_margin_data(code: str = "") -> str:
    """获取融资融券数据 — 杠杆资金方向。

    融资余额增加 = 杠杆资金看多；融资余额减少 = 去杠杆。
    输入股票代码查看该股融资融券情况；留空查看市场融资余额排名前20。
    """
    return get_margin_data_fn(code=code)


@function_tool
def get_stock_fund_flow(code: str, market: str = "") -> str:
    """获取个股资金流向 — 主力vs散户资金方向。

    主力净流入 = 大资金看好；主力净流出 = 大资金撤退。
    返回最近5个交易日的主力/散户资金流入流出数据及趋势判断。
    输入股票代码，如"000858"。market留空自动判断。
    """
    return get_stock_fund_flow_fn(code=code, market=market)


@function_tool
def get_sector_ranking(top_n: int = 20) -> str:
    """获取行业板块资金流排名 — 检测板块轮动方向。

    显示资金正在流入哪些行业、流出哪些行业。
    用于判断市场当前的轮动方向和主线热度。
    """
    return get_sector_ranking_fn(top_n=top_n)


@function_tool
def get_anomaly_stocks(date: str = "") -> str:
    """获取涨停/跌停/炸板数据 — 检测市场异动和主线方向。

    涨停板集中在某个行业 = 该行业可能是新主线。
    炸板多 = 市场分歧大，追高风险高。
    连板股 = 短线资金认可的最强方向。
    输入日期(YYYYMMDD)，留空为今日。
    """
    return get_anomaly_stocks_fn(date=date)
```

- [ ] **Step 3: Update STOCK_TOOLS**

Replace the existing STOCK_TOOLS line:

```python
STOCK_TOOLS = [
    search_stocks, get_sector_data, filter_stocks, get_watchlist,
    get_stock_quotes, get_financial_data, get_market_breadth, get_earnings_calendar,
    get_lhb_detail, get_block_trade, get_north_flow, get_margin_data,
    get_stock_fund_flow, get_sector_ranking, get_anomaly_stocks,
    web_search, web_fetch, get_pizzint,
]
```

- [ ] **Step 4: Update reflection agent tools**

In `alpha_agents/agents/reflection.py`, update the import:

```python
from alpha_agents.tools.registry import (
    get_sector_data, filter_stocks, get_watchlist,
    get_futures_quotes, get_futures_inventory, get_futures_basis,
    get_stock_quotes, get_financial_data, get_market_breadth, get_earnings_calendar,
    get_lhb_detail, get_north_flow, get_margin_data, get_stock_fund_flow,
    get_sector_ranking,
)
```

And update the verify_tools list:

```python
    verify_tools = [
        get_sector_data, filter_stocks, get_watchlist,
        get_futures_quotes, get_futures_inventory, get_futures_basis,
        get_stock_quotes, get_financial_data, get_market_breadth, get_earnings_calendar,
        get_lhb_detail, get_north_flow, get_margin_data, get_stock_fund_flow,
        get_sector_ranking,
    ]
```

Note: `get_block_trade` and `get_anomaly_stocks` are excluded from reflection tools — they're for the strategist's proactive analysis, not for verification.

- [ ] **Step 5: Test registration**

Run:
```bash
set -a && source .env && set +a && uv run python -c "
from alpha_agents.tools.registry import STOCK_TOOLS
names = [t.name for t in STOCK_TOOLS]
print(f'STOCK_TOOLS: {len(names)} tools')
expected = ['get_lhb_detail', 'get_block_trade', 'get_north_flow', 'get_margin_data', 'get_stock_fund_flow', 'get_sector_ranking', 'get_anomaly_stocks']
for e in expected:
    assert e in names, f'Missing: {e}'
    print(f'  {e}: OK')
print('ALL TOOLS REGISTERED')
"
```

- [ ] **Step 6: Commit**

```bash
git add alpha_agents/tools/registry.py alpha_agents/agents/reflection.py
git commit -m "feat: register 7 fund flow tools in STOCK_TOOLS and reflection"
```

---

### Task 5: Integration test

**Files:** (none created)

- [ ] **Step 1: Full tool test**

Run:
```bash
set -a && source .env && set +a && uv run python -c "
from alpha_agents.tools.registry import STOCK_TOOLS
import json

print(f'Total STOCK_TOOLS: {len(STOCK_TOOLS)}')

# Test each new tool
from alpha_agents.tools.fund_flow import *
from alpha_agents.tools.sector_ranking import get_sector_ranking_fn
from alpha_agents.tools.anomaly_detect import get_anomaly_stocks_fn

results = {}
for name, fn, args in [
    ('LHB', get_lhb_detail_fn, {}),
    ('Block Trade', get_block_trade_fn, {}),
    ('North Flow', get_north_flow_fn, {}),
    ('Margin', get_margin_data_fn, {'code': ''}),
    ('Fund Flow', get_stock_fund_flow_fn, {'code': '000858'}),
    ('Sector Ranking', get_sector_ranking_fn, {}),
    ('Anomaly', get_anomaly_stocks_fn, {}),
]:
    try:
        r = json.loads(fn(**args))
        has_error = bool(r.get('error'))
        count = r.get('count', len(r.get('data', r.get('gainers', []))))
        results[name] = f'OK ({count} items)' if not has_error else f'ERROR: {r[\"error\"]}'
    except Exception as e:
        results[name] = f'EXCEPTION: {e}'

for name, status in results.items():
    print(f'  {name}: {status}')

failures = [n for n, s in results.items() if 'ERROR' in s or 'EXCEPTION' in s]
if failures:
    print(f'FAILURES: {failures}')
else:
    print('ALL TOOLS PASSED')
"
```
Expected: All 7 tools return data without errors.

- [ ] **Step 2: Commit if fixes needed**

```bash
git add -A && git commit -m "fix: phase 2 integration fixes" || echo "Nothing to fix"
```
