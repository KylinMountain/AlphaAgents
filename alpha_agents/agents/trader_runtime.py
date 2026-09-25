"""Production adapter from morning research into the continuous Trader Runtime.

The morning analyst discovers and explains candidates. It does not own the
trade. This module restores one trader's cognitive state, records the research
as Candidate observations, asks the shared T1 planner for BUY/WAIT/REJECT/HOLD,
commits those decisions into TraderState, then sends only BUY decisions through
the existing portfolio intent door.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo

from alpha_agents.agents import t1_decider
from alpha_agents.data import (
    market_history as mh,
    trader_session,
    trader_state_store,
)
from alpha_agents.data.portfolio_intent import create_pending_order
from alpha_agents.evolution import feedback
from alpha_agents.trader import (
    DecisionContext, DecisionHorizon, EvidenceScope, Observation,
    ObservationType, Session, Timeframe, TraderRuntime, TraderState,
)

logger = logging.getLogger(__name__)
_TZ = ZoneInfo("Asia/Shanghai")


class MorningTraderError(RuntimeError):
    """The final trader decision could not be made safely."""


def _logical_now() -> datetime:
    """Replay-aware exchange-local decision time as an aware datetime."""
    raw = trader_session.instant()
    value = datetime.fromisoformat(raw)
    if value.tzinfo is None:
        value = value.replace(tzinfo=_TZ)
    else:
        value = value.astimezone(_TZ)
    return value.replace(microsecond=0)


def _context(cutoff: datetime) -> DecisionContext:
    from alpha_agents.evolution.replay_mode import replay_process
    replay = replay_process()
    return DecisionContext(
        mode="replay" if replay else "live",
        observation_resolution=Timeframe.DAILY,
        decision_horizon=DecisionHorizon.SWING,
        session=Session.PRE_OPEN,
        information_cutoff=cutoff,
        evidence_scope=(
            EvidenceScope.REPLAY_DAILY if replay
            else EvidenceScope.LIVE_DAILY),
    )


def _previous_session(day: str) -> str | None:
    cut = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    return mh.get_latest_trading_day_at_or_before(cut)


def _panel(recommendations: list[dict], prev_day: str) -> list[dict]:
    """Turn research candidates into the T1 planner's allowed universe."""
    out: list[dict] = []
    seen: set[str] = set()
    for rec in recommendations:
        code = str(rec.get("code") or "").strip()
        if not code or code in seen:
            continue
        history = mh.get_local_history(code, days=20, as_of=prev_day)
        if not history:
            logger.info("%s omitted from Trader panel: no prior history", code)
            continue
        last = history[-1]
        close = float(last.get("close") or 0)
        volumes = [float(row.get("volume") or 0) for row in history]
        adv20 = sum(volumes) / len(volumes) if volumes else 0
        if close <= 0 or adv20 <= 0:
            continue
        theme = str(rec.get("theme") or "").strip()
        out.append({
            "code": code,
            "name": str(rec.get("name") or ""),
            "close": close,
            "change_pct": round(float(last.get("change_pct") or 0), 2),
            "adv20": adv20,
            "turnover_rate": (
                round(float(last["turnover_rate"]), 2)
                if last.get("turnover_rate") is not None else None),
            "consecutive_limits": None,
            "net_amount": None,
            "concepts": [theme] if theme else [],
            "_theme": theme,
        })
        seen.add(code)
    return out


def _candidate_observations(
    recommendations: list[dict],
    prediction_ids: dict[str, int],
    cutoff: datetime,
) -> list[Observation]:
    fields = (
        "name", "theme", "reason", "confidence", "dims_passed",
        "entry_low", "entry_high", "stop_loss", "horizon_days",
    )
    out = []
    for rec in recommendations:
        code = str(rec.get("code") or "").strip()
        if not code:
            continue
        theme = str(rec.get("theme") or "").strip()
        subjects = [code] + ([theme] if theme and theme != code else [])
        pred_id = prediction_ids.get(code)
        out.append(Observation.create(
            observed_at=cutoff,
            available_at=cutoff,
            type=ObservationType.CANDIDATE,
            subjects=subjects,
            data={
                key: rec.get(key)
                for key in fields
                if rec.get(key) is not None
            },
            source="morning_research",
            evidence_refs=(
                [f"prediction:{pred_id}"] if pred_id is not None
                else [f"morning-candidate:{code}:{cutoff.isoformat()}"]),
            timeframe=Timeframe.DAILY,
        ))
    return out


def _portfolio_context(trader_id: str, prev_day: str) -> str:
    try:
        from alpha_agents.data.portfolio import get_open_positions
        price_map: dict[str, float] = {}
        for pos in get_open_positions(trader_id):
            code = pos.get("code")
            if not code:
                continue
            rows = mh.get_local_history(code, days=1, as_of=prev_day)
            if rows and rows[-1].get("close"):
                price_map[code] = float(rows[-1]["close"])
        return feedback.inject_portfolio(
            trader_id=trader_id, price_map=price_map)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Trader portfolio context unavailable: %s", exc)
        return ""


def _research_packet(recommendations: list[dict], events_context: str) -> dict:
    return {
        "source": "morning_research",
        "events_context": events_context[:6000],
        "candidates": [
            {
                key: rec.get(key)
                for key in (
                    "code", "name", "theme", "reason", "confidence",
                    "dims_passed", "entry_low", "entry_high", "stop_loss",
                    "horizon_days",
                )
                if rec.get(key) is not None
            }
            for rec in recommendations
        ],
    }


def _write_execution_thesis(
    order: dict, row: dict, trader, *, source: str = "morning",
) -> int | None:
    """Persist the existing executable thesis for a BUY decision."""
    from alpha_agents.data import thesis as T
    try:
        conditions = [
            T.Condition(**item) for item in (order.get("invalidations") or [])
        ]
        return T.create(T.Thesis(
            code=order["code"],
            name=row.get("name") or "",
            theme=row.get("_theme") or "",
            claim=order.get("reason") or "",
            horizon_days=int(
                order.get("horizon_days") or trader.default_horizon_days),
            prob=float(order.get("prob", 0.5)),
            conviction=float(order.get("conviction", 0.5)),
            size_pct=float(order.get("size_pct") or 0.0),
            conditions=conditions,
            created_by=f"trader_runtime:{source}",
            trader_id=trader.id,
        ))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Executable thesis for %s not written: %s",
                       order.get("code"), exc)
        return None


def _decision_knowledge(
    base: str, *, trader_id: str, run_id: str,
    context: DecisionContext, as_of: str,
) -> str:
    """Add only prior, run-local learning; keep its evidence labels intact."""
    from alpha_agents.evolution import trader_learning

    learned = trader_learning.inject(
        trader_id=trader_id,
        decision_horizon=context.decision_horizon.value,
        as_of=as_of,
        run_id=run_id,
    )
    return "\n\n".join(value for value in (base, learned) if value)


async def plan_morning(
    recommendations: list[dict],
    trader,
    *,
    prediction_ids: dict[str, int] | None = None,
    knowledge_block: str = "",
    events_context: str = "",
    allow_new_risk: bool = True,
    run_id: str | None = None,
) -> dict:
    """Advance the continuous trader and place only its BUY decisions."""
    cutoff = _logical_now()
    run = trader_session.namespace(run_id)
    if not allow_new_risk:
        return {"status": "research_only", "placed": [], "decisions": []}
    if cutoff.time() >= dt_time(9, 30):
        return {
            "status": "outside_preopen",
            "placed": [],
            "decisions": [],
            "reason": "pre-open risk window is closed",
        }

    day = cutoff.date().isoformat()
    prev_day = _previous_session(day)
    if prev_day is None:
        raise MorningTraderError("no previous market session for Trader plan")

    pred_ids = prediction_ids or {}
    panel = _panel(recommendations, prev_day)
    if not panel:
        return {
            "status": "empty_panel", "placed": [], "decisions": [],
            "reason": "morning research produced no executable panel",
        }

    context = _context(cutoff)
    runtime = TraderRuntime()
    state = trader_state_store.load_latest(
        run_id=run, trader_id=trader.id)
    if state is None:
        state = TraderState.create(trader_id=trader.id, as_of=cutoff)
        trader_state_store.save(state, run_id=run)
    if state.as_of > cutoff:
        raise MorningTraderError(
            "persisted TraderState is ahead of the morning cutoff")

    observed = await runtime.step(
        state,
        _candidate_observations(recommendations, pred_ids, cutoff),
        context,
    )
    state = observed.state
    if observed.changed:
        trader_state_store.save(state, run_id=run)

    decision_knowledge = _decision_knowledge(
        knowledge_block,
        trader_id=trader.id,
        run_id=run,
        context=context,
        as_of=prev_day,
    )
    verdict = await t1_decider.propose(
        day=day,
        prev_day=prev_day,
        panel=panel,
        news=[],
        book=_portfolio_context(trader.id, prev_day),
        knowledge=decision_knowledge,
        trader_note="\n\n".join(
            value for value in (trader.note, trader.extra_prompt) if value),
        picks=2,
        phase="open",
        tools=[],
        research_packet=_research_packet(recommendations, events_context),
        trader_id=trader.id,
        run_id=run,
        origin="trader_runtime:morning",
        information_cutoff=cutoff.isoformat(sep=" ", timespec="seconds"),
    )
    decisions = t1_decider.to_runtime_decisions(verdict, context)
    committed = await runtime.commit_decisions(state, decisions, context)
    trader_state_store.save(committed.state, run_id=run)

    by_code = {row["code"]: row for row in panel}
    placed = []
    try:
        from alpha_agents.pipeline.tasks import exit_decision
        wake_agent = exit_decision.enabled()
    except Exception:  # noqa: BLE001
        wake_agent = False

    for order in verdict.get("orders") or []:
        row = by_code.get(order["code"])
        if row is None:
            continue
        theme = row.get("_theme") or ""
        if not theme:
            logger.warning("%s BUY has no morning theme; order refused",
                           order["code"])
            continue
        thesis_id = _write_execution_thesis(order, row, trader)
        order_id = create_pending_order(
            code=order["code"],
            name=row.get("name") or "",
            theme=theme,
            order_date=day,
            entry_low=order.get("entry_low"),
            entry_high=order.get("entry_high"),
            stop_loss=order.get("stop_loss"),
            target_price=order.get("target_price"),
            source="trader_runtime",
            reason=f"{t1_decider.DECIDER_NAME}: {order.get('reason') or ''}",
            trader_id=trader.id,
            prediction_id=pred_ids.get(order["code"]),
            thesis_id=thesis_id,
            wake_agent=wake_agent,
        )
        if order_id is not None:
            placed.append({
                "code": order["code"],
                "order_id": order_id,
                "thesis_id": thesis_id,
                "reason": order.get("reason") or "",
            })

    return {
        "status": verdict.get("decision_status") or "complete",
        "verdict": verdict,
        "decisions": [item.as_dict() for item in decisions],
        "placed": placed,
        "state_hash": committed.state.state_hash,
        "state_version": committed.state.version,
        "run_id": run,
    }


def _intraday_context(cutoff: datetime) -> DecisionContext:
    from alpha_agents.evolution.replay_mode import replay_process

    replay = replay_process()
    return DecisionContext(
        mode="replay" if replay else "live",
        observation_resolution=Timeframe.MINUTE_5,
        decision_horizon=DecisionHorizon.SWING,
        session=Session.INTRADAY,
        information_cutoff=cutoff,
        evidence_scope=(
            EvidenceScope.REPLAY_INTRADAY if replay
            else EvidenceScope.LIVE_INTRADAY),
    )


def _recently_decided_codes(state: TraderState, cutoff: datetime) -> set[str]:
    return {
        item.code for item in state.recent_decisions
        if item.code and item.made_at.date() == cutoff.date()
    }


def _theme_for_code(
    state: TraderState, code: str, candidates: list[dict],
) -> str:
    for item in candidates:
        if item.get("code") == code and item.get("theme"):
            return str(item["theme"])
    for observation in reversed(state.recent_observations):
        if (code in observation.subjects
                and observation.type == ObservationType.CANDIDATE):
            theme = observation.data.get("theme")
            if isinstance(theme, str) and theme:
                return theme
    return ""


def _intraday_observations(
    state: TraderState,
    candidates: list[dict],
    prices: dict,
    cutoff: datetime,
    market_view: dict | None = None,
) -> list[Observation]:
    """Facts that changed now; the runtime decides which old plans they wake."""
    observations: list[Observation] = []
    codes = {
        item.code for item in state.watchlist
        if item.status.value in {"watching", "triggered"}
    }
    codes.update(
        str(item.get("code") or "") for item in candidates
        if item.get("code"))
    for code in sorted(codes):
        price = prices.get(code)
        if not price:
            continue
        payload = {"price": float(price)}
        change = prices.get(code + "_chg")
        if change is not None:
            payload["change_pct"] = float(change)
        observations.append(Observation.create(
            observed_at=cutoff,
            available_at=cutoff,
            type=ObservationType.PRICE_MOVE,
            subjects=[code],
            data=payload,
            source="intraday_quote",
            evidence_refs=[
                f"intraday-quote:{code}:{cutoff.isoformat(timespec='minutes')}"
            ],
            timeframe=Timeframe.MINUTE_5,
        ))

    market_view = market_view or {}
    explicit_subjects = {
        condition.subject
        for item in state.watchlist
        for condition in (*item.trigger_conditions, *item.invalidation_conditions)
        if condition.subject is not None
    }
    ranks = market_view.get("sector_ranks") or {}
    flows = market_view.get("sector_flows") or {}
    for subject in sorted(explicit_subjects):
        data = {}
        if subject in ranks:
            data["theme_rank"] = ranks[subject]
        if subject in flows:
            data["net_flow"] = flows[subject]
        if subject == "MARKET" and market_view.get("breadth_ratio") is not None:
            data["breadth_ratio"] = market_view["breadth_ratio"]
        if data:
            observations.append(Observation.create(
                observed_at=cutoff,
                available_at=cutoff,
                type=ObservationType.THEME_CHANGE,
                subjects=[subject],
                data=data,
                source="intraday_market_view",
                evidence_refs=[
                    f"intraday-market:{subject}:{cutoff.isoformat(timespec='minutes')}"
                ],
                timeframe=Timeframe.MINUTE_5,
            ))

    for item in candidates:
        code = str(item.get("code") or "")
        if not code:
            continue
        theme = str(item.get("theme") or "")
        subjects = [code] + ([theme] if theme and theme != code else [])
        observations.append(Observation.create(
            observed_at=cutoff,
            available_at=cutoff,
            type=ObservationType.CANDIDATE,
            subjects=subjects,
            data={
                key: item.get(key)
                for key in (
                    "name", "theme", "reason", "confidence", "score",
                    "change_pct", "institutional", "playbook_name",
                )
                if item.get(key) is not None
            },
            source="intraday_anomaly",
            evidence_refs=[
                f"intraday-candidate:{code}:{cutoff.isoformat(timespec='minutes')}"
            ],
            timeframe=Timeframe.MINUTE_5,
        ))
    return observations


def _intraday_panel(
    state: TraderState,
    candidates: list[dict],
    codes: set[str],
    prices: dict,
    prev_day: str,
) -> list[dict]:
    by_code = {str(item.get("code") or ""): item for item in candidates}
    out = []
    for code in sorted(codes):
        price = prices.get(code)
        if not price:
            continue
        history = mh.get_local_history(code, days=20, as_of=prev_day)
        volumes = [float(row.get("volume") or 0) for row in (history or [])]
        adv20 = sum(volumes) / len(volumes) if volumes else 0
        if adv20 <= 0:
            continue
        item = by_code.get(code, {})
        theme = _theme_for_code(state, code, candidates)
        out.append({
            "code": code,
            "name": str(item.get("name") or ""),
            "close": float(price),
            "change_pct": round(float(prices.get(code + "_chg") or 0), 2),
            "adv20": adv20,
            "turnover_rate": None,
            "consecutive_limits": None,
            "net_amount": None,
            "concepts": [theme] if theme else [],
            "_theme": theme,
        })
    return out


async def plan_intraday(
    candidates: list[dict],
    trader,
    *,
    prices: dict,
    prediction_ids: dict[str, int] | None = None,
    research_context: str = "",
    knowledge_block: str = "",
    market_view: dict | None = None,
    run_id: str | None = None,
) -> dict:
    """Advance intraday observations and re-evaluate only meaningful subjects."""
    cutoff = _logical_now()
    if not (dt_time(9, 30) <= cutoff.time() <= dt_time(15, 0)):
        return {
            "status": "outside_session", "placed": [], "decisions": [],
            "reason": "intraday risk window is closed",
        }

    run = trader_session.namespace(run_id)
    state = trader_state_store.load_latest(run_id=run, trader_id=trader.id)
    if state is None:
        # Intraday may be the first process after a restart. Starting empty is
        # explicit; account positions are reconciled in T5, never invented here.
        state = TraderState.create(trader_id=trader.id, as_of=cutoff)
        trader_state_store.save(state, run_id=run)
    if state.as_of > cutoff:
        raise MorningTraderError(
            "persisted TraderState is ahead of the intraday cutoff")

    context = _intraday_context(cutoff)
    runtime = TraderRuntime()
    observed = await runtime.step(
        state, _intraday_observations(
            state, candidates, prices, cutoff, market_view),
        context)
    state = observed.state
    if observed.changed:
        trader_state_store.save(state, run_id=run)

    triggered = set(observed.reevaluate_subjects)
    already_decided = _recently_decided_codes(state, cutoff)
    candidate_codes = {
        str(item.get("code") or "") for item in candidates
        if item.get("code")
    }
    new_codes = candidate_codes - already_decided
    decision_codes = triggered | new_codes
    if not decision_codes:
        return {
            "status": "observed", "placed": [], "decisions": [],
            "reevaluate_subjects": sorted(triggered),
            "state_hash": state.state_hash, "state_version": state.version,
            "run_id": run,
        }

    day = cutoff.date().isoformat()
    prev_day = _previous_session(day)
    if prev_day is None:
        raise MorningTraderError("no previous market session for intraday plan")
    panel = _intraday_panel(
        state, candidates, decision_codes, prices, prev_day)
    if not panel:
        return {
            "status": "empty_panel", "placed": [], "decisions": [],
            "reason": "no triggered/candidate code has executable market history",
        }

    decision_knowledge = _decision_knowledge(
        knowledge_block,
        trader_id=trader.id,
        run_id=run,
        context=context,
        as_of=prev_day,
    )
    verdict = await t1_decider.propose(
        day=day,
        prev_day=prev_day,
        panel=panel,
        news=[],
        book=feedback.inject_portfolio(
            trader_id=trader.id,
            price_map={
                code: float(price)
                for code, price in prices.items()
                if "_" not in code and isinstance(price, (int, float))
            }),
        knowledge=decision_knowledge,
        trader_note="\n\n".join(
            value for value in (trader.note, trader.extra_prompt) if value),
        picks=min(2, len(panel)),
        phase="intraday",
        tools=[],
        research_packet={
            "source": "intraday_observation",
            "context": research_context[:6000],
            "triggered_watch": sorted(triggered),
            "candidates": candidates,
        },
        trader_id=trader.id,
        run_id=run,
        origin="trader_runtime:intraday",
        information_cutoff=cutoff.isoformat(sep=" ", timespec="seconds"),
    )
    decisions = t1_decider.to_runtime_decisions(verdict, context)
    committed = await runtime.commit_decisions(state, decisions, context)
    trader_state_store.save(committed.state, run_id=run)

    pred_ids = prediction_ids or {}
    by_code = {row["code"]: row for row in panel}
    placed = []
    for order in verdict.get("orders") or []:
        row = by_code.get(order["code"])
        if row is None:
            continue
        theme = row.get("_theme") or ""
        if not theme:
            logger.warning("%s intraday BUY has no theme; order refused",
                           order["code"])
            continue
        thesis_id = _write_execution_thesis(
            order, row, trader, source="intraday")
        order_id = create_pending_order(
            code=order["code"],
            name=row.get("name") or "",
            theme=theme,
            order_date=day,
            entry_low=order.get("entry_low"),
            entry_high=order.get("entry_high"),
            stop_loss=order.get("stop_loss"),
            target_price=order.get("target_price"),
            source="trader_runtime_intraday",
            reason=f"{t1_decider.DECIDER_NAME}: {order.get('reason') or ''}",
            trader_id=trader.id,
            prediction_id=pred_ids.get(order["code"]),
            thesis_id=thesis_id,
            wake_agent=False,
        )
        if order_id is not None:
            placed.append({
                "code": order["code"], "order_id": order_id,
                "thesis_id": thesis_id, "reason": order.get("reason") or "",
            })

    return {
        "status": verdict.get("decision_status") or "complete",
        "verdict": verdict,
        "decisions": [item.as_dict() for item in decisions],
        "placed": placed,
        "reevaluate_subjects": sorted(triggered),
        "state_hash": committed.state.state_hash,
        "state_version": committed.state.version,
        "run_id": run,
    }
