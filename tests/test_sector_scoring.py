"""One definition of the within-sector ranking, used by live and replay.

The defect these pin. Until 2026-09-21 the live trader ranked a concept's
members by a four-factor score (``tools/sector_beta``) while the replay ranked
them by ``(change_rank + turnover_rank) / 2`` (``data/sector_panel``). A replay
therefore measured a decision the trader never makes, and its evidence
described the replay's rule rather than the strategy's. The owner's
instruction was that the two must be the same thing.

So the arithmetic moved to ``data/sector_scoring`` and both callers now
delegate. These tests pin the shared definition and — more importantly — that
neither caller has quietly grown its own copy again.
"""

import inspect

import pytest

from alpha_agents.data import sector_panel, sector_scoring
from alpha_agents.tools import sector_beta


class TestTheWeightsAreOneNumber:
    def test_the_weights_sum_to_one(self):
        assert sum(sector_scoring.FACTOR_WEIGHTS.values()) == pytest.approx(1.0)

    def test_the_live_module_re_exports_the_shared_weights(self):
        """``W_BETA`` and friends must be the shared numbers, not copies."""
        for name, key in (("W_BETA", "beta"), ("W_POSITION", "position"),
                          ("W_INSTITUTIONAL", "institutional"),
                          ("W_LIQUIDITY", "liquidity")):
            assert getattr(sector_beta, name) == pytest.approx(
                sector_scoring.FACTOR_WEIGHTS[key]), (
                f"{name} is a second definition of the ranking weights")


class TestBothPathsCallTheSameFunction:
    def test_the_live_selector_delegates_its_scoring(self, monkeypatch):
        """The live loop must call the shared scorer, not reimplement it."""
        source = inspect.getsource(sector_beta.get_sector_best_stocks_fn)
        assert "sector_scoring.score_members" in source

    def test_the_replay_panel_delegates_its_ordering(self):
        source = inspect.getsource(sector_panel._within_sector)
        assert "sector_scoring.score_members" in source

    def test_the_replay_panel_no_longer_ranks_by_turnover(self):
        """The old formula averaged change and turnover ranks.

        A regression here would silently restore the disagreement, because
        the two formulas agree often enough to pass a casual test.

        Comments are stripped first: the module docstring *describes* the
        removed formula in order to explain why it is gone, and matching that
        prose would make this test fail on its own history.
        """
        import io
        import tokenize
        code_only = []
        for token in tokenize.generate_tokens(
                io.StringIO(inspect.getsource(sector_panel)).readline):
            if token.type == tokenize.COMMENT:
                continue
            if token.type == tokenize.STRING:
                continue
            code_only.append(token.string)
        body = " ".join(code_only)
        assert "turnover_rank" not in body
        assert "_rank" not in body


class TestTheSharedScorerItself:
    def _row(self, code, change, beta=0.5, amount=8e8):
        return {"code": code, "name": code, "change_pct": change,
                "beta_weighted": beta, "avg_daily_amount": amount}

    def test_a_moderate_gain_beats_an_extreme_one(self):
        """3-6% is the sweet spot; >=6% is getting expensive."""
        rows = [self._row("600001", 4.0), self._row("600002", 8.0)]
        ranked = sector_scoring.score_members(rows)
        assert ranked[0]["code"] == "600001"

    def test_a_limit_up_name_is_dropped_not_ranked_last(self):
        rows = [self._row("600001", 4.0), self._row("600002", 9.9)]
        ranked = sector_scoring.score_members(rows)
        assert [r["code"] for r in ranked] == ["600001"]

    def test_an_illiquid_name_is_dropped(self):
        rows = [self._row("600001", 4.0),
                self._row("600002", 4.0, amount=1e6)]
        ranked = sector_scoring.score_members(rows)
        assert [r["code"] for r in ranked] == ["600001"]

    def test_an_untradable_board_is_dropped(self):
        rows = [self._row("600001", 4.0), self._row("688001", 4.0)]
        ranked = sector_scoring.score_members(rows)
        assert [r["code"] for r in ranked] == ["600001"]

    def test_beta_is_normalized_within_the_sector(self):
        """A concept of uniformly low-beta names still ranks its best first.

        A market-wide scale would hand every member of a quiet concept a
        near-zero beta score, which is a statement about the concept rather
        than about which member to buy.
        """
        rows = [self._row("600001", 4.0, beta=0.01),
                self._row("600002", 4.0, beta=0.02)]
        ranked = sector_scoring.score_members(rows)
        assert ranked[0]["code"] == "600002", (
            "the higher-beta member must win inside its own sector")

    def test_the_ranking_is_deterministic_on_ties(self):
        rows = [self._row("600002", 4.0), self._row("600001", 4.0)]
        first = [r["code"] for r in sector_scoring.score_members(rows)]
        second = [r["code"] for r in sector_scoring.score_members(rows)]
        assert first == second == ["600001", "600002"]
