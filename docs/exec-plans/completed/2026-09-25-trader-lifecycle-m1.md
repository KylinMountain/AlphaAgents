# Trader lifecycle M1: remember facts correctly

Date: 2026-09-25
Status: implemented; offline regression verified (see validation record)
Base: main@00023d5fdb7e8c6f7a98851d70843763ea48259e
Review: docs/reviews/2026-09-25-trader-lifecycle-review.md (AL-05 through AL-08)

## Delivered scope

This change addresses the M1 factual-integrity scope, not M2 or M3. No forced
orders, increased exposure, provider change, or profitability claim.

- Reject stale previous-session bars as today's returns. Market, board flow
  and account-related coverage is explicit, including missing observations.
- Store original pre-model morning inputs in append-only, hash-verified
  session events. Close review reads those inputs, not mutable evening themes.
  Visibility is distinct from selection; an absent snapshot remains unknown.
- For date-only executions, compute observed extrema from actual fills and
  strictly interior daily bars, excluding unknown entry/exit-day paths.
  Full-day potential is diagnostic; achievable exit remains unknown. Legacy
  facts are retained and are not pooled into the new extrema summary.
- Save facts before requesting an interpretation. Keep pending, failed and
  complete separate; record started/terminal attempt events. Atomic claims
  allow one logical attempt per processing day and at most three in total.
  No model consumes no attempt. Stale or duplicate callbacks cannot overwrite
  completed/newer interpretations; a cancelled attempt can resume a later day.
- Store facts and explanation availability separately. Late processing and
  midnight completion cannot appear in earlier day-level contexts. Legacy
  creation metadata is retained, never substituted by an earlier close date.
- Persist intraday views by run, trader, exchange day and security. Enrich
  shared candidates only after separating traders; never mutate the shared
  list. Missing answers are undecided, not invented intentional declines.
- Report completed interpretations and pending/failed/exhausted work distinctly.
  Intentionally facts-only replay is not a lost-learning error. Temporal
  integrity faults propagate out of the replay close-review boundary.

## Verification record

See [m1-verification.json](../../reviews/evidence/2026-09-25-trader-lifecycle/m1-verification.json)
for the exact tested source commit, workflow, commands, counts and existing
lint debt. The source and regression-test blobs submitted to main are the
exact ones tested remotely; temporary audit workflows and encoded staging
files are excluded. Tests are in `tests/test_trader_lifecycle_m1.py` plus the
existing regression suites identified in the verification record.

The existing historical-review fixture was updated to declare its simulated
write date explicitly and assert the new non-achievable peak-gap diagnostic.
Production temporal guards were not relaxed to satisfy that fixture.

## Migration and operational boundaries

Schema changes are additive on normal memory-store initialization. Stop old
workers before starting new code; retain a database backup for rollback. No
existing trade, fact or raw explanation is deleted or silently rewritten.
Old records without reliable availability metadata remain unavailable rather
than being backdated. No missing historical morning snapshot is fabricated.

Availability is conservative at day granularity, not a minute-level
point-in-time guarantee. Morning snapshots capture original input blocks,
not every internal tool call. Boundary-day exclusion may understate actual
held extrema; it does not estimate executable profits. The 45-minute memory
TTL limits context recall, not eligibility to trade or ask again.

Existing learned rules are not automatically erased or rehabilitated. Their
quality, new real-model behavior, original seven historical runs and trading
performance still require separate controlled evaluation. Changed contexts
may intentionally diverge from old recorded runs; do not silently splice them.

## Next boundary

M2: shared DecisionFrame/TraderState identity, low-cost frozen-decision tests
and continuous branching, followed by scheduler/execution isolation. M3:
connect one actionable behavior family to independent forward evaluation,
reversible promotion and truthful improvement criteria. Neither stage is
implemented by this M1 commit.
