"""Immutable cognitive state for one Trader Runtime."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import json
from typing import Any

from alpha_agents.trader.observation import Observation
from alpha_agents.trader.types import (
    Action, CompareOp, DecisionHorizon, EvidenceScope, ThesisLevel,
    ThesisStatus, Timeframe, TraderRuntimeError, WatchStatus, canonical_json,
    content_hash, require_aware, require_text,
)


@dataclass(frozen=True)
class Condition:
    """A small, auditable trigger grammar evaluated against observations."""

    metric: str
    op: CompareOp
    value: Any
    subject: str | None = None

    def __post_init__(self) -> None:
        require_text(self.metric, "condition metric")
        if self.subject is not None:
            require_text(self.subject, "condition subject")
        # Validation only; the canonical result is not stored.
        canonical_json({"value": self.value})

    def matches(self, observation: Observation) -> bool:
        if self.subject is not None and self.subject not in observation.subjects:
            return False
        data = observation.data
        if self.metric not in data:
            return False
        actual = data[self.metric]
        try:
            if self.op == CompareOp.EQ:
                return actual == self.value
            if self.op == CompareOp.NE:
                return actual != self.value
            if self.op == CompareOp.LT:
                return actual < self.value
            if self.op == CompareOp.LE:
                return actual <= self.value
            if self.op == CompareOp.GT:
                return actual > self.value
            if self.op == CompareOp.GE:
                return actual >= self.value
        except TypeError:
            return False
        raise TraderRuntimeError(f"unsupported comparison {self.op}")

    def as_dict(self) -> dict:
        return {
            "metric": self.metric,
            "op": self.op.value,
            "value": self.value,
            "subject": self.subject,
        }

    @classmethod
    def from_dict(cls, value: dict) -> "Condition":
        return cls(
            metric=value["metric"],
            op=CompareOp(value["op"]),
            value=value.get("value"),
            subject=value.get("subject"),
        )


@dataclass(frozen=True)
class WatchItem:
    code: str
    status: WatchStatus
    why: str
    trigger_conditions: tuple[Condition, ...]
    invalidation_conditions: tuple[Condition, ...]
    trigger_all: bool
    next_check: str
    created_at: datetime
    last_checked_at: datetime | None
    evidence_timeframe: Timeframe
    decision_horizon: DecisionHorizon
    evidence_scope: EvidenceScope

    def __post_init__(self) -> None:
        require_text(self.code, "watch code")
        require_text(self.why, "watch why")
        require_text(self.next_check, "watch next_check")
        require_aware(self.created_at, "watch created_at")
        if self.last_checked_at is not None:
            require_aware(self.last_checked_at, "watch last_checked_at")
            if self.last_checked_at < self.created_at:
                raise TraderRuntimeError(
                    "watch last_checked_at cannot precede created_at")
        if not self.trigger_conditions:
            raise TraderRuntimeError("watch item needs at least one trigger condition")

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "status": self.status.value,
            "why": self.why,
            "trigger_conditions": [item.as_dict() for item in self.trigger_conditions],
            "invalidation_conditions": [
                item.as_dict() for item in self.invalidation_conditions],
            "trigger_all": self.trigger_all,
            "next_check": self.next_check,
            "created_at": self.created_at.isoformat(),
            "last_checked_at": (
                self.last_checked_at.isoformat() if self.last_checked_at else None),
            "evidence_timeframe": self.evidence_timeframe.value,
            "decision_horizon": self.decision_horizon.value,
            "evidence_scope": self.evidence_scope.value,
        }

    @classmethod
    def from_dict(cls, value: dict) -> "WatchItem":
        return cls(
            code=value["code"],
            status=WatchStatus(value["status"]),
            why=value["why"],
            trigger_conditions=tuple(
                Condition.from_dict(item)
                for item in value.get("trigger_conditions") or []),
            invalidation_conditions=tuple(
                Condition.from_dict(item)
                for item in value.get("invalidation_conditions") or []),
            trigger_all=bool(value.get("trigger_all", False)),
            next_check=value["next_check"],
            created_at=datetime.fromisoformat(value["created_at"]),
            last_checked_at=(
                datetime.fromisoformat(value["last_checked_at"])
                if value.get("last_checked_at") else None),
            evidence_timeframe=Timeframe(value["evidence_timeframe"]),
            decision_horizon=DecisionHorizon(value["decision_horizon"]),
            evidence_scope=EvidenceScope(value["evidence_scope"]),
        )


@dataclass(frozen=True)
class ThesisState:
    thesis_id: str
    level: ThesisLevel
    subject: str
    claim: str
    status: ThesisStatus
    timeframe: Timeframe
    decision_horizon: DecisionHorizon
    evidence_scope: EvidenceScope
    conviction: float
    invalidations: tuple[Condition, ...]
    evidence_refs: tuple[str, ...]
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        require_text(self.thesis_id, "thesis_id")
        require_text(self.subject, "thesis subject")
        require_text(self.claim, "thesis claim")
        if not 0 <= float(self.conviction) <= 1:
            raise TraderRuntimeError("thesis conviction must be in [0, 1]")
        require_aware(self.created_at, "thesis created_at")
        require_aware(self.updated_at, "thesis updated_at")
        if self.updated_at < self.created_at:
            raise TraderRuntimeError("thesis updated_at cannot precede created_at")
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise TraderRuntimeError("thesis evidence_refs must be unique")

    def as_dict(self) -> dict:
        return {
            "thesis_id": self.thesis_id,
            "level": self.level.value,
            "subject": self.subject,
            "claim": self.claim,
            "status": self.status.value,
            "timeframe": self.timeframe.value,
            "decision_horizon": self.decision_horizon.value,
            "evidence_scope": self.evidence_scope.value,
            "conviction": self.conviction,
            "invalidations": [item.as_dict() for item in self.invalidations],
            "evidence_refs": list(self.evidence_refs),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, value: dict) -> "ThesisState":
        return cls(
            thesis_id=value["thesis_id"],
            level=ThesisLevel(value["level"]),
            subject=value["subject"],
            claim=value["claim"],
            status=ThesisStatus(value["status"]),
            timeframe=Timeframe(value["timeframe"]),
            decision_horizon=DecisionHorizon(value["decision_horizon"]),
            evidence_scope=EvidenceScope(value["evidence_scope"]),
            conviction=float(value["conviction"]),
            invalidations=tuple(
                Condition.from_dict(item)
                for item in value.get("invalidations") or []),
            evidence_refs=tuple(value.get("evidence_refs") or []),
            created_at=datetime.fromisoformat(value["created_at"]),
            updated_at=datetime.fromisoformat(value["updated_at"]),
        )


@dataclass(frozen=True)
class PositionState:
    code: str
    shares: int
    avg_price: float
    thesis_id: str | None = None

    def __post_init__(self) -> None:
        require_text(self.code, "position code")
        if type(self.shares) is not int or self.shares < 0:
            raise TraderRuntimeError("position shares must be a non-negative integer")
        if self.shares and self.avg_price <= 0:
            raise TraderRuntimeError("open position avg_price must be positive")
        if self.thesis_id is not None:
            require_text(self.thesis_id, "position thesis_id")

    def as_dict(self) -> dict:
        return {
            "code": self.code, "shares": self.shares,
            "avg_price": self.avg_price, "thesis_id": self.thesis_id,
        }

    @classmethod
    def from_dict(cls, value: dict) -> "PositionState":
        return cls(
            code=value["code"], shares=int(value["shares"]),
            avg_price=float(value["avg_price"]),
            thesis_id=value.get("thesis_id"),
        )


@dataclass(frozen=True)
class TraderDecision:
    decision_id: str
    made_at: datetime
    action: Action
    code: str | None
    thesis_id: str | None
    confidence: float
    reasoning: str
    timeframe: Timeframe
    decision_horizon: DecisionHorizon
    evidence_scope: EvidenceScope
    size_pct: float | None = None
    entry_low: float | None = None
    entry_high: float | None = None
    stop_loss: float | None = None
    target_price: float | None = None
    invalidations: tuple[Condition, ...] = ()
    next_check: tuple[Condition, ...] = ()

    def __post_init__(self) -> None:
        require_text(self.decision_id, "decision_id")
        require_aware(self.made_at, "decision made_at")
        require_text(self.reasoning, "decision reasoning")
        if self.code is not None:
            require_text(self.code, "decision code")
        if self.thesis_id is not None:
            require_text(self.thesis_id, "decision thesis_id")
        if not 0 <= float(self.confidence) <= 1:
            raise TraderRuntimeError("decision confidence must be in [0, 1]")
        if self.size_pct is not None and not 0 <= self.size_pct <= 1:
            raise TraderRuntimeError("size_pct must be in [0, 1]")
        if (self.entry_low is not None and self.entry_high is not None
                and self.entry_low > self.entry_high):
            raise TraderRuntimeError("entry_low cannot exceed entry_high")

    def as_dict(self) -> dict:
        return {
            "decision_id": self.decision_id,
            "made_at": self.made_at.isoformat(),
            "action": self.action.value,
            "code": self.code,
            "thesis_id": self.thesis_id,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "timeframe": self.timeframe.value,
            "decision_horizon": self.decision_horizon.value,
            "evidence_scope": self.evidence_scope.value,
            "size_pct": self.size_pct,
            "entry_low": self.entry_low,
            "entry_high": self.entry_high,
            "stop_loss": self.stop_loss,
            "target_price": self.target_price,
            "invalidations": [item.as_dict() for item in self.invalidations],
            "next_check": [item.as_dict() for item in self.next_check],
        }

    @classmethod
    def from_dict(cls, value: dict) -> "TraderDecision":
        return cls(
            decision_id=value["decision_id"],
            made_at=datetime.fromisoformat(value["made_at"]),
            action=Action(value["action"]),
            code=value.get("code"),
            thesis_id=value.get("thesis_id"),
            confidence=float(value["confidence"]),
            reasoning=value["reasoning"],
            timeframe=Timeframe(value["timeframe"]),
            decision_horizon=DecisionHorizon(value["decision_horizon"]),
            evidence_scope=EvidenceScope(value["evidence_scope"]),
            size_pct=value.get("size_pct"),
            entry_low=value.get("entry_low"),
            entry_high=value.get("entry_high"),
            stop_loss=value.get("stop_loss"),
            target_price=value.get("target_price"),
            invalidations=tuple(
                Condition.from_dict(item)
                for item in value.get("invalidations") or []),
            next_check=tuple(
                Condition.from_dict(item)
                for item in value.get("next_check") or []),
        )


@dataclass(frozen=True)
class StateTransition:
    kind: str
    subject: str
    from_status: str
    to_status: str
    at: datetime
    observation_hash: str
    reason: str

    def __post_init__(self) -> None:
        require_text(self.kind, "transition kind")
        require_text(self.subject, "transition subject")
        require_text(self.from_status, "transition from_status")
        require_text(self.to_status, "transition to_status")
        require_text(self.observation_hash, "transition observation_hash")
        require_text(self.reason, "transition reason")
        require_aware(self.at, "transition at")

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "subject": self.subject,
            "from_status": self.from_status,
            "to_status": self.to_status,
            "at": self.at.isoformat(),
            "observation_hash": self.observation_hash,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class TraderState:
    trader_id: str
    as_of: datetime
    version: int
    parent_state_hash: str | None
    _market_view_json: str
    _theme_views_json: str
    watchlist: tuple[WatchItem, ...] = ()
    theses: tuple[ThesisState, ...] = ()
    positions: tuple[PositionState, ...] = ()
    pending_orders: tuple[str, ...] = ()
    recent_decisions: tuple[TraderDecision, ...] = ()
    recent_observations: tuple[Observation, ...] = ()
    lessons: tuple[str, ...] = ()
    rules: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_text(self.trader_id, "trader_id")
        require_aware(self.as_of, "state as_of")
        if type(self.version) is not int or self.version < 0:
            raise TraderRuntimeError("state version must be a non-negative integer")
        if self.version == 0 and self.parent_state_hash is not None:
            raise TraderRuntimeError("initial state cannot have a parent hash")
        if self.version > 0:
            require_text(self.parent_state_hash, "parent_state_hash")
        for attr in ("_market_view_json", "_theme_views_json"):
            raw = getattr(self, attr)
            try:
                value = json.loads(raw)
            except (TypeError, ValueError) as exc:
                raise TraderRuntimeError(f"{attr} is not valid JSON") from exc
            if not isinstance(value, dict):
                raise TraderRuntimeError(f"{attr} must contain an object")
            object.__setattr__(self, attr, canonical_json(value))
        self._unique((item.code for item in self.watchlist), "watch codes")
        self._unique((item.thesis_id for item in self.theses), "thesis ids")
        self._unique((item.code for item in self.positions), "position codes")
        self._unique(
            (item.decision_id for item in self.recent_decisions),
            "decision ids")
        self._unique(
            (item.observation_hash for item in self.recent_observations),
            "observation hashes")

    @staticmethod
    def _unique(values, field: str) -> None:
        values = list(values)
        if len(values) != len(set(values)):
            raise TraderRuntimeError(f"duplicate {field}")

    @classmethod
    def create(
        cls, *, trader_id: str, as_of: datetime,
        market_view: dict[str, Any] | None = None,
        theme_views: dict[str, Any] | None = None,
        watchlist: tuple[WatchItem, ...] = (),
        theses: tuple[ThesisState, ...] = (),
        positions: tuple[PositionState, ...] = (),
        pending_orders: tuple[str, ...] = (),
        recent_decisions: tuple[TraderDecision, ...] = (),
        recent_observations: tuple[Observation, ...] = (),
        lessons: tuple[str, ...] = (),
        rules: tuple[str, ...] = (),
    ) -> "TraderState":
        return cls(
            trader_id=trader_id, as_of=as_of, version=0,
            parent_state_hash=None,
            _market_view_json=canonical_json(market_view or {}),
            _theme_views_json=canonical_json(theme_views or {}),
            watchlist=watchlist, theses=theses, positions=positions,
            pending_orders=pending_orders,
            recent_decisions=recent_decisions,
            recent_observations=recent_observations,
            lessons=lessons, rules=rules,
        )

    @property
    def market_view(self) -> dict:
        return json.loads(self._market_view_json)

    @property
    def theme_views(self) -> dict:
        return json.loads(self._theme_views_json)

    def _snapshot(self) -> dict:
        return {
            "schema_version": 1,
            "trader_id": self.trader_id,
            "as_of": self.as_of.isoformat(),
            "version": self.version,
            "parent_state_hash": self.parent_state_hash,
            "market_view": self.market_view,
            "theme_views": self.theme_views,
            "watchlist": [item.as_dict() for item in self.watchlist],
            "theses": [item.as_dict() for item in self.theses],
            "positions": [item.as_dict() for item in self.positions],
            "pending_orders": list(self.pending_orders),
            "recent_decisions": [
                item.as_dict() for item in self.recent_decisions],
            "recent_observations": [
                item.as_dict() for item in self.recent_observations],
            "lessons": list(self.lessons),
            "rules": list(self.rules),
        }

    @property
    def state_hash(self) -> str:
        return content_hash(self._snapshot())

    def as_dict(self) -> dict:
        return {**self._snapshot(), "state_hash": self.state_hash}

    @classmethod
    def from_dict(cls, value: dict) -> "TraderState":
        if value.get("schema_version") != 1:
            raise TraderRuntimeError("unsupported trader state schema")
        expected_hash = value.get("state_hash")
        state = cls(
            trader_id=value["trader_id"],
            as_of=datetime.fromisoformat(value["as_of"]),
            version=int(value["version"]),
            parent_state_hash=value.get("parent_state_hash"),
            _market_view_json=canonical_json(value.get("market_view") or {}),
            _theme_views_json=canonical_json(value.get("theme_views") or {}),
            watchlist=tuple(
                WatchItem.from_dict(item)
                for item in value.get("watchlist") or []),
            theses=tuple(
                ThesisState.from_dict(item)
                for item in value.get("theses") or []),
            positions=tuple(
                PositionState.from_dict(item)
                for item in value.get("positions") or []),
            pending_orders=tuple(value.get("pending_orders") or []),
            recent_decisions=tuple(
                TraderDecision.from_dict(item)
                for item in value.get("recent_decisions") or []),
            recent_observations=tuple(
                Observation.create(
                    observed_at=datetime.fromisoformat(item["observed_at"]),
                    available_at=datetime.fromisoformat(item["available_at"]),
                    type=__import__(
                        "alpha_agents.trader.types",
                        fromlist=["ObservationType"]).ObservationType(item["type"]),
                    subjects=item.get("subjects") or [],
                    data=item.get("data") or {},
                    source=item["source"],
                    evidence_refs=item.get("evidence_refs") or [],
                    timeframe=Timeframe(item["timeframe"]),
                )
                for item in value.get("recent_observations") or []),
            lessons=tuple(value.get("lessons") or []),
            rules=tuple(value.get("rules") or []),
        )
        if expected_hash is not None and state.state_hash != expected_hash:
            raise TraderRuntimeError("trader state hash mismatch")
        return state

    def evolve(
        self, *, as_of: datetime,
        watchlist: tuple[WatchItem, ...] | None = None,
        theses: tuple[ThesisState, ...] | None = None,
        positions: tuple[PositionState, ...] | None = None,
        pending_orders: tuple[str, ...] | None = None,
        recent_decisions: tuple[TraderDecision, ...] | None = None,
        recent_observations: tuple[Observation, ...] | None = None,
        market_view: dict | None = None,
        theme_views: dict | None = None,
    ) -> "TraderState":
        require_aware(as_of, "state as_of")
        if as_of < self.as_of:
            raise TraderRuntimeError("TraderState cannot move backwards in time")
        return replace(
            self,
            as_of=as_of,
            version=self.version + 1,
            parent_state_hash=self.state_hash,
            _market_view_json=(
                canonical_json(market_view)
                if market_view is not None else self._market_view_json),
            _theme_views_json=(
                canonical_json(theme_views)
                if theme_views is not None else self._theme_views_json),
            watchlist=self.watchlist if watchlist is None else watchlist,
            theses=self.theses if theses is None else theses,
            positions=self.positions if positions is None else positions,
            pending_orders=(
                self.pending_orders if pending_orders is None else pending_orders),
            recent_decisions=(
                self.recent_decisions
                if recent_decisions is None else recent_decisions),
            recent_observations=(
                self.recent_observations
                if recent_observations is None else recent_observations),
        )
