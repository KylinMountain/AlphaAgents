"""Exact policy-gene registry used by forward producers and evaluators.

Coverage is deliberately leaf-by-leaf.  A parent such as
``decision.selection_rank`` is not evidence that a producer executes every
future child added below it.
"""

from __future__ import annotations


CONFIDENCE_PRIOR_GENES = frozenset({
    "decision.confidence_priors.high",
    "decision.confidence_priors.medium",
    "decision.confidence_priors.low",
})

CONFIDENCE_DIMENSION_GENES = frozenset({
    "decision.dim_base",
    "decision.dim_step",
})

#: ``decision.selection_rank.change_share`` was removed 2026-09-21. It
#: governed a whole-market change/turnover lane mix that no trading path
#: ever ran: production selects by concept fund flow and a within-sector
#: multi-factor score, and the replay runner's sector arms never called
#: ``selection_policy`` either. Its own promotion-grade producer declared
#: the gene without executing it, so it was a declared-but-dead leaf. The
#: repository's RP-05 plan had already marked it "旧策略参数，不把它当作已经
#: 验证的 alpha 来源"; this removes it instead of carrying it forward.

THEME_GATE_GENES = frozenset({
    "decision.theme_gate.w_flow",
    "decision.theme_gate.w_rel",
    "decision.theme_gate.w_confirm",
    "decision.theme_gate.admit_score",
    "decision.theme_gate.cancel_score",
})

KNOWN_POLICY_GENES = frozenset().union(
    CONFIDENCE_PRIOR_GENES,
    CONFIDENCE_DIMENSION_GENES,
    THEME_GATE_GENES,
)


class GeneRegistryError(ValueError):
    """A changed gene is unknown or outside an exact observation surface."""


def assert_exact_coverage(changed_genes: list[str], supported_genes,
                          *, actor: str) -> None:
    """Fail closed unless every changed leaf is known and exactly supported."""
    supported = frozenset(supported_genes)
    non_leaf = sorted(gene for gene in supported if gene not in KNOWN_POLICY_GENES)
    if non_leaf:
        raise GeneRegistryError(
            f"{actor} registers unknown or non-leaf gene(s): "
            + ", ".join(non_leaf))
    unknown = sorted(gene for gene in changed_genes
                     if gene not in KNOWN_POLICY_GENES)
    if unknown:
        raise GeneRegistryError(
            "unknown changed policy gene(s): " + ", ".join(unknown))
    uncovered = sorted(gene for gene in changed_genes if gene not in supported)
    if uncovered:
        raise GeneRegistryError(
            f"{actor} cannot observe changed gene(s): "
            + ", ".join(uncovered))
