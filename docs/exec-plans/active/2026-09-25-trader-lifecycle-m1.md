# Trader lifecycle M1: remember facts correctly

Date: 2026-09-25
Status: in progress
Base: main@00023d5fdb7e8c6f7a98851d70843763ea48259e
Review: docs/reviews/2026-09-25-trader-lifecycle-review.md (AL-05 through AL-08)

## Scope

Factual integrity before trading-policy changes. No forced orders, increased
exposure, alternate model, or profitability claim. M2/M3 remain subsequent work.

## Acceptance

- Missing current bars never become today's return; partial market, flow and
  account-related coverage is explicit, not an assertion of complete data.
- Persist actual morning inputs before the model call, including a failed or
  empty run. Never reconstruct morning visibility from evening active themes.
- Date-only fills exclude unknown boundary-day extrema. Observed extrema,
  whole-day potential, and an achievable exit are not interchangeable.
- Facts survive failed reviews. Track pending/failed/complete, append-only
  attempts, bounded retries, no duplicate samples and no stale overwrite.
- Late interpretation/facts cannot appear in earlier day-level replay context.
- Intraday memory persists and is isolated by run/trader/exchange day/code.
  No response is not an intentional decline; prior views are not buy filters.
- Targeted and full offline tests and existing lint gates pass. Report skips.

## Decisions and limitations

Daily-only extrema are conservative: use actual fills and interior sessions;
no attainable exit or precise execution phase is fabricated. Historical facts
are retained with their original semantics, not silently recalculated.
Legacy review visibility uses actual creation metadata, not a backdated close.
One logical interpretation attempt per processing day, maximum three. Existing
provider transport retries may still occur; no model consumes no attempt.
The session-memory TTL remains 45 minutes for context, not eligibility.
M2 will unify full DecisionFrame/policy/state identity across entry points;
this change captures original morning inputs but not every internal tool call.
