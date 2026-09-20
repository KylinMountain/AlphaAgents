"""RP-03 research packet keeps evidence and thesis identity intact."""

import pytest

from alpha_agents.data import research_packet as R
from alpha_agents.data import sector_membership as SM
from alpha_agents.data import sector_selection as SS
from alpha_agents.pipeline.tasks import morning_scan as M


def _panel():
    return [{
        "code": "600001",
        "name": "甲",
        "close": 10.0,
        "change_pct": 2.0,
        "adv20": 100000,
        "turnover_rate": 3.0,
        "primary_theme": "AI",
        "supporting_themes": ["算力"],
        "eligible_themes": ["AI", "算力"],
        "theme_relation_evidence_ids": {
            "AI": "rel-ai",
            "算力": "rel-compute",
        },
        "primary_theme_relation_evidence_id": "rel-ai",
        "supporting_theme_relation_evidence_ids": ["rel-compute"],
        "membership_snapshot_id": "m1",
        "membership_hash": "h1",
    }]


def _choice(primary="算力"):
    return [{
        "code": "600001",
        "primary_theme": primary,
        "reason": "算力链关系更直接",
        "counterevidence": "位置偏高",
    }]


def test_stock_choice_replaces_round_robin_primary_theme_with_real_thesis():
    got = R.apply_stock_choices(_panel(), _choice())
    assert got[0]["primary_theme"] == "算力"
    assert got[0]["supporting_themes"] == ["AI"]
    assert got[0]["primary_theme_relation_evidence_id"] == "rel-compute"
    assert got[0]["supporting_theme_relation_evidence_ids"] == ["rel-ai"]
    assert got[0]["selection_reason"] == "算力链关系更直接"


def test_stock_choice_refuses_theme_outside_frozen_relations():
    with pytest.raises(R.ResearchPacketError, match="eligible themes"):
        R.apply_stock_choices(_panel(), _choice("机器人"))


def test_packet_hash_binds_cutoff_world_thesis_and_tool_facts():
    selected = R.apply_stock_choices(_panel(), _choice())
    packet = R.build(
        day="2026-01-30",
        cutoff="2026-01-30 09:00:00",
        architecture="sector_first_v0",
        policy_ref="policy-v1",
        membership_snapshot_id="m1",
        membership_hash="h1",
        directions=[{
            "sector_id": "算力",
            "thesis": "订单上修",
            "counterevidence": "估值偏高",
            "unknowns": "持续性",
            "invalidations": ["订单被下修"],
        }],
        selected_panel=selected,
        stock_choices=_choice(),
        research_trace=[{
            "tool": "get_stock_context",
            "code": "600001",
            "status": "ok",
            "elapsed_ms": 3,
            "result_hash": "fact-hash",
            "payload": {
                "available": True,
                "atr_pct": 3.2,
                "structure": "above_ma20",
            },
        }],
        budget={"used": 1, "deep_dive_names": ["600001"]},
        event_snapshot_refs=[{"event_key": "earnings-600001"}],
    )

    assert R.require_valid(
        packet,
        cutoff="2026-01-30 09:00:00",
        membership_snapshot_id="m1",
        membership_hash="h1",
    ) == packet["packet_hash"]
    stock = packet["stocks"][0]
    assert stock["primary_theme"] == "算力"
    assert stock["selector_reason"] == "算力链关系更直接"
    assert stock["direction_research"]["thesis"] == "订单上修"
    assert stock["tool_facts"][0]["payload"]["atr_pct"] == 3.2
    assert "订单被下修" in R.render(packet)

    packet["stocks"][0]["selector_reason"] = "事后改写"
    with pytest.raises(R.ResearchPacketError, match="hash mismatch"):
        R.require_valid(packet)


def test_explicit_tool_invalidation_is_deterministic_refusal():
    selected = R.apply_stock_choices(_panel(), _choice())
    packet = R.build(
        day="2026-01-30",
        cutoff="2026-01-30 09:00:00",
        architecture="sector_first_v0",
        policy_ref=None,
        membership_snapshot_id="m1",
        membership_hash="h1",
        directions=[{"sector_id": "算力"}],
        selected_panel=selected,
        stock_choices=_choice(),
        research_trace=[{
            "tool": "get_event_context",
            "code": "600001",
            "status": "ok",
            "elapsed_ms": 1,
            "result_hash": "block-hash",
            "payload": {
                "available": True,
                "must_not_buy": True,
                "reason": "公告已确认核心订单取消",
            },
        }],
        budget={"used": 1},
        event_snapshot_refs=[],
    )

    got = R.deterministic_refusals(packet)
    assert got == [{
        "code": "600001",
        "why": "research_invalidation",
        "stage": "research_packet_validation",
        "detail": "公告已确认核心订单取消",
        "evidence_hash": "block-hash",
    }]


def test_morning_and_t1_share_the_same_frozen_relation_gate():
    snapshot = SS.MembershipSnapshot(
        snapshot_id="m1",
        available_at="2026-01-29 15:00:00",
        source="fixture",
        sector_type="concept",
        members={"AI": ("600001",), "算力": ("600001",)},
        point_in_time=True,
    )
    row = {
        "code": "600001",
        "primary_theme": "AI",
        "supporting_themes": ["算力"],
        "membership_snapshot_id": snapshot.snapshot_id,
        "membership_hash": snapshot.content_hash,
        "primary_theme_relation_evidence_id": SM.relation_evidence_id(
            snapshot, sector_id="AI", code="600001"),
        "supporting_theme_relation_evidence_ids": [
            SM.relation_evidence_id(
                snapshot, sector_id="算力", code="600001")],
    }
    archive = (snapshot,)

    assert R.validate_relation_row(archive, row) == "AI"
    assert M._morning_relation_status(
        row, relation_archive=archive, strict_relations=True) == "verified"

    tampered = dict(row, membership_hash="tampered")
    with pytest.raises(R.ResearchPacketError, match="hash mismatch"):
        R.validate_relation_row(archive, tampered)
    with pytest.raises(R.ResearchPacketError, match="hash mismatch"):
        M._morning_relation_status(
            tampered, relation_archive=archive, strict_relations=True)


def test_legacy_morning_relation_is_explicitly_unverified():
    assert M._morning_relation_status(
        {"code": "600001", "theme": "AI"}) == "legacy_unverified"
    with pytest.raises(
            R.ResearchPacketError, match="needs a frozen archive"):
        M._morning_relation_status(
            {"code": "600001", "theme": "AI"}, strict_relations=True)
