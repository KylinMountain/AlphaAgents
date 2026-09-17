"""The Evidence Analyzer: the LEARN half, as a component.

The defect this replaces was structural rather than arithmetic. The split
between supporting and opposing evidence existed, was correct, and lived as
~60 lines inside ``scripts/walk_forward.py`` — outside the package, outside
the layer rules, and reachable from nothing else. The design's **Evidence
Analyzer** box had no implementation anywhere in ``alpha_agents/``.

The arithmetic rules were each learned the hard way in this repository, so
they are pinned here rather than trusted to the docstring:

* the per-trade verdict compares against the **window's median return**, not
  against zero — the first version compared against zero and, on a window
  where every trade lost money, collapsed into the grouping variable itself,
  making the support count an arithmetic identity;
* a trade with no episode counts towards ``n`` but cannot be cited;
* an empty list is written, because "searched, found none" must be tellable
  from "nobody looked".
"""

from __future__ import annotations

import pytest

from alpha_agents.evolution import evidence as EV


def _trade(pos: int, t1: float, ret: float, *, episode: int | None = None,
           code: str = "600000") -> EV.Trade:
    return EV.Trade(
        position_id=pos,
        code=code,
        return_pct=ret,
        close_date="2026-01-05",
        t1_change=t1,
        episode_id=pos if episode is None else episode,
        close_reason="target",
    )


class TestItRefusesToStateWhatItCannot:
    def test_too_few_trades_is_none_not_a_hedged_claim(self):
        assert EV.analyse([_trade(1, 1.0, 1.0), _trade(2, 2.0, -1.0)]) is None

    def test_an_empty_list_is_none(self):
        assert EV.analyse([]) is None

    def test_no_contrast_is_none(self):
        """Every trade on one side of the cut: a claim with no contrast
        cannot be wrong, which is not a virtue."""
        # All T-1 changes identical, so the cut splits nothing.
        same = [_trade(i, 1.0, float(i), code=f"60000{i}") for i in range(1, 5)]
        assert EV.analyse(same) is None

    def test_trades_without_an_episode_cannot_carry_the_claim(self):
        """Four trades but none citable: n is zero, not four."""
        rows = [_trade(i, float(i), float(i), episode=None, code=f"60000{i}")
                for i in range(1, 5)]
        for r in rows:
            object.__setattr__(r, "episode_id", None)
        assert EV.analyse(rows) is None


class TestTheSplitIsAboutReturnsNotGroupSize:
    def test_a_window_where_everything_lost_is_still_a_contrast(self):
        """The regression, as a test.

        With ``return_pct > 0`` as the test, every trade in a losing window is
        on the same side, so the support set is exactly the high-T-1 group —
        an identity that can never disagree with the medians printed beside
        it. Against the window's median the support set is a *different* set,
        which is the whole point: it can disagree, so it is evidence.

        Worked by hand on this data (median return −9.0, cut +2.5):

            trade  t1   ret   t1>cut  ret>med  supports
              1    5.0  -10.0   yes      no       yes
              2    4.0   -8.0   yes     yes        no
              3    1.0   -1.0    no     yes       yes
              4    0.5  -20.0    no      no        no

        so support is {1, 3} and opposition is {2, 4}. The zero-threshold
        version yields {1, 2} — precisely the high-T-1 group — and is why the
        count could never contradict the medians.
        """
        rows = [
            _trade(1, 5.0, -10.0),
            _trade(2, 4.0, -8.0),
            _trade(3, 1.0, -1.0),
            _trade(4, 0.5, -20.0),
        ]
        ev = EV.analyse(rows)
        assert ev is not None
        assert ev.n == 4
        assert ev.supporting == [1, 3]
        assert ev.opposing == [2, 4]
        # The load-bearing claim: support is not the high-T-1 group. If a
        # future edit reintroduces a zero threshold, this becomes [1, 2] and
        # the assertion below fails even though the counts still sum to n.
        high_group = {t.episode_id for t in rows if t.t1_change > ev.cut}
        assert set(ev.supporting) != high_group, (
            "the support set collapsed into the high-T-1 group, which makes "
            "the count an arithmetic identity rather than evidence")

    def test_the_best_loser_is_not_recorded_as_opposition(self):
        """D38's inversion, in miniature.

        The window's best trade — lowest T-1, smallest loss — is evidence
        *for* "ranking on T-1 change is backwards", and the zero-threshold
        version called it opposition.
        """
        rows = [
            _trade(1, 6.0, -12.0),
            _trade(2, 5.0, -9.0),
            _trade(3, 4.0, -7.0),
            _trade(4, 0.1, -0.5),    # the best result, lowest T-1
        ]
        ev = EV.analyse(rows)
        assert 4 in ev.supporting, (
            "the window's best trade was counted as opposing the proposition "
            "it supports")

    def test_moving_one_return_across_the_median_flips_its_side(self):
        """T-1 unchanged, so only the verdict can move."""
        rows = [
            _trade(1, 5.0, -10.0),
            _trade(2, 4.0, -8.0),
            _trade(3, 3.0, -6.0),
            _trade(4, 1.0, -20.0),
        ]
        before = EV.analyse(rows)
        assert 4 in before.opposing
        rows[3] = _trade(4, 1.0, 50.0)      # same T-1, now the best result
        after = EV.analyse(rows)
        assert 4 in after.supporting


class TestBothListsAreAlwaysStated:
    def test_an_empty_bucket_is_an_empty_list_not_a_missing_key(self):
        """``save_candidate`` refuses a missing key, so the analyzer must
        always build both — "we found no opposing evidence" has to be
        tellable from "nobody looked"."""
        # Low T-1 does much better, high T-1 does much worse: everything
        # supports, nothing opposes.
        rows = [
            _trade(1, 5.0, -10.0),
            _trade(2, 4.0, -9.0),
            _trade(3, 1.0, 8.0),
            _trade(4, 0.5, 9.0),
        ]
        ev = EV.analyse(rows)
        assert ev.opposing == []
        assert len(ev.supporting) == 4

    def test_the_payload_carries_every_per_trade_verdict(self):
        """The counts must be re-derivable from the record alone."""
        rows = [
            _trade(1, 5.0, -10.0), _trade(2, 4.0, -8.0),
            _trade(3, 1.0, 6.0), _trade(4, 0.5, 7.0),
        ]
        ev = EV.analyse(rows)
        payload = ev.payload()
        assert len(payload["trades"]) == ev.n
        assert sum(1 for t in payload["trades"] if t["supports"]) == len(ev.supporting)
        assert sum(1 for t in payload["trades"] if not t["supports"]) == len(ev.opposing)


class TestTheNumbersAreMedians:
    def test_the_contrast_is_high_minus_low(self):
        rows = [
            _trade(1, 5.0, -10.0), _trade(2, 4.0, -8.0),
            _trade(3, 1.0, 6.0), _trade(4, 0.5, 7.0),
        ]
        ev = EV.analyse(rows)
        assert ev.contrast == pytest.approx(ev.high_median - ev.low_median)
        assert ev.contrast < 0, "this window supports the proposition"

    def test_the_claim_states_n_and_the_observation_caveat(self):
        rows = [
            _trade(1, 5.0, -10.0), _trade(2, 4.0, -8.0),
            _trade(3, 1.0, 6.0), _trade(4, 0.5, 7.0),
        ]
        claim = EV.analyse(rows).claim
        assert "n=4" in claim
        assert "远低于 n≥50" in claim, (
            "the claim must not read as a result at this sample size")


class TestTheContextIsStatedFromTheRunsOwnFacts:
    def test_it_names_the_trader_the_zone_and_the_window(self):
        rows = [
            _trade(1, 5.0, -10.0), _trade(2, 4.0, -8.0),
            _trade(3, 1.0, 6.0), _trade(4, 0.5, 7.0),
        ]
        ev = EV.analyse(rows)
        ctx = EV.context_for(ev, trader="pullback", entry_zone=(0.97, 1.005),
                             window_start="2025-07-01")
        assert "pullback" in ctx
        assert "0.970" in ctx and "1.005" in ctx
        assert "2025-07-01" in ctx


class TestTheProposedDirectionFollowsTheEvidence:
    """The first production observation exposed this as a defect.

    The delta was a constant `direction: "down"`, written on the assumption
    that the proposition holds. The real book said otherwise — contrast
    **+7.68%**, high-T-1 names doing *better* — and the candidate still
    proposed ranking lower. A variant built from it would have moved the
    parameter opposite to the evidence it cited, and the citation would have
    made it look justified.
    """

    def _evidence(self, contrast: float) -> EV.Evidence:
        return EV.Evidence(claim="c", n=5, cut=0.0, median_return=0.0,
                           high_median=contrast, low_median=0.0)

    def test_a_supported_proposition_ranks_lower(self):
        assert EV.proposed_delta(self._evidence(-3.0))["direction"] == "down"

    def test_a_contradicted_proposition_ranks_higher(self):
        """Not `None`: an observation that contradicts its own proposition is
        still a finding, and the action it supports is the opposite one."""
        delta = EV.proposed_delta(self._evidence(7.68))
        assert delta["direction"] == "up"
        assert "相反" in delta["note"]

    def test_equal_medians_propose_nothing(self):
        """No direction is implied, and inventing one would be the same
        defect one step smaller."""
        assert EV.proposed_delta(self._evidence(0.0)) == {}

    def test_the_saved_candidate_carries_the_derived_direction(
            self, monkeypatch):
        """End to end through `save_observation`, not just the helper."""
        rows = [
            _trade(1, 5.0, 10.0), _trade(2, 4.0, 9.0),
            _trade(3, 1.0, -8.0), _trade(4, 0.5, -9.0),
        ]
        ev = EV.analyse(rows)
        assert ev.contrast > 0, "this window contradicts the proposition"

        captured = {}

        def _fake_save(**kwargs):
            captured.update(kwargs)
            return 1

        import alpha_agents.data.learning_candidates as LC
        monkeypatch.setattr(LC, "save_candidate", _fake_save)
        EV.save_observation(ev, source="t", source_date="2026-01-05",
                            applicable_context="ctx")
        assert captured["proposed_behavior_delta"]["direction"] == "up"

    def test_saving_a_zero_contrast_evidence_refuses(self):
        """Reachable, not hypothetical: four trades whose high and low groups
        have equal medians. Recording a candidate would need a direction, and
        there is none to derive."""
        import pytest as _pytest
        rows = [
            _trade(1, 5.0, -10.0), _trade(2, 4.0, 10.0),
            _trade(3, 1.0, 10.0), _trade(4, 0.5, -10.0),
        ]
        ev = EV.analyse(rows)
        assert ev is not None and ev.contrast == 0.0
        with _pytest.raises(ValueError, match="implies no direction"):
            EV.save_observation(ev, source="t", source_date="2026-01-05",
                                applicable_context="ctx")
