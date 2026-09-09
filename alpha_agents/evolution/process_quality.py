"""Grading the decision, not the outcome — on the day it was made.

A trade's result takes days to arrive and is mostly market noise. The
*quality of the decision* is visible immediately and does not need the
market's cooperation at all: whether the exit conditions cover more than
one way of being wrong, whether any of them is a threshold set where
price will never go, whether the reason cites a number or is a phrase
that would fit any stock on any day.

That distinction is the whole reason this file exists. Outcome learning
in this domain needs hundreds of closed trades and the regime changes
first; process learning iterates daily. Good process does not guarantee
good results, but it is the only lever that can be pulled at this sample
size — and the difference is already visible in the record. Two morning
scans a few hours apart produced:

    "低涨幅+流动性好"
    "机构评分−8强烈看空，不在任何活跃主线上，融资融券显示去杠杆"

Both were accepted. Nothing measured the gap.
"""

from __future__ import annotations

import logging
import re

from alpha_agents.data import thesis as T

logger = logging.getLogger(__name__)

# Which family a condition belongs to. A thesis guarded by three price
# levels has one way of being wrong checked three times; one guarded by a
# price, a theme and a clock has three. Coverage across families is what
# separates a plan from a stop-loss wearing a plan's clothes.
_FAMILIES = {
    "price_below": "价格", "price_above": "价格",
    "drawdown_from_peak": "价格", "loss_exceeds": "价格",
    "theme_strength_below": "主线", "theme_daily_score_below": "主线",
    "theme_flow_negative": "主线", "theme_rank_worse_than": "主线",
    "breadth_below": "大盘",
    "no_progress_by_day": "时间",
    "narrative": "叙事",
}

# A reason that contains no digit is asserting rather than citing. The
# test is deliberately shallow: it catches "技术面走弱" without pretending
# to judge whether the number quoted was the right one.
_HAS_NUMBER = re.compile(r"\d")

# Phrases that survive any market and describe any stock. Their presence
# is not itself a failure; their presence *without* a number is.
_EMPTY_PHRASES = (
    "技术面", "走势良好", "值得关注", "基本面良好", "趋势向好",
    "有望上涨", "风险可控", "机会较大",
)

MIN_CLAIM_LENGTH = 12


def grade_thesis(th: T.Thesis) -> dict:
    """Score one thesis on what was knowable when it was written.

    Returns per-check booleans plus a 0-4 total. Not a verdict on the
    idea — a thesis can be well-formed and wrong, which is exactly the
    case this system wants to distinguish from being badly formed.
    """
    families = {_FAMILIES.get(c.kind, "其他") for c in th.conditions}
    claim = (th.claim or "").strip()

    checks = {
        # One condition is a stop-loss. Two from different families is a
        # plan: it says the position can fail in more than one way.
        "multi_family": len(families) >= 2,
        # A claim short enough to fit any stock cannot be falsified later,
        # which makes grading it impossible.
        "specific_claim": (len(claim) >= MIN_CLAIM_LENGTH
                           and not _is_empty_talk(claim)),
        # Stating a probability is what makes the calibration curve
        # possible. The default 0.5 means it declined to.
        "stated_probability": bool(th.prob) and abs(th.prob - 0.5) > 1e-9,
        # Sizing is a decision; taking the default is not making it.
        "stated_size": bool(th.size_pct),
    }
    return {"thesis_id": th.id, "code": th.code, "families": sorted(families),
            "score": sum(checks.values()), "max": len(checks), **checks}


def _is_empty_talk(text: str) -> bool:
    """A phrase that would fit any stock, with nothing to check it against."""
    if _HAS_NUMBER.search(text):
        return False
    return any(p in text for p in _EMPTY_PHRASES) or len(text) < MIN_CLAIM_LENGTH


def grade_reason(reason: str) -> dict:
    """Score an exit or sizing reason, which is graded the same way.

    The reason on a close is what the review reads to decide whether the
    agent was thinking or narrating. "主线今日分转−1，主力净流出28.34亿"
    can be checked against the record; "技术面走弱" cannot be checked
    against anything.
    """
    reason = (reason or "").strip()
    return {
        "cites_number": bool(_HAS_NUMBER.search(reason)),
        "empty_talk": _is_empty_talk(reason),
        "length": len(reason),
    }


def summarise(days: int = 30, trader_id: str | None = None) -> dict:
    """Process quality across recent theses, live and closed."""
    theses = (T.get_active(trader_id=trader_id)
              + T.get_closed(days=days, trader_id=trader_id))
    if not theses:
        return {"n": 0}

    grades = [grade_thesis(t) for t in theses]
    n = len(grades)
    by_check = {
        k: round(sum(g[k] for g in grades) / n * 100, 1)
        for k in ("multi_family", "specific_claim",
                  "stated_probability", "stated_size")
    }
    # Which risks it habitually forgets to guard against. A family that
    # never appears is not caution, it is a blind spot waiting to happen —
    # and unlike the blind_spot statistic, this one is visible before the
    # money is lost.
    seen: dict[str, int] = {}
    for g in grades:
        for f in g["families"]:
            seen[f] = seen.get(f, 0) + 1
    missing = [f for f in ("价格", "主线", "时间", "大盘")
               if seen.get(f, 0) < n * 0.15]

    return {
        "n": n,
        "avg_score": round(sum(g["score"] for g in grades) / n, 2),
        "max_score": grades[0]["max"],
        "by_check": by_check,
        "family_counts": seen,
        "never_guarded": missing,
    }


def inject_process_quality(days: int = 30,
                           trader_id: str | None = None) -> str:
    """The process report, for the review and the morning prompt.

    Phrased as instructions rather than statistics because it is read by
    the agent that will write the next thesis, not by an analyst.
    """
    s = summarise(days=days, trader_id=trader_id)
    if not s.get("n"):
        return ""

    lines = [f"【决策质量】近{days}天 {s['n']} 条论点，"
             f"平均 {s['avg_score']:.1f}/{s['max_score']} 分"]

    c = s["by_check"]
    if c["multi_family"] < 60:
        lines.append(f"• 只有 {c['multi_family']:.0f}% 的论点覆盖了两类以上失效路径。"
                     "三条价格条件不是三重保险，是同一种错法查了三遍——"
                     "价格、主线、时间各一条才是计划。")
    if c["specific_claim"] < 70:
        lines.append(f"• {100 - c['specific_claim']:.0f}% 的 claim 没有可验证的对象。"
                     "「技术面走弱」这类说法放在任何一只票任何一天都成立，"
                     "事后无法判断当时想对了没有。")
    if c["stated_probability"] < 70:
        lines.append(f"• {100 - c['stated_probability']:.0f}% 没有自报概率。"
                     "不报概率就没有校准曲线，也就永远不知道自己有多不准。")
    if c["stated_size"] < 70:
        lines.append(f"• {100 - c['stated_size']:.0f}% 没有自己定仓位，用了默认值。"
                     "仓位是决策，不是配置项。")

    if s["never_guarded"]:
        lines.append(f"• 几乎从不设防的失效路径：{'、'.join(s['never_guarded'])}。"
                     "没想到的失效方式，最后都会变成盲点。")

    return "\n".join(lines)
