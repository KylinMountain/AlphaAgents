"""Historical Observation adapter for the continuous Trader Runtime.

Walk-forward owns the historical corpus and execution simulator. This adapter
only turns the point-in-time facts it already selected into Trader Observations,
advances the exact same TraderState used live, and commits the shared
TraderDecision vocabulary before any replay order is sent to the intent door.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from alpha_agents.data import trader_state_store
from alpha_agents.evolution import trader_learning
from alpha_agents.trader import (
    DecisionContext, DecisionHorizon, EvidenceScope, Observation,
    ObservationType, Session, Timeframe, TraderDecision, TraderRuntime,
    TraderRuntimeError, TraderState,
)

_TZ = ZoneInfo("Asia/Shanghai")


def context(day: str, phase: str) -> DecisionContext:
    if phase == "open":
        stamp = datetime.fromisoformat(day + " 09:00:00").replace(tzinfo=_TZ)
        session = Session.OPEN
    elif phase == "close":
        # Deliberately synthetic, matching walk_forward's existing close
        # contract: the daily bar is treated as visible at the 14:55 decision.
        stamp = datetime.fromisoformat(day + " 14:55:00").replace(tzinfo=_TZ)
        session = Session.CLOSE
    else:
        raise TraderRuntimeError(f"unsupported replay phase {phase!r}")
    return DecisionContext(
        mode="replay",
        observation_resolution=Timeframe.DAILY,
        decision_horizon=DecisionHorizon.SWING,
        session=session,
        information_cutoff=stamp,
        evidence_scope=EvidenceScope.REPLAY_DAILY,
    )


def _candidate_observations(
    panel: list[dict], *, day: str, phase: str, ctx: DecisionContext,
) -> list[Observation]:
    out = []
    for row in panel:
        code = str(row.get("code") or "").strip()
        if not code:
            continue
        theme = str(
            row.get("primary_theme") or row.get("_theme") or "").strip()
        subjects = [code] + ([theme] if theme and theme != code else [])
        data = {
            key: row.get(key)
            for key in (
                "name", "close", "change_pct", "adv20", "turnover_rate",
                "consecutive_limits", "net_amount", "primary_theme",
                "supporting_themes", "concepts", "membership_snapshot_id",
                "membership_hash", "primary_theme_relation_evidence_id",
                "supporting_theme_relation_evidence_ids",
            )
            if row.get(key) is not None
        }
        out.append(Observation.create(
            observed_at=ctx.information_cutoff,
            available_at=ctx.information_cutoff,
            type=ObservationType.CANDIDATE,
            subjects=subjects,
            data=data,
            source="walk_forward_panel",
            evidence_refs=[f"walk:{day}:{phase}:{code}"],
            timeframe=Timeframe.DAILY,
        ))
    return out


def _price_observations(
    state: TraderState, panel: list[dict], marks: dict[str, dict],
    *, day: str, phase: str, ctx: DecisionContext,
) -> list[Observation]:
    codes = {str(row.get("code") or "") for row in panel if row.get("code")}
    codes.update(
        item.code for item in state.watchlist
        if item.status.value in {"watching", "triggered"})
    out = []
    for code in sorted(codes):
        mark = marks.get(code) or {}
        price = mark.get("price")
        if price is None:
            continue
        data = {"price": float(price)}
        if mark.get("change_pct") is not None:
            data["change_pct"] = float(mark["change_pct"])
        out.append(Observation.create(
            observed_at=ctx.information_cutoff,
            available_at=ctx.information_cutoff,
            type=ObservationType.PRICE_MOVE,
            subjects=[code],
            data=data,
            source="walk_forward_mark",
            evidence_refs=[f"walk-mark:{day}:{phase}:{code}"],
            timeframe=Timeframe.DAILY,
        ))
    return out


def _market_observation(
    market: dict | None, *, day: str, phase: str, ctx: DecisionContext,
) -> Observation | None:
    if not market:
        return None
    return Observation.create(
        observed_at=ctx.information_cutoff,
        available_at=ctx.information_cutoff,
        type=ObservationType.EVENT,
        subjects=["MARKET"],
        data=dict(market),
        source="walk_forward_market",
        evidence_refs=[f"walk-market:{day}:{phase}"],
        timeframe=Timeframe.DAILY,
    )


async def prepare(
    *, run_id: str, trader_id: str, day: str, phase: str,
    panel: list[dict], market: dict | None = None,
    marks: dict[str, dict] | None = None,
) -> tuple[TraderState, DecisionContext, tuple[str, ...]]:
    """Persist replay observations before the provider is invoked."""
    dctx = context(day, phase)
    state = trader_state_store.load_latest(
        run_id=run_id, trader_id=trader_id)
    if state is None:
        state = TraderState.create(
            trader_id=trader_id, as_of=dctx.information_cutoff)
        trader_state_store.save(state, run_id=run_id)
    if state.as_of > dctx.information_cutoff:
        raise TraderRuntimeError(
            "replay TraderState is ahead of the decision cutoff")

    observations = _candidate_observations(
        panel, day=day, phase=phase, ctx=dctx)
    observations.extend(_price_observations(
        state, panel, marks or {}, day=day, phase=phase, ctx=dctx))
    market_obs = _market_observation(
        market, day=day, phase=phase, ctx=dctx)
    if market_obs is not None:
        observations.append(market_obs)

    result = await TraderRuntime().step(state, observations, dctx)
    if result.changed:
        trader_state_store.save(result.state, run_id=run_id)
    return result.state, dctx, result.reevaluate_subjects


async def commit(
    state: TraderState,
    decisions: tuple[TraderDecision, ...] | list[TraderDecision],
    *,
    run_id: str,
    context: DecisionContext,
) -> TraderState:
    """Seal the replay decision before existing execution simulates a fill."""
    result = await TraderRuntime().commit_decisions(
        state, tuple(decisions), context)
    if result.changed:
        trader_state_store.save(result.state, run_id=run_id)
    return result.state


def learning_block(
    *, run_id: str, trader_id: str, day: str,
    decision_horizon: str = "3-5d",
) -> str:
    """Only experience already materialized by this replay run."""
    return trader_learning.inject(
        trader_id=trader_id,
        decision_horizon=decision_horizon,
        as_of=day,
        run_id=run_id,
    )
