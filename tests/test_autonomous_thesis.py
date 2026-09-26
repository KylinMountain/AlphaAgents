"""The agent decides size, exits and what counts as being wrong.

Three things were not the agent's before: how much to buy, whether something
else could force a sale, and whether its stated invalidations were ever
checked. All three come from one missing object — the replay placed orders
and never wrote a Thesis, and its own report said so ("no theses and no
predictions: exits are stop/target only").

Without a thesis, portfolio_sizing._wanted_pct finds nothing and every
position is the constant in traders/*.yaml; thesis.evaluate has no conditions
to check, so the only exits left are price ones; and the learning step can ask
whether a trade made money but not whether its reason was right.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from alpha_agents.agents.t1_decider import _thesis_fields  # noqa: E402


def test_the_agents_own_numbers_come_through():
    out = _thesis_fields(
        {"size_pct": 0.08, "prob": 0.55, "conviction": 0.7,
         "horizon_days": 7}, "600519")
    assert out == {"size_pct": 0.08, "prob": 0.55,
                   "conviction": 0.7, "horizon_days": 7}


def test_saying_nothing_is_not_saying_zero():
    """An absent size means "use the trader's default", never "open nothing"."""
    assert _thesis_fields({"reason": "r"}, "600519") == {}
    assert _thesis_fields({"size_pct": None, "prob": ""}, "600519") == {}


@pytest.mark.parametrize("field,value", [
    ("size_pct", 0.0),       # not an order
    ("prob", 1.5),
    ("conviction", -0.2),
    ("horizon_days", 400),
])
def test_an_out_of_range_number_is_dropped_not_clamped(field, value):
    """Clamping would let a model asking for 90% read as if it asked for 50%."""
    assert field not in _thesis_fields({field: value}, "600519")


@pytest.mark.parametrize("value", [0.0001, 0.10, 0.5, 0.9, 1.0])
def test_size_pct_has_no_ceiling(value):
    """How much to bet is the decision this design exists to hand over.

    The ceiling was 0.5, and because out-of-range values are *dropped*, an
    agent asking for 90% of the book silently became the trader's 3%
    default with nothing saying its answer had been replaced. What bounds a
    position now is cash and T+1 settlement, which reservations enforce
    because they are facts about the account rather than opinions about
    risk.
    """
    assert _thesis_fields({"size_pct": value}, "600519")["size_pct"] == value


def test_a_non_numeric_value_is_dropped():
    assert _thesis_fields({"size_pct": "大仓"}, "600519") == {}


def test_only_conditions_the_evaluator_can_read_survive():
    """A condition nothing will check is worse than no condition at all.

    It looks like the risk was considered while nothing will ever fire.
    """
    out = _thesis_fields({"invalidations": [
        {"kind": "theme_flow_negative", "value": 1, "note": "资金转净流出"},
        {"kind": "vibes_turn_bad", "value": 3, "note": "感觉不对"},
        {"kind": "drawdown_from_peak", "value": 8},
    ]}, "600519")
    kinds = [c["kind"] for c in out["invalidations"]]
    assert kinds == ["theme_flow_negative", "drawdown_from_peak"]


def test_no_readable_condition_means_no_key_at_all():
    assert "invalidations" not in _thesis_fields(
        {"invalidations": [{"kind": "made_up"}]}, "600519")


# ── the single switch ────────────────────────────────────────────────────

def _flags(**kw):
    """The real derivation, not a copy of it."""
    import walk_forward as wf
    base = dict(decider="llm", autonomous=False, agent_exits=False,
                mechanical_stop=True, mechanical_target=True)
    base.update(kw)
    out = wf.autonomy_flags(SimpleNamespace(**base))
    out.pop("autonomous")
    return out


_AGENT_ONLY = {"agent_exits": True, "mechanical_stop": False,
               "mechanical_target": False}


def test_an_llm_run_has_no_stop_and_no_target():
    """No price sells on the trader's behalf (2026-09-26), whatever flags an
    older command line still passes."""
    assert _flags() == _AGENT_ONLY
    assert _flags(agent_exits=True) == _AGENT_ONLY
    assert _flags(autonomous=True) == _AGENT_ONLY


def test_only_the_placeholder_keeps_mechanical_levels():
    """It has no agent to ask, so without levels nothing could ever exit."""
    assert _flags(decider="placeholder") == {
        "agent_exits": False, "mechanical_stop": True,
        "mechanical_target": True}


class TestThePromptDoesNotAnchorTheThreshold:
    """A number in the example becomes the answer.

    Measured on a 29-day autonomous replay: of 15 theses that declared
    ``drawdown_from_peak``, **11 chose exactly 8.0** — the value the example
    carried. The notes justified it as "约1.5倍ATR" on one stock and "约2倍
    ATR" on another, and on a third as exceeding "ATR 2.37%"; a single
    threshold cannot be 1.5, 2 and 3.4 ATRs at once, so the ATR arithmetic
    was written after the number was chosen, not before.

    The example's own note made it worse: it paired ``value: 8`` with
    "峰值回吐八成", which is 80%, not 8%. The prompt taught the pairing of a
    number with an unrelated justification.
    """

    def _prompt(self) -> str:
        from pathlib import Path
        import alpha_agents
        return (Path(alpha_agents.__file__).parent / "prompts"
                / "sector_trade_plan.md").read_text()

    def test_the_example_drawdown_is_not_a_round_number(self):
        """A value that looks computed resists being copied as a default."""
        import re
        m = re.search(r'"drawdown_from_peak", "value": ([\d.]+)', self._prompt())
        assert m, "the example must still show the field"
        value = float(m.group(1))
        assert value != int(value), f"{value} is round enough to copy"

    def test_the_example_note_states_the_arithmetic(self):
        import re
        m = re.search(r'"drawdown_from_peak"[^}]*"note": "([^"]+)"', self._prompt())
        assert m and "ATR" in m.group(1), "the note must show where it came from"

    def test_the_example_note_agrees_with_its_own_value(self):
        """"八成" described 80% next to a value of 8."""
        assert "峰值回吐八成" not in self._prompt()

    def test_the_rules_forbid_copying_the_example(self):
        text = self._prompt()
        assert "不要照抄" in text
        assert "ATR" in text


class TestAutonomousNeedsAPromptThatAsksForTheThesis:
    """``--autonomous`` turns off the mechanical stop and target because the
    agent's own invalidations are meant to replace them. With a template that
    never asks for them, both are off and nothing is on.

    Measured 2026-09-22 on a run launched that way: 7 theses, every one
    ``conditions=[]`` and ``entry_fraction=0.0``. Sizing fell back to the
    trader default and the report said ``thesis_triggered 0``, which reads as
    "none fired" rather than "there were none to fire". The default
    architecture loads ``t1_decide.md`` — six price fields and nothing else.
    """

    def _check(self, *, autonomous, prompt, arch="dual_rank_v0"):
        import walk_forward as wf
        return wf._require_thesis_prompt(
            SimpleNamespace(autonomous=autonomous), prompt, arch)

    def test_a_prompt_without_invalidations_is_refused(self):
        with pytest.raises(SystemExit) as e:
            self._check(autonomous=True, prompt="只输出 code/entry_low/stop_loss")
        assert "invalidations" in str(e.value)
        assert "sector_first_v0" in str(e.value)

    def test_a_prompt_that_asks_for_them_passes(self):
        assert self._check(
            autonomous=True,
            prompt='{"invalidations": [{"kind": "drawdown_from_peak"}]}',
            arch="sector_first_v0") is None

    def test_without_autonomous_the_rails_are_on_and_it_is_not_this_check(self):
        """The mechanical stop still runs, so a thesis-less prompt is a
        legitimate configuration — it is the pairing that is empty."""
        assert self._check(autonomous=False, prompt="no thesis here") is None

    def test_the_shipped_templates_split_the_way_the_message_says(self):
        """The remedy names sector_trade_plan.md; if that stopped being true
        the message would send the operator somewhere useless."""
        from alpha_agents.config import PROMPTS_DIR
        assert "invalidations" in (PROMPTS_DIR / "sector_trade_plan.md").read_text()
        assert "invalidations" not in (PROMPTS_DIR / "t1_decide.md").read_text()
