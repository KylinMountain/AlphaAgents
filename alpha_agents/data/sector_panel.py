"""Deterministic stock panel construction inside selected sectors.

**Ordering comes from ``data/sector_scoring``, the same function the live
trader calls.** Until 2026-09-21 this module ranked within a sector by
``(change_rank + turnover_rank) / 2`` while the live path ranked by a
four-factor score (beta/position/institutional/liquidity). Two formulas meant
a replay measured a decision the trader never makes, so its evidence was about
this module rather than about the strategy.
"""

from __future__ import annotations

from alpha_agents.data import sector_scoring


def _within_sector(rows: list[dict]) -> list[str]:
    """Members of one sector, best first, by the shared live scoring rule.

    ``rows`` must already carry the four resolved inputs
(``beta_weighted``, ``change_pct``, ``avg_daily_amount``, and ``code``);
    the caller resolves them from the replayed corpus. Anything missing is
    treated as its zero value by the shared scorer rather than guessed at.
    """
    if not rows:
        return []
    ranked = sector_scoring.score_members(rows)
    return [str(row["code"]) for row in ranked]


def materialize(*, candidates: dict[str, dict],
                selected_sectors: list[str],
                members: dict[str, tuple[str, ...]],
                limit: int) -> list[dict]:
    """Round-robin selected sectors, then dedupe stocks globally.

    The first selected sector that offers a stock owns primary_theme. All
    selected sectors containing the stock are retained as supporting themes.
    """
    if limit <= 0 or not selected_sectors:
        return []

    selected = list(dict.fromkeys(
        str(value).strip() for value in selected_sectors
        if str(value).strip()))
    selected_set = set(selected)
    support = {
        code: [
            sector for sector in selected
            if code in set(members.get(sector, ()))
        ]
        for code in candidates
    }

    lanes = {}
    for sector in selected:
        rows = [
            candidates[code]
            for code in members.get(sector, ())
            if code in candidates
        ]
        lanes[sector] = _within_sector(rows)

    cursors = {sector: 0 for sector in selected}
    out = []
    seen = set()

    while len(out) < limit:
        progressed = False
        for sector in selected:
            lane = lanes[sector]
            while cursors[sector] < len(lane):
                code = lane[cursors[sector]]
                cursors[sector] += 1
                if code in seen:
                    continue
                seen.add(code)
                themes = [
                    value for value in support.get(code, [])
                    if value in selected_set and value != sector
                ]
                out.append({
                    "code": code,
                    "primary_theme": sector,
                    "supporting_themes": themes,
                })
                progressed = True
                break
            if len(out) >= limit:
                break
        if not progressed:
            break
    return out
