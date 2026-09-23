"""The themes page shows today's board beside the lifecycle, not the origin.

On 2026-09-23 the card for 小金属概念 read its discovery-day catalyst
"净流入57.1亿" while the board was −53.0亿 at 14:57. The API now carries
today's print per theme and names the strongest inflows it is not tracking.
"""

from unittest.mock import patch

from alpha_agents.server.app import _themes_with_today

THEMES = [
    {"name": "小金属概念", "status": "declining", "strength": 10,
     "catalyst": "概念涨1.3%, 净流入57.1亿"},
    {"name": "人工智能", "status": "active", "strength": 6, "catalyst": ""},
]
BOARDS = [  # ordered by net inflow, as read_latest_sector_flow returns them
    {"captured_at": "2026-09-23 14:57", "sector_name": "先进封装",
     "change_pct": 1.38, "net_flow_yi": 47.12, "leader": "X", "leader_change_pct": 9.9},
    {"captured_at": "2026-09-23 14:57", "sector_name": "小金属概念",
     "change_pct": -0.61, "net_flow_yi": -53.01, "leader": "振华股份",
     "leader_change_pct": 3.1},
    {"captured_at": "2026-09-23 14:57", "sector_name": "机器人概念",
     "change_pct": -0.15, "net_flow_yi": -138.29, "leader": "Y", "leader_change_pct": 1.0},
]


def _run(boards=BOARDS):
    with patch("alpha_agents.data.memory_store.get_active_themes",
               return_value=[dict(t) for t in THEMES]), \
         patch("alpha_agents.data.snapshot_store.read_latest_sector_flow",
               return_value=boards):
        return _themes_with_today()


def test_a_theme_carries_todays_board_not_its_origin():
    out = _run()
    t = next(x for x in out["themes"] if x["name"] == "小金属概念")
    assert t["today"]["net_flow_yi"] == -53.01
    assert t["catalyst"] == "概念涨1.3%, 净流入57.1亿", "origin kept, but separate"
    assert out["snapshot_at"] == "2026-09-23 14:57"


def test_a_theme_with_no_board_says_so():
    t = next(x for x in _run()["themes"] if x["name"] == "人工智能")
    assert t["today"] is None


def test_untracked_leaders_are_inflows_the_table_is_not_following():
    names = [u["name"] for u in _run()["untracked_leaders"]]
    assert names == ["先进封装"], "tracked lines and outflows are not leaders"


def test_no_snapshot_is_an_empty_strip_not_an_error():
    out = _run(boards=[])
    assert out["snapshot_at"] is None and out["untracked_leaders"] == []
    assert all(t["today"] is None for t in out["themes"])
