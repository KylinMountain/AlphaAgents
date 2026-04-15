"""Sector beta stock selector — find the best stocks within a concept sector.

Combines pre-computed beta (weekly) with realtime data to produce a
multi-factor score: beta 40% + position 25% + institutional 20% + liquidity 15%.
"""

import json
import logging

from alpha_agents.data.beta_calculator import get_cached_betas, calculate_concept_betas, save_concept_betas
from alpha_agents.data.market_data import get_realtime_quotes

logger = logging.getLogger(__name__)

# Factor weights
W_BETA = 0.40
W_POSITION = 0.25
W_INSTITUTIONAL = 0.20
W_LIQUIDITY = 0.15


def _score_position(change_pct: float) -> float:
    """Score based on intraday change — prefer stocks with moderate gains (2-6%).

    V2 design: "从板块内选涨幅中段（2-6%）、还没涨停的标的"
    Logic: stocks actively being bought (rising) but not yet overextended.
    Negative change = market is buying the sector but not this stock = weak, avoid.
    """
    if change_pct >= 9.8:
        return 0    # Limit up, can't buy
    if change_pct >= 6:
        return 30   # Rising fast, getting expensive
    if change_pct >= 3:
        return 100  # Sweet spot: actively rising, confirmed by market
    if change_pct >= 1:
        return 80   # Starting to move, good entry
    if change_pct >= 0:
        return 50   # Flat while sector rises — lukewarm
    if change_pct >= -2:
        return 20   # Falling while sector rises — weak, market doesn't want it
    return 0        # Falling hard — something wrong, avoid


def _score_liquidity(avg_daily_amount: float) -> float:
    """Score based on average daily trading amount."""
    yi = avg_daily_amount / 1e8  # Convert to 亿
    if yi < 0.5:
        return 0    # Too illiquid, skip
    if yi < 1:
        return 40
    if yi < 5:
        return 70
    return 100


def _score_institutional(code: str, north_data: dict, lhb_data: dict) -> float:
    """Score based on institutional recognition signals."""
    score = 0

    # North flow
    if code in north_data:
        nd = north_data[code]
        if nd.get("pct", 0) > 1:
            score += 30
        if nd.get("change", "") == "增持":
            score += 20

    # LHB
    if code in lhb_data:
        ld = lhb_data[code]
        if ld.get("is_institutional") and ld.get("net_buy", 0) > 0:
            score += 30

    return min(score, 100)


def _normalize_beta_scores(betas: list[dict]) -> dict[str, float]:
    """Normalize beta_weighted to 0-100 scale across the sector."""
    if not betas:
        return {}
    values = [b["beta_weighted"] for b in betas if b.get("beta_weighted")]
    if not values:
        return {}
    max_b = max(values)
    min_b = min(values)
    range_b = max_b - min_b if max_b > min_b else 1
    return {
        b["code"]: round(((b.get("beta_weighted") or 0) - min_b) / range_b * 100, 1)
        for b in betas
    }


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
    except Exception:
        pass

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
        logger.info("No cached betas for '%s', computing on-the-fly...", concept_name)
        try:
            computed = calculate_concept_betas(concept_name)
            if computed:
                save_concept_betas(concept_name, computed)
                betas = get_cached_betas(concept_name)
        except Exception as e:
            logger.warning("On-the-fly beta calculation failed: %s", e)

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

    # 4. Normalize beta scores
    beta_scores = _normalize_beta_scores(betas)

    # 5. Multi-factor scoring
    scored = []
    for b in betas:
        code = b["code"]
        name = b.get("name", "")

        # Get realtime data
        real = rt.get(code, {})
        price = real.get("price", 0)
        change_pct = real.get("change_pct", 0)

        # Skip limit-up stocks (can't buy)
        if change_pct >= 9.8:
            continue

        # Skip illiquid stocks
        avg_amount = b.get("avg_daily_amount", 0)
        if avg_amount < 5e7:  # < 5000万
            continue

        # Score each factor
        s_beta = beta_scores.get(code, 50)
        s_position = _score_position(change_pct)
        s_institutional = _score_institutional(code, north_data, lhb_data)
        s_liquidity = _score_liquidity(avg_amount)

        total = round(
            s_beta * W_BETA +
            s_position * W_POSITION +
            s_institutional * W_INSTITUTIONAL +
            s_liquidity * W_LIQUIDITY,
            1,
        )

        # Build note
        notes = []
        if s_beta >= 70:
            notes.append("高beta")
        if s_position >= 80:
            notes.append("低涨幅")
        if s_institutional >= 50:
            notes.append("机构认可")
        if s_liquidity >= 70:
            notes.append("流动性好")

        inst_signals = []
        if code in north_data and north_data[code].get("change") == "增持":
            inst_signals.append("北向增持")
        if code in lhb_data and lhb_data[code].get("is_institutional"):
            inst_signals.append("龙虎榜机构买入")

        scored.append({
            "code": code,
            "name": name,
            "score": total,
            "beta_weighted": b.get("beta_weighted", 0),
            "today_change_pct": change_pct,
            "price": price,
            "institutional": "+".join(inst_signals) if inst_signals else "无",
            "avg_amount_yi": round(avg_amount / 1e8, 2),
            "note": "+".join(notes) if notes else "",
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
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
