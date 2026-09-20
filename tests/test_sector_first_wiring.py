"""The opt-in sector-first path owns stock themes without changing incumbent defaults."""

from collections import Counter
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import walk_forward as wf  # noqa: E402

from alpha_agents.agents import t1_decider  # noqa: E402
from alpha_agents.data import opportunity_journal as OJ  # noqa: E402
from alpha_agents.data import sector_membership as SM  # noqa: E402
from alpha_agents.data import sector_selection as SS  # noqa: E402


class _Ctx:
    selection_architecture = "sector_first_v0"
    panel_size = 40
    news_limit = 60
    trader = "pullback"
    trader_note = ""
    picks = 2
    prompt = "unused"
    model = object()
    loop = None
    max_turns = None
    participation = 0.10
    theme = "RUN-THEME"
    run_id = "r1"
    trader_tools = False
    counters = Counter()
    capacity = {}
    last_panel_candidate_pool = []
    last_sector_context = {}

    class _Corpus:
        pass

    corpus = _Corpus()


def _order():
    return {
        "code": "600001",
        "entry_low": 10.0,
        "entry_high": 10.5,
        "stop_loss": 9.5,
        "target_price": 12.0,
        "reason": "fixture",
    }


def _wire_common(monkeypatch):
    monkeypatch.setattr(wf, "_market_state", lambda *a, **k: {})
    monkeypatch.setattr(wf, "_news_window", lambda *a, **k: [])
    monkeypatch.setattr(wf, "_book_and_knowledge", lambda *a, **k: ("", ""))
    monkeypatch.setattr(wf, "_trader_tools", lambda *a, **k: [])
    monkeypatch.setattr(wf, "_event_snapshot_refs", lambda *a, **k: [])
    monkeypatch.setattr(wf, "capacity_shares", lambda *a, **k: 1000)
    monkeypatch.setattr(OJ, "record_decision", lambda **k: 1)
    monkeypatch.setattr(
        t1_decider,
        "propose_sync",
        lambda **k: {
            "orders": [_order()],
            "refused": [],
            "parse_error": None,
            "raw": "{}",
            "research_budget": None,
        },
    )


def test_sector_first_order_uses_stock_primary_theme(monkeypatch):
    ctx = _Ctx()
    snapshot = SS.MembershipSnapshot(
        snapshot_id="m1",
        available_at="2026-01-29 15:00:00",
        source="fixture",
        sector_type="concept",
        members={"AI": ("600001",), "算力": ("600001",)},
        point_in_time=True,
    )
    ctx.sector_membership_archive = (snapshot,)
    primary_relation = SM.relation_evidence_id(
        snapshot, sector_id="AI", code="600001")
    supporting_relation = SM.relation_evidence_id(
        snapshot, sector_id="算力", code="600001")
    panel = [{
        "code": "600001",
        "name": "甲",
        "adv20": 100000,
        "primary_theme": "AI",
        "supporting_themes": ["算力"],
        "membership_snapshot_id": snapshot.snapshot_id,
        "membership_hash": snapshot.content_hash,
        "primary_theme_relation_evidence_id": primary_relation,
        "supporting_theme_relation_evidence_ids": [supporting_relation],
    }]
    monkeypatch.setattr(
        wf, "_sector_first_stage", lambda *a, **k: (panel, None))
    monkeypatch.setattr(
        wf, "_sector_stock_choice",
        lambda *a, **k: {
            "stocks": [{
                "code": "600001", "primary_theme": "算力",
                "reason": "算力关系更直接", "counterevidence": "位置偏高",
            }],
            "refused": [], "parse_error": None, "research_budget": None,
            "research_trace": [],
        })
    monkeypatch.setattr(
        wf, "_sector_trade_plan",
        lambda *a, **k: {
            "orders": [_order()], "refused": [], "parse_error": None,
            "raw": "{}", "research_budget": None,
        })
    _wire_common(monkeypatch)

    captured = {}
    monkeypatch.setattr(
        wf,
        "create_pending_order",
        lambda **kwargs: captured.update(kwargs) or 7,
    )

    got = wf._decide_llm(ctx, "2026-01-30", "2026-01-29")
    assert got[0]["theme"] == "算力"
    assert got[0]["supporting_themes"] == ["AI"]
    assert captured["theme"] == "算力"
    assert captured["theme"] != ctx.theme
    assert got[0]["membership_snapshot_id"] == "m1"
    assert got[0]["membership_hash"] == snapshot.content_hash
    assert got[0]["primary_theme_relation_evidence_id"] == supporting_relation
    assert got[0]["supporting_theme_relation_evidence_ids"] == [
        primary_relation]


def test_dual_rank_default_keeps_run_theme(monkeypatch):
    ctx = _Ctx()
    ctx.selection_architecture = "dual_rank_v0"
    panel = [{
        "code": "600001",
        "name": "甲",
        "adv20": 100000,
    }]
    monkeypatch.setattr(wf, "_build_panel", lambda *a, **k: panel)
    _wire_common(monkeypatch)

    captured = {}
    monkeypatch.setattr(
        wf,
        "create_pending_order",
        lambda **kwargs: captured.update(kwargs) or 8,
    )

    got = wf._decide_llm(ctx, "2026-01-30", "2026-01-29")
    assert got[0]["theme"] == "RUN-THEME"
    assert captured["theme"] == "RUN-THEME"


def test_cli_default_does_not_change_incumbent_architecture():
    args = wf.build_parser().parse_args(["--start", "2026-01-30"])
    assert args.selection_architecture == "dual_rank_v0"
    assert args.sector_membership is None


def test_sector_first_context_requires_pit_archive(tmp_path, monkeypatch):
    class _Corpus:
        def __init__(self, _data_dir):
            pass

    monkeypatch.setattr(wf, "Corpus", _Corpus)
    args = wf.build_parser().parse_args([
        "--start", "2026-01-30",
        "--decider", "llm",
        "--selection-architecture", "sector_first_v0",
    ])
    args.run_id = "x"
    with pytest.raises(SystemExit, match="sector-membership"):
        wf.Context(args)



def test_no_flow_arm_filters_direction_flow_news_only():
    ctx = _Ctx()
    ctx.selection_architecture = "sector_first_no_flow"
    news = [
        {"title": "AI 主力资金净流入 10 亿", "content": ""},
        {"title": "AI 新产品发布", "content": "订单增长"},
        {"title": "北向资金净买入", "content": "市场"},
    ]
    got = wf._direction_news(ctx, news)
    assert [row["title"] for row in got] == ["AI 新产品发布"]


def test_no_flow_arm_is_opt_in_cli_choice():
    args = wf.build_parser().parse_args([
        "--start", "2026-01-30",
        "--selection-architecture", "sector_first_no_flow",
    ])
    assert args.selection_architecture == "sector_first_no_flow"



def test_c_arm_simple_stock_choice_never_calls_llm_selector(monkeypatch):
    from alpha_agents.agents import sector_stock_selector

    ctx = _Ctx()
    ctx.selection_architecture = "sector_first_simple_selector"
    panel = [
        {"code": "600001"},
        {"code": "600002"},
        {"code": "600003"},
    ]
    monkeypatch.setattr(
        sector_stock_selector, "propose_sync",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("C must not call the LLM stock selector")),
    )
    got = wf._sector_stock_choice(
        ctx, day="2026-01-30", prev_day="2026-01-29",
        panel=panel, news=[], market={}, book="", knowledge="",
        research_budget=None)
    assert [row["code"] for row in got["stocks"]] == [
        "600001", "600002"]


def test_sector_trade_plan_is_toolless(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        t1_decider, "propose_sync",
        lambda **kwargs: captured.update(kwargs) or {
            "orders": [], "refused": [], "parse_error": None,
            "raw": "", "research_budget": None,
        })
    ctx = _Ctx()
    ctx.prompt = "plan"
    wf._sector_trade_plan(
        ctx, day="2026-01-30", prev_day="2026-01-29",
        panel=[{"code": "600001"}], news=[], market={},
        book="", knowledge="", research_packet_payload={
            "version": 1, "packet_hash": "fixture"})
    assert captured["tools"] == []
    assert captured["research_budget"] is None



def test_sector_stock_card_renders_leave_one_out_peer_strength():
    panel = [{
        "code": "600001",
        "name": "甲",
        "close": 10.0,
        "change_pct": 2.0,
        "adv20": 100000,
        "turnover_rate": 3.0,
        "primary_theme": "AI",
        "supporting_themes": ["算力"],
        "primary_theme_peer_covered": 3,
        "primary_theme_peer_total": 4,
        "primary_theme_peer_relative_5d_pct": 1.234,
    }]
    rendered = t1_decider.format_panel(panel)
    assert "主方向去自身5日相对" in rendered
    assert "+1.23% (3/4)" in rendered



def test_formal_dual_rank_arm_uses_the_preregistered_research_budget(monkeypatch):
    from alpha_agents.tools.budget import ResearchBudget

    ctx = _Ctx()
    ctx.selection_architecture = "dual_rank_v0"
    ctx.experiment_manifest = {"bound": True}
    panel = [{
        "code": "600001",
        "name": "甲",
        "adv20": 100000,
    }]
    monkeypatch.setattr(wf, "_build_panel", lambda *a, **k: panel)
    _wire_common(monkeypatch)

    captured = {}
    monkeypatch.setattr(
        t1_decider,
        "propose_sync",
        lambda **kwargs: captured.update(kwargs) or {
            "orders": [],
            "refused": [],
            "parse_error": None,
            "raw": "{}",
            "research_budget": kwargs["research_budget"].summary(),
        },
    )

    wf._decide_llm(ctx, "2026-01-30", "2026-01-29")
    assert isinstance(captured["research_budget"], ResearchBudget)
    assert captured["research_budget"].max_total_calls == 20


def test_ordinary_dual_rank_run_keeps_legacy_unbounded_research(monkeypatch):
    ctx = _Ctx()
    ctx.selection_architecture = "dual_rank_v0"
    panel = [{
        "code": "600001",
        "name": "甲",
        "adv20": 100000,
    }]
    monkeypatch.setattr(wf, "_build_panel", lambda *a, **k: panel)
    _wire_common(monkeypatch)

    captured = {}
    monkeypatch.setattr(
        t1_decider,
        "propose_sync",
        lambda **kwargs: captured.update(kwargs) or {
            "orders": [],
            "refused": [],
            "parse_error": None,
            "raw": "{}",
            "research_budget": None,
        },
    )

    wf._decide_llm(ctx, "2026-01-30", "2026-01-29")
    assert captured["research_budget"] is None


def test_event_snapshot_refs_use_the_decision_cutoff(monkeypatch):
    from alpha_agents.data import event_expectations as EE

    captured = {}

    def fake_snapshot_refs(**kwargs):
        captured.update(kwargs)
        return [{"event_key": "earnings-600001"}]

    monkeypatch.setattr(EE, "snapshot_refs", fake_snapshot_refs)
    cutoff = "2026-01-30 09:00:00"
    got = wf._event_snapshot_refs(
        [{"code": "600001"}, {"code": "600001"}, {"code": ""}],
        cutoff,
    )

    assert got == [{"event_key": "earnings-600001"}]
    assert captured["as_of"] == cutoff
    assert captured["subjects"] == ["600001", "600001"]
    assert captured["days_back"] == 30
    assert captured["days_ahead"] == 30


def test_sector_first_missing_relation_is_refused_before_intent(monkeypatch):
    ctx = _Ctx()
    snapshot = SS.MembershipSnapshot(
        snapshot_id="m1",
        available_at="2026-01-29 15:00:00",
        source="fixture",
        sector_type="concept",
        members={"AI": ("600001",)},
        point_in_time=True,
    )
    ctx.sector_membership_archive = (snapshot,)
    panel = [{
        "code": "600001",
        "name": "甲",
        "adv20": 100000,
        "primary_theme": "AI",
        "supporting_themes": [],
        "membership_snapshot_id": snapshot.snapshot_id,
        "membership_hash": snapshot.content_hash,
        # Deliberately absent: primary_theme_relation_evidence_id.
        "supporting_theme_relation_evidence_ids": [],
    }]
    monkeypatch.setattr(
        wf, "_sector_first_stage", lambda *a, **k: (panel, None))
    monkeypatch.setattr(
        wf, "_sector_stock_choice",
        lambda *a, **k: {
            "stocks": [{"code": "600001", "reason": "pick"}],
            "refused": [], "parse_error": None, "research_budget": None,
        })
    monkeypatch.setattr(
        wf, "_sector_trade_plan",
        lambda *a, **k: {
            "orders": [_order()], "refused": [], "parse_error": None,
            "raw": "{}", "research_budget": None,
        })
    _wire_common(monkeypatch)

    journal = {}
    monkeypatch.setattr(
        OJ, "record_decision", lambda **kwargs: journal.update(kwargs) or 1)
    monkeypatch.setattr(
        wf, "create_pending_order",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("unresolved relation must never create an intent")),
    )

    got = wf._decide_llm(ctx, "2026-01-30", "2026-01-29")

    assert got == []
    refusal = next(
        row for row in journal["refusals"]
        if row.get("why") == "theme_unresolved")
    assert refusal["code"] == "600001"
    assert refusal["stage"] == "relation_validation"
    assert "primary_relation_evidence_mismatch" in refusal["detail"]


def test_sector_relation_validation_rejects_tampered_membership_hash():
    ctx = _Ctx()
    snapshot = SS.MembershipSnapshot(
        snapshot_id="m1",
        available_at="2026-01-29 15:00:00",
        source="fixture",
        sector_type="concept",
        members={"AI": ("600001",)},
        point_in_time=True,
    )
    ctx.sector_membership_archive = (snapshot,)
    panel = [{
        "code": "600001",
        "primary_theme": "AI",
        "supporting_themes": [],
        "membership_snapshot_id": "m1",
        "membership_hash": "tampered",
        "primary_theme_relation_evidence_id": SM.relation_evidence_id(
            snapshot, sector_id="AI", code="600001"),
        "supporting_theme_relation_evidence_ids": [],
    }]
    accepted, refused = wf._validate_sector_order_relations(
        ctx, panel, [{"code": "600001"}])

    assert accepted == []
    assert refused[0]["why"] == "theme_unresolved"
    assert "hash mismatch" in refused[0]["detail"]


def _research_stage(ctx, snapshot, panel):
    ctx.last_sector_context = {
        "membership_snapshot_id": snapshot.snapshot_id,
        "membership_hash": snapshot.content_hash,
        "selected_themes": ["算力"],
        "direction_research": [{
            "sector_id": "算力",
            "thesis": "订单上修",
            "counterevidence": "估值偏高",
            "unknowns": "持续性",
            "invalidations": ["核心订单被取消"],
        }],
    }
    return panel, None


def test_sector_planner_receives_direction_stock_and_tool_evidence(monkeypatch):
    from alpha_agents.data import policy_registry

    ctx = _Ctx()
    snapshot = SS.MembershipSnapshot(
        snapshot_id="m1",
        available_at="2026-01-29 15:00:00",
        source="fixture",
        sector_type="concept",
        members={"AI": ("600001",), "算力": ("600001",)},
        point_in_time=True,
    )
    ctx.sector_membership_archive = (snapshot,)
    rel_ai = SM.relation_evidence_id(
        snapshot, sector_id="AI", code="600001")
    rel_compute = SM.relation_evidence_id(
        snapshot, sector_id="算力", code="600001")
    panel = [{
        "code": "600001", "name": "甲", "adv20": 100000,
        "close": 10.0, "change_pct": 2.0, "turnover_rate": 3.0,
        "primary_theme": "AI", "supporting_themes": ["算力"],
        "eligible_themes": ["AI", "算力"],
        "theme_relation_evidence_ids": {
            "AI": rel_ai, "算力": rel_compute},
        "membership_snapshot_id": snapshot.snapshot_id,
        "membership_hash": snapshot.content_hash,
        "primary_theme_relation_evidence_id": rel_ai,
        "supporting_theme_relation_evidence_ids": [rel_compute],
    }]
    monkeypatch.setattr(
        wf, "_sector_first_stage",
        lambda *a, **k: _research_stage(ctx, snapshot, panel))
    monkeypatch.setattr(
        wf, "_sector_stock_choice",
        lambda *a, **k: {
            "stocks": [{
                "code": "600001", "primary_theme": "算力",
                "reason": "同方向里关系更直接",
                "counterevidence": "短线位置偏高",
            }],
            "refused": [], "parse_error": None,
            "research_budget": {
                "used": 1, "deep_dive_names": ["600001"]},
            "research_trace": [{
                "tool": "get_stock_context",
                "code": "600001",
                "status": "ok",
                "elapsed_ms": 2,
                "result_hash": "fact-hash",
                "payload": {
                    "available": True,
                    "atr_pct": 3.2,
                    "structure": "above_ma20",
                },
            }],
        })
    monkeypatch.setattr(policy_registry, "active_ref", lambda: "policy-v1")
    captured = {}

    def planner(**kwargs):
        captured["packet"] = kwargs["research_packet_payload"]
        return {
            "orders": [], "refused": [], "parse_error": None,
            "raw": "{}", "research_budget": None,
        }

    monkeypatch.setattr(wf, "_sector_trade_plan", planner)
    _wire_common(monkeypatch)
    journal = {}
    monkeypatch.setattr(
        OJ, "record_decision", lambda **kwargs: journal.update(kwargs) or 1)

    assert wf._decide_llm(ctx, "2026-01-30", "2026-01-29") == []

    packet = captured["packet"]
    assert packet["decision"]["cutoff"] == "2026-01-30 09:00:00"
    assert packet["decision"]["policy_ref"] == "policy-v1"
    assert packet["world"]["membership_snapshot_id"] == "m1"
    assert packet["stocks"][0]["primary_theme"] == "算力"
    assert packet["stocks"][0]["selector_reason"] == "同方向里关系更直接"
    assert packet["stocks"][0]["counterevidence"] == "短线位置偏高"
    assert packet["stocks"][0]["direction_research"]["thesis"] == "订单上修"
    assert packet["stocks"][0]["tool_facts"][0]["payload"]["atr_pct"] == 3.2
    assert journal["context"]["research_packet"]["packet_hash"] == (
        packet["packet_hash"])


def test_hard_research_invalidation_never_reaches_planner(monkeypatch):
    from alpha_agents.data import policy_registry

    ctx = _Ctx()
    snapshot = SS.MembershipSnapshot(
        snapshot_id="m1",
        available_at="2026-01-29 15:00:00",
        source="fixture",
        sector_type="concept",
        members={"算力": ("600001",)},
        point_in_time=True,
    )
    ctx.sector_membership_archive = (snapshot,)
    relation = SM.relation_evidence_id(
        snapshot, sector_id="算力", code="600001")
    panel = [{
        "code": "600001", "name": "甲", "adv20": 100000,
        "close": 10.0, "change_pct": 2.0,
        "primary_theme": "算力", "supporting_themes": [],
        "eligible_themes": ["算力"],
        "theme_relation_evidence_ids": {"算力": relation},
        "membership_snapshot_id": snapshot.snapshot_id,
        "membership_hash": snapshot.content_hash,
        "primary_theme_relation_evidence_id": relation,
        "supporting_theme_relation_evidence_ids": [],
    }]
    monkeypatch.setattr(
        wf, "_sector_first_stage",
        lambda *a, **k: _research_stage(ctx, snapshot, panel))
    monkeypatch.setattr(
        wf, "_sector_stock_choice",
        lambda *a, **k: {
            "stocks": [{
                "code": "600001", "primary_theme": "算力",
                "reason": "候选", "counterevidence": "",
            }],
            "refused": [], "parse_error": None,
            "research_budget": {"used": 1, "deep_dive_names": ["600001"]},
            "research_trace": [{
                "tool": "get_event_context",
                "code": "600001",
                "status": "ok",
                "elapsed_ms": 1,
                "result_hash": "block-hash",
                "payload": {
                    "available": True,
                    "must_not_buy": True,
                    "reason": "核心订单取消已确认",
                },
            }],
        })
    monkeypatch.setattr(policy_registry, "active_ref", lambda: "policy-v1")
    monkeypatch.setattr(
        wf, "_sector_trade_plan",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("hard invalidation must not reach planner")))
    _wire_common(monkeypatch)
    journal = {}
    monkeypatch.setattr(
        OJ, "record_decision", lambda **kwargs: journal.update(kwargs) or 1)
    monkeypatch.setattr(
        wf, "create_pending_order",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("hard invalidation must not create intent")))

    got = wf._decide_llm(ctx, "2026-01-30", "2026-01-29")

    assert got == []
    refusal = next(
        row for row in journal["refusals"]
        if row.get("why") == "research_invalidation")
    assert refusal["code"] == "600001"
    assert refusal["detail"] == "核心订单取消已确认"
