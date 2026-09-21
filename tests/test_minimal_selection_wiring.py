"""RP-05 runner wiring: the minimal experiment changes discovery only."""

from collections import Counter
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import walk_forward as wf  # noqa: E402

from alpha_agents.data import sector_selection as SS  # noqa: E402
from alpha_agents.evolution import selection_experiment as E  # noqa: E402


class _Corpus:
    def __init__(self):
        self.instruments = {
            "600001": {"name": "甲", "is_st": 0, "is_suspended": 0},
            "600002": {"name": "乙", "is_st": 0, "is_suspended": 0},
        }
        self.days = [
            "2026-01-28", "2026-01-29", "2026-01-30",
        ]
        self.index = {day: i for i, day in enumerate(self.days)}
        self.first_bar = {code: "2020-01-01" for code in self.instruments}
        self._bars = {
            # ``turnover_rate`` is no longer what orders a sector's members:
            # ``data/sector_scoring`` is, and it reads beta and traded amount.
            # ``volume`` is sized so ADV20 x close clears the shared
            # liquidity floor, or both names would be dropped as ineligible.
            "2026-01-29": {
                "600001": {
                    "code": "600001", "close": 10.0, "change_pct": 2.0,
                    "turnover_rate": 3.0, "volume": 2_000_000,
                },
                "600002": {
                    "code": "600002", "close": 20.0, "change_pct": 1.0,
                    "turnover_rate": 4.0, "volume": 2_000_000,
                },
            },
            "2026-01-28": {},
            "2026-01-30": {},
        }

    def bars(self, day):
        return self._bars.get(day, {})

    def sector_beta(self, code, peers, before):
        """The replay computes beta as-of from the corpus; so does this stub."""
        return 1.0

    def adv20(self, code, before):
        # ADV20 in shares. The replay multiplies it by the T-1 close to get
        # an amount, and ``data/sector_scoring`` drops anything below
        # 5e7 yuan as too illiquid to rank. A small number here would make
        # the panel empty for a reason that has nothing to do with what the
        # test is about.
        return 20_000_000.0

    def is_listed(self, code, day):
        return True

    def previous(self, day):
        pos = self.index[day]
        return self.days[pos - 1] if pos else None


class _Ctx:
    def __init__(self, architecture):
        self.selection_architecture = architecture
        self.corpus = _Corpus()
        self.counters = Counter()
        self.last_panel_candidate_pool = []
        self.last_sector_candidate_pool = []
        self.trader = "pullback"
        self.picks = 1


def _boom(name):
    def inner(*args, **kwargs):
        raise AssertionError(f"{name} must not be read")
    return inner


def test_control_minimal_discovery_never_reads_ablated_sources(monkeypatch):
    ctx = _Ctx("dual_rank_price_v1")
    monkeypatch.setattr(wf, "_concepts_map", _boom("concepts"))
    monkeypatch.setattr(wf, "_limit_pool_map", _boom("limit pool"))
    monkeypatch.setattr(wf, "_fund_flow_map", _boom("fund flow"))

    panel = wf._build_panel(
        ctx, "2026-01-30", "2026-01-29", limit=2)

    assert [row["code"] for row in panel] == ["600001", "600002"]
    assert all(row["concepts"] == [] for row in panel)
    assert all(row["net_amount"] is None for row in panel)


def test_sector_minimal_discovery_never_reads_ablated_sources(monkeypatch):
    ctx = _Ctx("sector_rank_price_v1")
    membership = SS.MembershipSnapshot(
        snapshot_id="m1",
        available_at="2026-01-29 15:00:00",
        source="fixture",
        sector_type="concept",
        members={"AI": ("600001", "600002")},
        point_in_time=True,
    )
    monkeypatch.setattr(wf, "_limit_pool_map", _boom("limit pool"))
    monkeypatch.setattr(wf, "_fund_flow_map", _boom("fund flow"))
    monkeypatch.setattr(
        wf.sector_membership, "concepts_by_code", _boom("concept map"))
    monkeypatch.setattr(
        wf.sector_selection, "candidate_leave_one_out_5d",
        _boom("leave-one-out"))

    panel = wf._build_sector_panel(
        ctx, "2026-01-30", "2026-01-29",
        membership, ["AI"], limit=2)

    assert [row["code"] for row in panel] == ["600001", "600002"]
    assert all(row["net_amount"] is None for row in panel)
    assert all(row["concepts"] == [] for row in panel)


def test_minimal_planner_uses_transparent_prefix_and_field_whitelist():
    panel = [
        {
            "code": "600001", "name": "甲", "close": 10.0,
            "change_pct": 2.0, "adv20": 1000.0, "turnover_rate": 3.0,
            "net_amount": 999.0, "primary_theme": "AI",
            "concepts": ["AI"],
        },
        {
            "code": "600002", "name": "乙", "close": 20.0,
            "change_pct": 1.0, "adv20": 2000.0, "turnover_rate": 4.0,
            "net_amount": 888.0, "primary_theme": "算力",
            "concepts": ["算力"],
        },
    ]

    got = wf._minimal_planner_panel(panel, limit=1)

    assert got == [{
        "code": "600001", "name": "甲", "close": 10.0,
        "change_pct": 2.0, "adv20": 1000.0, "turnover_rate": 3.0,
    }]


def test_minimal_book_does_not_read_learning_inputs(monkeypatch):
    from alpha_agents.evolution import feedback

    ctx = _Ctx("dual_rank_price_v1")
    monkeypatch.setattr(
        feedback, "inject_portfolio", lambda **kwargs: "BOOK")
    monkeypatch.setattr(
        feedback, "inject_sentiment", _boom("sentiment"))
    monkeypatch.setattr(
        feedback, "inject_principles", _boom("principles"))
    monkeypatch.setattr(
        feedback, "inject_playbooks", _boom("playbooks"))
    monkeypatch.setattr(wf, "_knowledge_block", _boom("knowledge"))

    book, knowledge = wf._book_and_knowledge(
        ctx, "2026-01-30", include_learning=False)

    assert book == "BOOK"
    assert knowledge == ""


def test_selection_experiment_window_is_exactly_30_trading_days():
    days = [f"2026-01-{day:02d}" for day in range(1, 31)]
    ctx = type("Ctx", (), {
        "experiment_manifest": {
            "validation_windows": [{
                "start": days[0], "end": days[-1],
            }],
            "expected_days_per_window": 30,
        },
        "experiment_family": E.FAMILY,
    })()

    wf._verify_experiment_window(ctx, days)

    short = [days[0], *days[2:-1], days[-1]]
    assert len(short) == 29
    with pytest.raises(SystemExit, match="expected exactly 30"):
        wf._verify_experiment_window(ctx, short)
