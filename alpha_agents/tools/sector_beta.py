"""Sector beta stock selector — find the best stocks within a concept sector.

Combines pre-computed beta (weekly) with realtime data to produce a
multi-factor score: beta 40% + position 25% + institutional 20% + liquidity 15%.
"""

import json
import logging

from alpha_agents.data import sector_scoring
from alpha_agents.data.beta_calculator import get_cached_betas, calculate_concept_betas, save_concept_betas
from alpha_agents.data.market_data import get_realtime_quotes

logger = logging.getLogger(__name__)

# The four factor weights and the score functions live in
# ``data/sector_scoring``, which the replay runner calls too. They were
# duplicated here until 2026-09-21, and the replay's copy ranked by a
# different formula entirely, so its evidence described its own rule rather
# than this one. Re-exported for callers that imported them from here.
W_BETA = sector_scoring.FACTOR_WEIGHTS["beta"]
W_POSITION = sector_scoring.FACTOR_WEIGHTS["position"]
W_INSTITUTIONAL = sector_scoring.FACTOR_WEIGHTS["institutional"]
W_LIQUIDITY = sector_scoring.FACTOR_WEIGHTS["liquidity"]
_score_position = sector_scoring.score_position
_score_liquidity = sector_scoring.score_liquidity
_normalize_beta_scores = sector_scoring.normalize_beta_scores


def _score_institutional(code: str, north_data: dict, lhb_data: dict) -> float:
    """Kept as a positional-argument shim over the shared implementation."""
    return sector_scoring.score_institutional(
        code, north=north_data, lhb=lhb_data)


def _fuzzy_match_concept(name: str) -> str | None:
    """Fuzzy match a concept name against cached beta concepts and stocks.db concepts.

    E.g., "电池" → "锂电池概念", "芯片" → "芯片概念"
    """
    from alpha_agents.data.memory_store import _get_conn

    # First try beta cache
    conn = _get_conn()
    rows = conn.execute("SELECT DISTINCT concept FROM sector_betas").fetchall()
    cached_concepts = [r["concept"] for r in rows]

    # Exact substring match in cached concepts
    for c in cached_concepts:
        if name in c or c in name:
            return c

    # Try stocks.db concepts table
    try:
        from alpha_agents.data.db import get_connection
        from alpha_agents.config import DB_PATH
        sconn = get_connection(DB_PATH)
        rows2 = sconn.execute("SELECT name FROM concepts WHERE name LIKE ?", (f"%{name}%",)).fetchall()
        sconn.close()
        if rows2:
            # Prefer concepts that also have beta data
            for r in rows2:
                if r["name"] in cached_concepts:
                    return r["name"]
            # Otherwise return first match
            return rows2[0]["name"]
    except Exception as e:
        logger.debug("sector_beta: concept fuzzy match failed for %r: %s", name, e)

    return None


def get_sector_best_stocks_fn(concept_name: str, top_n: int = 10) -> str:
    """Get top stocks in a concept sector by multi-factor score.

    Uses cached beta (weekly) + realtime price + institutional data.

    Args:
        concept_name: Concept sector name, e.g. "电池", "芯片概念"
        top_n: Number of top stocks to return
    """
    # 1. Get cached betas — try exact match first, then fuzzy
    betas = get_cached_betas(concept_name)
    if not betas:
        # Fuzzy match: "电池" → "锂电池概念", "芯片" → "芯片概念"
        matched_name = _fuzzy_match_concept(concept_name)
        if matched_name and matched_name != concept_name:
            logger.info("Fuzzy matched '%s' → '%s'", concept_name, matched_name)
            betas = get_cached_betas(matched_name)
            concept_name = matched_name  # Use matched name for rest of function

    if not betas:
        logger.info("No cached betas for '%s', computing on-the-fly (timeout 90s)...", concept_name)
        # calculate_concept_betas iterates up to 50 stocks × 3 time windows of
        # baostock calls under a global _bs_lock. A single hung baostock socket
        # locks the whole call indefinitely (the chat-process hang on 化肥 was
        # caused by exactly this path). Bound it with a hard timeout.
        import concurrent.futures as _cf
        ex = _cf.ThreadPoolExecutor(max_workers=1)
        try:
            fut = ex.submit(calculate_concept_betas, concept_name)
            try:
                computed = fut.result(timeout=90)
            except _cf.TimeoutError:
                logger.warning("On-the-fly beta calc for '%s' timed out after 90s "
                               "(baostock likely wedged); skipping", concept_name)
                computed = None
            except Exception as e:
                logger.warning("On-the-fly beta calculation failed: %s", e)
                computed = None
            if computed:
                save_concept_betas(concept_name, computed)
                betas = get_cached_betas(concept_name)
        finally:
            # Don't wait for the leaked worker — its baostock socket op will
            # eventually error out at OS level.
            ex.shutdown(wait=False)

    if not betas:
        return json.dumps({"concept": concept_name, "error": "无该板块的beta数据", "top": []}, ensure_ascii=False)

    # 2. Get realtime prices
    codes = [b["code"] for b in betas]
    rt = get_realtime_quotes(codes) or {}

    # 3. Get institutional data — per-stock northbound is no longer published
    # by HK Exchange since 2024-08-19, so north_data stays empty. LHB still works.
    north_data: dict = {}
    lhb_data = {}
    try:
        from alpha_agents.tools.fund_flow import get_lhb_detail_fn
        import json as _j

        # LHB (still working)
        lhb_raw = _j.loads(get_lhb_detail_fn())
        for item in lhb_raw.get("data", []):
            lhb_data[item["code"]] = {
                "is_institutional": item.get("is_institutional", False),
                "net_buy": item.get("net_buy", 0),
            }
    except Exception as e:
        logger.debug("LHB data fetch failed: %s", e)

    # 4-5. Multi-factor scoring — the shared definition, not a local copy.
    #
    # This loop used to be written out here, and the replay had a *different*
    # one (``sector_panel._within_sector`` averaged change and turnover
    # ranks). Two implementations meant a replay measured a different
    # decision from the one this function makes. The arithmetic now lives in
    # ``data/sector_scoring``; only the fetching stays here, because live and
    # replay get their inputs from different places.
    members = []
    for b in betas:
        code = b["code"]
        real = rt.get(code, {})
        members.append({
            "code": code,
            "name": b.get("name", ""),
            "beta_weighted": b.get("beta_weighted", 0),
            "change_pct": real.get("change_pct", 0),
            "avg_daily_amount": b.get("avg_daily_amount", 0),
            "price": real.get("price", 0),
        })

    ranked = sector_scoring.score_members(
        members, north=north_data, lhb=lhb_data)

    scored = []
    for row in ranked:
        code = row["code"]
        inst_signals = []
        if north_data.get(code, {}).get("change") == "增持":
            inst_signals.append("北向增持")
        if lhb_data.get(code, {}).get("is_institutional"):
            inst_signals.append("龙虎榜机构买入")
        scored.append({
            "code": code,
            "name": row.get("name", ""),
            "score": row["score"],
            "beta_weighted": row.get("beta_weighted", 0),
            "today_change_pct": row.get("change_pct", 0),
            "price": row.get("price", 0),
            "institutional": "+".join(inst_signals) if inst_signals else "无",
            "avg_amount_yi": round(
                (row.get("avg_daily_amount") or 0) / 1e8, 2),
            "note": row.get("note", ""),
        })

    top = scored[:top_n]

    # Add rank
    for i, item in enumerate(top):
        item["rank"] = i + 1

    return json.dumps({
        "concept": concept_name,
        "total_in_sector": len(betas),
        "scored": len(scored),
        "top": top,
    }, ensure_ascii=False)
