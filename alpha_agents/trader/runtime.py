"""Deterministic state transition engine for one continuous trader."""
from __future__ import annotations

from dataclasses import dataclass, replace

from alpha_agents.trader.observation import Observation
from alpha_agents.trader.state import (
    StateTransition, ThesisState, TraderDecision, TraderState, WatchItem,
)
from alpha_agents.trader.types import (
    DecisionContext, ObservationType, ThesisStatus, TraderRuntimeError,
    WatchStatus,
)

MAX_RECENT_OBSERVATIONS = 200
MAX_RECENT_DECISIONS = 200

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
    decisions: tuple[TraderDecision, ...]
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
                decisions=(),
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
            decisions=(),
            changed=True,
        )

    async def commit_decisions(
        self,
        state: TraderState,
        decisions: list[TraderDecision] | tuple[TraderDecision, ...],
        context: DecisionContext,
    ) -> StepResult:
        """Persist reasoning output into the same continuous cognitive state.

        Reasoning is deliberately outside this domain layer. This method owns
        only the deterministic consequences of a decision: WAIT becomes a
        durable watch, REJECT closes that watch, and BUY/ADD converts it into
        an acted-on opportunity. Execution and positions remain downstream.
        """
        if context.information_cutoff != state.as_of:
            raise TraderRuntimeError(
                "decisions must commit against the exact state cutoff")
        existing_ids = {item.decision_id for item in state.recent_decisions}
        incoming_ids: set[str] = set()
        watch_by_code = {item.code: item for item in state.watchlist}
        order = [item.code for item in state.watchlist]
        transitions: list[StateTransition] = []

        for decision in decisions:
            if not isinstance(decision, TraderDecision):
                raise TraderRuntimeError(
                    "commit_decisions accepts TraderDecision objects only")
            if decision.decision_id in existing_ids or decision.decision_id in incoming_ids:
                raise TraderRuntimeError("duplicate decision id")
            incoming_ids.add(decision.decision_id)
            if decision.made_at < state.as_of:
                raise TraderRuntimeError(
                    "decision cannot predate the state it was made from")
            if decision.evidence_scope != context.evidence_scope:
                raise TraderRuntimeError(
                    "decision evidence scope does not match DecisionContext")
            if decision.decision_horizon != context.decision_horizon:
                raise TraderRuntimeError(
                    "decision horizon does not match DecisionContext")
            if decision.timeframe != context.observation_resolution:
                raise TraderRuntimeError(
                    "decision timeframe does not match DecisionContext")
            if not decision.code:
                raise TraderRuntimeError("trade decision requires a code")

            code = decision.code
            current = watch_by_code.get(code)
            if decision.action.value == "wait":
                if not decision.next_check:
                    raise TraderRuntimeError(
                        "WAIT decision needs at least one next_check condition")
                next_watch = WatchItem(
                    code=code,
                    status=WatchStatus.WATCHING,
                    why=decision.reasoning,
                    trigger_conditions=decision.next_check,
                    invalidation_conditions=decision.invalidations,
                    trigger_all=False,
                    next_check=self._describe_conditions(decision.next_check),
                    created_at=decision.made_at,
                    last_checked_at=None,
                    evidence_timeframe=decision.timeframe,
                    decision_horizon=decision.decision_horizon,
                    evidence_scope=decision.evidence_scope,
                )
                watch_by_code[code] = next_watch
                if code not in order:
                    order.append(code)
                transitions.append(StateTransition(
                    kind="watch",
                    subject=code,
                    from_status=(
                        current.status.value if current else "absent"),
                    to_status=WatchStatus.WATCHING.value,
                    at=decision.made_at,
                    observation_hash="decision:" + decision.decision_id,
                    reason="WAIT decision created or refreshed watch",
                ))
            elif decision.action.value == "reject" and current is not None:
                if current.status != WatchStatus.REJECTED:
                    watch_by_code[code] = replace(
                        current, status=WatchStatus.REJECTED,
                        last_checked_at=decision.made_at)
                    transitions.append(StateTransition(
                        kind="watch",
                        subject=code,
                        from_status=current.status.value,
                        to_status=WatchStatus.REJECTED.value,
                        at=decision.made_at,
                        observation_hash="decision:" + decision.decision_id,
                        reason="REJECT decision closed watch",
                    ))
            elif decision.action.value in {"buy", "add"} and current is not None:
                if current.status != WatchStatus.CONVERTED:
                    watch_by_code[code] = replace(
                        current, status=WatchStatus.CONVERTED,
                        last_checked_at=decision.made_at)
                    transitions.append(StateTransition(
                        kind="watch",
                        subject=code,
                        from_status=current.status.value,
                        to_status=WatchStatus.CONVERTED.value,
                        at=decision.made_at,
                        observation_hash="decision:" + decision.decision_id,
                        reason=f"{decision.action.value.upper()} acted on watch",
                    ))

        if not decisions:
            return StepResult(
                state=state, transitions=(), reevaluate_subjects=(),
                accepted_observation_hashes=(), decisions=(), changed=False)

        next_state = state.evolve(
            as_of=state.as_of,
            watchlist=tuple(watch_by_code[code] for code in order),
            recent_decisions=tuple(
                (*state.recent_decisions, *decisions)[-MAX_RECENT_DECISIONS:]),
        )
        return StepResult(
            state=next_state,
            transitions=tuple(transitions),
            reevaluate_subjects=(),
            accepted_observation_hashes=(),
            decisions=tuple(decisions),
            changed=True,
        )

    @staticmethod
    def _describe_conditions(conditions) -> str:
        return " OR ".join(
            f"{item.subject + ':' if item.subject else ''}"
            f"{item.metric} {item.op.value} {item.value}"
            for item in conditions)

    @staticmethod
    def _reject_duplicate_batch(observations: list[Observation]) -> None:
        hashes = [item.observation_hash for item in observations]
        if len(hashes) != len(set(hashes)):
            raise TraderRuntimeError(
                "the same new observation appears twice in one step")

    @staticmethod
    def _condition_matches(condition, observation, *, default_subject: str) -> bool:
        if condition.subject is None and default_subject not in observation.subjects:
            return False
        return condition.matches(observation)

    @classmethod
    def _conditions_met(
        cls, conditions, observations, *, require_all: bool,
        default_subject: str,
    ) -> bool:
        if not conditions:
            return False
        matched = [
            any(cls._condition_matches(
                condition, observation, default_subject=default_subject)
                for observation in observations)
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
            watched_subjects = {
                item.code,
                *(condition.subject for condition in (
                    *item.trigger_conditions, *item.invalidation_conditions)
                  if condition.subject is not None),
            }
            relevant = [
                observation for observation in observations
                if observation.available_at >= item.created_at
                and watched_subjects.intersection(observation.subjects)
            ]
            if not relevant:
                updated.append(item)
                continue

            checked_at = max(observation.available_at for observation in relevant)
            current = replace(item, last_checked_at=checked_at)

            invalidated = self._conditions_met(
                item.invalidation_conditions, relevant, require_all=False,
                default_subject=item.code)
            triggered = self._conditions_met(
                item.trigger_conditions, relevant,
                require_all=item.trigger_all, default_subject=item.code)

            if invalidated:
                current = replace(current, status=WatchStatus.REJECTED)
                witness = self._first_witness(
                    item.invalidation_conditions, relevant,
                    default_subject=item.code)
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
                witness = self._first_witness(
                    item.trigger_conditions, relevant,
                    default_subject=item.code)
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

            thesis_subjects = {
                thesis.subject,
                *(condition.subject for condition in thesis.invalidations
                  if condition.subject is not None),
            }
            relevant = [
                observation for observation in observations
                if observation.available_at >= thesis.created_at
                and thesis_subjects.intersection(observation.subjects)
            ]
            current = thesis

            if self._conditions_met(
                    thesis.invalidations, relevant, require_all=False,
                    default_subject=thesis.subject):
                witness = self._first_witness(
                    thesis.invalidations, relevant,
                    default_subject=thesis.subject)
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

    @classmethod
    def _first_witness(
        cls, conditions, observations, *, default_subject: str,
    ) -> Observation:
        for observation in observations:
            if any(cls._condition_matches(
                    condition, observation, default_subject=default_subject)
                   for condition in conditions):
                return observation
        raise TraderRuntimeError("matched condition has no observation witness")
