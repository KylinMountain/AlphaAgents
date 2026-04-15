"""Unified market data provider — abstract layer over akshare, baostock, etc.

All market data fetching goes through this module. Individual tools call
these functions instead of directly importing akshare/baostock. This makes
it easy to swap backends, add caching, or handle proxy issues in one place.

Backend selection:
- Stock quotes/history: baostock (TCP, bypasses HTTP proxy)
- Sector/concept fund flow: akshare via THS (data.10jqka.com, reliable)
- LHB/block trade/margin: akshare via eastmoney (data.eastmoney.com, reliable)
- North flow: akshare (push2, may fail with Clash — fallback to empty)
- Futures: akshare via sina (reliable)
- US market/bonds: akshare via sina (reliable)
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Optional

import baostock as bs
import pandas as pd

from alpha_agents.config import no_proxy

logger = logging.getLogger(__name__)

# ── baostock helpers ─────────────────────────────────────────

import threading

_bs_logged_in = False
_bs_lock = threading.Lock()


def _bs_login():
    global _bs_logged_in
    if not _bs_logged_in:
        lg = bs.login()
        if lg.error_code != "0":
            raise RuntimeError(f"baostock login failed: {lg.error_msg}")
        _bs_logged_in = True


def _bs_logout():
    global _bs_logged_in
    if _bs_logged_in:
        try:
            bs.logout()
        except Exception:
            pass
        _bs_logged_in = False


def _to_bs_code(code: str) -> str:
    """Convert 6-digit code to baostock format (sh.XXXXXX / sz.XXXXXX)."""
    if code.startswith("6"):
        return f"sh.{code}"
    return f"sz.{code}"


def _bs_query(bs_code: str, fields: str, start: str, end: str, frequency: str = "d") -> list[list[str]]:
    """Run a baostock query and return raw row data."""
    rs = bs.query_history_k_data_plus(
        bs_code, fields,
        start_date=start, end_date=end,
        frequency=frequency, adjustflag="2",
    )
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    return rows


# ── akshare helper ───────────────────────────────────────────

def _ak_call(fn, *args, **kwargs):
    """Call an akshare function inside no_proxy(), return DataFrame or None."""
    try:
        with no_proxy():
            result = fn(*args, **kwargs)
        if result is None or (hasattr(result, 'empty') and result.empty):
            return None
        return result
    except Exception as e:
        logger.debug("akshare call %s failed: %s", fn.__name__, e)
        return None


# ═══════════════════════════════════════════════════════════════
# PUBLIC API — all tools should call these instead of akshare/bs
# ═══════════════════════════════════════════════════════════════


# ── Stock Quotes (baostock TCP) ──────────────────────────────

def get_realtime_quotes(codes: list[str]) -> Optional[dict]:
    """Get real-time intraday quotes via Sina finance API.

    Returns dict mapping code -> {price, change_pct, volume, turnover_rate, ...}
    Works during trading hours. Sina API is lightweight and reliable.
    """
    import urllib.request

    if not codes:
        return None

    # Build Sina symbol list: sz300475, sh600519
    symbols = []
    for code in codes:
        prefix = "sh" if code.startswith("6") else "sz"
        symbols.append(f"{prefix}{code}")

    url = f"https://hq.sinajs.cn/list={','.join(symbols)}"
    try:
        proxy_handler = urllib.request.ProxyHandler({})
        opener = urllib.request.build_opener(proxy_handler)
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn",
        })
        resp = opener.open(req, timeout=10)
        text = resp.read().decode("gbk")
    except Exception as e:
        logger.debug("Sina realtime API failed: %s", e)
        return None

    # Parse Sina format: var hq_str_sz300475="name,open,prev_close,price,high,low,..."
    result = {}
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line or '="' not in line:
            continue
        # Extract symbol and data
        var_part, data_part = line.split("=", 1)
        symbol = var_part.split("_")[-1]  # e.g. "sz300475"
        code = symbol[2:]  # e.g. "300475"
        data_str = data_part.strip().strip('"').strip(";").strip('"')
        if not data_str:
            continue

        fields = data_str.split(",")
        if len(fields) < 32:
            continue

        # Sina fields: 0=name, 1=open, 2=prev_close, 3=price, 4=high, 5=low,
        # 6=bid, 7=ask, 8=volume(shares), 9=amount(yuan), ...
        try:
            name = fields[0]
            open_price = float(fields[1]) if fields[1] else 0
            prev_close = float(fields[2]) if fields[2] else 0
            price = float(fields[3]) if fields[3] else 0
            high = float(fields[4]) if fields[4] else 0
            low = float(fields[5]) if fields[5] else 0
            volume = int(fields[8]) if fields[8] else 0
            amount = float(fields[9]) if fields[9] else 0

            if price <= 0 or prev_close <= 0:
                continue

            change_pct = round((price - prev_close) / prev_close * 100, 2)

            result[code] = {
                "code": code,
                "name": name,
                "price": price,
                "change_pct": change_pct,
                "high": high,
                "low": low,
                "open": open_price,
                "prev_close": prev_close,
                "volume": volume,
                "amount_yi": round(amount / 1e8, 2),
                "volume_ratio": 0,  # Sina doesn't provide this
                "turnover_rate": 0,  # Need float shares to compute
                "date": fields[30] if len(fields) > 30 else "",
            }
        except (ValueError, IndexError):
            continue

    return result if result else None


def get_stock_history(code: str, days: int = 5) -> Optional[list[dict]]:
    """Get recent daily OHLCV for a stock. Checks local DB first, falls back to baostock."""
    # Try local market history DB first (fast, no network)
    try:
        from alpha_agents.data.market_history import get_local_history
        local = get_local_history(code, days)
        if local:
            return local
    except Exception:
        pass

    with _bs_lock:
        try:
            _bs_login()
            end = datetime.now().strftime("%Y-%m-%d")
            start = (datetime.now() - timedelta(days=days * 3)).strftime("%Y-%m-%d")
            rows = _bs_query(
                _to_bs_code(code),
                "date,open,high,low,close,volume,turn,pctChg",
                start, end,
            )
            if not rows:
                return None
            result = []
            for r in rows[-days:]:
                result.append({
                    "date": r[0],
                    "open": float(r[1]) if r[1] else 0,
                    "high": float(r[2]) if r[2] else 0,
                    "low": float(r[3]) if r[3] else 0,
                    "close": float(r[4]) if r[4] else 0,
                    "volume": int(r[5]) if r[5] else 0,
                    "turnover_rate": float(r[6]) if r[6] else 0,
                    "change_pct": float(r[7]) if r[7] else 0,
                })
            return result
        except Exception as e:
            logger.debug("get_stock_history(%s) failed: %s", code, e)
            # Force re-login on next call if something went wrong
            _bs_logout()
            return None


def get_stock_latest_price(code: str) -> Optional[dict]:
    """Get latest close price and change for a stock."""
    history = get_stock_history(code, days=2)
    if not history:
        return None
    latest = history[-1]
    prev = history[-2] if len(history) > 1 else latest
    return {
        "code": code,
        "date": latest["date"],
        "price": latest["close"],
        "change_pct": round((latest["close"] - prev["close"]) / prev["close"] * 100, 2) if prev["close"] else 0,
        "volume": latest["volume"],
        "turnover_rate": latest["turnover_rate"],
    }


# ── Sector/Concept Fund Flow (akshare THS) ──────────────────

def get_industry_fund_flow() -> Optional[pd.DataFrame]:
    """Get industry-level fund flow ranking (90 sectors)."""
    import akshare as ak
    return _ak_call(ak.stock_fund_flow_industry)


def get_concept_fund_flow() -> Optional[pd.DataFrame]:
    """Get concept-level fund flow ranking (387 concepts)."""
    import akshare as ak
    return _ak_call(ak.stock_fund_flow_concept)


# ── LHB / Block Trade / Margin (akshare eastmoney) ──────────

def get_lhb(date: str = "") -> Optional[pd.DataFrame]:
    """Get dragon-tiger board detail for a date (YYYYMMDD)."""
    import akshare as ak
    if not date:
        date = datetime.now().strftime("%Y%m%d")
    return _ak_call(ak.stock_lhb_detail_em, start_date=date, end_date=date)


def get_block_trades(date: str = "") -> Optional[pd.DataFrame]:
    """Get block trade summary for a date (YYYYMMDD)."""
    import akshare as ak
    if not date:
        date = datetime.now().strftime("%Y%m%d")
    return _ak_call(ak.stock_dzjy_mrtj, start_date=date, end_date=date)


def get_margin_detail() -> Optional[pd.DataFrame]:
    """Get margin trading detail (SSE)."""
    import akshare as ak
    date = datetime.now().strftime("%Y%m%d")
    df = _ak_call(ak.stock_margin_detail_sse, date=date)
    if df is None:
        prev = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
        df = _ak_call(ak.stock_margin_detail_sse, date=prev)
    return df


def get_north_holdings() -> Optional[pd.DataFrame]:
    """Get northbound capital stock holdings."""
    import akshare as ak
    return _ak_call(ak.stock_hsgt_hold_stock_em, market="北向", indicator="今日排行")


def get_individual_fund_flow(code: str, market: str = "") -> Optional[pd.DataFrame]:
    """Get individual stock fund flow (main force vs retail)."""
    import akshare as ak
    if not market:
        market = "sh" if code.startswith("6") else "sz"
    return _ak_call(ak.stock_individual_fund_flow, stock=code, market=market)


# ── Anomaly Detection (akshare eastmoney) ────────────────────

def get_limit_up_pool(date: str = "") -> Optional[pd.DataFrame]:
    """Get limit-up stock pool."""
    import akshare as ak
    if not date:
        date = datetime.now().strftime("%Y%m%d")
    return _ak_call(ak.stock_zt_pool_em, date=date)


def get_broken_limit_pool(date: str = "") -> Optional[pd.DataFrame]:
    """Get broken limit-up stock pool."""
    import akshare as ak
    if not date:
        date = datetime.now().strftime("%Y%m%d")
    return _ak_call(ak.stock_zt_pool_zbgc_em, date=date)


def get_limit_down_pool(date: str = "") -> Optional[pd.DataFrame]:
    """Get limit-down stock pool."""
    import akshare as ak
    if not date:
        date = datetime.now().strftime("%Y%m%d")
    return _ak_call(ak.stock_zt_pool_dtgc_em, date=date)


# ── Market Breadth (akshare legu) ────────────────────────────

def get_market_activity() -> Optional[pd.DataFrame]:
    """Get A-share market activity (advance/decline/limit up-down)."""
    import akshare as ak
    return _ak_call(ak.stock_market_activity_legu)


# ── Financial Data (akshare THS) ────────────────────────────

def get_financial_indicator(code: str) -> Optional[pd.DataFrame]:
    """Get financial analysis indicators for a stock."""
    import akshare as ak
    year = str(datetime.now().year - 1)
    return _ak_call(ak.stock_financial_analysis_indicator, symbol=code, start_year=year)


# ── Market index history (for VPA relative strength) ────────
# In-process cache: index history changes once per day, no need to hit
# akshare on every VPA call. Reset by restarting the process.

_INDEX_CACHE: dict[str, tuple[str, list[dict]]] = {}  # symbol → (date, rows)


def get_market_index_history(symbol: str = "sh000001", days: int = 30) -> list[dict]:
    """Get daily history for a market index (for VPA relative strength).

    Returns last N days of [{date, close, change_pct}, ...] sorted oldest→newest.
    Cached per-day in-process to avoid repeated akshare hits.

    Args:
        symbol: "sh000001" (上证指数, default), "sz399001" (深证), "sz399006" (创业板)
        days: How many trailing trading days to return
    """
    today = datetime.now().strftime("%Y-%m-%d")
    cached = _INDEX_CACHE.get(symbol)
    if cached and cached[0] == today and len(cached[1]) >= days:
        return cached[1][-days:]

    import akshare as ak
    try:
        df = _ak_call(ak.stock_zh_index_daily, symbol=symbol)
    except Exception:
        return []
    if df is None or df.empty:
        return []

    # Normalize columns and compute change_pct from close diff
    df = df.tail(max(days * 2, 60)).copy()  # extra buffer for the diff
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["change_pct"] = df["close"].pct_change() * 100
    df = df.dropna(subset=["close"])

    rows = [
        {"date": str(r["date"]), "close": float(r["close"]),
         "change_pct": float(r["change_pct"]) if pd.notna(r["change_pct"]) else 0.0}
        for _, r in df.iterrows()
    ]
    _INDEX_CACHE[symbol] = (today, rows)
    return rows[-days:]


# ── Earnings Forecast (akshare eastmoney) ────────────────────

def get_earnings_forecast(date: str = "") -> Optional[pd.DataFrame]:
    """Get earnings forecast (业绩预告)."""
    import akshare as ak
    if not date:
        now = datetime.now()
        if now.month <= 4:
            date = f"{now.year - 1}1231"
        elif now.month <= 8:
            date = f"{now.year}0630"
        elif now.month <= 10:
            date = f"{now.year}0930"
        else:
            date = f"{now.year}0930"
    return _ak_call(ak.stock_yjyg_em, date=date)


# ── Futures (akshare sina) ───────────────────────────────────

def get_futures_history(symbol: str, start: str, end: str) -> Optional[pd.DataFrame]:
    """Get futures main contract OHLCV."""
    import akshare as ak
    return _ak_call(ak.futures_main_sina, symbol=symbol, start_date=start, end_date=end)


def get_futures_inventory(symbol: str) -> Optional[pd.DataFrame]:
    """Get futures warehouse inventory."""
    import akshare as ak
    return _ak_call(ak.futures_inventory_em, symbol=symbol)


def get_futures_spot_price(date: str = "") -> Optional[pd.DataFrame]:
    """Get spot-futures basis data."""
    import akshare as ak
    if not date:
        date = datetime.now().strftime("%Y%m%d")
    return _ak_call(ak.futures_spot_price, date=date)


def get_cftc_holdings() -> Optional[pd.DataFrame]:
    """Get CFTC commitment of traders data."""
    import akshare as ak
    return _ak_call(ak.macro_usa_cftc_c_holding)


# ── Global Markets (akshare sina) ────────────────────────────

def get_us_index(symbol: str) -> Optional[pd.DataFrame]:
    """Get US stock index history."""
    import akshare as ak
    return _ak_call(ak.index_us_stock_sina, symbol=symbol)


def get_bond_yields() -> Optional[pd.DataFrame]:
    """Get China and US government bond yields."""
    import akshare as ak
    return _ak_call(ak.bond_zh_us_rate)
