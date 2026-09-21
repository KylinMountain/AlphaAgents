"""Concept-membership archives for sector-first replay.

Two ways in, and the difference between them is the whole point:

* :func:`load` reads an **explicit point-in-time archive** supplied by the
  operator. Every snapshot carries the instant it became knowable, and
  ``as_of`` picks the newest one at or before the decision. This is the only
  path that is strict-PIT eligible.
* :func:`current_from_corpus` builds one snapshot from ``stocks.db``'s
  ``concept_stocks``, which has **no date column**. The result is what each
  name is tagged with *now*, applied to every replayed session. That is a
  real lookahead and it is labelled as one: ``point_in_time=False``, which
  makes ``as_of`` refuse it under ``strict_pit`` unless the caller has
  explicitly opted out.

Why the second path exists at all. The owner asked for it on 2026-09-21:
"实在搞不到就用历史的概念也问题不大" — the only dated membership source
available is Tushare's ``concept`` / ``concept_cons``, and this account is
rate-limited to **one call per hour** on both, so a 375-concept backfill would
take about sixteen days. Without a fallback the sector replay cannot run at
all, and "cannot run" is not more honest than "runs with a stated lookahead".

The caveat is not carried only in this docstring. ``replay_capabilities``
records ``concept_membership: current_only`` with ``point_in_time: False``,
``_limitations`` states it in every report, and the panel column is labelled
概念（当前成分）. A reader who never opens this file still meets the limit.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from pathlib import Path

from alpha_agents.config import DATA_DIR
from alpha_agents.data import corpus_access
from alpha_agents.data.sector_selection import (
    MembershipSnapshot,
    SectorSnapshotError,
)

logger = logging.getLogger(__name__)

#: Stamped into every current-only snapshot, so a reader can tell from the id
#: alone that this membership was not knowable at the replayed instant.
CURRENT_ONLY_SOURCE = "stocks.db:concept_stocks (no as-of column)"


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


def current_from_corpus(*, as_of: str | None = None,
                        db_path: Path | None = None,
                        max_concepts_per_name: int | None = None
                        ) -> tuple[MembershipSnapshot, ...]:
    """One snapshot of *today's* concept membership, for a replay to use.

    ``point_in_time=False``, so this is refused by :func:`as_of` under
    ``strict_pit`` unless the caller explicitly passes ``strict_pit=False``.
    That is deliberate: the opt-out should be a line somebody wrote, not a
    default somebody inherited.

    ``as_of`` is the timestamp stamped on the snapshot. It defaults to the
    empty string rather than to today, because the caller is replaying a past
    session and "available at" must not claim a knowability the data does not
    have — with ``point_in_time=False`` the value is documentation, and a
    fake recent date would read as though the snapshot were fresh.

    A missing or unreadable ``concept_stocks`` raises rather than returning an
    empty archive: an empty membership would make every sector have no
    candidates, and a replay that silently selects nothing looks like a
    strategy result.
    """
    path = db_path or (DATA_DIR / "stocks.db")
    try:
        conn = corpus_access.connect(path)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        raise SectorSnapshotError(
            f"cannot open {path} for concept membership: {exc}") from exc
    try:
        rows = conn.execute(
            "SELECT c.name AS sector, cs.stock_code AS code "
            "  FROM concept_stocks cs "
            "  JOIN concepts c ON c.id = cs.concept_id "
            " ORDER BY c.name, cs.stock_code").fetchall()
    except sqlite3.Error as exc:
        raise SectorSnapshotError(
            f"cannot read concept membership from {path}: {exc}") from exc
    finally:
        conn.close()

    members: dict[str, list[str]] = {}
    for row in rows:
        sector = str(row["sector"] or "").strip()
        code = str(row["code"] or "").strip()
        if sector and code:
            members.setdefault(sector, []).append(code)
    if not members:
        raise SectorSnapshotError(
            f"{path} holds no concept membership; a sector replay needs a "
            "non-empty universe or it would report an empty strategy")

    snapshot = MembershipSnapshot(
        snapshot_id="current-only",
        available_at=str(as_of or ""),
        source=CURRENT_ONLY_SOURCE,
        sector_type="concept",
        members={key: tuple(value) for key, value in members.items()},
        point_in_time=False,
    )
    logger.info(
        "Concept membership: %d sectors, %d memberships, from %s "
        "(current snapshot, not point-in-time)",
        len(snapshot.members), sum(len(v) for v in snapshot.members.values()),
        path)
    return (snapshot,)


def as_of(archive: tuple[MembershipSnapshot, ...],
          decision_at: str, *, strict_pit: bool = True
          ) -> MembershipSnapshot:
    """The newest snapshot knowable at ``decision_at``.

    ``strict_pit`` defaults to True and refuses a current-only snapshot. A
    caller replaying with today's membership has to say so by passing
    ``strict_pit=False``; the requirement is that the choice is visible at the
    call site, because it is the difference between evidence about the
    strategy and evidence about a hindsight-informed version of it.
    """
    eligible = [
        item for item in archive
        if item.available_at <= decision_at
    ]
    if not eligible:
        # A current-only snapshot carries no meaningful availability time, so
        # it is always eligible. Said here rather than by stamping it with a
        # fake ancient date, which would corrupt the content hash.
        eligible = [item for item in archive if not item.point_in_time]
    if not eligible:
        raise SectorSnapshotError(
            f"no membership snapshot available at {decision_at}")
    snapshot = eligible[-1]
    snapshot.validate(decision_at, strict_pit=strict_pit)
    return snapshot


def concepts_by_code(snapshot: MembershipSnapshot) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for sector in sorted(snapshot.members):
        for code in snapshot.members[sector]:
            out.setdefault(code, []).append(sector)
    return out



def relation_evidence_id(snapshot: MembershipSnapshot, *,
                         sector_id: str, code: str) -> str:
    """Stable proof that a code belonged to a sector in this PIT world."""
    sector = str(sector_id or "").strip()
    stock = str(code or "").strip()
    members = set(snapshot.members.get(sector, ()))
    if not sector or not stock or stock not in members:
        raise SectorSnapshotError(
            f"{stock!r} is not a member of {sector!r} in "
            f"snapshot {snapshot.snapshot_id!r}")
    payload = {
        "snapshot_id": snapshot.snapshot_id,
        "membership_hash": snapshot.content_hash,
        "sector_id": sector,
        "code": stock,
    }
    blob = json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()

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
