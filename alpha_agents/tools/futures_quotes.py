"""Futures market data tools — quotes, inventory, basis."""

import json
import logging
from datetime import datetime, timedelta

import akshare as ak

from alpha_agents.config import no_proxy

logger = logging.getLogger(__name__)

# Main contract symbol mapping: Chinese name → sina symbol code
MAIN_CONTRACTS = {
    "沪铜": "CU0", "沪铝": "AL0", "沪锌": "ZN0", "沪镍": "NI0",
    "沪锡": "SN0", "沪铅": "PB0", "沪金": "AU0", "沪银": "AG0",
    "螺纹钢": "RB0", "热卷": "HC0", "铁矿石": "I0", "焦煤": "JM0", "焦炭": "J0",
    "原油": "SC0", "燃油": "FU0", "沥青": "BU0", "LPG": "PG0",
    "甲醇": "MA0", "PTA": "TA0", "乙二醇": "EG0", "聚丙烯": "PP0",
    "聚乙烯": "L0", "PVC": "V0", "纯碱": "SA0",
    "豆粕": "M0", "豆油": "Y0", "棕榈油": "P0", "菜籽油": "OI0",
    "玉米": "C0", "棉花": "CF0", "白糖": "SR0", "生猪": "LH0",
    "鸡蛋": "JD0", "苹果": "AP0", "橡胶": "RU0",
}

# Inventory symbol mapping for futures_inventory_em
INVENTORY_SYMBOLS = {
    "铜": "沪铜", "铝": "沪铝", "锌": "沪锌", "镍": "沪镍", "锡": "沪锡",
    "铅": "沪铅", "金": "沪金", "银": "沪银",
    "螺纹钢": "螺纹钢", "热卷": "热卷", "铁矿石": "铁矿石",
    "焦煤": "焦煤", "焦炭": "焦炭",
    "原油": "原油", "燃油": "燃油", "沥青": "沥青",
    "甲醇": "甲醇", "PTA": "PTA", "乙二醇": "乙二醇",
    "聚丙烯": "聚丙烯", "聚乙烯": "塑料", "PVC": "PVC", "纯碱": "纯碱",
    "豆粕": "豆粕", "豆油": "豆油", "棕榈油": "棕榈", "菜籽油": "菜油",
    "玉米": "玉米", "棉花": "郑棉", "白糖": "白糖", "橡胶": "橡胶",
    "沪铜": "沪铜", "沪铝": "沪铝", "沪锌": "沪锌", "沪镍": "沪镍",
}


def get_futures_quotes_fn(symbols: str = "", days: int = 5) -> str:
    """Get recent OHLCV data for futures main contracts.

    Args:
        symbols: Comma-separated contract names (e.g. "沪铜,沪金,螺纹钢").
                 Empty string returns all available main contracts.
        days: Number of recent trading days to return.
    """
    try:
        end_date = datetime.now().strftime("%Y%m%d")
        start_date = (datetime.now() - timedelta(days=max(days * 2, 14))).strftime("%Y%m%d")

        if symbols:
            names = [s.strip() for s in symbols.split(",")]
        else:
            names = list(MAIN_CONTRACTS.keys())

        results = []
        for name in names[:10]:  # limit to 10 to avoid timeout
            code = MAIN_CONTRACTS.get(name)
            if not code:
                continue
            try:
                with no_proxy():
                    df = ak.futures_main_sina(symbol=code, start_date=start_date, end_date=end_date)
                if df.empty:
                    continue
                df = df.tail(days)
                records = []
                for _, row in df.iterrows():
                    records.append({
                        "date": str(row["日期"]),
                        "open": float(row["开盘价"]),
                        "high": float(row["最高价"]),
                        "low": float(row["最低价"]),
                        "close": float(row["收盘价"]),
                        "volume": int(row["成交量"]),
                        "open_interest": int(row["持仓量"]),
                    })
                if records:
                    latest = records[-1]
                    prev = records[-2] if len(records) > 1 else latest
                    change_pct = round((latest["close"] - prev["close"]) / prev["close"] * 100, 2) if prev["close"] != 0 else 0
                    results.append({
                        "name": name,
                        "symbol": code,
                        "latest_close": latest["close"],
                        "change_pct": change_pct,
                        "volume": latest["volume"],
                        "open_interest": latest["open_interest"],
                        "history": records,
                    })
            except Exception as e:
                logger.debug("Failed to fetch %s: %s", name, e)

        return json.dumps({"count": len(results), "quotes": results}, ensure_ascii=False)
    except Exception as e:
        logger.error("get_futures_quotes failed: %s", e)
        return json.dumps({"count": 0, "quotes": [], "error": str(e)}, ensure_ascii=False)


# Fuzzy mapping for inventory: agent may pass various names
_INVENTORY_ALIASES = {
    "铜": "沪铜", "铝": "沪铝", "锌": "沪锌", "镍": "沪镍", "锡": "沪锡", "铅": "沪铅",
    "金": "沪金", "银": "沪银", "原油": "燃油",  # 原油无库存数据，用燃油近似
    "聚乙烯": "塑料", "棕榈油": "棕榈", "菜籽油": "菜油", "棉花": "郑棉",
}


def get_futures_inventory_fn(symbol: str) -> str:
    """Get warehouse inventory data for a futures commodity.

    Args:
        symbol: Commodity name (e.g. "沪铜", "螺纹钢", "铁矿石").
    """
    try:
        # Try direct, then alias, then INVENTORY_SYMBOLS mapping
        inv_symbol = _INVENTORY_ALIASES.get(symbol, INVENTORY_SYMBOLS.get(symbol, symbol))
        with no_proxy():
            df = ak.futures_inventory_em(symbol=inv_symbol)
        if df.empty:
            return json.dumps({"symbol": symbol, "data": [], "error": None}, ensure_ascii=False)

        df = df.tail(30)  # last 30 data points
        records = []
        for _, row in df.iterrows():
            records.append({
                "date": str(row["日期"]),
                "inventory": float(row["库存"]),
                "change": float(row["增减"]) if row["增减"] == row["增减"] else 0,
            })

        latest = records[-1] if records else {}
        trend = "去库存" if len(records) >= 3 and records[-1]["inventory"] < records[-3]["inventory"] else "累库存"

        return json.dumps({
            "symbol": symbol,
            "latest_inventory": latest.get("inventory", 0),
            "latest_change": latest.get("change", 0),
            "trend": trend,
            "data": records,
            "error": None,
        }, ensure_ascii=False)
    except Exception as e:
        logger.error("get_futures_inventory failed: %s", e)
        return json.dumps({"symbol": symbol, "data": [], "error": str(e)}, ensure_ascii=False)


def get_futures_basis_fn(date: str = "") -> str:
    """Get spot-futures basis data (现期差/基差).

    Args:
        date: Date in YYYYMMDD format. Empty for latest available.
    """
    try:
        if not date:
            date = datetime.now().strftime("%Y%m%d")

        with no_proxy():
            df = ak.futures_spot_price(date=date)
        if df.empty:
            # Try previous day
            prev = (datetime.strptime(date, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
            with no_proxy():
                df = ak.futures_spot_price(date=prev)

        if df.empty:
            return json.dumps({"date": date, "data": [], "error": "no data"}, ensure_ascii=False)

        records = []
        for _, row in df.iterrows():
            records.append({
                "symbol": str(row["symbol"]),
                "spot_price": float(row["spot_price"]) if row["spot_price"] == row["spot_price"] else None,
                "dominant_contract": str(row["dominant_contract"]),
                "dominant_price": float(row["dominant_contract_price"]) if row["dominant_contract_price"] == row["dominant_contract_price"] else None,
                "basis": float(row["dom_basis"]) if row["dom_basis"] == row["dom_basis"] else None,
                "basis_rate": float(row["dom_basis_rate"]) if row["dom_basis_rate"] == row["dom_basis_rate"] else None,
            })

        return json.dumps({"date": date, "count": len(records), "data": records}, ensure_ascii=False)
    except Exception as e:
        logger.error("get_futures_basis failed: %s", e)
        return json.dumps({"date": date, "data": [], "error": str(e)}, ensure_ascii=False)


# CFTC commodity name mapping for column lookup
CFTC_COMMODITIES = {
    "原油": "纽约原油", "黄金": "黄金", "白银": "白银", "铂金": "铂金",
    "天然气": "天然气", "铜": "铜", "玉米": "玉米", "大豆": "大豆",
    "豆油": "豆油", "豆粕": "豆粕", "棉花": "棉花", "白糖": "原糖",
    "钯金": "钯金",
}


def get_cftc_positions_fn(commodity: str = "") -> str:
    """Get CFTC Commitment of Traders data for commodity futures.

    Args:
        commodity: Commodity name (e.g. "原油", "黄金", "大豆"). Empty for all.
    """
    try:
        with no_proxy():
            df = ak.macro_usa_cftc_c_holding()
        if df.empty:
            return json.dumps({"data": [], "error": "no data"}, ensure_ascii=False)

        # Get last 10 weeks
        df = df.tail(10)

        if commodity:
            cn_name = CFTC_COMMODITIES.get(commodity, commodity)
            # Find columns matching this commodity
            long_col = f"{cn_name}_多单"
            short_col = f"{cn_name}_空单"
            net_col = f"{cn_name}_净多"
            if net_col not in df.columns:
                available = [c.rsplit("_", 1)[0] for c in df.columns if c.endswith("_净多")]
                return json.dumps({
                    "commodity": commodity,
                    "error": f"未找到 {cn_name}，可选: {list(set(available))}",
                    "data": [],
                }, ensure_ascii=False)

            records = []
            for _, row in df.iterrows():
                records.append({
                    "date": str(row["日期"]),
                    "long": int(row[long_col]) if long_col in df.columns else None,
                    "short": int(row[short_col]) if short_col in df.columns else None,
                    "net": int(row[net_col]),
                })

            # Trend analysis
            if len(records) >= 2:
                latest_net = records[-1]["net"]
                prev_net = records[-2]["net"]
                trend = "净多增加" if latest_net > prev_net else "净多减少"
            else:
                trend = "数据不足"

            return json.dumps({
                "commodity": commodity,
                "latest_net": records[-1]["net"] if records else 0,
                "trend": trend,
                "data": records,
            }, ensure_ascii=False)
        else:
            # Return summary of all commodities
            latest = df.iloc[-1]
            prev = df.iloc[-2] if len(df) > 1 else latest
            summary = []
            for display_name, cn_name in CFTC_COMMODITIES.items():
                net_col = f"{cn_name}_净多"
                if net_col in df.columns:
                    net_now = int(latest[net_col])
                    net_prev = int(prev[net_col])
                    summary.append({
                        "commodity": display_name,
                        "net_position": net_now,
                        "change": net_now - net_prev,
                        "direction": "净多" if net_now > 0 else "净空",
                    })
            return json.dumps({
                "date": str(latest["日期"]),
                "summary": summary,
            }, ensure_ascii=False)
    except Exception as e:
        logger.error("get_cftc_positions failed: %s", e)
        return json.dumps({"data": [], "error": str(e)}, ensure_ascii=False)
