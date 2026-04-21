"""Tushare-sourced EOD data: write/read helpers backed by market_snapshots.db.

Five Tushare endpoints have dedicated tables (schemas in snapshot_store._SCHEMA):
  - moneyflow_dc       → stock_fund_flow_daily
  - top_list           → lhb_daily
  - top_inst           → lhb_inst_daily
  - hm_detail          → hm_daily
  - moneyflow_hsgt     → north_flow_daily
  - margin             → margin_daily

Each writer takes a DataFrame from the Tushare call and upserts rows.
Each reader returns data shaped for the existing consumer (so proxy functions
can drop-in replace their live source).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

import pandas as pd

from alpha_agents.data.snapshot_store import _get_conn, _write_lock

logger = logging.getLogger(__name__)


def _bare_code(ts_code: str) -> str:
    """'600519.SH' → '600519'."""
    return ts_code.split(".")[0] if ts_code else ""


def _f(row, key):
    try:
        v = row.get(key)
        return float(v) if v is not None and v == v else None
    except Exception:
        return None


def _s(row, key):
    v = row.get(key)
    return str(v) if v is not None else ""


# ── moneyflow_dc ──

def save_stock_fund_flow_daily(df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0
    rows = []
    for _, r in df.iterrows():
        ts_code = _s(r, "ts_code").strip()
        if not ts_code:
            continue
        rows.append((
            _s(r, "trade_date"), ts_code, _bare_code(ts_code),
            _s(r, "name"), _f(r, "pct_change"), _f(r, "close"),
            _f(r, "net_amount"), _f(r, "net_amount_rate"),
            _f(r, "buy_elg_amount"), _f(r, "buy_elg_amount_rate"),
            _f(r, "buy_lg_amount"), _f(r, "buy_lg_amount_rate"),
            _f(r, "buy_md_amount"), _f(r, "buy_md_amount_rate"),
            _f(r, "buy_sm_amount"), _f(r, "buy_sm_amount_rate"),
        ))
    if not rows:
        return 0
    with _write_lock:
        _get_conn().executemany(
            "INSERT OR REPLACE INTO stock_fund_flow_daily VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


def read_stock_fund_flow_history(code: str, as_of: str,
                                  days: int = 5) -> dict | None:
    """Return last ``days`` EOD records for ``code`` at or before ``as_of``,
    shaped like ``get_stock_fund_flow_fn`` output (trend / consecutive days /
    records list). Enables the replay proxy to drop in seamlessly.
    """
    if len(as_of) == 10:
        as_of_date = as_of.replace("-", "")
    else:
        as_of_date = as_of[:10].replace("-", "")
    rows = _get_conn().execute(
        "SELECT trade_date, close, pct_change, net_amount, net_amount_rate "
        "FROM stock_fund_flow_daily "
        "WHERE code = ? AND trade_date <= ? "
        "ORDER BY trade_date DESC LIMIT ?",
        (code, as_of_date, days),
    ).fetchall()
    if not rows:
        return None

    records = []
    for r in reversed(rows):  # chronological
        net = r["net_amount"] or 0.0
        # Tushare reports net_amount in 万元; live fund_flow uses 元. Convert.
        main_net_yuan = net * 10000
        records.append({
            "date": f"{r['trade_date'][:4]}-{r['trade_date'][4:6]}-{r['trade_date'][6:8]}",
            "close": r["close"] or 0.0,
            "change_pct": r["pct_change"] or 0.0,
            "main_net_flow": main_net_yuan,
            "main_net_flow_yi": round(main_net_yuan / 1e8, 2),
            "main_net_pct": r["net_amount_rate"] or 0.0,
        })

    # Trend detection (matches get_stock_fund_flow_fn logic)
    consecutive_inflow = 0
    for rec in reversed(records):
        if rec["main_net_flow"] > 0:
            consecutive_inflow += 1
        else:
            break
    consecutive_outflow = 0
    for rec in reversed(records):
        if rec["main_net_flow"] < 0:
            consecutive_outflow += 1
        else:
            break

    if consecutive_inflow >= 2:
        trend = "主力连续流入"
    elif consecutive_outflow >= 2:
        trend = "主力连续流出"
    else:
        trend = "无明显趋势"

    return {
        "code": code,
        "trend": trend,
        "consecutive_inflow_days": consecutive_inflow,
        "consecutive_outflow_days": consecutive_outflow,
        "data": records,
    }


# ── top_list (LHB) ──

def save_lhb_daily(df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0
    rows = []
    for _, r in df.iterrows():
        ts_code = _s(r, "ts_code").strip()
        if not ts_code:
            continue
        rows.append((
            _s(r, "trade_date"), ts_code, _bare_code(ts_code),
            _s(r, "name"), _f(r, "close"), _f(r, "pct_change"),
            _f(r, "turnover_rate"), _f(r, "amount"),
            _f(r, "l_sell"), _f(r, "l_buy"), _f(r, "l_amount"),
            _f(r, "net_amount"), _f(r, "net_rate"), _f(r, "amount_rate"),
            _f(r, "float_values"), _s(r, "reason"),
        ))
    if not rows:
        return 0
    with _write_lock:
        _get_conn().executemany(
            "INSERT OR REPLACE INTO lhb_daily VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


def save_lhb_inst_daily(df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0
    rows = []
    for _, r in df.iterrows():
        ts_code = _s(r, "ts_code").strip()
        exalter = _s(r, "exalter").strip()
        if not ts_code or not exalter:
            continue
        side_raw = r.get("side")
        try:
            side = int(side_raw) if side_raw is not None else 0
        except (ValueError, TypeError):
            side = 0
        rows.append((
            _s(r, "trade_date"), ts_code, exalter, side,
            _f(r, "buy"), _f(r, "buy_rate"),
            _f(r, "sell"), _f(r, "sell_rate"), _f(r, "net_buy"),
            _s(r, "reason"),
        ))
    if not rows:
        return 0
    with _write_lock:
        _get_conn().executemany(
            "INSERT OR REPLACE INTO lhb_inst_daily VALUES (?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


def save_hm_daily(df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0
    rows = []
    for _, r in df.iterrows():
        ts_code = _s(r, "ts_code").strip()
        hm_name = _s(r, "hm_name").strip()
        if not ts_code or not hm_name:
            continue
        rows.append((
            _s(r, "trade_date"), ts_code, _bare_code(ts_code),
            _s(r, "ts_name"),
            _f(r, "buy_amount"), _f(r, "sell_amount"), _f(r, "net_amount"),
            hm_name, _s(r, "hm_orgs"), _s(r, "tag"),
        ))
    if not rows:
        return 0
    with _write_lock:
        _get_conn().executemany(
            "INSERT OR REPLACE INTO hm_daily VALUES (?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


def read_hm_for_date(trade_date: str) -> dict | None:
    """Return hot-money (游资) activity for the latest trade_date at or before
    ``trade_date``. Groups by hm_name so the caller sees 'who traded what'.
    """
    td = trade_date[:10].replace("-", "")
    conn = _get_conn()
    latest = conn.execute(
        "SELECT MAX(trade_date) AS t FROM hm_daily WHERE trade_date <= ?",
        (td,),
    ).fetchone()
    if not latest or not latest["t"]:
        return None
    td = latest["t"]
    rows = conn.execute(
        "SELECT code, ts_name, hm_name, hm_orgs, tag, "
        "       buy_amount, sell_amount, net_amount "
        "FROM hm_daily WHERE trade_date = ?",
        (td,),
    ).fetchall()
    if not rows:
        return None

    # Tushare hm_detail buy/sell/net amounts are in 元 (not 万 despite some
    # docs). Convert to 亿 for LLM consumption: 元 / 1e8 = 亿.
    by_hm: dict[str, dict] = {}
    for r in rows:
        entry = by_hm.setdefault(r["hm_name"], {
            "hm_name": r["hm_name"],
            "hm_orgs": r["hm_orgs"] or "",
            "tag": r["tag"] or "",
            "total_net_yuan": 0.0,
            "stocks": [],
        })
        net = r["net_amount"] or 0
        entry["total_net_yuan"] += net
        entry["stocks"].append({
            "code": r["code"], "name": r["ts_name"],
            "buy_yi": round((r["buy_amount"] or 0) / 1e8, 4),
            "sell_yi": round((r["sell_amount"] or 0) / 1e8, 4),
            "net_yi": round(net / 1e8, 4),
        })

    hm_list = sorted(by_hm.values(),
                     key=lambda x: abs(x["total_net_yuan"]),
                     reverse=True)
    for hm in hm_list:
        hm["total_net_yi"] = round(hm["total_net_yuan"] / 1e8, 4)
        del hm["total_net_yuan"]

    return {
        # Return the actual trading date whose data we loaded, not the caller's
        # query date (which may be a weekend/holiday).
        "date": f"{td[:4]}-{td[4:6]}-{td[6:8]}",
        "hm_count": len(hm_list),
        "stock_count": len(rows),
        "hm_list": hm_list,
    }


def read_lhb_for_date(trade_date: str) -> dict | None:
    """Return LHB+inst+hm for the latest trade_date at or before ``trade_date``
    (handles weekends/holidays correctly). Shape matches get_lhb_detail_fn."""
    td = trade_date[:10].replace("-", "")
    conn = _get_conn()

    # Resolve to the most recent trading date we have data for
    latest = conn.execute(
        "SELECT MAX(trade_date) AS t FROM lhb_daily WHERE trade_date <= ?",
        (td,),
    ).fetchone()
    if not latest or not latest["t"]:
        return None
    td = latest["t"]

    main = conn.execute(
        "SELECT * FROM lhb_daily WHERE trade_date = ?", (td,),
    ).fetchall()
    if not main:
        return None

    inst = conn.execute(
        "SELECT ts_code, exalter, side, buy, sell, net_buy "
        "FROM lhb_inst_daily WHERE trade_date = ?", (td,),
    ).fetchall()
    hm = conn.execute(
        "SELECT ts_code, hm_name, buy_amount, sell_amount, net_amount "
        "FROM hm_daily WHERE trade_date = ?", (td,),
    ).fetchall()

    # Roll up inst: who had most institutional buyers
    inst_by_code: dict[str, list[dict]] = {}
    for r in inst:
        inst_by_code.setdefault(r["ts_code"], []).append({
            "exalter": r["exalter"], "side": r["side"],
            "buy": r["buy"], "sell": r["sell"], "net_buy": r["net_buy"],
        })
    hm_by_code: dict[str, list[dict]] = {}
    for r in hm:
        hm_by_code.setdefault(r["ts_code"], []).append({
            "hm_name": r["hm_name"], "net_amount": r["net_amount"],
        })

    data = []
    for r in main:
        code = r["code"]
        net_buy_yi = (r["net_amount"] or 0) / 1e8
        has_inst = any(
            "机构" in (i["exalter"] or "") or "专用" in (i["exalter"] or "")
            for i in inst_by_code.get(r["ts_code"], [])
        )
        data.append({
            "code": code, "name": r["name"],
            "close": r["close"], "change_pct": r["pct_change"],
            "turnover_rate": r["turnover_rate"],
            "net_buy_yi": round(net_buy_yi, 2),
            "reason": r["reason"],
            "institutional": has_inst,
            "inst_seats": inst_by_code.get(r["ts_code"], []),
            "hm_seats": hm_by_code.get(r["ts_code"], []),
        })
    return {
        # Return the resolved trading date (may differ from input if weekend/holiday)
        "date": f"{td[:4]}-{td[4:6]}-{td[6:8]}",
        "count": len(data),
        "institutional_buys": sum(1 for d in data if d["institutional"]),
        "data": data,
    }


# ── moneyflow_hsgt ──

def save_north_flow_daily(df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0
    rows = []
    for _, r in df.iterrows():
        td = _s(r, "trade_date").strip()
        if not td:
            continue
        rows.append((
            td, _f(r, "ggt_ss"), _f(r, "ggt_sz"),
            _f(r, "hgt"), _f(r, "sgt"),
            _f(r, "north_money"), _f(r, "south_money"),
        ))
    if not rows:
        return 0
    with _write_lock:
        _get_conn().executemany(
            "INSERT OR REPLACE INTO north_flow_daily VALUES (?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


def read_north_flow_for_date(trade_date: str) -> dict | None:
    """Tushare stores moneyflow_hsgt values in 万元 (yuan/10000) despite docs
    saying 百万. Convert to 亿 for consistency with the rest of the codebase."""
    td = trade_date[:10].replace("-", "")
    row = _get_conn().execute(
        "SELECT * FROM north_flow_daily WHERE trade_date <= ? "
        "ORDER BY trade_date DESC LIMIT 1", (td,),
    ).fetchone()
    if not row:
        return None
    return {
        "date": f"{row['trade_date'][:4]}-{row['trade_date'][4:6]}-{row['trade_date'][6:8]}",
        "north_money_yi": round((row["north_money"] or 0) / 10000, 2),
        "hgt_yi": round((row["hgt"] or 0) / 10000, 2),
        "sgt_yi": round((row["sgt"] or 0) / 10000, 2),
    }


# ── margin ──

def save_margin_daily(df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0
    rows = []
    for _, r in df.iterrows():
        td = _s(r, "trade_date").strip()
        ex = _s(r, "exchange_id").strip()
        if not td or not ex:
            continue
        rows.append((
            td, ex,
            _f(r, "rzye"), _f(r, "rzmre"), _f(r, "rzche"),
            _f(r, "rqye"), _f(r, "rqmcl"),
            _f(r, "rzrqye"), _f(r, "rqyl"),
        ))
    if not rows:
        return 0
    with _write_lock:
        _get_conn().executemany(
            "INSERT OR REPLACE INTO margin_daily VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


# ── kpl_list (涨停原因) ──

def save_kpl_limit_list_daily(df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0
    rows = []
    for _, r in df.iterrows():
        ts_code = _s(r, "ts_code").strip()
        if not ts_code:
            continue
        rows.append((
            _s(r, "trade_date"), ts_code, _bare_code(ts_code),
            _s(r, "name"), _s(r, "lu_time"),
            _s(r, "lu_desc"), _s(r, "theme"),
            _s(r, "status"), _s(r, "tag"),
            _f(r, "amount"), _f(r, "turnover_rate"),
            _f(r, "net_change"),
            _f(r, "limit_order"), _f(r, "lu_limit_order"),
        ))
    if not rows:
        return 0
    with _write_lock:
        _get_conn().executemany(
            "INSERT OR REPLACE INTO kpl_limit_list_daily VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


def read_kpl_limit_list_for_date(trade_date: str) -> list[dict]:
    """Return limit-up stocks for a date, each with its concept reason (lu_desc).

    If trade_date falls on non-trading day, resolves to latest trading date <=.
    """
    td = trade_date[:10].replace("-", "")
    conn = _get_conn()
    latest = conn.execute(
        "SELECT MAX(trade_date) AS t FROM kpl_limit_list_daily "
        "WHERE trade_date <= ?",
        (td,),
    ).fetchone()
    if not latest or not latest["t"]:
        return []
    rows = conn.execute(
        "SELECT code, name, lu_time, lu_desc, theme, status, tag, "
        "       amount, turnover_rate, net_change "
        "FROM kpl_limit_list_daily WHERE trade_date = ? "
        "ORDER BY lu_time ASC",
        (latest["t"],),
    ).fetchall()
    return [dict(r) for r in rows]


def read_limit_reason_for_stock(code: str, trade_date: str) -> dict | None:
    """Get the limit-up reason (lu_desc + theme) for a specific stock on a date.

    Used by intraday_monitor to display the true concept reason for limit-up
    stocks (replaces akshare's static 所属行业 field).
    """
    td = trade_date[:10].replace("-", "")
    row = _get_conn().execute(
        "SELECT lu_desc, theme, status, lu_time "
        "FROM kpl_limit_list_daily "
        "WHERE code = ? AND trade_date <= ? "
        "ORDER BY trade_date DESC LIMIT 1",
        (code, td),
    ).fetchone()
    return dict(row) if row else None


# ── limit_cpt_list (当日涨停最集中的题材) ──

def save_kpl_concept_daily(df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0
    rows = []
    for _, r in df.iterrows():
        ts_code = _s(r, "ts_code").strip()
        name = _s(r, "name").strip()
        if not ts_code or not name:
            continue

        def _int(k):
            try:
                v = r.get(k)
                return int(v) if v is not None and v == v else None
            except Exception:
                return None

        rows.append((
            _s(r, "trade_date"), ts_code, name,
            _int("days"), _s(r, "up_stat"), _s(r, "cons_nums"),
            _int("up_nums"), _f(r, "pct_chg"), _int("rank"),
        ))
    if not rows:
        return 0
    with _write_lock:
        _get_conn().executemany(
            "INSERT OR REPLACE INTO kpl_concept_daily VALUES (?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


def read_top_concepts_for_date(trade_date: str, top_n: int = 10) -> list[dict]:
    """Return the most active (by limit-up concentration) concepts on a date.

    This is THE authoritative "today's hot themes" list — Kaipanla/同花顺 style.
    """
    td = trade_date[:10].replace("-", "")
    conn = _get_conn()
    latest = conn.execute(
        "SELECT MAX(trade_date) AS t FROM kpl_concept_daily WHERE trade_date <= ?",
        (td,),
    ).fetchone()
    if not latest or not latest["t"]:
        return []
    rows = conn.execute(
        "SELECT name, up_stat, cons_nums, up_nums, pct_chg, rank "
        "FROM kpl_concept_daily WHERE trade_date = ? "
        "ORDER BY rank ASC LIMIT ?",
        (latest["t"], top_n),
    ).fetchall()
    return [dict(r) for r in rows]


def read_margin_for_date(trade_date: str) -> dict | None:
    td = trade_date[:10].replace("-", "")
    rows = _get_conn().execute(
        "SELECT exchange_id, rzye, rzmre, rqye, rzrqye FROM margin_daily "
        "WHERE trade_date <= (SELECT MAX(trade_date) FROM margin_daily "
        "                     WHERE trade_date <= ?) "
        "  AND trade_date = (SELECT MAX(trade_date) FROM margin_daily "
        "                    WHERE trade_date <= ?)",
        (td, td),
    ).fetchall()
    if not rows:
        return None
    total_rzye = sum(r["rzye"] or 0 for r in rows)
    total_rzmre = sum(r["rzmre"] or 0 for r in rows)
    total_rzrqye = sum(r["rzrqye"] or 0 for r in rows)
    return {
        "total_rzye_yi": round(total_rzye / 1e8, 2),
        "total_rzmre_yi": round(total_rzmre / 1e8, 2),
        "total_rzrqye_yi": round(total_rzrqye / 1e8, 2),
        "by_exchange": [{"exchange": r["exchange_id"],
                         "rzye_yi": round((r["rzye"] or 0) / 1e8, 2),
                         "rzmre_yi": round((r["rzmre"] or 0) / 1e8, 2)}
                        for r in rows],
    }
