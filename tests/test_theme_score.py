"""A theme's strength for *today*, and the two bars that read it.

`strength` counts confirmed sessions; `daily_score` is a raw ±1 sum, so a
0.01億 inflow and a 55億 inflow scored the same, and a two-week-old line
out-accumulated one that broke out this morning. `trend_score` is the third
number: both raw signals as percentiles of the whole board, mixed with
confirmation, refreshed every cycle, read by the theme gate in place of
`strength >= 4`.

Three things here are load-bearing and each has its own class:

  * **the bar is a band.** Admission and cancellation read *different*
    thresholds on purpose. One bar read at both ends meant a theme oscillating
    around it killed its own pending orders — 106 of 117 orders died that way.
  * **absence is not evidence.** The board endpoint serves 287–380 of ~387 rows
    per call with the membership varying between calls, so a theme missing from
    one frame is usually a truncated response. A single miss must not retire a
    line, and writing NULL there would make the gate fail open on a short HTTP
    response.
  * **an unscored theme is admitted, not refused.** Not being able to measure a
    line is not the same as measuring it as weak; on 2026-09-14 the board could
    not name the strongest theme in the book at all.
"""

import pytest

from alpha_agents.data.memory_store import get_theme_by_name, upsert_theme
from alpha_agents.data.scoring import DEFAULT_DECISION_PARAMS
from alpha_agents.pipeline.theme_manager import (
    UNMEASURED_DECAY_AFTER, mark_theme_unscored, refresh_theme_scores, theme_score,
)
from alpha_agents.tools.sector_ranking import board_match

GATE = DEFAULT_DECISION_PARAMS["theme_gate"]
ADMIT = GATE["admit_score"]
CANCEL = GATE["cancel_score"]
TODAY = "2026-09-14"

# Strengths chosen so `confirm` (strength/10 × w_confirm) cannot by itself push a
# score across a bar: a theme at strength 4 contributes 0.08, well under the gap
# the tests below rely on.
MID_STRENGTH = 4


def cs(pairs):
    """A cross-section from (name, change_pct, net_flow_yi) triples."""
    return [{"concept": n, "scope": "concept", "change_pct": c, "net_flow_yi": f}
            for n, c, f in pairs]


class TestBoardMatching:
    """Boards and themes disagree about spelling; that must not read as zero."""

    ROWS = [{"concept": "小金属", "change_pct": 1.0, "net_flow_yi": 5.0},
            {"concept": "共封装光学(CPO)", "change_pct": 2.0, "net_flow_yi": 9.0},
            {"concept": "PCB概念", "change_pct": -1.0, "net_flow_yi": -3.0},
            {"concept": "F5G概念", "change_pct": 0.5, "net_flow_yi": 1.0}]

    def test_exact_name_matches(self):
        assert board_match("PCB概念", self.ROWS)["concept"] == "PCB概念"

    def test_a_suffix_the_theme_keeps_and_the_board_drops(self):
        assert board_match("小金属概念", self.ROWS)["concept"] == "小金属"

    def test_a_bracketed_english_name_matches(self):
        assert board_match("共封装光学(CPO)", self.ROWS)["concept"] == "共封装光学(CPO)"

    def test_a_name_without_cjk_only_matches_exactly(self):
        """`5G` must not be scored on `F5G概念`'s flow."""
        assert board_match("5G", self.ROWS) is None

    def test_a_latin_fragment_must_agree(self):
        assert board_match("5G概念", self.ROWS) is None

    def test_an_unnameable_theme_returns_none(self):
        assert board_match("不存在的概念", self.ROWS) is None

    def test_an_empty_board_is_not_a_match(self):
        assert board_match("PCB概念", []) is None


class TestThemeScore:
    def test_a_stronger_board_scores_higher(self):
        rows = cs([("甲", 0.1, 1.0), ("乙", 0.2, 2.0), ("丙", 5.0, 90.0)])
        assert theme_score(rows, "丙", 0)["score"] > theme_score(rows, "甲", 0)["score"]

    def test_the_percentile_covers_the_whole_frame(self):
        """Mid-pack must read as mid-pack, not as 'the worst of the leaders'."""
        rows = cs([(f"概念{i}", float(i), float(i)) for i in range(10)])
        got = theme_score(rows, "概念4", 0)
        assert got["flow_pct"] == pytest.approx(0.45)
        assert got["of"] == 10

    def test_confirmation_moves_the_score(self):
        rows = cs([("甲", 1.0, 1.0), ("乙", 1.0, 1.0)])
        low = theme_score(rows, "甲", 0)["score"]
        high = theme_score(rows, "甲", 10)["score"]
        assert high > low

    def test_an_unnameable_theme_has_no_score(self):
        assert theme_score(cs([("甲", 1.0, 1.0)]), "乙", 5) is None


class TestGateIsABand:
    """Admission asks more than cancellation, and the gap is the buffer."""

    def _theme(self, score, status="active", strength=MID_STRENGTH):
        upsert_theme("金属铜", status=status, strength=strength, trend_score=score)

    def test_a_strong_theme_passes_both(self):
        from alpha_agents.data.theme_gate import theme_admits, theme_gate
        self._theme(min(ADMIT + 0.2, 1.0))
        assert theme_admits("金属铜") is None
        assert theme_gate("金属铜", "cancel") is None

    def test_the_band_admits_nothing_new_but_pulls_nothing_either(self):
        """The 106-cancellation fix: no new order, but no self-inflicted kill."""
        from alpha_agents.data.theme_gate import theme_admits, theme_gate
        self._theme((ADMIT + CANCEL) / 2)
        assert theme_admits("金属铜") is not None, "below admission"
        assert theme_gate("金属铜", "cancel") is None, "but above cancellation"

    def test_a_collapsed_theme_is_pulled(self):
        from alpha_agents.data.theme_gate import theme_gate
        self._theme(CANCEL / 2)
        reason = theme_gate("金属铜", "cancel")
        assert reason is not None and "走弱" in reason

    def test_an_unscored_theme_is_admitted(self):
        """Not being able to measure it is not the same as measuring it weak."""
        from alpha_agents.data.theme_gate import theme_admits, theme_gate
        self._theme(None)
        assert theme_admits("金属铜") is None
        assert theme_gate("金属铜", "cancel") is None

    def test_a_retired_theme_passes_neither(self):
        from alpha_agents.data.theme_gate import theme_admits, theme_gate
        self._theme(1.0, status="declining")
        assert theme_admits("金属铜") is not None
        assert theme_gate("金属铜", "cancel") is not None


class TestRefreshScores:
    def test_a_named_theme_gets_todays_score(self):
        upsert_theme("甲", status="active", strength=5)
        got = refresh_theme_scores(cs([("甲", 3.0, 50.0), ("乙", -1.0, -9.0)]))
        assert got["scored"] == 1
        assert get_theme_by_name("甲")["trend_score"] is not None

    def test_a_missed_day_is_forgiven_by_the_next_hit(self):
        upsert_theme("甲", status="active", strength=5, unmeasured_days=1)
        refresh_theme_scores(cs([("甲", 3.0, 50.0)]))
        assert get_theme_by_name("甲")["unmeasured_days"] == 0

    def test_a_missing_board_writes_nothing(self):
        """An outage keeps last cycle's numbers — it is not 'found nothing'."""
        upsert_theme("甲", status="active", strength=5, trend_score=0.42)
        got = refresh_theme_scores([])
        assert got["board"] is False and got["scored"] == 0
        assert get_theme_by_name("甲")["trend_score"] == 0.42

    def test_a_theme_the_board_does_not_name_keeps_its_score(self):
        """The board serves a partial list; absence must not fail the gate open."""
        upsert_theme("甲", status="active", strength=5, trend_score=0.42)
        got = refresh_theme_scores(cs([("乙", 1.0, 1.0)]))
        assert got["unmatched"] == ["甲"]
        assert get_theme_by_name("甲")["trend_score"] == 0.42


class TestUnmeasuredDecay:
    def test_one_miss_is_the_network(self):
        upsert_theme("甲", status="active", strength=5)
        got = mark_theme_unscored("甲", today=TODAY)
        theme = get_theme_by_name("甲")
        assert got == {"days": 1, "decayed": False}
        assert theme["strength"] == 5, "a truncated response must not cost strength"

    def test_a_run_of_misses_costs_a_point(self):
        upsert_theme("甲", status="active", strength=5)
        for _ in range(UNMEASURED_DECAY_AFTER - 1):
            mark_theme_unscored("甲", today="2026-09-13")
        got = mark_theme_unscored("甲", today=TODAY)
        assert got["decayed"] is True
        assert get_theme_by_name("甲")["strength"] == 4

    def test_it_counts_once_a_day(self):
        upsert_theme("甲", status="active", strength=5, unmeasured_days=UNMEASURED_DECAY_AFTER)
        mark_theme_unscored("甲", today=TODAY)
        mark_theme_unscored("甲", today=TODAY)
        assert get_theme_by_name("甲")["strength"] == 4

    def test_decay_retires_a_watching_line_in_the_end(self):
        upsert_theme("乙", status="watching", strength=1, unmeasured_days=UNMEASURED_DECAY_AFTER)
        mark_theme_unscored("乙", today=TODAY)
        assert get_theme_by_name("乙")["status"] == "archived"

    def test_a_decayed_theme_loses_its_stale_score(self):
        """Once we conclude the board cannot see it, a stale number must not gate."""
        upsert_theme("甲", status="active", strength=5, trend_score=0.9,
                     unmeasured_days=UNMEASURED_DECAY_AFTER)
        mark_theme_unscored("甲", today=TODAY)
        assert get_theme_by_name("甲")["trend_score"] is None

    def test_strength_never_goes_negative(self):
        upsert_theme("甲", status="watching", strength=0, unmeasured_days=UNMEASURED_DECAY_AFTER)
        mark_theme_unscored("甲", today=TODAY)
        assert get_theme_by_name("甲")["strength"] == 0
