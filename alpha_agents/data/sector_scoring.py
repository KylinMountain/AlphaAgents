"""The one definition of how a concept's member stocks are ranked.

Why this module exists
----------------------
Until 2026-09-21 there were two implementations of "which stocks inside this
concept are best", and they disagreed:

* the live intraday trader ranked by a four-factor score - beta 0.40,
  position 0.25, institutional 0.20, liquidity 0.15
  (``tools/sector_beta``);
* the replay runner ranked by ``(change_rank + turnover_rank) / 2``
  (``data/sector_panel._within_sector``).

So a replay measured a different decision from the one the trader makes, and
any evidence it produced was about the replay's rule rather than the trader's.
This module is the single implementation. It is **pure**: every input is an
argument, and it reads no database and no clock. The live path supplies
realtime quotes and today's LHB; the replay supplies the same fields as of the
session it is replaying. Both then run the identical arithmetic, which is what
makes a replay a statement about the live strategy.

What is deliberately *not* here: fetching. Where a number comes from is the
caller's problem, and it has to be, because live and replay get it from
different places (a quote API versus ``daily_kline``). Keeping the fetch out
is what lets one function serve both without a branch on "am I replaying".
"""

from __future__ import annotations

from alpha_agents.config import is_tradable

#: The four factor weights. Named once, here, because they are the definition
#: of the ranking and two literals that must agree is how they come to
#: disagree.
FACTOR_WEIGHTS = {
    "beta": 0.40,
    "position": 0.25,
    "institutional": 0.20,
    "liquidity": 0.15,
}

#: Below this average daily amount a name is too illiquid to bother with.
MIN_AVG_AMOUNT = 5e7

#: At or above this the name is at the daily limit and cannot be bought.
LIMIT_UP_PCT = 9.8


def score_position(change_pct: float) -> float:
    """Score an intraday change - prefer moderate gains, not yet overextended.

    A negative change while the sector rises means the market is buying the
    sector but not this name.
    """
    if change_pct >= LIMIT_UP_PCT:
        return 0.0     # limit up, cannot buy
    if change_pct >= 6:
        return 30.0    # rising fast, getting expensive
    if change_pct >= 3:
        return 100.0   # sweet spot: rising, confirmed by the market
    if change_pct >= 1:
        return 80.0    # starting to move, good entry
    if change_pct >= 0:
        return 50.0    # flat while the sector rises
    if change_pct >= -2:
        return 20.0    # falling while the sector rises - weak
    return 0.0


def score_liquidity(avg_daily_amount: float) -> float:
    """Score average daily traded amount, in yuan."""
    yi = (avg_daily_amount or 0.0) / 1e8
    if yi < 0.5:
        return 0.0
    if yi < 1:
        return 40.0
    if yi < 5:
        return 70.0
    return 100.0


def score_institutional(code: str, *, north: dict | None = None,
                        lhb: dict | None = None) -> float:
    """Score institutional recognition from the sources that still exist.

    ``north`` is kept for shape but is empty in practice: the HK Exchange
    stopped publishing per-stock northbound holdings on 2024-08-19, so the
    live path passes nothing and this contributes zero on both sides. It is
    not deleted because the field it scores may return, and a silent
    resurrection would be worse than a named empty input.
    """
    score = 0.0
    record = (north or {}).get(code)
    if record:
        if (record.get("pct") or 0) > 1:
            score += 30.0
        if record.get("change") == "增持":
            score += 20.0
    entry = (lhb or {}).get(code)
    if entry:
        if entry.get("is_institutional") and (entry.get("net_buy") or 0) > 0:
            score += 30.0
    return min(score, 100.0)


def normalize_beta_scores(betas: list[dict]) -> dict[str, float]:
    """Map ``beta_weighted`` onto 0-100 across the sector.

    Normalised *within the sector*, not across the market: the question is
    which member of this concept has the most sector sensitivity, and a
    market-wide scale would rank every member of a quiet concept low.
    """
    values = [b["beta_weighted"] for b in betas if b.get("beta_weighted")]
    if not values:
        return {}
    max_b, min_b = max(values), min(values)
    span = max_b - min_b if max_b > min_b else 1.0
    return {
        b["code"]: round(
            ((b.get("beta_weighted") or 0.0) - min_b) / span * 100, 1)
        for b in betas
    }


def is_eligible(code: str, change_pct: float, avg_daily_amount: float) -> bool:
    """Whether a concept member may be ranked at all.

    Shared for the same reason the score is: a filter that lives on one side
    only is a difference between the replay and the trader that nobody wrote
    down.
    """
    if not is_tradable(code):
        return False
    if (change_pct or 0.0) >= LIMIT_UP_PCT:
        return False
    if (avg_daily_amount or 0.0) < MIN_AVG_AMOUNT:
        return False
    return True


def score_members(members: list[dict], *, north: dict | None = None,
                  lhb: dict | None = None,
                  weights: dict | None = None) -> list[dict]:
    """Rank one concept's members, best first.

    ``members`` rows carry the resolved inputs::

        {code, name, beta_weighted, change_pct, avg_daily_amount}

    Nothing else is read. The caller resolves those five fields from whatever
    world it has - a live quote API or a replayed corpus - and the ranking
    that follows is the same arithmetic on both.

    Rows failing :func:`is_eligible` are dropped, not scored zero: an
    ineligible name is not a bad candidate, it is not a candidate, and
    averaging it in would move the sector's score distribution.
    """
    weight = {**FACTOR_WEIGHTS, **(weights or {})}
    kept = [
        row for row in members
        if is_eligible(
            str(row.get("code") or ""),
            row.get("change_pct") or 0.0,
            row.get("avg_daily_amount") or 0.0)
    ]
    beta_scores = normalize_beta_scores(kept)
    out = []
    for row in kept:
        code = str(row["code"])
        inst = score_institutional(code, north=north, lhb=lhb)
        pos = score_position(row.get("change_pct") or 0.0)
        liq = score_liquidity(row.get("avg_daily_amount") or 0.0)
        total = (
            beta_scores.get(code, 50.0) * weight["beta"]
            + pos * weight["position"]
            + inst * weight["institutional"]
            + liq * weight["liquidity"]
        )
        notes = []
        if beta_scores.get(code, 0) >= 70:
            notes.append("高beta")
        if pos >= 80:
            notes.append("低涨幅")
        if inst >= 50:
            notes.append("机构认可")
        if liq >= 70:
            notes.append("流动性好")
        out.append({
            **row,
            "code": code,
            "score": round(total, 1),
            "note": "+".join(notes),
        })
    out.sort(key=lambda item: (-item["score"], item["code"]))
    for rank, item in enumerate(out, start=1):
        item["rank"] = rank
    return out
