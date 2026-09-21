"""A concept name's spaces must not cost the day's orders.

Measured on the 2026-01-06 session: the stock selector answered
``"primary_theme": "中国AI50"`` for the offered ``中国AI 50``, both of its
picks were refused as ``invalid_primary_theme``, and the replay placed no
orders — on a day whose direction, stock choice and written reasoning were all
correct. THS ships names like "中国AI 50" and "同花顺漂亮100"; a model writes
the space back about as often as it drops it.

Matching ignores whitespace. It does not ignore anything else, and the name
that gets stored is the offered spelling, never the model's.
"""

import pytest

from alpha_agents.agents.sector_stock_selector import parse


def _reply(theme: str, code: str = "002230") -> str:
    return (
        '{"stocks": [{"code": "%s", "primary_theme": "%s", '
        '"reason": "r", "counterevidence": "c"}]}' % (code, theme))


OFFERED = {"002230"}
THEMES = {"002230": ["脑机接口", "中国AI 50"]}


@pytest.mark.parametrize("written", ["中国AI50", "中国AI 50", "中国AI  50",
                                     " 中国AI50 "])
def test_a_dropped_or_extra_space_still_matches(written):
    out = parse(_reply(written), OFFERED, picks=2, offered_themes=THEMES)
    assert out["refused"] == [], out["refused"]
    assert len(out["stocks"]) == 1


def test_the_offered_spelling_is_what_gets_stored():
    """Downstream joins on the concept name, so the model's spelling cannot win."""
    out = parse(_reply("中国AI50"), OFFERED, picks=2, offered_themes=THEMES)
    assert out["stocks"][0]["primary_theme"] == "中国AI 50"


def test_a_theme_that_was_not_offered_is_still_refused():
    """Whitespace tolerance is not a licence to invent a direction."""
    out = parse(_reply("中国AI 500"), OFFERED, picks=2, offered_themes=THEMES)
    assert out["stocks"] == []
    assert out["refused"][0]["why"] == "invalid_primary_theme"


def test_a_theme_offered_for_another_stock_is_still_refused():
    out = parse(_reply("光刻胶"), OFFERED, picks=2, offered_themes=THEMES)
    assert out["refused"][0]["why"] == "invalid_primary_theme"


# ── the direction stage has the same contract ────────────────────────────

from alpha_agents.agents.sector_selector import parse_selection  # noqa: E402


def _direction_reply(sector_id: str) -> str:
    return ('{"themes": [{"sector_id": "%s", "thesis": "t", '
            '"counterevidence": "c", "unknowns": "u", '
            '"invalidations": ["a", "b"]}]}' % sector_id)


@pytest.mark.parametrize("written", ["中国AI50", "中国AI 50", "中国AI  50"])
def test_a_direction_matches_despite_the_space(written):
    """Worse than the stock stage: a refused direction costs the whole day.

    No direction means no panel, and no panel means no orders — where the same
    mismatch in the stock selector only drops one name.
    """
    out = parse_selection(_direction_reply(written), {"中国AI 50", "存储芯片"})
    assert out["refused"] == [], out["refused"]
    assert [row["sector_id"] for row in out["themes"]] == ["中国AI 50"]


def test_a_direction_outside_the_shortlist_is_still_refused():
    out = parse_selection(_direction_reply("我编的概念"), {"中国AI 50"})
    assert out["themes"] == []
    assert out["refused"][0]["why"] == "outside_shortlist"


def test_two_spellings_of_one_direction_count_as_a_duplicate():
    """Otherwise squeezing would let the model take one slot twice."""
    reply = ('{"themes": ['
             '{"sector_id": "中国AI50", "thesis": "t", "counterevidence": "c",'
             ' "unknowns": "u", "invalidations": ["a", "b"]},'
             '{"sector_id": "中国AI 50", "thesis": "t", "counterevidence": "c",'
             ' "unknowns": "u", "invalidations": ["a", "b"]}]}')
    out = parse_selection(reply, {"中国AI 50", "存储芯片"})
    assert len(out["themes"]) == 1
    assert out["refused"][0]["why"] == "duplicate"
