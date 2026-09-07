"""Principle utility, derived from market outcomes only.

The Echo Gap: an agent asked to judge its own memories accepts its own
*wrong* ones 31% of the time (Claude Haiku 4.5), 41% (GPT-5.4), 54%
(GPT-5.4-mini). On BIRD, self-scored memory gave back roughly 2.9 of the
points memory was supposed to add. The formal result is that correcting
this needs error-independence between the writer and the scorer, and
swapping in a *stronger judge model does not satisfy it* — residual error
correlation stays ≥ +0.30. Only retrieval/execution-grounded verification
does (+0.05).

So nothing here asks a model whether a principle is any good. A principle
cites evidence — (code, date) pairs — and those predictions were graded
against price data by data/scoring.py. The utility of the principle is
the utility of what it cited. See docs/self_improvement_roadmap.md G3.

Scores decay with age because the market is non-stationary: a principle
that worked in a 2024 regime should not keep its standing forever on
those cases alone.
"""

import json
import logging
import math
from datetime import datetime

from alpha_agents.data.memory_store import _get_conn, _write_lock

logger = logging.getLogger(__name__)

# Evidence older than this contributes little. 90 trading days ≈ a
# quarter, long enough to span a regime but not so long that a stale
# principle coasts.
HALF_LIFE_DAYS = 90

# A principle needs this many graded cases before its win rate is taken
# seriously. Below it, win_rate is recorded but never used to retire.
MIN_CASES_TO_JUDGE = 5

# Retire below this. 0.5 is the base rate of beating the market, so this
# is "meaningfully worse than a coin flip", not "not great".
RETIRE_WIN_RATE = 0.40

# Brier above this is worse than always shrugging (0.25).
RETIRE_BRIER = 0.30


def _decay_weight(case_date: str, as_of: datetime) -> float:
    """Exponential decay by age. 1.0 today, 0.5 at one half-life."""
    try:
        d = datetime.strptime(case_date[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return 0.0
    age = (as_of - d).days
    if age < 0:
        return 0.0
    return math.pow(0.5, age / HALF_LIFE_DAYS)


def score_principle(principle: dict, as_of: datetime | None = None) -> dict:
    """Utility of one principle, from the graded predictions it cites.

    Returns counts, a decay-weighted win rate and mean Brier, and whether
    the principle should be retired. Never consults an LLM.
    """
    as_of = as_of or datetime.now()

    try:
        evidence = json.loads(principle.get("evidence") or "[]")
    except (json.JSONDecodeError, TypeError):
        evidence = []

    pairs = [
        (e.get("code"), e.get("date"))
        for e in evidence
        if isinstance(e, dict) and e.get("code") and e.get("date")
    ]
    if not pairs:
        return {
            "principle_id": principle.get("id"),
            "cases": 0, "graded": 0,
            "win_rate": None, "brier": None,
            "should_retire": False,
            "reason": "无可核验证据",
        }

    conn = _get_conn()
    weighted_wins = weighted_total = 0.0
    briers: list[tuple[float, float]] = []
    graded = 0

    for code, date in pairs:
        row = conn.execute(
            "SELECT hit, brier FROM predictions "
            "WHERE code = ? AND date = ? AND scored_at IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (code, date),
        ).fetchone()
        if not row:
            continue
        graded += 1
        w = _decay_weight(date, as_of)
        if w <= 0:
            continue
        weighted_total += w
        if row["hit"]:
            weighted_wins += w
        if row["brier"] is not None:
            briers.append((w, row["brier"]))

    win_rate = (weighted_wins / weighted_total) if weighted_total else None
    brier = (sum(w * b for w, b in briers) / sum(w for w, _ in briers)
             if briers else None)

    should_retire, reason = False, ""
    if graded >= MIN_CASES_TO_JUDGE:
        if win_rate is not None and win_rate < RETIRE_WIN_RATE:
            should_retire = True
            reason = f"胜率{win_rate*100:.0f}% < {RETIRE_WIN_RATE*100:.0f}% (n={graded})"
        elif brier is not None and brier > RETIRE_BRIER:
            should_retire = True
            reason = f"Brier {brier:.3f} > {RETIRE_BRIER} (n={graded})"

    return {
        "principle_id": principle.get("id"),
        "cases": len(pairs),
        "graded": graded,
        "win_rate": round(win_rate, 4) if win_rate is not None else None,
        "brier": round(brier, 4) if brier is not None else None,
        "should_retire": should_retire,
        "reason": reason,
    }


def rescore_all_principles(as_of: datetime | None = None) -> dict:
    """Re-derive every principle's win rate from graded predictions.

    Fills trading_principles.win_rate, which the LLM path left NULL
    forever, and retires principles the market has contradicted. Returns
    counts for the review report.
    """
    from alpha_agents.data.memory_store import (
        get_all_principles_including_weakened, set_principle_status,
    )

    as_of = as_of or datetime.now()
    principles = get_all_principles_including_weakened()
    scored = retired = 0

    for p in principles:
        result = score_principle(p, as_of)
        if result["win_rate"] is None and result["brier"] is None:
            continue

        with _write_lock:
            conn = _get_conn()
            conn.execute(
                "UPDATE trading_principles SET win_rate = ? WHERE id = ?",
                (result["win_rate"], p["id"]),
            )
            conn.commit()
        scored += 1

        if result["should_retire"] and p.get("status") == "active":
            set_principle_status(p["id"], "weakened")
            retired += 1
            logger.info("Retired principle #%s on evidence: %s",
                        p["id"], result["reason"])

    return {"scored": scored, "retired": retired,
            "total": len(principles)}


def format_principle_health(as_of: datetime | None = None) -> str:
    """A short block for the review report — evidence-backed only."""
    from alpha_agents.data.memory_store import get_all_principles_including_weakened

    as_of = as_of or datetime.now()
    rows = [score_principle(p, as_of)
            for p in get_all_principles_including_weakened()]
    graded = [r for r in rows if r["graded"] >= MIN_CASES_TO_JUDGE]
    if not graded:
        ungraded = sum(1 for r in rows if r["graded"] < MIN_CASES_TO_JUDGE)
        if not rows:
            return ""
        return (f"【经验健康度】{len(rows)} 条原则，{ungraded} 条证据不足"
                f"(<{MIN_CASES_TO_JUDGE} 条已评分案例)，暂不判定")

    wins = [r["win_rate"] for r in graded if r["win_rate"] is not None]
    lines = [f"【经验健康度】(市场核验，非LLM判断)",
             f"• {len(graded)}/{len(rows)} 条原则有足够证据"]
    if wins:
        lines.append(f"• 胜率区间 {min(wins)*100:.0f}%~{max(wins)*100:.0f}%")
    weak = [r for r in graded if r["should_retire"]]
    if weak:
        lines.append(f"• {len(weak)} 条被证据否定，已退休")
    return "\n".join(lines)
