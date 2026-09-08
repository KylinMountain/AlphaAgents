"""What the agent learns fastest: how wrong its own confidence is.

The project already computes Brier scores and reports them in the review.
But the probability being scored was derived *by code* from how many
cross-validation dimensions passed — the agent never stated one and never
saw the result. So the number measured the cross-validator's calibration
and could not, even in principle, change the agent's behaviour.

Now the agent states ``prob`` on every thesis. That makes calibration the
highest-sample-efficiency signal in the system, for a reason worth stating
plainly: **every thesis contributes, not only the profitable ones.** P&L
learning needs hundreds of closed trades before the noise clears. A
calibration curve is readable at twenty, because "you said 70% eleven
times and were right five" is a fact about the eleven, not about the
market.

The blind-spot rate is the other half. A thesis that failed the way it
predicted is a reasoning success with a bad outcome, and reinforcing
against it would teach the agent to stop taking risk. A thesis that failed
with nothing it listed having fired is the only failure that says the
*thinking* was incomplete.
"""

from __future__ import annotations

import logging

from alpha_agents.data.thesis import (
    BLIND_SPOT, EXPIRED, INVALIDATED, VALIDATED, get_closed,
)

logger = logging.getLogger(__name__)

# Wide buckets. Narrow ones look precise and hold two samples each; the
# question being answered is "do you systematically overstate", which
# three buckets answer and ten do not.
_BUCKETS = ((0.0, 0.55, "≤55%"), (0.55, 0.70, "55–70%"), (0.70, 1.01, ">70%"))

# Below this a bucket's hit rate is noise. Reporting "you said 70% and hit
# 0%" off one sample would teach the agent to distrust a number that was
# fine.
MIN_BUCKET_N = 3


def _bucket(prob: float) -> str | None:
    for lo, hi, label in _BUCKETS:
        if lo <= prob < hi:
            return label
    return None


def calibration(days: int = 60) -> list[dict]:
    """Stated probability against realised validation rate, by bucket."""
    rows: dict[str, list[bool]] = {label: [] for _, _, label in _BUCKETS}
    for th in get_closed(days=days):
        if th.status not in (VALIDATED, INVALIDATED, EXPIRED, BLIND_SPOT):
            continue
        label = _bucket(th.prob or 0.5)
        if label:
            rows[label].append(th.status == VALIDATED)

    out = []
    for _, _, label in _BUCKETS:
        hits = rows[label]
        if len(hits) < MIN_BUCKET_N:
            continue
        out.append({"bucket": label, "n": len(hits),
                    "hit_rate": sum(hits) / len(hits)})
    return out


def blind_spot_rate(days: int = 60) -> dict:
    """How often it lost money for a reason it never wrote down."""
    closed = [t for t in get_closed(days=days) if t.status != VALIDATED]
    if not closed:
        return {"n": 0}
    blind = [t for t in closed if t.status == BLIND_SPOT]
    no_conditions = [t for t in get_closed(days=days) if not t.conditions]
    return {
        "n": len(closed),
        "blind": len(blind),
        "rate": len(blind) / len(closed),
        "unguarded": len(no_conditions),
        "examples": [f"{t.code} {t.name} — {t.claim[:40]}" for t in blind[:3]],
    }


def condition_usefulness(days: int = 60) -> list[dict]:
    """Which invalidation kinds actually fire, and which never do.

    A condition that never fires across dozens of theses is not caution —
    it is a threshold set where the price never goes, and it makes the
    thesis look guarded while guarding nothing.
    """
    written: dict[str, int] = {}
    fired: dict[str, int] = {}
    for th in get_closed(days=days):
        for c in th.conditions:
            written[c.kind] = written.get(c.kind, 0) + 1
        if th.close_kind:
            fired[th.close_kind] = fired.get(th.close_kind, 0) + 1

    return sorted(
        ({"kind": k, "written": n, "fired": fired.get(k, 0)}
         for k, n in written.items()),
        key=lambda r: -r["written"])


def inject_calibration(days: int = 60) -> str:
    """The block that goes back into the agent's own prompt.

    This is the whole point of the module: the agent has to *see* its
    curve, in its own terms, or stating a probability is just paperwork.
    """
    sections = []

    curve = calibration(days=days)
    if curve:
        lines = ["【你的概率校准】(近60天已结论点)"]
        for row in curve:
            gap = row["hit_rate"] * 100
            lines.append(f"• 你说 {row['bucket']} 的 {row['n']} 条，"
                         f"实际兑现 {gap:.0f}%")
        worst = max(curve, key=lambda r: abs(r["hit_rate"] - 0.62))
        if worst["hit_rate"] < 0.45:
            lines.append("→ 你在系统性高估自己。同样的证据，把概率往下调。")
        elif worst["hit_rate"] > 0.80:
            lines.append("→ 你在低估自己。信心足的时候可以给更高的概率和仓位。")
        sections.append("\n".join(lines))

    blind = blind_spot_rate(days=days)
    if blind.get("n"):
        lines = [f"【盲点率】{blind['blind']}/{blind['n']} 笔亏损是"
                 f"「没有任何你列出的条件触发就被风控平掉」({blind['rate']*100:.0f}%)"]
        for ex in blind["examples"]:
            lines.append(f"• {ex}")
        if blind.get("unguarded"):
            lines.append(f"• 另有 {blind['unguarded']} 条论点从头就没写失效条件")
        lines.append("→ 这些是你没想到的失效路径，写 invalidations 时想想它们。")
        sections.append("\n".join(lines))

    useless = [r for r in condition_usefulness(days=days)
               if r["written"] >= 5 and r["fired"] == 0]
    if useless:
        kinds = "、".join(r["kind"] for r in useless[:3])
        sections.append(f"【从未触发的条件】{kinds} —— 写了很多次但一次没响，"
                        f"阈值可能设在了价格根本不会去的地方，等于没设防。")

    return "\n\n".join(sections)
