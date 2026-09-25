"""Original session observations, isolated by run, trader and exchange day."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("Asia/Shanghai")
KINDS = frozenset({"morning_input", "entry_observation"})


def instant() -> str:
    """Logical exchange-local time; loss of a replay context must fail closed."""
    from alpha_agents.evolution.replay_mode import get_replay_as_of, replay_process
    from alpha_agents.data.clock import LookAheadError
    value = get_replay_as_of()
    if value is None and replay_process():
        raise LookAheadError("Session memory lost its replay instant")
    if value:
        now = datetime.fromisoformat(value + " 23:59:59.999999" if len(value) == 10 else value)
        if now.tzinfo:
            now = now.astimezone(_TZ).replace(tzinfo=None)
    else:
        now = datetime.now(_TZ).replace(tzinfo=None)
    return now.isoformat(sep=" ", timespec="microseconds")


def namespace(run_id: str | None = None) -> str:
    return _identity(run_id if run_id is not None else
                     os.environ.get("ALPHAAGENTS_RUN_ID", "live"))


def _identity(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("An explicit non-empty session identity is required")
    return value.strip()


def _hash(run, trader, kind, stamp, code, payload) -> str:
    raw = json.dumps([run, trader, kind, stamp, code, payload], ensure_ascii=False,
                     sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def append(*, trader_id: str, kind: str, payload: dict, code: str = "",
           run_id: str | None = None, conn: sqlite3.Connection | None = None) -> str:
    """Store before awaiting a provider; explicit connections own their transaction."""
    if kind not in KINDS or not isinstance(payload, dict):
        raise ValueError("Invalid session observation")
    run, trader, at = namespace(run_id), _identity(trader_id), instant()
    digest = _hash(run, trader, kind, at, code, payload)
    owned = conn is None
    if conn is None:
        from alpha_agents.data.memory_store import _get_conn
        conn = _get_conn()
    conn.execute("INSERT OR IGNORE INTO trader_session_events "
                 "(id,run_id,trader_id,session_day,kind,code,observed_at,payload_json) "
                 "VALUES (?,?,?,?,?,?,?,?)", (digest, run, trader, at[:10], kind,
                 code, at, json.dumps(payload, ensure_ascii=False, allow_nan=False)))
    if owned:
        conn.commit()
    return digest


def read(*, trader_id: str, kind: str, day: str | None = None,
         code: str | None = None, run_id: str | None = None,
         conn: sqlite3.Connection | None = None) -> list[dict]:
    """Do not substitute another book, run, day or future observation."""
    run, trader, at = namespace(run_id), _identity(trader_id), instant()
    if kind not in KINDS:
        raise ValueError("Invalid observation kind")
    if conn is None:
        from alpha_agents.data.memory_store import _get_conn
        conn = _get_conn()
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='trader_session_events'").fetchone():
        return []
    sql = ("SELECT id,observed_at,code,payload_json FROM trader_session_events "
           "WHERE run_id=? AND trader_id=? AND session_day=? AND kind=? AND observed_at<=?")
    args = [run, trader, day or at[:10], kind, at]
    if code is not None:
        sql += " AND code=?"
        args.append(code)
    out = []
    for digest, stamp, symbol, raw in conn.execute(sql + " ORDER BY observed_at,rowid", args):
        payload = json.loads(raw)
        if digest != _hash(run, trader, kind, stamp, symbol, payload):
            raise ValueError("Session observation hash mismatch")
        out.append({"id": digest, "observed_at": stamp, "code": symbol, "payload": payload})
    return out
