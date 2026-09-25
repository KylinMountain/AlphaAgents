# Trader lifecycle M2-B: continuous replay branches

Status: in progress
Base: main@dd58114f5e6177b2d8b8d3b4772fcf8dbb9d0f5c

## Goal

Continue the existing walk-forward engine from a successful end-of-session
checkpoint, with independent accounts, learning stores and fresh model journals.
This is not a live-account restore service or strategy-promotion approval.

## Acceptance criteria

- A checkpoint is emitted by a clean runner boundary, not reconstructed from
  an arbitrary old directory. Store memory.db, trader rule histories, local
  indexes, pinned corpus, effective arguments and carried decision state.
- SQLite backups include committed WAL transactions. No mutable DB/file is
  hardlinked between branches. Reject symlinks in private state and changes
  during capture. Check hashes before launching and after completion.
- Restore only at the next corpus session, on the identical source/dependency
  and runtime contract. Do not reseed an existing account or merge observations
  back to production. Never read the parent's later model answers.
- One optional exact knowledge intervention is applied to the first eligible
  open plan in the first branch session, never to historical rule records.
  The two branches then trade and learn independently. It is not a permanent
  rule removal; an absent/ambiguous/unused intervention is not success.
- Per-branch private process, identity, deadline, report, and journal; at most
  16 branches, 30 sessions each and 200 branch-sessions per experiment.
  Failed branches remain in the report. No retry-until-profit or resume/splice.
- Tests compare uninterrupted and checkpoint-resumed mechanical executions;
  cover snapshot tampering, private-state isolation, pending settlement,
  carried learning context, time-boundary rejection and intervention semantics.
- Full existing offline regression suite and invariant checks must pass before
  the application changes reach main. Real model/profitability tests deferred.

## Decision log

2026-09-25: implement a runner-owned end-of-window checkpoint first. Do not
claim every old journal is a restorable account. Reuse the existing execution,
valuation and learning loop rather than introduce a second backtest engine.
