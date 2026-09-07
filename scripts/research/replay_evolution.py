"""Walk-forward replay of the evolution pipeline from a historical date.

For each trading day in [start, end]:
  1. Set replay_as_of(today) — downstream fetchers read historical DB
  2. Generate candidates from historical industry/concept fund flow snapshot
     (top 3 sectors) + each sector's top stocks from market_history (low chg_pct,
     high liquidity)
  3. VPA gate each candidate with as_of=today (uses daily K-lines up to today)
  4. Save predictions with features_json
  5. Verify yesterday's predictions against today's daily close
  6. Run review_agent LLM with constructed context (predictions/themes/stats)
  7. post_review: extract_daily_lessons + consolidate_principles +
     update_playbook_stats + scan_and_auto_create + compute_evolution_metrics

Usage:
    # Dry run 10 days
    uv run python scripts/replay_evolution.py --start 2026-04-01 --end 2026-04-10

    # Full 75-day walk-forward
    uv run python scripts/replay_evolution.py --start 2026-01-02 --end 2026-04-16
"""

from __future__ import annotations

# Load .env BEFORE any alpha_agents.config import so API keys land in env.
from dotenv import load_dotenv
load_dotenv()

import argparse
import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

from alpha_agents.evolution.replay_mode import replay_as_of
from alpha_agents.data.memory_store import (
    _get_conn, save_prediction, update_prediction_result,
    get_active_themes, upsert_theme, save_theme_snapshot,
    get_pending_predictions,
)
from alpha_agents.tools.vpa import compute_vpa_with_llm
from alpha_agents.agents.review_agent import run_review_analysis
from alpha_agents.evolution import post_review

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S")
logger = logging.getLogger("replay")


def _trading_days(start: str, end: str) -> list[str]:
    """Filter to days that actually have K-lines in market_history.db."""
    from alpha_agents.data.market_history import _get_conn as _mh_conn
    rows = _mh_conn().execute(
        "SELECT DISTINCT date FROM daily_kline WHERE date >= ? AND date <= ? ORDER BY date",
        (start, end),
    ).fetchall()
    return [r["date"] for r in rows]


def _top_sectors(as_of: str, snapshot_key: str, n: int = 5) -> list[dict]:
    """Read latest sector ranking on or before as_of, return top-N by main_net inflow."""
    row = _get_conn().execute(
        "SELECT data FROM daily_snapshots WHERE data_type = ? AND date <= ? "
        "ORDER BY date DESC LIMIT 1",
        (snapshot_key, as_of),
    ).fetchone()
    if not row:
        return []
    data = json.loads(row["data"])
    sectors = data.get("sectors", [])
    # Filter to only those with positive inflow (gainers)
    gainers = [s for s in sectors if s.get("main_net", 0) > 0][:n]
    return gainers


_THS_CONCEPTS_CACHE: list[str] | None = None


def _all_ths_concepts() -> list[str]:
    """Cached list of all concept names in stocks.db."""
    global _THS_CONCEPTS_CACHE
    if _THS_CONCEPTS_CACHE is None:
        import sqlite3
        conn = sqlite3.connect("data/stocks.db")
        rows = conn.execute("SELECT name FROM concepts").fetchall()
        conn.close()
        _THS_CONCEPTS_CACHE = [r[0] for r in rows]
    return _THS_CONCEPTS_CACHE


def _find_ths_concept(em_name: str) -> str | None:
    """Fuzzy match an Eastmoney concept name to a THS concept in stocks.db.

    Strips common suffixes like '概念' and tries substring matching both ways.
    Examples:
        'CPO概念' → '共封装光学(CPO)' (CPO in both)
        '半导体概念' → '半导体' (exact after strip)
        '光通信模块' → '光通信' or '光模块' if present
    """
    def _strip(s: str) -> str:
        return (s.replace("概念", "").replace("(", "")
                 .replace(")", "").replace(" ", "").strip())

    em_base = _strip(em_name)
    if not em_base:
        return None

    best_match = None
    best_score = 0
    for ths in _all_ths_concepts():
        ths_base = _strip(ths)
        if not ths_base:
            continue
        # Exact match after strip
        if em_base == ths_base:
            return ths
        # Substring overlap (longer match wins)
        if em_base in ths_base or ths_base in em_base:
            score = min(len(em_base), len(ths_base))
            if score > best_score:
                best_score = score
                best_match = ths
    return best_match


def _liquid_pool(as_of: str, limit: int = 12) -> list[dict]:
    """Fallback: pick liquid stocks from market_history when sector matching fails."""
    from alpha_agents.data.market_history import _get_conn as _mh_conn
    rows = _mh_conn().execute(
        "SELECT code, AVG(close * volume) as avg_dollar_vol, "
        "       AVG(change_pct) as avg_chg "
        "FROM daily_kline "
        "WHERE date > date(?, '-5 days') AND date <= ? "
        "GROUP BY code "
        "HAVING avg_dollar_vol > 1e8 AND avg_chg BETWEEN -3 AND 8 "
        "ORDER BY avg_dollar_vol DESC LIMIT ?",
        (as_of, as_of, limit),
    ).fetchall()
    return [{"code": r["code"], "name": "", "avg_chg": r["avg_chg"]}
            for r in rows]


def _sector_stocks(sector_name: str, as_of: str, limit: int = 10) -> list[dict]:
    """Pick candidate stocks from a sector's member list (from stocks.db
    concept_stocks table), filtered by liquidity + moderate recent change
    over 5 trading days ending at ``as_of``.
    """
    if not sector_name:
        return []
    # Get concept members from stocks.db
    import sqlite3
    conn = sqlite3.connect("data/stocks.db")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT s.code, s.name FROM concepts c "
        "JOIN concept_stocks cs ON c.id = cs.concept_id "
        "JOIN stocks s ON cs.stock_code = s.code "
        "WHERE c.name = ?",
        (sector_name,),
    ).fetchall()
    conn.close()
    member_codes = [r["code"] for r in rows]
    if not member_codes:
        return []
    name_map = {r["code"]: r["name"] for r in rows}

    # Filter by liquidity + recent change using market_history
    from alpha_agents.data.market_history import _get_conn as _mh_conn
    placeholders = ",".join("?" * len(member_codes))
    hist_rows = _mh_conn().execute(
        f"SELECT code, AVG(close * volume) as avg_dollar_vol, "
        f"       AVG(change_pct) as avg_chg "
        f"FROM daily_kline "
        f"WHERE code IN ({placeholders}) "
        f"  AND date > date(?, '-5 days') AND date <= ? "
        f"GROUP BY code "
        f"HAVING avg_dollar_vol > 5e6 AND avg_chg BETWEEN -3 AND 8 "
        f"ORDER BY avg_dollar_vol DESC LIMIT ?",
        [*member_codes, as_of, as_of, limit],
    ).fetchall()
    return [{"code": r["code"], "name": name_map.get(r["code"], ""),
             "avg_chg": r["avg_chg"], "sector": sector_name}
            for r in hist_rows]


def _get_stock_name(code: str) -> str:
    """Best-effort stock name lookup (may return empty)."""
    from alpha_agents.data.market_history import _get_conn as _mh_conn
    # market_history.db has no name column — try stocks.db
    try:
        import sqlite3
        conn = sqlite3.connect("data/stocks.db")
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT name FROM stocks WHERE code = ? LIMIT 1", (code,)
        ).fetchone()
        conn.close()
        return row["name"] if row else ""
    except Exception:
        return ""


def _verify_previous_day(yesterday: str, today: str) -> int:
    """Using yesterday's predictions + today's close, compute hit/next_day_return.

    Returns count of predictions verified.
    """
    preds = get_pending_predictions(yesterday)
    if not preds:
        return 0

    from alpha_agents.data.market_history import _get_conn as _mh_conn
    verified = 0
    for pred in preds:
        code = pred.get("code")
        entry_price = pred.get("entry_price")
        if not code or not entry_price:
            continue

        # Next day close
        row = _mh_conn().execute(
            "SELECT close FROM daily_kline WHERE code = ? AND date = ?",
            (code, today),
        ).fetchone()
        if not row:
            continue
        today_close = row["close"]
        return_pct = round((today_close - entry_price) / entry_price * 100, 2)
        direction = pred.get("direction", "")
        is_bullish = direction in ("看多", "bullish", "买入", "long")
        hit_val = (1 if return_pct > 0 else 0) if is_bullish else (1 if return_pct < 0 else 0)
        update_prediction_result(pred["id"], next_day_return=return_pct, hit=hit_val)
        verified += 1

        # Phase 3 playbook trade recording
        try:
            from alpha_agents.evolution.playbook import match_playbook
            from alpha_agents.data.memory_store import record_playbook_trade
            features = json.loads(pred.get("features_json") or "{}")
            if features:
                pb = match_playbook(features)
                if pb:
                    record_playbook_trade(pb["id"], hit=bool(hit_val), return_pct=return_pct)
        except Exception as e:
            logger.debug("Playbook trade recording failed: %s", e)

    return verified


async def _replay_one_day(target_date: str, next_date: Optional[str]) -> dict:
    """Run one day of the replay. Returns stats dict."""
    logger.info("=== Replay %s ===", target_date)
    stats = {"date": target_date, "candidates": 0, "actionable": 0,
             "verified_prev": 0}

    # 1. Select top sectors from historical snapshot
    industries = _top_sectors(target_date, "industry_fund_flow_hist", n=3)
    concepts = _top_sectors(target_date, "concept_fund_flow_hist", n=3)
    theme_names = [s["name"] for s in industries + concepts]
    logger.info("  Top sectors: %s", theme_names)

    # 2. Build candidate stock list
    # Strategy: for each top sector try to match a concept in stocks.db (fuzzy
    # keyword match since Eastmoney and THS use different names). Fall back to
    # liquid-stock pool if no match.
    seen_codes = set()
    candidates = []
    theme_for_code: dict[str, str] = {}

    for s in industries + concepts:
        sec_name = s["name"]
        matched = _find_ths_concept(sec_name)
        if not matched:
            continue
        members = _sector_stocks(matched, target_date, limit=5)
        for m in members:
            if m["code"] in seen_codes:
                continue
            seen_codes.add(m["code"])
            m["sector"] = sec_name  # keep original em name as theme tag
            candidates.append(m)
            theme_for_code[m["code"]] = sec_name

    # Fallback: if no candidates matched, use liquid-stock pool
    if not candidates:
        liquid = _liquid_pool(target_date, limit=12)
        for m in liquid:
            m["sector"] = theme_names[0] if theme_names else ""
            candidates.append(m)

    candidates = candidates[:12]
    stats["candidates"] = len(candidates)
    logger.info("  Candidates: %d (matched %d sectors)",
                len(candidates), len({c.get("sector") for c in candidates}))

    # 3. VPA gate each candidate — SERIAL (py_mini_racer/V8 used by akshare THS
    #    code paths is not thread-safe; any parallelism crashes). 1 day ≈ 12 min.
    actionable = []
    for c in candidates:
        code = c["code"]
        name = c.get("name") or _get_stock_name(code)
        try:
            r = await asyncio.to_thread(compute_vpa_with_llm, code, name, 60, 20, target_date)
        except Exception as e:
            logger.debug("VPA failed for %s: %s", code, e)
            continue
        if not r.get("ok"):
            continue
        raw_v = r.get("llm_verdict", "中性")
        if raw_v in ("看空", "偏空"):
            continue
        actionable.append({
            "code": code, "name": name,
            "vpa_verdict": "bullish" if raw_v in ("看多", "偏多") else "neutral",
            "vpa_phase": r.get("llm_phase", ""),
            "score": 60,
            "theme": c.get("sector", theme_names[0] if theme_names else ""),
            "institutional": "",
            "change_pct": c.get("avg_chg", 0),
            "vpa_result": r,
        })
    stats["actionable"] = len(actionable)

    # Close price as entry baseline
    from alpha_agents.data.market_history import _get_conn as _mh_conn
    for a in actionable:
        row = _mh_conn().execute(
            "SELECT close FROM daily_kline WHERE code = ? AND date = ?",
            (a["code"], target_date),
        ).fetchone()
        a["entry_price"] = float(row["close"]) if row else None

    # 4. Save predictions with features_json
    saved_pred_ids = []
    for a in actionable:
        if not a["entry_price"]:
            continue
        features = {
            "vpa_verdict": a["vpa_verdict"],
            "vpa_phase": a["vpa_phase"],
            "theme": a["theme"],
            "score": a["score"],
            "change_pct": a["change_pct"],
            "institutional": a["institutional"],
            "rec_type": "actionable",
        }
        pid = save_prediction(
            date=target_date, report_type="intraday", code=a["code"],
            name=a["name"], direction="bullish", confidence="medium",
            theme_line=a["theme"], entry_price=a["entry_price"],
            reason=f"replay VPA gate: {a['vpa_phase']}",
            features=features,
        )
        saved_pred_ids.append(pid)
    logger.info("  Saved %d predictions", len(saved_pred_ids))

    # 5. Verify yesterday's predictions (using today's close as "next day")
    # Find the previous trading day
    prev_row = _mh_conn().execute(
        "SELECT DISTINCT date FROM daily_kline WHERE date < ? ORDER BY date DESC LIMIT 1",
        (target_date,),
    ).fetchone()
    if prev_row:
        prev_date = prev_row["date"]
        verified = _verify_previous_day(prev_date, target_date)
        stats["verified_prev"] = verified
        logger.info("  Verified %d predictions from %s", verified, prev_date)

    # 6. Build minimal review context + run review_agent
    #    (We skip morning_scan output + portfolio; just use current predictions + themes + stats)
    themes_ctx = "本日模拟主线（from 历史资金流 snapshot）:\n" + "\n".join(
        f"- {s['name']}（主力净流入 {s.get('main_net', 0) / 1e8:.1f}亿）"
        for s in industries + concepts
    )
    pred_summary = "\n".join(
        f"- {a['code']} {a['name']} VPA={a['vpa_verdict']}/{a['vpa_phase']}"
        for a in actionable[:8]
    ) or "（今日无通过 VPA gate 的候选）"
    pred_ctx = f"本日 {len(actionable)} 个 actionable / {len(candidates)} 个候选\n{pred_summary}"
    stats_ctx = f"近期回测累计状态: {stats}"

    try:
        report = await run_review_analysis(pred_ctx, themes_ctx, stats_ctx)
    except Exception as e:
        logger.warning("review_agent failed: %s", e)
        report = ""

    # 7. post_review: extract lessons + consolidate + playbook + metrics
    if report:
        evolution_report = await post_review(target_date, report)
        if evolution_report:
            logger.info("  %s", evolution_report.replace("\n", " | "))

    return stats


async def run_replay(start: str, end: str) -> None:
    days = _trading_days(start, end)
    logger.info("Replay plan: %d trading days (%s → %s)", len(days), days[0] if days else "?", days[-1] if days else "?")

    all_stats = []
    for i, d in enumerate(days):
        next_d = days[i + 1] if i + 1 < len(days) else None
        with replay_as_of(d):
            try:
                s = await _replay_one_day(d, next_d)
                all_stats.append(s)
            except Exception as e:
                logger.exception("Day %s failed: %s", d, e)

    # Final summary
    logger.info("=== Replay complete ===")
    total_cand = sum(s["candidates"] for s in all_stats)
    total_act = sum(s["actionable"] for s in all_stats)
    logger.info("Totals: %d candidates, %d actionable across %d days",
                total_cand, total_act, len(all_stats))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2026-04-01", help="YYYY-MM-DD")
    parser.add_argument("--end", default="2026-04-10", help="YYYY-MM-DD")
    args = parser.parse_args()
    asyncio.run(run_replay(args.start, args.end))


if __name__ == "__main__":
    main()
