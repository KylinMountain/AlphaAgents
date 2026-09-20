"""Freeze B-arm direction choices for the C-arm stock-selection ablation.

The C arm must not re-sample the direction model. This archive is exported from
the append-only direction journal of a completed sector_first_v0 run and then
replayed by exact day, membership hash and shortlist.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from alpha_agents.data import memory_store, theme_opportunity_journal


SCHEMA_VERSION = 1
SOURCE_ARCHITECTURE = "sector_first_v0"


class FrozenDirectionError(ValueError):
    pass


def _dump(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False)


def _hash(value) -> str:
    return hashlib.sha256(_dump(value).encode("utf-8")).hexdigest()


def export(*, run_id: str,
           conn: sqlite3.Connection | None = None) -> dict:
    target = conn if conn is not None else memory_store._get_conn()
    theme_opportunity_journal.init_schema(target)
    rows = target.execute(
        "SELECT * FROM theme_opportunity_sets "
        "WHERE run_id=? AND architecture=? AND phase='open' "
        "ORDER BY day,id",
        (str(run_id), SOURCE_ARCHITECTURE),
    ).fetchall()
    if not rows:
        raise FrozenDirectionError(
            f"run {run_id!r} has no {SOURCE_ARCHITECTURE} open decisions")

    days = {}
    for row in rows:
        day = str(row["day"])[:10]
        if day in days:
            raise FrozenDirectionError(
                f"run {run_id!r} has multiple open direction sets on {day}")
        item_rows = target.execute(
            "SELECT snapshot_json FROM theme_opportunity_items "
            "WHERE theme_opportunity_set_id=? ORDER BY id",
            (row["id"],),
        ).fetchall()
        memberships = set()
        for item in item_rows:
            snapshot = json.loads(item["snapshot_json"])
            snapshot_id = str(
                snapshot.get("membership_snapshot_id") or "").strip()
            snapshot_hash = str(snapshot.get("membership_hash") or "").strip()
            if snapshot_id and snapshot_hash:
                memberships.add((snapshot_id, snapshot_hash))
        if len(memberships) != 1:
            raise FrozenDirectionError(
                f"direction set #{row['id']} does not freeze exactly one "
                "membership snapshot")
        membership_id, membership_hash = next(iter(memberships))
        days[day] = {
            "day": day,
            "information_cutoff": str(row["information_cutoff"]),
            "membership_snapshot_id": membership_id,
            "membership_hash": membership_hash,
            "shortlist": json.loads(row["shortlist_json"]),
            "selected": json.loads(row["selected_json"]),
            "source_set_id": int(row["id"]),
            "source_set_hash": str(row["content_hash"]),
        }

    body = {
        "schema_version": SCHEMA_VERSION,
        "source_run_id": str(run_id),
        "source_architecture": SOURCE_ARCHITECTURE,
        "days": days,
    }
    return {**body, "archive_hash": _hash(body)}


def write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def load(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise FrozenDirectionError(
            f"unsupported frozen-direction schema "
            f"{payload.get('schema_version')!r}")
    if payload.get("source_architecture") != SOURCE_ARCHITECTURE:
        raise FrozenDirectionError(
            "C arm requires directions frozen from sector_first_v0")
    archive_hash = str(payload.get("archive_hash") or "")
    body = {key: value for key, value in payload.items()
            if key != "archive_hash"}
    expected = _hash(body)
    if archive_hash != expected:
        raise FrozenDirectionError("frozen-direction archive hash mismatch")
    if not isinstance(payload.get("days"), dict) or not payload["days"]:
        raise FrozenDirectionError("frozen-direction archive has no days")
    return payload


def decision_for(archive: dict, *, day: str,
                 information_cutoff: str,
                 membership_snapshot_id: str,
                 membership_hash: str,
                 shortlist: list[str]) -> dict:
    row = (archive.get("days") or {}).get(str(day)[:10])
    if row is None:
        raise FrozenDirectionError(
            f"no frozen B-arm direction decision for {str(day)[:10]}")
    checks = {
        "information_cutoff": information_cutoff,
        "membership_snapshot_id": membership_snapshot_id,
        "membership_hash": membership_hash,
    }
    for field, actual in checks.items():
        if str(row.get(field) or "") != str(actual):
            raise FrozenDirectionError(
                f"frozen direction {field} mismatch on {day}: "
                f"{row.get(field)!r} != {actual!r}")
    frozen_shortlist = [str(value) for value in row.get("shortlist") or []]
    current_shortlist = [str(value) for value in shortlist]
    if frozen_shortlist != current_shortlist:
        raise FrozenDirectionError(
            f"sector shortlist changed on {day}; C cannot isolate stock "
            "selection on a different direction world")
    selected = [str(value) for value in row.get("selected") or []]
    outside = [value for value in selected if value not in frozen_shortlist]
    if outside:
        raise FrozenDirectionError(
            f"frozen selected directions left their shortlist: {outside}")
    return {
        **row,
        "selected": selected,
        "archive_hash": archive["archive_hash"],
        "source_run_id": archive["source_run_id"],
    }
