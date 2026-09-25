"""Immutable facts presented to a Trader Runtime step."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from typing import Any

from alpha_agents.trader.types import (
    ObservationType, Timeframe, TraderRuntimeError, canonical_json,
    content_hash, require_aware, require_text,
)


@dataclass(frozen=True)
class Observation:
    """One fact and the time at which the trader was allowed to know it."""

    observed_at: datetime
    available_at: datetime
    type: ObservationType
    subjects: tuple[str, ...]
    source: str
    evidence_refs: tuple[str, ...]
    timeframe: Timeframe
    _data_json: str

    def __post_init__(self) -> None:
        require_aware(self.observed_at, "observed_at")
        require_aware(self.available_at, "available_at")
        if self.available_at < self.observed_at:
            raise TraderRuntimeError(
                "available_at cannot precede the event it reports")
        require_text(self.source, "source")
        if any(not isinstance(value, str) or not value.strip()
               for value in self.subjects):
            raise TraderRuntimeError("subjects must be non-empty strings")
        if len(self.subjects) != len(set(self.subjects)):
            raise TraderRuntimeError("subjects must be unique")
        if any(not isinstance(value, str) or not value.strip()
               for value in self.evidence_refs):
            raise TraderRuntimeError("evidence_refs must be non-empty strings")
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise TraderRuntimeError("evidence_refs must be unique")
        try:
            data = json.loads(self._data_json)
        except (TypeError, ValueError) as exc:
            raise TraderRuntimeError("observation data is not valid JSON") from exc
        if not isinstance(data, dict):
            raise TraderRuntimeError("observation data must be an object")
        object.__setattr__(self, "_data_json", canonical_json(data))

    @classmethod
    def create(
        cls, *, observed_at: datetime, available_at: datetime,
        type: ObservationType, subjects: tuple[str, ...] | list[str] = (),
        data: dict[str, Any] | None = None, source: str,
        evidence_refs: tuple[str, ...] | list[str] = (),
        timeframe: Timeframe,
    ) -> "Observation":
        return cls(
            observed_at=observed_at,
            available_at=available_at,
            type=type,
            subjects=tuple(subjects),
            source=source,
            evidence_refs=tuple(evidence_refs),
            timeframe=timeframe,
            _data_json=canonical_json(data or {}),
        )

    @property
    def data(self) -> dict[str, Any]:
        return json.loads(self._data_json)

    def as_dict(self) -> dict:
        return {
            "observed_at": self.observed_at.isoformat(),
            "available_at": self.available_at.isoformat(),
            "type": self.type.value,
            "subjects": list(self.subjects),
            "source": self.source,
            "evidence_refs": list(self.evidence_refs),
            "timeframe": self.timeframe.value,
            "data": self.data,
        }

    @property
    def observation_hash(self) -> str:
        return content_hash(self.as_dict())
