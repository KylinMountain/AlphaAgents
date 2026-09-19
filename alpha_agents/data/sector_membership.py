"""Explicit point-in-time membership archives for sector-first replay."""

from __future__ import annotations

import json
from pathlib import Path

from alpha_agents.data.sector_selection import (
    MembershipSnapshot,
    SectorSnapshotError,
)


def _snapshot(raw: dict) -> MembershipSnapshot:
    members = {
        str(sector): tuple(str(code) for code in codes)
        for sector, codes in (raw.get("members") or {}).items()
    }
    snapshot = MembershipSnapshot(
        snapshot_id=str(raw.get("snapshot_id") or "").strip(),
        available_at=str(raw.get("available_at") or "").strip(),
        source=str(raw.get("source") or "").strip(),
        sector_type=str(raw.get("sector_type") or "").strip(),
        members=members,
        point_in_time=bool(raw.get("point_in_time")),
    )
    if not snapshot.snapshot_id:
        raise SectorSnapshotError("membership snapshot_id is required")
    if not snapshot.available_at:
        raise SectorSnapshotError("membership available_at is required")
    if not snapshot.source:
        raise SectorSnapshotError("membership source is required")
    if not snapshot.sector_type:
        raise SectorSnapshotError("membership sector_type is required")
    if not snapshot.members:
        raise SectorSnapshotError("membership members cannot be empty")
    return snapshot


def load(path: Path) -> tuple[MembershipSnapshot, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw = payload.get("snapshots") if isinstance(payload, dict) else None
    if raw is None:
        raw = [payload]
    if not isinstance(raw, list) or not raw:
        raise SectorSnapshotError("membership archive needs snapshot rows")
    snapshots = tuple(sorted(
        (_snapshot(item) for item in raw),
        key=lambda item: (item.available_at, item.snapshot_id),
    ))
    ids = [item.snapshot_id for item in snapshots]
    if len(ids) != len(set(ids)):
        raise SectorSnapshotError("membership snapshot_id must be unique")
    return snapshots


def as_of(archive: tuple[MembershipSnapshot, ...],
          decision_at: str) -> MembershipSnapshot:
    eligible = [
        item for item in archive
        if item.available_at <= decision_at
    ]
    if not eligible:
        raise SectorSnapshotError(
            f"no membership snapshot available at {decision_at}")
    snapshot = eligible[-1]
    snapshot.validate(decision_at, strict_pit=True)
    return snapshot


def concepts_by_code(snapshot: MembershipSnapshot) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for sector in sorted(snapshot.members):
        for code in snapshot.members[sector]:
            out.setdefault(code, []).append(sector)
    return out



def by_id(archive: tuple[MembershipSnapshot, ...], snapshot_id: str,
          expected_hash: str | None = None) -> MembershipSnapshot:
    matches = [item for item in archive if item.snapshot_id == snapshot_id]
    if len(matches) != 1:
        raise SectorSnapshotError(
            f"membership snapshot {snapshot_id!r} is not uniquely available")
    snapshot = matches[0]
    if expected_hash is not None and snapshot.content_hash != expected_hash:
        raise SectorSnapshotError(
            f"membership snapshot {snapshot_id!r} hash mismatch")
    return snapshot
