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
