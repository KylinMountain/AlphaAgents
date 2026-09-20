"""Sector-first stock panel is balanced and keeps theme ownership."""

from alpha_agents.data import sector_panel as P


def _candidates():
    return {
        "600001": {"code": "600001", "change_pct": 8.0, "turnover_rate": 2.0},
        "600002": {"code": "600002", "change_pct": 1.0, "turnover_rate": 20.0},
        "600003": {"code": "600003", "change_pct": 5.0, "turnover_rate": 8.0},
        "600004": {"code": "600004", "change_pct": 4.0, "turnover_rate": 7.0},
        "600005": {"code": "600005", "change_pct": 2.0, "turnover_rate": 12.0},
    }


def test_round_robin_prevents_one_sector_from_filling_the_panel():
    got = P.materialize(
        candidates=_candidates(),
        selected_sectors=["AI", "存储"],
        members={
            "AI": ("600001", "600002", "600003"),
            "存储": ("600003", "600004", "600005"),
        },
        limit=4,
    )
    assert len(got) == 4
    assert got[0]["primary_theme"] == "AI"
    assert got[1]["primary_theme"] == "存储"
    assert {row["primary_theme"] for row in got} == {"AI", "存储"}


def test_overlap_is_one_stock_with_supporting_themes():
    got = P.materialize(
        candidates=_candidates(),
        selected_sectors=["AI", "存储"],
        members={
            "AI": ("600003",),
            "存储": ("600003", "600004"),
        },
        limit=3,
    )
    rows = [row for row in got if row["code"] == "600003"]
    assert len(rows) == 1
    assert rows[0]["primary_theme"] == "AI"
    assert rows[0]["supporting_themes"] == ["存储"]


def test_selected_sector_order_controls_primary_theme_deterministically():
    members = {
        "AI": ("600003",),
        "存储": ("600003",),
    }
    first = P.materialize(
        candidates=_candidates(),
        selected_sectors=["AI", "存储"],
        members=members,
        limit=1,
    )
    second = P.materialize(
        candidates=_candidates(),
        selected_sectors=["存储", "AI"],
        members=members,
        limit=1,
    )
    assert first[0]["primary_theme"] == "AI"
    assert second[0]["primary_theme"] == "存储"
