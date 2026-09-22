"""How much to bet, and how many bets, are the agent's — by default.

Calling ``size_pct`` "the agent's decision" was not true. Its number was the
first term of a ``min()`` over six others, and the code said so itself:
"the size the agent asked for is a target, not a ceiling — the ceiling is
MAX_POSITION_PCT". The six were MAX_POSITION_PCT at 10%, a risk budget
derived from HARD_STOP_PCT, MAX_THEME_PCT at 30%, the sentiment phase's
total exposure, a correlated-cluster cap, and a drawdown gate that cancels
outright.

One of them was incoherent as well as unasked-for: the risk budget sizes
from HARD_STOP_PCT, and ``--autonomous`` switches that stop off. Positions
were sized against a mechanism that could not fire.

They are not deleted. They are **policy parameters**, defaulting to off, in
the same block the probability mapping and the theme gate already live in —
so a cap that returns does so because ``holdout_gate`` moved the pointer on
a forward-only paired test, not because someone edited a yaml. That is the
difference between "no limits" and "limits that learn".

What stays unconditional is not policy: cash on hand, T+1 settlement and
ADV20 liquidity are facts about the market and the account.
"""

import pytest

from alpha_agents.agents.json_reply import picks_line
from alpha_agents.agents.t1_decider import _thesis_fields
from alpha_agents.data import scoring


class TestTheEnvelopeIsPolicyAndDefaultsOff:
    def test_every_term_is_off_by_default(self):
        sizing = scoring.DEFAULT_DECISION_PARAMS["sizing"]
        assert sizing["max_position_pct"] is None
        assert sizing["max_theme_pct"] is None
        assert sizing["cluster_cap"] is False
        assert sizing["sentiment_scaling"] is False
        assert sizing["risk_budget_sizing"] is False
        assert sizing["drawdown_gate"] is False

    def test_it_lives_where_the_other_decision_params_do(self):
        """Not a parallel mechanism: holdout_gate already moves this block."""
        params = scoring.in_force_decision_params()
        assert "sizing" in params
        assert "confidence_priors" in params, "same block as the rest"

    def test_a_version_can_bring_a_cap_back(self, monkeypatch):
        from alpha_agents.data import portfolio
        monkeypatch.setattr(scoring, "in_force_decision_params",
                            lambda: {"sizing": {"max_position_pct": 0.10}})
        assert portfolio._sizing_policy()["max_position_pct"] == 0.10
        assert portfolio._sizing_policy()["cluster_cap"] is False, "merged"

    def test_a_broken_policy_read_does_not_reinstate_a_cap(self, monkeypatch, caplog):
        """A run that quietly bet small would read as a cautious trader."""
        from alpha_agents.data import portfolio

        def _boom():
            raise RuntimeError("registry down")
        monkeypatch.setattr(scoring, "in_force_decision_params", _boom)
        with caplog.at_level("WARNING"):
            assert portfolio._sizing_policy()["max_position_pct"] is None
        assert "Sizing policy unavailable" in caplog.text


class TestThePerTradeNumberStands:
    @pytest.mark.parametrize("value", [0.0001, 0.03, 0.25, 0.6, 1.0])
    def test_no_ceiling_on_what_the_agent_asks(self, value):
        assert _thesis_fields({"size_pct": value}, "600519")["size_pct"] == value

    def test_zero_is_still_not_an_order(self):
        assert "size_pct" not in _thesis_fields({"size_pct": 0.0}, "600519")

    def test_the_sizing_lookup_no_longer_raises_a_small_probe(self):
        """max(0.005, ...) turned a deliberate 0.1% probe into 0.5%."""
        import inspect
        from alpha_agents.data import portfolio_sizing
        src = inspect.getsource(portfolio_sizing._wanted_pct)
        assert "max(0.005" not in src
        assert "min(1.0" in src, "the whole book is still the whole book"


class TestHowManyIsAlsoTheAgents:
    def test_unlimited_is_a_sentence_not_a_number(self):
        line = picks_line(None)
        assert "由你决定" in line and "没有条数上限" in line
        assert "可用现金" in line, "the real constraint replaces the fake one"

    def test_a_cap_still_renders_when_one_is_asked_for(self):
        assert "最多 **2** 单" in picks_line(2)

    def test_the_selector_stops_truncating(self):
        from alpha_agents.agents import sector_stock_selector as S
        panel = [{"code": f"00000{i}", "primary_theme": "AI"} for i in range(5)]
        raw = '{"stocks": [%s]}' % ",".join(
            f'{{"code": "00000{i}", "reason": "r", "primary_theme": "AI"}}'
            for i in range(5))
        got = S.parse(raw, {row["code"] for row in panel}, picks=None)
        assert len(got["stocks"]) == 5
        assert not [r for r in got["refused"] if r["why"] == "too_many"]

    def test_it_still_truncates_when_a_cap_is_given(self):
        from alpha_agents.agents import sector_stock_selector as S
        panel = [{"code": f"00000{i}", "primary_theme": "AI"} for i in range(5)]
        raw = '{"stocks": [%s]}' % ",".join(
            f'{{"code": "00000{i}", "reason": "r", "primary_theme": "AI"}}'
            for i in range(5))
        got = S.parse(raw, {row["code"] for row in panel}, picks=2)
        assert len(got["stocks"]) == 2

    def test_the_replay_default_is_no_cap(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import walk_forward as wf
        parser = wf._parser() if hasattr(wf, "_parser") else None
        if parser is None:
            import inspect
            src = inspect.getsource(wf)
            assert '"--picks", type=int, default=None' in src
        else:
            assert parser.get_default("picks") is None
