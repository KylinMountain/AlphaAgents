"""Silence after a wake-up is not the same as choosing to hold.

A crossed invalidation now asks the agent "还持有吗？" instead of closing
the position. ``hold`` is a decision; no answer at all leaves the position
open too, and the book cannot tell them apart. That distinction is the
entire reason the redesign exists — the point is to find out whether the
agent honours its own stated line, and silence scored as a deliberate hold
puts the answer into the data without the agent ever giving it.

Seen in a replay on 2026-01-15: 600276 filled, its own
``theme_flow_negative`` fired the same session, the wake-up was handed to
the exit turn, and the reply covered only 002555. The next session it
answered ``sell``. Nothing in the run said the question had gone
unanswered.

The failure paths matter most. A timed-out model answers nothing, and "the
provider was slow" must not settle into the record as "the trader held
with conviction".
"""

from unittest.mock import patch

import pytest

from alpha_agents.pipeline.tasks import exit_decision as ED


def _wake(code, thesis_id=7, kind="theme_flow_negative"):
    return {"type": "signal", "code": code, "thesis_id": thesis_id,
            "kind": kind, "reason": "你声明的失效条件触发。还持有吗？"}


class TestWhatCountsAsUnanswered:
    def test_a_woken_code_missing_from_the_reply_is_named(self):
        with patch("alpha_agents.data.thesis.add_checkpoint"):
            missing = ED.note_unanswered(
                [_wake("600276"), _wake("002555", 8)],
                [{"code": "002555", "action": "hold"}])
        assert missing == ["600276"]

    def test_an_explicit_hold_is_an_answer(self):
        with patch("alpha_agents.data.thesis.add_checkpoint"):
            assert ED.note_unanswered(
                [_wake("600276")],
                [{"code": "600276", "action": "hold"}]) == []

    def test_no_decisions_at_all_leaves_every_wake_unanswered(self):
        """The timeout case."""
        with patch("alpha_agents.data.thesis.add_checkpoint"):
            missing = ED.note_unanswered([_wake("600276"), _wake("002555", 8)],
                                         [])
        assert sorted(missing) == ["002555", "600276"]

    def test_a_bare_rule_signal_is_not_a_wake(self):
        """Only a thesis coming due asks the agent about its own words."""
        bare = {"type": "signal", "code": "600276", "reason": "跌破均线"}
        assert ED.note_unanswered([bare], []) == []

    def test_no_signals_is_not_a_finding(self):
        assert ED.note_unanswered(None, [{"code": "x", "action": "sell"}]) == []


class TestItIsRecordedNotJustLogged:
    def test_a_checkpoint_says_the_question_went_unanswered(self):
        with patch("alpha_agents.data.thesis.add_checkpoint") as point:
            ED.note_unanswered([_wake("600276", thesis_id=11)], [])
        assert point.call_args.args[0] == 11
        assert "未作答" in point.call_args.args[1]
        assert point.call_args.kwargs["kind"] == "theme_flow_negative"

    def test_it_is_logged_as_not_a_hold(self, caplog):
        with patch("alpha_agents.data.thesis.add_checkpoint"), \
             caplog.at_level("WARNING"):
            ED.note_unanswered([_wake("600276")], [])
        assert "600276" in caplog.text
        assert "not as hold" in caplog.text

    def test_a_failed_write_costs_the_record_not_the_run(self, caplog):
        with patch("alpha_agents.data.thesis.add_checkpoint",
                   side_effect=OSError("locked")):
            assert ED.note_unanswered([_wake("600276")], []) == ["600276"]


class TestEveryExitPathChecks:
    def test_the_replay_path_calls_it_on_success_and_on_failure(self):
        import inspect
        src = inspect.getsource(ED.decide_for_replay)
        assert src.count("note_unanswered") == 1, (
            "one call after the try/except covers the timeout and the "
            "failure paths as well as the answer")
        assert "return []" not in src.split("except Exception")[1][:200], (
            "an early return would skip the check on the path where "
            "nothing at all was answered")

    def test_the_live_path_calls_it_on_every_path(self):
        import inspect
        src = inspect.getsource(ED.run)
        assert src.count("note_unanswered") == 3
