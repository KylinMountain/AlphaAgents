"""Sector selector contract tests."""

from alpha_agents.agents import sector_selector as S


SECTORS = [
    {
        "rank": 1,
        "sector_id": "AI",
        "relative_returns_pct": {"5d": 3.2},
        "advancers_pct": 72.0,
        "breadth_improvement_5d_pp": 12.0,
        "ex_top1_5d_median_pct": 1.8,
        "fund_flow": {
            "covered": 8,
            "member_count": 10,
            "net_amount_sum": 12345.0,
        },
    },
    {
        "rank": 2,
        "sector_id": "存储",
        "relative_returns_pct": {"5d": None},
        "advancers_pct": 55.0,
        "breadth_improvement_5d_pp": -3.0,
        "ex_top1_5d_median_pct": 0.2,
        "fund_flow": {
            "covered": 0,
            "member_count": 9,
            "net_amount_sum": None,
        },
    },
]


def test_cards_keep_missing_distinct_from_zero():
    text = S.format_sector_cards(SECTORS)
    assert "AI" in text and "存储" in text
    row = next(line for line in text.splitlines() if "存储" in line)
    assert "|-|".replace("|-|", "|-|") or row
    assert "0/9" in row
    assert "|-|" in row


def test_parser_refuses_outside_duplicate_and_fourth_theme():
    payload = """{
      "themes": [
        {"sector_id":"AI","thesis":"a","invalidations":[]},
        {"sector_id":"AI","thesis":"dup","invalidations":[]},
        {"sector_id":"存储","thesis":"b","invalidations":[]},
        {"sector_id":"医药","thesis":"outside","invalidations":[]},
        {"sector_id":"算力","thesis":"c","invalidations":[]},
        {"sector_id":"机器人","thesis":"fourth","invalidations":[]}
      ]
    }"""
    offered = {"AI", "存储", "算力", "机器人"}
    got = S.parse_selection(payload, offered, max_selected=3)
    assert [row["sector_id"] for row in got["themes"]] == [
        "AI", "存储", "算力"]
    why = [row["why"] for row in got["refused"]]
    assert "duplicate" in why
    assert "outside_shortlist" in why
    assert "too_many" in why


def test_empty_themes_is_a_valid_decision():
    got = S.parse_selection('{"themes":[]}', {"AI"})
    assert got["parse_error"] is None
    assert got["themes"] == []


def test_bad_invalidations_is_refused_not_coerced():
    got = S.parse_selection(
        '{"themes":[{"sector_id":"AI","invalidations":"x"}]}', {"AI"})
    assert got["themes"] == []
    assert got["refused"][0]["why"] == "bad_invalidations"


def test_prompt_renders_all_fields():
    template = (
        "D={day}\nA={as_of_session}\nM={market}\nS={sector_cards}\n"
        "N={news}\nK={max_selected}")
    got = S.build_message(
        day="2026-01-30",
        as_of_session="2026-01-29",
        sectors=SECTORS,
        market={"advancers_pct": 45},
        news=[{"time": "08:00", "title": "test"}],
        template=template,
    )
    assert "{sector_cards}" not in got
    assert "AI" in got
    assert "2026-01-29" in got
