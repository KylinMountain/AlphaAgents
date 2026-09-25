"""Deterministic state transition engine for one continuous trader."""
from __future__ import annotations

from dataclasses import dataclass, replace

from alpha_agents.trader.observation import Observation
from alpha_agents.trader.state import StateTransition, ThesisState, TraderState, WatchItem
from alpha_agents.trader.types import (
    DecisionContext, ObservationType, ThesisStatus, TraderRuntimeError,
    WatchStatus,
)

MAX_RECENT_OBSERVATIONS = 200

_THESIS_SIGNALS = {
    "activate": ThesisStatus.ACTIVE,
    "strengthen": ThesisStatus.STRENGTHENED,
    "weaken": ThesisStatus.WEAKENED,
    "invalidate": ThesisStatus.INVALIDATED,
    "close": ThesisStatus.CLOSED,
}


@dataclass(frozen=True)
class StepResult:
    state: TraderState
    transitions: tuple[StateTransition, ...]
    reevaluate_subjects: tuple[str, ...]
    accepted_observation_hashes: tuple[str, ...]
    changed: bool


class TraderRuntime:
    """One state machine for replay and live event streams.

    The runtime intentionally contains no branch on context.mode. Replay and
    live differ only in what an adapter was able to observe before the cutoff.
    Strategic decisions are added by the reasoning layer later; this core owns
    causal ingestion and lifecycle state transitions.
    """

    async def step(
        self,
        state: TraderState,
        observations: list[Observation] | tuple[Observation, ...],
        context: DecisionContext,
    ) -> StepResult:
        if context.information_cutoff < state.as_of:
            raise TraderRuntimeError(
                "decision cutoff cannot precede current TraderState")

        known = {item.observation_hash for item in state.recent_observations}
        incoming = []
        for observation in observations:
            if not isinstance(observation, Observation):
                raise TraderRuntimeError("step accepts Observation objects only")
            if observation.observation_hash in known:
                continue
            if observation.available_at > context.information_cutoff:
                raise TraderRuntimeError(
                    "observation is not available at the decision cutoff")
            if observation.available_at < state.as_of:
                raise TraderRuntimeError(
                    "new observation would backfill an already-sealed state")
            incoming.append(observation)

        incoming.sort(key=lambda item: (
            item.available_at, item.observed_at, item.observation_hash))
        self._reject_duplicate_batch(incoming)

        watchlist, watch_transitions, watch_recheck = self._update_watchlist(
            state.watchlist, incoming)
        theses, thesis_transitions, thesis_recheck = self._update_theses(
            state.theses, incoming)

        transitions = tuple(watch_transitions + thesis_transitions)
        reevaluate = tuple(sorted(watch_recheck | thesis_recheck))
        accepted = tuple(item.observation_hash for item in incoming)

        watermark_moves = context.information_cutoff > state.as_of
        changed = bool(incoming or transitions or watermark_moves)
        if not changed:
            return StepResult(
                state=state,
                transitions=(),
                reevaluate_subjects=(),
                accepted_observation_hashes=(),
                changed=False,
            )

        history = tuple(
            (*state.recent_observations, *incoming)[-MAX_RECENT_OBSERVATIONS:])
        next_state = state.evolve(
            as_of=context.information_cutoff,
            watchlist=watchlist,
            theses=theses,
            recent_observations=history,
        )
        return StepResult(
            state=next_state,
            transitions=transitions,
            reevaluate_subjects=reevaluate,
            accepted_observation_hashes=accepted,
            changed=True,
        )

    @staticmethod
    def _reject_duplicate_batch(observations: list[Observation]) -> None:
        hashes = [item.observation_hash for item in observations]
        if len(hashes) != len(set(hashes)):
            raise TraderRuntimeError(
                "the same new observation appears twice in one step")

    @staticmethod
    def _conditions_met(conditions, observations, *, require_all: bool) -> bool:
        if not conditions:
            return False
        matched = [
            any(condition.matches(observation) for observation in observations)
            for condition in conditions
        ]
        return all(matched) if require_all else any(matched)

    def _update_watchlist(
        self,
        watchlist: tuple[WatchItem, ...],
        observations: list[Observation],
    ) -> tuple[tuple[WatchItem, ...], list[StateTransition], set[str]]:
        updated = []
        transitions: list[StateTransition] = []
        recheck: set[str] = set()

        for item in watchlist:
            if item.status != WatchStatus.WATCHING:
                updated.append(item)
                continue
            relevant = [
                observation for observation in observations
                if item.code in observation.subjects
            ]
            if not relevant:
                updated.append(item)
                continue

            checked_at = max(observation.available_at for observation in relevant)
            current = replace(item, last_checked_at=checked_at)

            invalidated = self._conditions_met(
                item.invalidation_conditions, relevant, require_all=False)
            triggered = self._conditions_met(
                item.trigger_conditions, relevant,
                require_all=item.trigger_all)

            if invalidated:
                current = replace(current, status=WatchStatus.REJECTED)
                witness = self._first_witness(
                    item.invalidation_conditions, relevant)
                transitions.append(StateTransition(
                    kind="watch",
                    subject=item.code,
                    from_status=item.status.value,
                    to_status=current.status.value,
                    at=checked_at,
                    observation_hash=witness.observation_hash,
                    reason="watch invalidation condition matched",
                ))
                recheck.add(item.code)
            elif triggered:
                current = replace(current, status=WatchStatus.TRIGGERED)
                witness = self._first_witness(item.trigger_conditions, relevant)
                transitions.append(StateTransition(
                    kind="watch",
                    subject=item.code,
                    from_status=item.status.value,
                    to_status=current.status.value,
                    at=checked_at,
                    observation_hash=witness.observation_hash,
                    reason="watch trigger condition matched",
                ))
                recheck.add(item.code)

            updated.append(current)

        return tuple(updated), transitions, recheck

    def _update_theses(
        self,
        theses: tuple[ThesisState, ...],
        observations: list[Observation],
    ) -> tuple[tuple[ThesisState, ...], list[StateTransition], set[str]]:
        updated = []
        transitions: list[StateTransition] = []
        recheck: set[str] = set()

        for thesis in theses:
            if thesis.status in {ThesisStatus.INVALIDATED, ThesisStatus.CLOSED}:
                updated.append(thesis)
                continue

            relevant = [
                observation for observation in observations
                if thesis.subject in observation.subjects
            ]
            current = thesis

            if self._conditions_met(
                    thesis.invalidations, relevant, require_all=False):
                witness = self._first_witness(thesis.invalidations, relevant)
                current = replace(
                    thesis, status=ThesisStatus.INVALIDATED,
                    updated_at=witness.available_at)
                transitions.append(StateTransition(
                    kind="thesis",
                    subject=thesis.subject,
                    from_status=thesis.status.value,
                    to_status=current.status.value,
                    at=witness.available_at,
                    observation_hash=witness.observation_hash,
                    reason="thesis invalidation condition matched",
                ))
                recheck.add(thesis.subject)
                updated.append(current)
                continue

            signals = [
                observation for observation in relevant
                if observation.type == ObservationType.THESIS_SIGNAL
                and observation.data.get("thesis_id") == thesis.thesis_id
            ]
            if signals:
                signal = signals[-1]
                value = str(signal.data.get("signal") or "")
                target = _THESIS_SIGNALS.get(value)
                if target is None:
                    raise TraderRuntimeError(
                        f"unknown thesis signal {value!r}")
                if target != thesis.status:
                    current = replace(
                        thesis, status=target,
                        updated_at=signal.available_at)
                    transitions.append(StateTransition(
                        kind="thesis",
                        subject=thesis.subject,
                        from_status=thesis.status.value,
                        to_status=target.value,
                        at=signal.available_at,
                        observation_hash=signal.observation_hash,
                        reason=f"explicit thesis signal: {value}",
                    ))
                    recheck.add(thesis.subject)

            updated.append(current)

        return tuple(updated), transitions, recheck

    @staticmethod
    def _first_witness(conditions, observations) -> Observation:
        for observation in observations:
            if any(condition.matches(observation) for condition in conditions):
                return observation
        raise TraderRuntimeError("matched condition has no observation witness")
