"""Core vocabulary for the timeframe-agnostic Trader Runtime.

This package is the domain layer. It knows neither SQLite nor providers nor
market APIs. Daily replay and live intraday execution differ in the stream of
facts they feed it, not in the vocabulary of the trader.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import hashlib
import json
import math
from typing import Any


class TraderRuntimeError(ValueError):
    """A malformed or causally impossible trader input."""


class Timeframe(str, Enum):
    DAILY = "1d"
    HOUR_60 = "60m"
    MINUTE_30 = "30m"
    MINUTE_5 = "5m"
    TICK = "tick"


class EvidenceScope(str, Enum):
    REPLAY_DAILY = "replay_daily"
    REPLAY_INTRADAY = "replay_intraday"
    LIVE_DAILY = "live_daily"
    LIVE_INTRADAY = "live_intraday"


class DecisionHorizon(str, Enum):
    INTRADAY = "intraday"
    DAY = "1d"
    SWING = "3-5d"
    POSITION = "position"


class Session(str, Enum):
    PRE_OPEN = "pre_open"
    OPEN = "open"
    INTRADAY = "intraday"
    CLOSE = "close"


class Action(str, Enum):
    BUY = "buy"
    ADD = "add"
    HOLD = "hold"
    WAIT = "wait"
    REDUCE = "reduce"
    SELL = "sell"
    REJECT = "reject"


class ObservationType(str, Enum):
    MARKET_OPEN = "market_open"
    MARKET_CLOSE = "market_close"
    DAILY_BAR = "daily_bar"
    PRICE_MOVE = "price_move"
    VOLUME_CHANGE = "volume_change"
    SECTOR_FLOW = "sector_flow"
    THEME_CHANGE = "theme_change"
    NEWS = "news"
    EVENT = "event"
    ORDER_FILLED = "order_filled"
    POSITION_CHANGED = "position_changed"
    THESIS_SIGNAL = "thesis_signal"


class WatchStatus(str, Enum):
    WATCHING = "watching"
    TRIGGERED = "triggered"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CONVERTED = "converted"


class ThesisLevel(str, Enum):
    MARKET = "market"
    TRADE = "trade"
    EXECUTION = "execution"


class ThesisStatus(str, Enum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    STRENGTHENED = "strengthened"
    WEAKENED = "weakened"
    INVALIDATED = "invalidated"
    CLOSED = "closed"


class CompareOp(str, Enum):
    LT = "<"
    LE = "<="
    GT = ">"
    GE = ">="
    EQ = "=="
    NE = "!="


def _finite_json(value: Any) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TraderRuntimeError("non-finite number is not valid trader state")
        return
    if isinstance(value, list):
        for item in value:
            _finite_json(item)
        return
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise TraderRuntimeError("trader JSON object keys must be strings")
        for item in value.values():
            _finite_json(item)
        return
    raise TraderRuntimeError(
        f"unsupported trader JSON value {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Canonical, finite JSON used for immutable nested payloads and hashes."""
    _finite_json(value)
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False)


def content_hash(value: Any) -> str:
    blob = canonical_json(value).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def require_aware(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TraderRuntimeError(f"{field} must be a timezone-aware datetime")
    return value


def require_text(value: str, field: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise TraderRuntimeError(f"{field} must be an explicit string")
    return value


@dataclass(frozen=True)
class DecisionContext:
    """The causal boundary for one Trader Runtime decision.

    mode is provenance, not a strategy switch. A replay and a live call with
    the same state and observations enter the same runtime logic.
    """

    mode: str
    observation_resolution: Timeframe
    decision_horizon: DecisionHorizon
    session: Session
    information_cutoff: datetime
    evidence_scope: EvidenceScope

    def __post_init__(self) -> None:
        if self.mode not in {"replay", "live"}:
            raise TraderRuntimeError("mode must be replay or live")
        require_aware(self.information_cutoff, "information_cutoff")
        if self.mode == "replay" and not self.evidence_scope.value.startswith("replay_"):
            raise TraderRuntimeError("replay context needs replay evidence scope")
        if self.mode == "live" and not self.evidence_scope.value.startswith("live_"):
            raise TraderRuntimeError("live context needs live evidence scope")

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "observation_resolution": self.observation_resolution.value,
            "decision_horizon": self.decision_horizon.value,
            "session": self.session.value,
            "information_cutoff": self.information_cutoff.isoformat(),
            "evidence_scope": self.evidence_scope.value,
        }
