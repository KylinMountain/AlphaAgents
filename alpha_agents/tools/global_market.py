"""Global market tools — US indices, bond yields, cross-market signals."""

import json
import logging

from alpha_agents.data.market_data import get_us_index as _get_us_index, get_bond_yields as _get_bond_yields

logger = logging.getLogger(__name__)

US_INDICES = {
    "道琼斯": ".DJI",
    "标普500": ".INX",
    "纳斯达克": ".IXIC",
}


def get_us_market_fn() -> str:
    """Get latest US stock index data (Dow, S&P 500, Nasdaq)."""
    try:
        results = []
        for name, symbol in US_INDICES.items():
            try:
                df = _get_us_index(symbol=symbol)
                if df is None or df.empty:
                    continue
                df = df.tail(2)
                latest = df.iloc[-1]
                prev = df.iloc[-2] if len(df) > 1 else latest
                close = float(latest["close"])
                prev_close = float(prev["close"])
                change_pct = round((close - prev_close) / prev_close * 100, 2) if prev_close else 0
                results.append({"name": name, "close": close, "change_pct": change_pct, "date": str(latest["date"])})
            except Exception as e:
                logger.debug("Failed to fetch %s: %s", name, e)
        return json.dumps({"count": len(results), "indices": results, "error": None}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"indices": [], "error": str(e)}, ensure_ascii=False)


def get_bond_yields_fn() -> str:
    """Get China and US government bond yields (2Y, 5Y, 10Y, 30Y)."""
    try:
        df = _get_bond_yields()
        if df is None or df.empty:
            return json.dumps({"error": "no bond data", "data": {}}, ensure_ascii=False)
        df = df.dropna(subset=["美国国债收益率10年"], how="all").tail(2)
        if df.empty:
            return json.dumps({"error": "no recent bond data", "data": {}}, ensure_ascii=False)

        latest = df.iloc[-1]
        prev = df.iloc[-2] if len(df) > 1 else latest

        def sf(val):
            try:
                v = float(val)
                return v if v == v else None
            except (ValueError, TypeError):
                return None

        us_10y = sf(latest.get("美国国债收益率10年"))
        us_2y = sf(latest.get("美国国债收益率2年"))
        cn_10y = sf(latest.get("中国国债收益率10年"))
        us_10y_prev = sf(prev.get("美国国债收益率10年"))

        signals = []
        if us_10y and us_10y_prev:
            change = round(us_10y - us_10y_prev, 2)
            if change > 0.05:
                signals.append(f"美债10Y上升{change}%，成长股估值承压")
            elif change < -0.05:
                signals.append(f"美债10Y下降{abs(change)}%，成长股估值支撑")
        if us_10y and us_2y:
            spread = round(us_10y - us_2y, 2)
            if spread < 0:
                signals.append(f"美债10Y-2Y倒挂({spread}%)，衰退风险信号")
        if cn_10y and us_10y:
            cn_us = round(cn_10y - us_10y, 2)
            if cn_us < -2.0:
                signals.append(f"中美利差{cn_us}%，资本外流压力")

        return json.dumps({
            "date": str(latest.get("日期", "")),
            "us": {"2y": us_2y, "5y": sf(latest.get("美国国债收益率5年")), "10y": us_10y,
                   "30y": sf(latest.get("美国国债收益率30年")),
                   "10y_2y_spread": round(us_10y - us_2y, 2) if us_10y and us_2y else None},
            "cn": {"2y": sf(latest.get("中国国债收益率2年")), "5y": sf(latest.get("中国国债收益率5年")),
                   "10y": cn_10y, "30y": sf(latest.get("中国国债收益率30年"))},
            "cn_us_10y_spread": round(cn_10y - us_10y, 2) if cn_10y and us_10y else None,
            "signals": signals, "error": None,
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e), "data": {}}, ensure_ascii=False)


def get_global_overview_fn() -> str:
    """Combined global market overview — US indices + bond yields + key signals."""
    us = json.loads(get_us_market_fn())
    bonds = json.loads(get_bond_yields_fn())
    all_signals = bonds.get("signals", [])
    for idx in us.get("indices", []):
        change = idx.get("change_pct", 0)
        if abs(change) > 1.5:
            all_signals.append(f"{idx['name']}{'大涨' if change > 0 else '大跌'}{abs(change):.1f}%")
    return json.dumps({
        "us_indices": us.get("indices", []),
        "bond_yields": {"us_10y": bonds.get("us", {}).get("10y"), "cn_10y": bonds.get("cn", {}).get("10y"),
                        "us_10y_2y_spread": bonds.get("us", {}).get("10y_2y_spread"),
                        "cn_us_spread": bonds.get("cn_us_10y_spread")},
        "signals": all_signals, "signal_count": len(all_signals),
    }, ensure_ascii=False)
