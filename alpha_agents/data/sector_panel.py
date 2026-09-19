"""Deterministic stock panel construction inside selected sectors."""

from __future__ import annotations


def _rank(rows: list[dict], key: str) -> dict[str, int]:
    ordered = sorted(
        rows,
        key=lambda row: (
            -float(row.get(key) or 0.0),
            str(row["code"]),
        ),
    )
    return {str(row["code"]): index + 1 for index, row in enumerate(ordered)}


def _within_sector(rows: list[dict]) -> list[str]:
    if not rows:
        return []
    by_change = _rank(rows, "change_pct")
    by_turnover = _rank(rows, "turnover_rate")
    return [
        str(row["code"])
        for row in sorted(
            rows,
            key=lambda row: (
                (by_change[str(row["code"])] +
                 by_turnover[str(row["code"])]) / 2.0,
                by_change[str(row["code"])],
                str(row["code"]),
            ),
        )
    ]


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
                    if value in selected_set
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
