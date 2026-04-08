"""Stock quote tool — fetch prices and basic metrics for A-share stocks.

Uses baostock (TCP connection) for price data — completely bypasses
HTTP proxy issues with push2.eastmoney.com.
"""

import json
import logging
from datetime import datetime, timedelta

import baostock as bs

logger = logging.getLogger(__name__)

# baostock needs sh./sz. prefix
def _to_bs_code(code: str) -> str:
    if code.startswith("6"):
        return f"sh.{code}"
    return f"sz.{code}"


def _get_bs_connection():
    """Login to baostock. Caller must logout when done."""
    lg = bs.login()
    if lg.error_code != "0":
        raise RuntimeError(f"baostock login failed: {lg.error_msg}")
    return lg


def get_stock_quotes_fn(codes: str) -> str:
    """Fetch recent quotes for a list of A-share stocks.

    Uses baostock (TCP, no HTTP proxy issues) for price history.

    Args:
        codes: Comma-separated stock codes, e.g. "000858,600519,002594"
    """
    code_list = [c.strip() for c in codes.split(",") if c.strip()]
    if not code_list:
        return json.dumps({"error": "no stock codes provided", "quotes": []}, ensure_ascii=False)

    results = []
    try:
        _get_bs_connection()

        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")

        for code in code_list[:10]:
            try:
                bs_code = _to_bs_code(code)
                rs = bs.query_history_k_data_plus(
                    bs_code,
                    "date,code,open,high,low,close,volume,turn,pctChg",
                    start_date=start, end_date=end,
                    frequency="d", adjustflag="2",  # 前复权
                )
                rows = []
                while rs.error_code == "0" and rs.next():
                    rows.append(rs.get_row_data())

                if not rows:
                    results.append({"code": code, "error": "no data"})
                    continue

                # Last 5 days
                recent = rows[-5:] if len(rows) >= 5 else rows
                latest = recent[-1]
                prev = recent[-2] if len(recent) > 1 else latest

                close = float(latest[5])
                prev_close = float(prev[5])
                change_pct = round((close - prev_close) / prev_close * 100, 2) if prev_close else 0

                week_high = max(float(r[3]) for r in recent)
                week_low = min(float(r[4]) for r in recent)

                # Calculate 5-day cumulative change
                first_close = float(recent[0][5])
                week_change_pct = round((close - first_close) / first_close * 100, 2) if first_close else 0

                results.append({
                    "code": code,
                    "price": close,
                    "change_pct": change_pct,
                    "week_change_pct": week_change_pct,
                    "week_high": week_high,
                    "week_low": week_low,
                    "volume": int(latest[6]) if latest[6] else 0,
                    "turnover_rate": float(latest[7]) if latest[7] else 0,
                    "date": latest[0],
                })
            except Exception as e:
                logger.debug("Failed to fetch quote for %s: %s", code, e)
                results.append({"code": code, "error": str(e)})

    except Exception as e:
        logger.error("baostock connection failed: %s", e)
        return json.dumps({"error": str(e), "quotes": []}, ensure_ascii=False)
    finally:
        try:
            bs.logout()
        except Exception:
            pass

    return json.dumps({"count": len(results), "quotes": results}, ensure_ascii=False)
