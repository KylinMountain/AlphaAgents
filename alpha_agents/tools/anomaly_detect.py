"""Anomaly detection tool — find stocks with unusual volume/price behavior.

Detects stocks with abnormal volume ratio (量比 > 3) or extreme turnover,
which often signal institutional activity or news-driven moves.
Uses the existing stock_zt_pool_em for limit-up detection.
"""

import json
import logging
from datetime import datetime

from alpha_agents.data.market_data import get_limit_up_pool, get_broken_limit_pool, get_limit_down_pool

logger = logging.getLogger(__name__)


def _reconstruct_limit_pools_from_kline(as_of: str) -> str:
    """Rebuild limit-up/limit-down lists from daily_kline change_pct for as_of."""
    from alpha_agents.data.market_history import _get_conn as _mh_conn
    rows = _mh_conn().execute(
        "SELECT code, change_pct, close FROM daily_kline WHERE date = ? "
        "AND (change_pct >= 9.8 OR change_pct <= -9.8) ORDER BY change_pct DESC",
        (as_of,),
    ).fetchall()
    up, down = [], []
    for r in rows:
        entry = {"code": r["code"], "change_pct": r["change_pct"],
                 "close": r["close"]}
        if r["change_pct"] >= 9.8:
            up.append(entry)
        else:
            down.append(entry)
    return json.dumps({
        "date": as_of, "limit_up": up, "limit_down": down, "broken_limit": [],
        "error": None, "note": "reconstructed from daily_kline",
    }, ensure_ascii=False)


def get_anomaly_stocks_fn(date: str = "") -> str:
    """Detect stocks with unusual price/volume behavior.

    Returns:
    - Limit-up stocks (涨停) with seal strength info
    - Limit-down stocks (跌停)
    - Stocks breaking out of limit-up (炸板)

    Args:
        date: Date in YYYYMMDD format. Empty for today.
    """
    # Replay mode: limit-up pool is EOD data (published after close); in
    # point-in-time replay before 15:00 we must roll back to T-1.
    try:
        from alpha_agents.evolution.replay_mode import get_replay_as_of, effective_eod_cut_date
        as_of = get_replay_as_of()
    except Exception:
        as_of = None
    if as_of:
        from alpha_agents.data.memory_store import _get_conn
        cut = effective_eod_cut_date(as_of) or as_of
        row = _get_conn().execute(
            "SELECT date, data FROM daily_snapshots WHERE data_type='limit_up_pool' "
            "AND date <= ? ORDER BY date DESC LIMIT 1",
            (cut,),
        ).fetchone()
        if row:
            data = json.loads(row["data"])
            return json.dumps({
                "date": row["date"],
                "limit_up": data.get("limit_up", []),
                "limit_down": data.get("limit_down", []),
                "broken_limit": data.get("broken_board", []),
                "error": None,
            }, ensure_ascii=False)
        # Fallback: reconstruct from daily_kline (also needs cut-date semantics)
        return _reconstruct_limit_pools_from_kline(cut)

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

        # 1. Limit-up pool (涨停) — return up to 100 to cover active days
        # Per-stock "lu_desc" (Kaipanla 涨停原因, e.g. "算力"/"锂电池") comes
        # from Tushare kpl_limit_list_daily — this is the authoritative theme
        # attribution that 同花顺 displays. akshare's 所属行业 is kept as the
        # structural label (industry classification) alongside.
        try:
            df_zt = get_limit_up_pool(date=date)
            if df_zt is None:
                raise ValueError("no data")

            # Batch-lookup lu_desc/theme for this day's limit-up set
            from alpha_agents.data.tushare_store import read_kpl_limit_list_for_date
            trade_date_iso = f"{date[:4]}-{date[4:6]}-{date[6:8]}" if len(date) == 8 and date.isdigit() else date
            kpl_rows = read_kpl_limit_list_for_date(trade_date_iso)
            kpl_by_code = {r["code"]: r for r in kpl_rows}

            # Filter to user's tradable universe: 科创板 (688/689) and
            # 北交所 stocks show up in limit-up pool but we can't buy them,
            # so there's no point surfacing them as signals.
            from alpha_agents.config import is_tradable
            for _, row in df_zt.head(200).iterrows():
                code = str(row.get("代码", ""))
                if not is_tradable(code):
                    continue
                kpl = kpl_by_code.get(code, {})
                result["limit_up"].append({
                    "code": code,
                    "name": str(row.get("名称", "")),
                    "change_pct": float(row.get("涨跌幅", 0) or 0),
                    "turnover_rate": float(row.get("换手率", 0) or 0),
                    "seal_amount_yi": round(float(row.get("封板资金", 0) or 0) / 1e8, 2),
                    "first_seal_time": str(row.get("首次封板时间", "")),
                    "break_count": int(row.get("炸板次数", 0) or 0),
                    "consecutive_limits": int(row.get("连板数", 0) or 0),
                    "industry": str(row.get("所属行业", "")),   # structural label
                    "lu_desc": kpl.get("lu_desc") or "",       # concept reason (primary theme)
                    "theme": kpl.get("theme") or "",            # related themes, comma-sep
                    "board_status": kpl.get("status") or "",    # 首板 / 2连板 / ...
                })
                if len(result["limit_up"]) >= 100:
                    break
        except Exception as e:
            logger.debug("Limit-up pool failed: %s", e)

        # 2. Broken limit-up pool (炸板)
        try:
            df_zb = get_broken_limit_pool(date=date)
            if df_zb is None:
                raise ValueError("no data")
            for _, row in df_zb.head(30).iterrows():
                code = str(row.get("代码", ""))
                if not is_tradable(code):
                    continue
                if len(result["broken_limit"]) >= 10:
                    break
                kpl = kpl_by_code.get(code, {})
                result["broken_limit"].append({
                    "code": code,
                    "name": str(row.get("名称", "")),
                    "change_pct": float(row.get("涨跌幅", 0) or 0),
                    "turnover_rate": float(row.get("换手率", 0) or 0),
                    "industry": str(row.get("所属行业", "")),
                    "lu_desc": kpl.get("lu_desc") or "",
                    "theme": kpl.get("theme") or "",
                })
        except Exception as e:
            logger.debug("Broken limit pool failed: %s", e)

        # 3. Limit-down pool (跌停)
        try:
            df_dt = get_limit_down_pool(date=date)
            if df_dt is None:
                raise ValueError("no data")
            for _, row in df_dt.head(30).iterrows():
                code = str(row.get("代码", ""))
                if not is_tradable(code):
                    continue
                if len(result["limit_down"]) >= 10:
                    break
                # KPL has a 'tag' column for 涨停/跌停; filtering happens in the
                # backfill query (tag='涨停') so kpl_by_code here only covers
                # limit-up stocks. Limit-down just gets industry + empty lu_desc.
                result["limit_down"].append({
                    "code": code,
                    "name": str(row.get("名称", "")),
                    "change_pct": float(row.get("涨跌幅", 0) or 0),
                    "industry": str(row.get("所属行业", "")),
                    "lu_desc": "",
                    "theme": "",
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
    """Find the most common theme among a list of stocks.

    Prefers Kaipanla lu_desc (涨停原因, concept-level) over industry, falling
    back to the legacy 'sector' key for limit_down/broken_limit entries which
    haven't been enriched with KPL data yet.
    """
    if not stocks:
        return ""
    sectors: dict[str, int] = {}
    for s in stocks:
        key = s.get("lu_desc") or s.get("industry") or s.get("sector", "")
        if key:
            sectors[key] = sectors.get(key, 0) + 1
    return max(sectors, key=sectors.get) if sectors else ""
