"""The gap between the decision and the fill is where a pullback thesis dies.

A pullback trader picks a theme because money is flowing in, then waits for
a dip. Measured on a 20-day autonomous replay, **six of the seven
theme_flow_negative closes were orders that waited two to four sessions and
then closed on day 0 of holding** — bought and sold in the same session,
paying a round trip for a premise that had already gone.

300475 is the clean case:

    ordered  2026-01-08, on a table showing 存储芯片 +294.2亿 over 5 sessions
    written  "out if this turns to a 30亿 outflow" -- a 324亿 buffer
    filled   2026-01-12, same measure now -72.7亿
             (the window rolled -110.2, -5.9, -250.9 across three sessions,
              more than the level itself)
    result   invalidation fired on the fill, closed day 0

``thesis_already_broken`` existed for exactly this and could not see it: the
view carried price and the theme table only, so ``theme_net_flow_yi``,
``theme_rank`` and ``breadth_ratio`` were None and ``evaluate`` skipped every
condition needing them. The one check that would have caught it was
structurally unable to run at the one moment it mattered.

With the flow supplied these become cancellations, which are free, instead
of same-session round trips, which pay spread twice and enter the learning
loop as trades that say nothing about the trader.
"""

from unittest.mock import patch

import pytest

from alpha_agents.data import portfolio_entry as PE
from alpha_agents.data import thesis as T


@pytest.fixture
def order():
    return {"theme": "存储芯片", "entry_low": 40.0, "entry_high": 41.0,
            "trader_id": "pullback"}


def _thesis(conditions):
    th = T.Thesis(code="300475", name="香农芯创", theme="存储芯片",
                  conditions=conditions)
    th.id = 5
    th.position_id = None
    return th


class TestTheViewCarriesTheFlow:
    def test_a_flow_condition_can_fire_before_the_fill(self, order):
        """The 300475 case: +294.2亿 at the decision, -72.7亿 at the fill."""
        cond = [T.Condition("theme_flow_negative", 30.0, "资金逻辑破裂")]
        with patch.object(T, "get_active", return_value=[_thesis(cond)]), \
             patch.object(T, "close_unlocked") as closed, \
             patch.object(PE, "get_theme_by_name", return_value=None), \
             patch("alpha_agents.data.theme_state.concept_state",
                   return_value=({"存储芯片": 348}, {"存储芯片": -72.7})):
            fired = PE.thesis_already_broken("300475", 40.5, order)
        assert fired is not None, "the order must be refused, not filled"
        # close_unlocked, not close: this runs inside the fill loop's
        # _write_lock and the lock is not reentrant.
        assert closed.call_args.args[1] == T.INVALIDATED

    def test_a_theme_still_flowing_in_is_not_refused(self, order):
        cond = [T.Condition("theme_flow_negative", 30.0, "资金逻辑破裂")]
        with patch.object(T, "get_active", return_value=[_thesis(cond)]), \
             patch.object(PE, "get_theme_by_name", return_value=None), \
             patch("alpha_agents.data.theme_state.concept_state",
                   return_value=({"存储芯片": 4}, {"存储芯片": 294.2})):
            assert PE.thesis_already_broken("300475", 40.5, order) is None

    def test_the_rank_condition_can_fire_too(self, order):
        cond = [T.Condition("theme_rank_worse_than", 50.0, "主线易位")]
        with patch.object(T, "get_active", return_value=[_thesis(cond)]), \
             patch.object(T, "close_unlocked"), \
             patch.object(PE, "get_theme_by_name", return_value=None), \
             patch("alpha_agents.data.theme_state.concept_state",
                   return_value=({"存储芯片": 348}, {"存储芯片": -72.7})):
            assert PE.thesis_already_broken("300475", 40.5, order) is not None


class TestAFailedReadNeverRefusesAFill:
    def test_an_unreadable_flow_leaves_the_fields_unset(self, order):
        """"Could not check" must not read as "the thesis broke"."""
        cond = [T.Condition("theme_flow_negative", 30.0, "x")]
        with patch.object(T, "get_active", return_value=[_thesis(cond)]), \
             patch.object(PE, "get_theme_by_name", return_value=None), \
             patch("alpha_agents.data.theme_state.concept_state",
                   side_effect=OSError("db busy")):
            assert PE.thesis_already_broken("300475", 40.5, order) is None

    def test_a_theme_absent_from_the_ranking_is_not_a_breakage(self, order):
        cond = [T.Condition("theme_flow_negative", 30.0, "x")]
        with patch.object(T, "get_active", return_value=[_thesis(cond)]), \
             patch.object(PE, "get_theme_by_name", return_value=None), \
             patch("alpha_agents.data.theme_state.concept_state",
                   return_value=({}, {})):
            assert PE.thesis_already_broken("300475", 40.5, order) is None


class TestBothCheckersReadOneSource:
    def test_the_pre_fill_check_uses_concept_state(self):
        import inspect
        src = inspect.getsource(PE._attach_theme_flow)
        assert "concept_state" in src

    def test_the_post_fill_check_uses_the_same_one(self):
        import inspect
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import walk_forward as wf
        assert "concept_state" in inspect.getsource(wf._check_theses)
