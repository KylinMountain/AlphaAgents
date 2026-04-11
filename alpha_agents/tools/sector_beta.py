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
    """Score based on intraday change — prefer stocks that haven't run yet."""
    if change_pct >= 9.8:
        return 0    # Limit up, can't buy
    if change_pct >= 6:
        return 20
    if change_pct >= 3:
        return 50
    if change_pct >= 0:
        return 80
    return 100  # Negative = hasn't started, best entry


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


def get_sector_best_stocks_fn(concept_name: str, top_n: int = 10) -> str:
    """Get top stocks in a concept sector by multi-factor score.

    Uses cached beta (weekly) + realtime price + institutional data.

    Args:
        concept_name: Concept sector name, e.g. "电池", "芯片概念"
        top_n: Number of top stocks to return
    """
    # 1. Get cached betas (or compute on-the-fly if missing)
    betas = get_cached_betas(concept_name)
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

    # 3. Get institutional data (from daily archive or live)
    north_data = {}
    lhb_data = {}
    try:
        from alpha_agents.tools.fund_flow import get_north_flow_fn, get_lhb_detail_fn
        import json as _j

        # North flow
        north_raw = _j.loads(get_north_flow_fn("today"))
        for item in north_raw.get("data", []):
            north_data[item["code"]] = {
                "pct": item.get("pct_of_float", 0),
                "change": "增持" if item.get("change_value_wan", 0) > 0 else "减持",
            }

        # LHB
        lhb_raw = _j.loads(get_lhb_detail_fn())
        for item in lhb_raw.get("data", []):
            lhb_data[item["code"]] = {
                "is_institutional": item.get("is_institutional", False),
                "net_buy": item.get("net_buy", 0),
            }
    except Exception as e:
        logger.debug("Institutional data fetch failed: %s", e)

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
