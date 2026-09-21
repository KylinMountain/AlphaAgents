"""``--allow-current-membership`` must reach the decision read set too.

``_decision_world_read_set`` called ``sector_membership.as_of`` without
``strict_pit``, so it took the default (True) and refused a current-only
archive — while ``_sector_cards`` passed the flag and accepted one. A run
started with the flag therefore built its panel fine and then died inside the
decider with "strict sector replay requires point-in-time membership", so the
flag was unusable for every architecture that reaches this function.

``as_of`` says in its own docstring that the choice has to be visible at the
call site. These tests hold that: the permissive call site now states it, and
the guard underneath is unchanged.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import walk_forward as wf  # noqa: E402

from alpha_agents.data import sector_membership  # noqa: E402
from alpha_agents.data.sector_selection import (  # noqa: E402
    MembershipSnapshot, SectorSnapshotError)


class _Ctx:
    """Only the attributes ``_decision_world_read_set`` actually reads."""
    input_identity = {"input_hash": "test"}
    selection_architecture = "sector_first_v0"

    def __init__(self, archive):
        self.sector_membership_archive = archive


def _snapshot(*, point_in_time: bool, available_at: str) -> MembershipSnapshot:
    return MembershipSnapshot(
        snapshot_id="current" if not point_in_time else "2026-01-02",
        available_at=available_at,
        source="stocks.db:concept_stocks",
        sector_type="concept",
        members={"人工智能": ("600519",)},
        point_in_time=point_in_time,
    )


def _current_only():
    # A current-only snapshot carries no meaningful availability time.
    return (_snapshot(point_in_time=False, available_at=""),)


def _read_set(ctx):
    return wf._decision_world_read_set(
        ctx, day="2026-01-05", ranking_day="2026-01-02", phase="open",
        panel=[{"code": "600519"}], news=[])


def test_current_only_archive_is_accepted_when_the_flag_is_set():
    ctx = _Ctx(_current_only())
    assert wf._membership_is_strict(ctx) is False
    out = _read_set(ctx)
    assert out is not None


def test_a_point_in_time_archive_still_reads_as_strict():
    """The permissive path must not become the only path."""
    ctx = _Ctx((_snapshot(point_in_time=True,
                          available_at="2026-01-02 15:00:00"),))
    assert wf._membership_is_strict(ctx) is True
    assert _read_set(ctx) is not None


def test_as_of_still_refuses_a_current_only_archive_by_default():
    """The guard itself is unchanged — only the call site now states a choice."""
    with pytest.raises(SectorSnapshotError,
                       match="strict sector replay requires"):
        sector_membership.as_of(_current_only(), "2026-01-05 09:00:00")
