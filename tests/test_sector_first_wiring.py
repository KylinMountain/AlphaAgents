"""The opt-in sector-first path owns stock themes without changing incumbent defaults."""

from collections import Counter
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import walk_forward as wf  # noqa: E402

from alpha_agents.agents import t1_decider  # noqa: E402
from alpha_agents.data import opportunity_journal as OJ  # noqa: E402


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
    panel = [{
        "code": "600001",
        "name": "甲",
        "adv20": 100000,
        "primary_theme": "AI",
        "supporting_themes": ["AI", "算力"],
    }]
    monkeypatch.setattr(
        wf, "_sector_first_stage", lambda *a, **k: (panel, None))
    _wire_common(monkeypatch)

    captured = {}
    monkeypatch.setattr(
        wf,
        "create_pending_order",
        lambda **kwargs: captured.update(kwargs) or 7,
    )

    got = wf._decide_llm(ctx, "2026-01-30", "2026-01-29")
    assert got[0]["theme"] == "AI"
    assert captured["theme"] == "AI"
    assert captured["theme"] != ctx.theme


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
