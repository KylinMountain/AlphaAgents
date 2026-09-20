"""PIT membership archive tests."""

import json

import pytest

from alpha_agents.data import sector_membership as M
from alpha_agents.data.sector_selection import SectorSnapshotError


def _write(tmp_path, snapshots):
    path = tmp_path / "membership.json"
    path.write_text(
        json.dumps({"snapshots": snapshots}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def test_as_of_uses_latest_snapshot_known_at_decision(tmp_path):
    path = _write(tmp_path, [
        {
            "snapshot_id": "m1",
            "available_at": "2026-01-01 00:00:00",
            "source": "fixture",
            "sector_type": "concept",
            "point_in_time": True,
            "members": {"AI": ["600001"]},
        },
        {
            "snapshot_id": "m2",
            "available_at": "2026-02-01 00:00:00",
            "source": "fixture",
            "sector_type": "concept",
            "point_in_time": True,
            "members": {"AI": ["600001", "600002"]},
        },
    ])
    archive = M.load(path)
    assert M.as_of(archive, "2026-01-15 09:00:00").snapshot_id == "m1"
    assert M.as_of(archive, "2026-02-15 09:00:00").snapshot_id == "m2"


def test_current_only_snapshot_is_refused(tmp_path):
    path = _write(tmp_path, [{
        "snapshot_id": "current",
        "available_at": "2026-01-01 00:00:00",
        "source": "stocks.db",
        "sector_type": "concept",
        "point_in_time": False,
        "members": {"AI": ["600001"]},
    }])
    with pytest.raises(SectorSnapshotError, match="point-in-time"):
        M.as_of(M.load(path), "2026-01-15 09:00:00")


def test_no_future_snapshot_is_backfilled(tmp_path):
    path = _write(tmp_path, [{
        "snapshot_id": "future",
        "available_at": "2026-02-01 00:00:00",
        "source": "fixture",
        "sector_type": "concept",
        "point_in_time": True,
        "members": {"AI": ["600001"]},
    }])
    with pytest.raises(SectorSnapshotError, match="no membership snapshot"):
        M.as_of(M.load(path), "2026-01-15 09:00:00")


def test_concepts_by_code_inverts_the_same_snapshot(tmp_path):
    path = _write(tmp_path, [{
        "snapshot_id": "m1",
        "available_at": "2026-01-01 00:00:00",
        "source": "fixture",
        "sector_type": "concept",
        "point_in_time": True,
        "members": {
            "AI": ["600001", "600002"],
            "算力": ["600001"],
        },
    }])
    snapshot = M.load(path)[0]
    assert M.concepts_by_code(snapshot)["600001"] == ["AI", "算力"]



def test_relation_evidence_id_is_stable_and_membership_bound(tmp_path):
    path = _write(tmp_path, [{
        "snapshot_id": "m1",
        "available_at": "2026-01-01 00:00:00",
        "source": "fixture",
        "sector_type": "concept",
        "point_in_time": True,
        "members": {
            "AI": ["600001", "600002"],
            "算力": ["600001"],
        },
    }])
    snapshot = M.load(path)[0]
    first = M.relation_evidence_id(
        snapshot, sector_id="AI", code="600001")
    second = M.relation_evidence_id(
        snapshot, sector_id="AI", code="600001")
    other = M.relation_evidence_id(
        snapshot, sector_id="算力", code="600001")

    assert first == second
    assert first != other
    with pytest.raises(SectorSnapshotError, match="not a member"):
        M.relation_evidence_id(
            snapshot, sector_id="算力", code="600002")
