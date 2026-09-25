# Decision and learning integrity repair

Date: 2026-09-25
Status: completed (repair and review; follow-on lifecycle work remains)
Base: ce83ce6b507be55b190c585c4f3d09495c17dd19
Repair commit: 2af26fb8fbaef817b6d2e2add2dfb2bcbdd0921f

## Goal

Repair the three confirmed defects without forcing additional trades or claiming
an improvement in returns. Review the complete simulated-trader lifecycle.

## Acceptance and results

1. Empty model orders without a non-empty no-trade explanation are incomplete,
   not successful abstention. Explanations and candidate rejections reach the
   immutable opportunity context and the close review. Covered by regression tests.
2. A valid empty handbook clears active rules. Malformed non-empty lists cannot
   clear or partially replace rules. Old history stays available. Tests pass.
3. Changed/reintroduced rules have new versions and start dates. Forward evidence
   requires the exact version and a later order date; legacy unbound reviews
   remain observations, not retroactively certified evidence. Tests pass.
4. Order-time rule bindings replace close-time rule attribution in trade reviews.
   No new model calls, minimum exposure targets or retry-until-buy behavior.
5. Locked GitHub Actions run 36086737643: 108 targeted tests passed; 3435 full-suite
   tests passed, 20 skipped, 2 existing fork warnings. All three repository linters
   passed. Historical corpus and real-provider performance were not validated.

## Decisions

- Reuse immutable opportunity contexts instead of introducing another database.
- Keep R1/R2 display IDs; add machine-computed content/version bindings.
- Review notes are observations, not proof of causal profitability.
- The seven local runs remain user-reported, not reproduced here.
- Main receives only tested application blobs, not temporary audit utilities.
- Day-level history binding is not yet a complete actual-input snapshot.

## Review and follow-on work

See [the lifecycle review](../../reviews/2026-09-25-trader-lifecycle-review.md)
for AL-01 through AL-10, synthetic reproductions, and the ordered M1/M2/M3 plan.
Those findings are not represented as repaired by this change.
