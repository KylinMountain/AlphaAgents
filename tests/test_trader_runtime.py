"""Trader Runtime contracts: one causal state machine across timeframes."""
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from alpha_agents.trader import (
    Action, CompareOp, Condition, DecisionContext, DecisionHorizon, EvidenceScope,
    Observation, ObservationType, Session, ThesisLevel, ThesisState,
    ThesisStatus, Timeframe, TraderDecision, TraderRuntime, TraderRuntimeError, TraderState,
    WatchItem, WatchStatus,
)

TZ = ZoneInfo("Asia/Shanghai")


def at(hour=9, minute=0, day=25):
    return datetime(2026, 9, day, hour, minute, tzinfo=TZ)


def context(*, mode="replay", cutoff=None, scope=None, resolution=Timeframe.DAILY):
    return DecisionContext(
        mode=mode,
        observation_resolution=resolution,
        decision_horizon=DecisionHorizon.SWING,
        session=Session.OPEN,
        information_cutoff=cutoff or at(),
        evidence_scope=scope or (
            EvidenceScope.REPLAY_DAILY if mode == "replay"
            else EvidenceScope.LIVE_DAILY),
    )


def watch(*, scope=EvidenceScope.REPLAY_DAILY, invalidations=(), trigger_all=False):
    return WatchItem(
        code="600001",
        status=WatchStatus.WATCHING,
        why="strong theme, price too high",
        trigger_conditions=(Condition("price", CompareOp.LE, 30.0),),
        invalidation_conditions=tuple(invalidations),
        trigger_all=trigger_all,
        next_check="price <= 30",
        created_at=at(9, 0, 24),
        last_checked_at=None,
        evidence_timeframe=Timeframe.DAILY,
        decision_horizon=DecisionHorizon.SWING,
        evidence_scope=scope,
    )


def state(*, watchlist=(), theses=()):
    return TraderState.create(
        trader_id="pullback", as_of=at(9, 0, 24),
        watchlist=tuple(watchlist), theses=tuple(theses))


def obs(*, price=29.8, available=None, subject="600001",
        kind=ObservationType.PRICE_MOVE, timeframe=Timeframe.DAILY,
        data=None):
    available = available or at()
    payload = {"price": price} if data is None else data
    return Observation.create(
        observed_at=available,
        available_at=available,
        type=kind,
        subjects=[subject],
        data=payload,
        source="test",
        evidence_refs=["bar:1"],
        timeframe=timeframe,
    )


def run(s, observations, ctx):
    return asyncio.run(TraderRuntime().step(s, observations, ctx))


def test_observation_payload_is_immutable_by_copy():
    item = obs()
    leaked = item.data
    leaked["price"] = 99
    assert item.data["price"] == 29.8


def test_future_observation_is_rejected():
    with pytest.raises(TraderRuntimeError, match="not available"):
        run(state(), [obs(available=at(10))], context(cutoff=at(9, 30)))


def test_unrecorded_backfill_is_rejected():
    s = TraderState.create(trader_id="x", as_of=at(10))
    with pytest.raises(TraderRuntimeError, match="backfill"):
        run(s, [obs(available=at(9, 30))], context(cutoff=at(10, 30)))


def test_daily_replay_wait_condition_triggers_same_watch_state():
    result = run(state(watchlist=[watch()]), [obs()], context())
    item = result.state.watchlist[0]
    assert item.status == WatchStatus.TRIGGERED
    assert result.reevaluate_subjects == ("600001",)
    assert result.transitions[0].kind == "watch"
    assert result.decisions == ()


def test_intraday_live_uses_same_watch_state_machine():
    live_watch = watch(scope=EvidenceScope.LIVE_INTRADAY)
    price_tick = obs(
        available=at(10, 40), timeframe=Timeframe.MINUTE_5)
    result = run(
        state(watchlist=[live_watch]), [price_tick],
        context(
            mode="live", cutoff=at(10, 40),
            scope=EvidenceScope.LIVE_INTRADAY,
            resolution=Timeframe.MINUTE_5))
    assert result.state.watchlist[0].status == WatchStatus.TRIGGERED
    assert result.reevaluate_subjects == ("600001",)


def test_context_mode_does_not_select_a_different_runtime_path():
    item = obs()
    base = state(watchlist=[watch()])
    replay = run(base, [item], context())
    live = run(
        base, [item],
        context(mode="live", scope=EvidenceScope.LIVE_DAILY))
    assert replay.state.state_hash == live.state.state_hash
    assert replay.transitions == live.transitions


def test_unrelated_stock_price_cannot_trigger_watch():
    result = run(
        state(watchlist=[watch()]),
        [obs(subject="600999")],
        context())
    assert result.state.watchlist[0].status == WatchStatus.WATCHING
    assert not result.transitions


def test_explicit_theme_invalidation_can_reject_stock_watch():
    invalidation = Condition(
        "net_flow", CompareOp.LT, 0, subject="AI算力")
    result = run(
        state(watchlist=[watch(invalidations=[invalidation])]),
        [obs(
            subject="AI算力", kind=ObservationType.SECTOR_FLOW,
            data={"net_flow": -100.0})],
        context())
    assert result.state.watchlist[0].status == WatchStatus.REJECTED
    assert result.reevaluate_subjects == ("600001",)


def test_invalidation_wins_when_trigger_and_rejection_arrive_together():
    invalidation = Condition("theme_broken", CompareOp.EQ, True)
    result = run(
        state(watchlist=[watch(invalidations=[invalidation])]),
        [
            obs(price=29.0),
            obs(
                available=at(9, 1),
                kind=ObservationType.EVENT,
                data={"theme_broken": True}),
        ],
        context(cutoff=at(9, 1)))
    assert result.state.watchlist[0].status == WatchStatus.REJECTED


def test_thesis_signal_updates_existing_thesis():
    thesis = ThesisState(
        thesis_id="t1",
        level=ThesisLevel.TRADE,
        subject="600001",
        claim="theme remains strong",
        status=ThesisStatus.ACTIVE,
        timeframe=Timeframe.DAILY,
        decision_horizon=DecisionHorizon.SWING,
        evidence_scope=EvidenceScope.REPLAY_DAILY,
        conviction=0.6,
        invalidations=(),
        evidence_refs=("daily:1",),
        created_at=at(9, 0, 24),
        updated_at=at(9, 0, 24),
    )
    signal = obs(
        kind=ObservationType.THESIS_SIGNAL,
        data={"thesis_id": "t1", "signal": "weaken"})
    result = run(state(theses=[thesis]), [signal], context())
    assert result.state.theses[0].status == ThesisStatus.WEAKENED
    assert result.reevaluate_subjects == ("600001",)


def test_thesis_invalidation_can_come_from_cross_subject_daily_fact():
    thesis = ThesisState(
        thesis_id="t1",
        level=ThesisLevel.TRADE,
        subject="600001",
        claim="AI theme flow remains positive",
        status=ThesisStatus.ACTIVE,
        timeframe=Timeframe.DAILY,
        decision_horizon=DecisionHorizon.SWING,
        evidence_scope=EvidenceScope.REPLAY_DAILY,
        conviction=0.7,
        invalidations=(
            Condition("net_flow", CompareOp.LT, 0, subject="AI算力"),),
        evidence_refs=(),
        created_at=at(9, 0, 24),
        updated_at=at(9, 0, 24),
    )
    result = run(
        state(theses=[thesis]),
        [obs(
            subject="AI算力", kind=ObservationType.SECTOR_FLOW,
            data={"net_flow": -1})],
        context())
    assert result.state.theses[0].status == ThesisStatus.INVALIDATED


def test_state_round_trip_and_hash_tamper_detection():
    original = state(watchlist=[watch()])
    encoded = original.as_dict()
    restored = TraderState.from_dict(encoded)
    assert restored == original
    assert restored.state_hash == original.state_hash

    missing_hash = dict(encoded)
    missing_hash.pop("state_hash")
    with pytest.raises(TraderRuntimeError, match="state_hash"):
        TraderState.from_dict(missing_hash)

    encoded["market_view"]["regime"] = "invented"
    with pytest.raises(TraderRuntimeError, match="hash mismatch"):
        TraderState.from_dict(encoded)


def test_state_lineage_points_to_exact_parent_hash():
    original = state(watchlist=[watch()])
    result = run(original, [obs()], context())
    assert result.state.version == 1
    assert result.state.parent_state_hash == original.state_hash


def test_same_cutoff_and_only_known_observation_is_noop():
    first = run(state(), [obs()], context())
    second = run(first.state, [obs()], context())
    assert second.state is first.state
    assert not second.changed


def test_watermark_advances_even_without_observation_and_seals_past():
    initial = state()
    moved = run(initial, [], context(cutoff=at(10)))
    assert moved.state.as_of == at(10)
    assert moved.changed
    with pytest.raises(TraderRuntimeError, match="backfill"):
        run(
            moved.state, [obs(available=at(9, 30))],
            context(cutoff=at(10, 30)))


def test_duplicate_new_observation_in_one_step_is_refused():
    item = obs()
    with pytest.raises(TraderRuntimeError, match="twice"):
        run(state(), [item, item], context())


def test_daily_evidence_stays_labelled_daily_in_live_runtime():
    item = watch(scope=EvidenceScope.REPLAY_DAILY)
    result = run(
        state(watchlist=[item]),
        [obs(timeframe=Timeframe.MINUTE_5)],
        context(
            mode="live", scope=EvidenceScope.LIVE_INTRADAY,
            resolution=Timeframe.MINUTE_5))
    kept = result.state.watchlist[0]
    assert kept.evidence_timeframe == Timeframe.DAILY
    assert kept.evidence_scope == EvidenceScope.REPLAY_DAILY


def test_decision_context_rejects_scope_mode_mismatch():
    with pytest.raises(TraderRuntimeError, match="replay evidence"):
        context(mode="replay", scope=EvidenceScope.LIVE_DAILY)


def test_naive_timestamps_are_refused():
    naive = datetime(2026, 9, 25, 9, 0)
    with pytest.raises(TraderRuntimeError, match="timezone-aware"):
        Observation.create(
            observed_at=naive, available_at=naive,
            type=ObservationType.EVENT, subjects=[],
            data={}, source="test", evidence_refs=[],
            timeframe=Timeframe.DAILY)


def decision(*, decision_id="d1", action=Action.WAIT, made_at=None,
             scope=EvidenceScope.LIVE_INTRADAY,
             timeframe=Timeframe.MINUTE_5, next_check=None):
    return TraderDecision(
        decision_id=decision_id,
        made_at=made_at or at(),
        action=action,
        code="600001",
        thesis_id=None,
        confidence=0.6,
        reasoning="price too high; wait for planned level",
        timeframe=timeframe,
        decision_horizon=DecisionHorizon.SWING,
        evidence_scope=scope,
        invalidations=(),
        next_check=tuple(
            (Condition("price", CompareOp.LE, 30.0),)
            if next_check is None else next_check),
    )


def live_context(cutoff=None):
    return context(
        mode="live", cutoff=cutoff or at(),
        scope=EvidenceScope.LIVE_INTRADAY,
        resolution=Timeframe.MINUTE_5)


def commit(s, decisions, ctx):
    return asyncio.run(TraderRuntime().commit_decisions(s, decisions, ctx))


def test_wait_decision_becomes_durable_watch_and_is_recorded():
    base = TraderState.create(trader_id="x", as_of=at())
    result = commit(base, [decision()], live_context())
    item = result.state.watchlist[0]
    assert item.code == "600001"
    assert item.status == WatchStatus.WATCHING
    assert item.next_check == "price <= 30.0"
    assert result.state.recent_decisions[-1].decision_id == "d1"
    assert result.transitions[0].from_status == "absent"
    assert result.decisions[0].action == Action.WAIT


def test_state_rejects_watch_created_in_its_future():
    future_watch = WatchItem(
        code="600001", status=WatchStatus.WATCHING, why="created later",
        trigger_conditions=(Condition("price", CompareOp.LE, 30.0),),
        invalidation_conditions=(), trigger_all=False,
        next_check="price <= 30", created_at=at(9, 3),
        last_checked_at=None, evidence_timeframe=Timeframe.MINUTE_5,
        decision_horizon=DecisionHorizon.SWING,
        evidence_scope=EvidenceScope.LIVE_INTRADAY)
    with pytest.raises(TraderRuntimeError, match="state future"):
        TraderState.create(
            trader_id="x", as_of=at(), watchlist=(future_watch,))


def test_wait_then_new_tick_then_buy_converts_same_watch():
    base = TraderState.create(trader_id="x", as_of=at())
    waited = commit(base, [decision()], live_context()).state

    tick = obs(
        price=29.8, available=at(9, 4), timeframe=Timeframe.MINUTE_5)
    triggered = run(waited, [tick], live_context(cutoff=at(9, 4)))
    assert triggered.state.watchlist[0].status == WatchStatus.TRIGGERED
    assert triggered.reevaluate_subjects == ("600001",)

    buy = decision(
        decision_id="d2", action=Action.BUY, made_at=at(9, 4),
        next_check=())
    bought = commit(
        triggered.state, [buy], live_context(cutoff=at(9, 4)))
    assert len(bought.state.watchlist) == 1
    assert bought.state.watchlist[0].status == WatchStatus.CONVERTED
    assert [item.decision_id for item in bought.state.recent_decisions] == [
        "d1", "d2"]


def test_reject_closes_existing_watch_without_creating_another():
    base = TraderState.create(trader_id="x", as_of=at())
    waited = commit(base, [decision()], live_context()).state
    rejected = commit(
        waited,
        [decision(
            decision_id="d2", action=Action.REJECT, made_at=at(),
            next_check=())],
        live_context())
    assert len(rejected.state.watchlist) == 1
    assert rejected.state.watchlist[0].status == WatchStatus.REJECTED


def test_wait_requires_recheck_condition():
    base = TraderState.create(trader_id="x", as_of=at())
    with pytest.raises(TraderRuntimeError, match="next_check"):
        commit(
            base,
            [decision(next_check=())],
            live_context())


@pytest.mark.parametrize(
    "bad,match",
    [
        ({"scope": EvidenceScope.REPLAY_DAILY}, "evidence scope"),
        ({"timeframe": Timeframe.DAILY}, "timeframe"),
    ],
)
def test_decision_metadata_must_match_decision_context(bad, match):
    base = TraderState.create(trader_id="x", as_of=at())
    d = decision(
        scope=bad.get("scope", EvidenceScope.LIVE_INTRADAY),
        timeframe=bad.get("timeframe", Timeframe.MINUTE_5))
    with pytest.raises(TraderRuntimeError, match=match):
        commit(base, [d], live_context())


def test_decision_cannot_be_committed_again():
    base = TraderState.create(trader_id="x", as_of=at())
    first = commit(base, [decision()], live_context()).state
    with pytest.raises(TraderRuntimeError, match="duplicate decision"):
        commit(first, [decision()], live_context())


def test_commit_requires_exact_state_cutoff():
    base = TraderState.create(trader_id="x", as_of=at())
    with pytest.raises(TraderRuntimeError, match="exact state cutoff"):
        commit(base, [decision()], live_context(cutoff=at(9, 5)))


def test_empty_decision_commit_is_noop():
    base = TraderState.create(trader_id="x", as_of=at())
    result = commit(base, [], live_context())
    assert result.state is base
    assert not result.changed


def test_repeated_wait_preserves_original_watch_identity():
    base = TraderState.create(trader_id="x", as_of=at())
    first = commit(base, [decision(made_at=at())], live_context()).state
    advanced = run(first, [], live_context(cutoff=at(9, 8))).state
    refreshed = commit(
        advanced,
        [decision(decision_id="d2", made_at=at(9, 8))],
        live_context(cutoff=at(9, 8))).state
    assert len(refreshed.watchlist) == 1
    assert refreshed.watchlist[0].created_at == at()
    assert refreshed.watchlist[0].last_checked_at == at(9, 8)


def test_decision_wall_clock_cannot_replace_logical_cutoff():
    base = TraderState.create(trader_id="x", as_of=at())
    with pytest.raises(TraderRuntimeError, match="logical information cutoff"):
        commit(
            base,
            [decision(made_at=at(9, 3))],
            live_context(cutoff=at()))


def test_daily_replay_wait_carries_into_next_session_and_triggers():
    base = TraderState.create(trader_id="x", as_of=at())
    wait_daily = decision(
        scope=EvidenceScope.REPLAY_DAILY,
        timeframe=Timeframe.DAILY,
        made_at=at())
    waited = commit(base, [wait_daily], context()).state
    assert waited.watchlist[0].evidence_scope == EvidenceScope.REPLAY_DAILY

    next_day = obs(
        price=29.7,
        available=at(day=26),
        kind=ObservationType.DAILY_BAR,
        timeframe=Timeframe.DAILY)
    triggered = run(
        waited, [next_day],
        context(cutoff=at(day=26)))
    assert triggered.state.watchlist[0].status == WatchStatus.TRIGGERED
    assert triggered.reevaluate_subjects == ("600001",)


def test_state_rejects_future_decision_and_observation():
    future_decision = decision(made_at=at(9, 1))
    with pytest.raises(TraderRuntimeError, match="decision.*state future"):
        TraderState.create(
            trader_id="x", as_of=at(), recent_decisions=(future_decision,))
    future_observation = obs(available=at(9, 1))
    with pytest.raises(TraderRuntimeError, match="observation.*state future"):
        TraderState.create(
            trader_id="x", as_of=at(),
            recent_observations=(future_observation,))
