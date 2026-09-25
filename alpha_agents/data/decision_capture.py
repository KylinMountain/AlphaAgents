"""Append-only model input/output, independent of mutable account projections."""
from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
from uuid import uuid4

from alpha_agents.data import trader_session
from alpha_agents.data.decision_frame import DecisionFrame, FrameError


@dataclass
class Capture:
    frame: DecisionFrame
    enabled: bool = True
    output: dict | None = None
    invocation_id: str | None = None

    def _append(self, kind: str, payload: dict) -> None:
        from alpha_agents.data.memory_store import _get_conn
        ident = self.frame.as_dict()["identity"]
        at = trader_session.instant()
        run, trader = ident["run_id"], ident["trader_id"]
        digest = trader_session._hash(run, trader, kind, at, "", payload)
        conn = _get_conn()
        if conn.in_transaction:
            raise FrameError("Decision capture cannot commit an unrelated transaction")
        conn.execute("INSERT INTO decision_capture_events "
                     "(id,invocation_id,run_id,trader_id,session_day,kind,observed_at,payload_json) "
                     "VALUES (?,?,?,?,?,?,?,?)", (digest, self.invocation_id, run, trader,
                     ident["session_day"], kind, at, json.dumps(payload, ensure_ascii=False,
                                                              allow_nan=False)))
        conn.commit()

    def __enter__(self):
        if self.enabled:
            self.invocation_id = uuid4().hex
            self._append("decision_input", {"invocation_id": self.invocation_id,
                                            "frame": self.frame.as_dict()})
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.enabled:
            self._append("decision_output", {
                "invocation_id": self.invocation_id, "frame_hash": self.frame.frame_hash,
                "status": "failed" if exc_type else ("answered" if self.output is not None
                                                       else "missing_output"),
                "error_type": exc_type.__name__ if exc_type else None,
                "output": self.output,
            })
        return False


def read_inputs(conn: sqlite3.Connection, *, run_id: str | None = None,
                trader_id: str | None = None, invocation_id: str | None = None,
                limit: int = 100) -> list[dict]:
    """Research/export only; no schema initialization or current-world queries.

    Historical exports must never be used as unrestricted decision context.
    Verify both the original event hash and the nested frame's identities.
    """
    if type(limit) is not int or not 1 <= limit <= 10000:
        raise ValueError("limit must be between 1 and 10000")
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND "
                        "name='decision_capture_events'").fetchone():
        return []
    where, args = ["kind='decision_input'"], []
    for column, val in (("run_id", run_id), ("trader_id", trader_id),
                        ("invocation_id", invocation_id)):
        if val is not None:
            where.append(f"{column}=?")
            args.append(val)
    rows = conn.execute(
        "SELECT id,run_id,trader_id,kind,observed_at,code,payload_json "
        "FROM decision_capture_events WHERE " + " AND ".join(where)
        + " ORDER BY observed_at DESC,rowid DESC LIMIT ?", [*args, limit])
    out = []
    for digest, run, trader, kind, stamp, code, text in rows:
        payload = json.loads(text)
        if digest != trader_session._hash(run, trader, kind, stamp, code, payload):
            raise FrameError("Decision observation hash mismatch")
        frame = DecisionFrame.from_dict(payload["frame"])
        ident = frame.as_dict()["identity"]
        if (ident["run_id"], ident["trader_id"]) != (run, trader):
            raise FrameError("Decision observation identity mismatch")
        out.append({"invocation_id": payload["invocation_id"], "observed_at": stamp,
                    "frame": frame.as_dict()})
    return out
