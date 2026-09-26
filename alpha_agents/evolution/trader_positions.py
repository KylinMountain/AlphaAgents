"""Adapt one portfolio snapshot into Trader Runtime position facts."""
from __future__ import annotations

from datetime import datetime

from alpha_agents.trader import (
    Observation, ObservationType, Timeframe, TraderState,
)


def position_observations(
    state: TraderState, positions: list[dict], cutoff: datetime,
    timeframe: Timeframe,
) -> list[Observation]:
    """Emit only booked position changes, including explicit close tombstones."""
    before = {item.code: item for item in state.positions}
    now = {str(pos.get("code") or ""): pos for pos in positions
           if pos.get("code")}
    observations: list[Observation] = []

    for code, pos in sorted(now.items()):
        shares = int(pos.get("shares") or 0)
        avg_price = float(
            pos.get("avg_price") or pos.get("open_price") or 0)
        thesis_id = pos.get("thesis_id")
        previous = before.get(code)
        if (previous is not None and previous.shares == shares
                and abs(previous.avg_price - avg_price) < 1e-12
                and previous.thesis_id == (
                    str(thesis_id) if thesis_id is not None else None)):
            continue
        observations.append(Observation.create(
            observed_at=cutoff,
            available_at=cutoff,
            type=ObservationType.POSITION_CHANGED,
            subjects=[code],
            data={
                "code": code,
                "shares": shares,
                "avg_price": avg_price,
                "thesis_id": thesis_id,
            },
            source="portfolio_book",
            evidence_refs=[
                f"position:{pos.get('id', code)}:{shares}:{avg_price}"
            ],
            timeframe=timeframe,
        ))

    for code, previous in sorted(before.items()):
        if code in now:
            continue
        observations.append(Observation.create(
            observed_at=cutoff,
            available_at=cutoff,
            type=ObservationType.POSITION_CHANGED,
            subjects=[code],
            data={
                "code": code,
                "shares": 0,
                "avg_price": previous.avg_price,
                "thesis_id": previous.thesis_id,
            },
            source="portfolio_book",
            evidence_refs=[f"position-closed:{code}:{cutoff.isoformat()}"],
            timeframe=timeframe,
        ))
    return observations
