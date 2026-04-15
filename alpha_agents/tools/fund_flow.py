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

from alpha_agents.data.market_data import (
    get_lhb, get_block_trades, get_north_holdings,
    get_margin_detail, get_individual_fund_flow,
)

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
        else:
            # Normalize to YYYYMMDD — accept 2026-04-13, 2026/04/13, 20260413
            date = date.replace("-", "").replace("/", "").strip()
            if len(date) != 8 or not date.isdigit():
                return json.dumps({"date": date, "data": [], "error": f"无效日期格式: {date}，需要 YYYYMMDD 或 YYYY-MM-DD"}, ensure_ascii=False)

        df = get_lhb(date)

        if df is None or df.empty:
            prev = (datetime.strptime(date, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
            df = get_lhb(prev)

        if df is None or df.empty:
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

        df = get_block_trades(date)

        if df is None or df.empty:
            prev = (datetime.strptime(date, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
            df = get_block_trades(prev)

        if df is None or df.empty:
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
    """Get northbound capital (北向资金) aggregate flow — today's net inflow summary.

    NOTE: Per-stock northbound holdings data is NO LONGER AVAILABLE since
    HK Exchange stopped publishing real-time per-stock holdings on 2024-08-19.
    This tool now returns aggregate (market-wide) data only.

    Args:
        indicator: Only "today" is supported. Per-stock queries return unavailable.
    """
    import akshare as ak

    # Per-stock queries are no longer supported — data source is dead.
    if indicator != "today":
        return json.dumps({
            "code": indicator.strip(),
            "available": False,
            "note": "港交所自2024-08-19起不再公布个股级北向持仓，数据源已失效",
            "data": [],
        }, ensure_ascii=False)

    try:
        # stock_hsgt_fund_flow_summary_em returns today's aggregate summary
        # with 4 rows: 沪股通(北向)/深股通(北向)/港股通(南向 via 沪)/港股通(南向 via 深)
        df = ak.stock_hsgt_fund_flow_summary_em()

        if df is None or df.empty:
            return json.dumps({"data": [], "error": "no data"}, ensure_ascii=False)

        # Filter to northbound rows (资金方向 = 北向)
        north = df[df["资金方向"] == "北向"]

        segments = []
        total_net_buy_yi = 0.0
        for _, row in north.iterrows():
            net_buy = float(row.get("成交净买额", 0) or 0)  # akshare: 亿元
            inflow = float(row.get("资金净流入", 0) or 0)
            segments.append({
                "segment": str(row.get("板块", "")),  # 沪股通/深股通
                "net_buy_yi": round(net_buy, 2),
                "net_inflow_yi": round(inflow, 2),
                "advance_count": int(row.get("上涨数", 0) or 0),
                "decline_count": int(row.get("下跌数", 0) or 0),
                "flat_count": int(row.get("持平数", 0) or 0),
                "index_name": str(row.get("相关指数", "")),
                "index_change_pct": float(row.get("指数涨跌幅", 0) or 0),
            })
            total_net_buy_yi += net_buy

        trading_date = ""
        if not north.empty:
            trading_date = str(north.iloc[0].get("交易日", ""))

        return json.dumps({
            "date": trading_date,
            "total_net_buy_yi": round(total_net_buy_yi, 2),
            "direction": "净买入" if total_net_buy_yi > 0 else "净卖出" if total_net_buy_yi < 0 else "平",
            "segments": segments,
            "note": "仅汇总数据，个股级北向持仓自2024-08-19起不可用",
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
        df_sse = get_margin_detail()

        if df_sse is None or df_sse.empty:
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

        df = get_individual_fund_flow(code, market)

        if df is None or df.empty:
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
