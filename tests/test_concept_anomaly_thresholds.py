"""The concept-anomaly thresholds are named, and that is all they are.

Why this test exists. Before 2026-09-21 the five conditions lived as literal
pairs inside `if` branches, so the set of variables that decide *whether the
trader looks at a concept at all* could not be enumerated. Naming them is the
precondition for ever moving one on evidence.

What this pins, and what it deliberately does not:

* the values are unchanged from the inline literals, so the extraction is a
  rename and not a behaviour change;
* they are **not** in the policy gene registry. A gene needs a forward sample
  to be graded on, and the concept-flow history is 13 sessions with 9
  overlapping the replay corpus, far short of the 50 the repository requires.
"""

from alpha_agents.data import scoring
from alpha_agents.evolution import gene_registry
from alpha_agents.pipeline.tasks import anomaly_scan as A


class TestTheThresholdsAreTheOnesThatWereInline:
    def test_the_five_conditions_keep_their_original_numbers(self):
        """A rename, not a behaviour change."""
        t = A.CONCEPT_ANOMALY_THRESHOLDS
        assert (t["a_change_pct"], t["a_net_flow_yi"]) == (1.0, 3.0)
        assert (t["b_change_pct"], t["b_net_flow_yi"]) == (2.0, -1.0)
        assert (t["c_change_pct"], t["c_net_flow_yi"]) == (1.0, 5.0)
        assert (t["d_change_pct"], t["d_net_flow_yi"]) == (-1.5, -3.0)
        assert (t["e_change_pct"], t["e_net_flow_yi"]) == (-2.0, 2.0)

    def test_the_scan_limits_are_named_too(self):
        assert A.CONCEPT_SCAN_LIMITS["ranking_top_n"] == 10
        assert A.CONCEPT_SCAN_LIMITS["gainers_examined"] == 5
        assert A.CONCEPT_SCAN_LIMITS["losers_examined"] == 3

    def test_no_literal_threshold_survives_in_the_branches(self):
        """The extraction has to leave no second copy behind."""
        import inspect
        source = inspect.getsource(A._detect_anomalies)
        for literal in ("chg > 1.0", "flow > 3", "chg > 2.0", "flow < -1",
                        "chg < 1.0", "flow > 5", "chg < -1.5", "flow < -3",
                        "chg < -2.0", "flow > 2"):
            assert literal not in source, (
                f"{literal!r} is still inline; the named table is not the "
                "only definition any more")


class TestTheyAreNotGenesYet:
    def test_they_are_absent_from_the_policy_registry(self):
        """Nothing may move them until a forward sample can grade the move."""
        for name in A.CONCEPT_ANOMALY_THRESHOLDS:
            assert f"decision.concept_anomaly.{name}" not in (
                gene_registry.KNOWN_POLICY_GENES)

    def test_they_are_absent_from_the_decision_params(self):
        """Keeping them out of the decision block keeps every frozen version
        and variant hash from churning for a number nothing can yet move."""
        assert "concept_anomaly_thresholds" not in scoring.DEFAULT_DECISION_PARAMS
