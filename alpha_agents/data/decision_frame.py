"""Immutable model-bound input, not a reconstructed market snapshot.

Hashes identify content, not whether it was point-in-time correct. A frame
with tools contains the initial input only, not the ensuing tool trajectory.
TraderState is the context shown, not a complete portfolio checkpoint.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from zoneinfo import ZoneInfo

VERSION = 1
_TZ = ZoneInfo("Asia/Shanghai")
_HASH_FIELDS = {"input_hash", "state_snapshot_hash", "policy_build_hash", "frame_hash"}


class FrameError(ValueError):
    """Incomplete, corrupted or unsupported input; never fill it from today."""


def canonical(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise FrameError("Frame content is not finite JSON") from exc


def fingerprint(value) -> str:
    return "sha256:" + hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _text(value, name: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise FrameError(f"{name} must be an explicit string")
    return value


def _keys(value, keys: set[str], name: str) -> None:
    if not isinstance(value, dict) or set(value) != keys:
        raise FrameError(f"Unexpected {name} fields")


@dataclass(frozen=True)
class TraderState:
    """A canonical string prevents mutation through a nested dictionary."""
    _json: str

    def __post_init__(self):
        try:
            value = json.loads(self._json)
        except (TypeError, ValueError) as exc:
            raise FrameError("Invalid trader state") from exc
        _keys(value, {"book", "knowledge", "trader_note"}, "trader state")
        for key, text in value.items():
            _text(text, key, empty=True)
        object.__setattr__(self, "_json", canonical(value))

    @classmethod
    def create(cls, *, book: str = "", knowledge: str = "", trader_note: str = ""):
        return cls(canonical({"book": book, "knowledge": knowledge,
                              "trader_note": trader_note}))

    def as_dict(self) -> dict:
        return json.loads(self._json)

    @property
    def snapshot_hash(self) -> str:
        return fingerprint(self.as_dict())


def _validate(body: dict) -> None:
    _keys(body, {"schema_version", "identity", "state", "request", "producer",
                 "panel_codes", "provenance"}, "frame")
    if type(body["schema_version"]) is not int or body["schema_version"] != VERSION:
        raise FrameError("Unsupported frame schema")
    ident = body["identity"]
    _keys(ident, {"run_id", "trader_id", "stage", "phase", "session_day",
                  "information_cutoff", "information_grade", "origin"}, "identity")
    for key, value in ident.items():
        _text(value, key)
    cutoff = ident["information_cutoff"]
    try:
        instant = datetime.fromisoformat(cutoff)
        if len(cutoff) < 16:
            raise ValueError("A time is required")
        if instant.tzinfo is not None:
            instant = instant.astimezone(_TZ)
        if instant.date().isoformat() != ident["session_day"]:
            raise ValueError("Cutoff and exchange day disagree")
    except ValueError as exc:
        raise FrameError("Invalid information cutoff/exchange day") from exc
    TraderState(canonical(body["state"]))
    req = body["request"]
    _keys(req, {"agent_name", "instructions", "message", "max_turns", "tools",
                "model", "model_settings"}, "request")
    for key in ("agent_name", "instructions", "message", "model"):
        _text(req[key], key)
    if type(req["max_turns"]) is not int or req["max_turns"] <= 0:
        raise FrameError("max_turns must be a positive integer")
    for key, values in (("tools", req["tools"]), ("panel_codes", body["panel_codes"])):
        if not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values):
            raise FrameError(f"Invalid {key}")
        if len(values) != len(set(values)):
            raise FrameError(f"Duplicate {key}")
    if req["model_settings"] is not None and not isinstance(req["model_settings"], dict):
        raise FrameError("Invalid model settings")
    if not isinstance(body["producer"], dict):
        raise FrameError("Missing producer identity")
    for key in ("contract", "code_hash"):
        _text(body["producer"].get(key), key)
    if not isinstance(body["provenance"], dict):
        raise FrameError("Invalid provenance")


def _seal(body: dict) -> dict:
    _validate(body)
    req = body["request"]
    hashes = {
        "input_hash": fingerprint(req),
        "state_snapshot_hash": fingerprint(body["state"]),
        "policy_build_hash": fingerprint({
            "producer": body["producer"],
            **{key: req[key] for key in ("instructions", "model", "model_settings",
                                         "tools", "max_turns")},
        }),
    }
    return {**body, **hashes, "frame_hash": fingerprint({**body, **hashes})}


@dataclass(frozen=True)
class DecisionFrame:
    _json: str

    def __post_init__(self):
        try:
            value = json.loads(self._json)
        except (TypeError, ValueError) as exc:
            raise FrameError("Invalid frame JSON") from exc
        if not isinstance(value, dict):
            raise FrameError("Frame must be an object")
        body = {k: v for k, v in value.items() if k not in _HASH_FIELDS}
        if _seal(body) != value:
            raise FrameError("Decision frame hash mismatch")
        object.__setattr__(self, "_json", canonical(value))

    @classmethod
    def create(cls, *, identity: dict, state: TraderState, request: dict,
               producer: dict, panel_codes: list[str], provenance: dict | None = None):
        body = {"schema_version": VERSION, "identity": identity,
                "state": state.as_dict(), "request": request, "producer": producer,
                "panel_codes": panel_codes, "provenance": provenance or {}}
        return cls(canonical(_seal(body)))

    @classmethod
    def from_dict(cls, value: dict):
        return cls(canonical(value))

    def as_dict(self) -> dict:
        return json.loads(self._json)

    @property
    def frame_hash(self) -> str:
        return self.as_dict()["frame_hash"]

    def change_knowledge(self, *, old: str, new: str, name: str) -> DecisionFrame:
        """One exact intervention; ambiguity is an error, never a fuzzy edit."""
        _text(old, "old")
        _text(new, "new", empty=True)
        _text(name, "intervention name")
        value = self.as_dict()
        knowledge, message = value["state"]["knowledge"], value["request"]["message"]
        if old == new or knowledge.count(old) != 1:
            raise FrameError("Intervention must change one unique knowledge fragment")
        if not knowledge or message.count(knowledge) != 1:
            raise FrameError("Knowledge block is absent or ambiguous in frozen input")
        changed = knowledge.replace(old, new, 1)
        value["state"]["knowledge"] = changed
        value["request"]["message"] = message.replace(knowledge, changed, 1)
        body = {k: v for k, v in value.items() if k not in _HASH_FIELDS}
        body["provenance"] = {
            "parent_frame_hash": self.frame_hash,
            "intervention": {"name": name, "field": "knowledge", "old": old, "new": new},
        }
        return DecisionFrame(canonical(_seal(body)))
