# Decision and learning integrity repair

Date: 2026-09-25
Status: in progress
Base: ce83ce6b507be55b190c585c4f3d09495c17dd19

## Goal

Repair the three confirmed defects without forcing additional trades or claiming
an improvement in returns. Review the complete simulated-trader lifecycle.

## Acceptance

1. An empty model order list without a non-empty no-trade explanation is an
   incomplete decision, not a successful abstention. Preserve explanations and
   candidate rejections through the opportunity record into the close review.
2. A valid empty handbook clears its active rules. A malformed non-empty rule
   list cannot clear the handbook by accident.
3. Changed/reintroduced rule text has a new version and start date. Forward
   evidence must bind the exact rule version visible before the order; a trade
   opened before a rule existed is not its forward test. Legacy unbound reviews
   remain visible but cannot count as version-specific forward evidence.
4. Regression tests cover positive/negative boundaries, history, reintroduction,
   stored explanations and exact evidence attribution. No new model calls,
   minimum exposure targets or retry-until-buy behavior.
5. Full repository tests and existing linters are run in an isolated environment.
   Missing historical corpus / provider validation is disclosed separately.

## Decisions

- Reuse immutable opportunity contexts instead of introducing another database.
- Keep R1/R2 display IDs; add machine-computed content/version bindings.
- Review notes are observations, not proof of causal profitability.
- The original seven local runs are user-reported, not reproduced here.
