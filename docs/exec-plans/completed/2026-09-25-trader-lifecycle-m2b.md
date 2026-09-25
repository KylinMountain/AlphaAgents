# Trader lifecycle M2-B: continuous account and learning branches

Date: 2026-09-25
Status: implemented; see the linked offline validation record
Base: main@dd58114f5e6177b2d8b8d3b4772fcf8dbb9d0f5c
Scope: continuous replay checkpoint/fork adapter, not all remaining M2 or M3

## Delivered

The existing walk-forward engine can seal a successful prefix at the end of a
session and continue multiple independent accounts from the next corpus session.
The adapter reuses its actual order, settlement, valuation and learning loop;
it does not create a second simulator or splice recorded future answers.

- `walk_forward.py --checkpoint-out` validates a fresh initial account, pins the
  model client, disables note merging, and seals only a complete unchanged-input
  prefix. Reused tables/private memory are not certified as clean history.
  Invalid or existing output destinations fail preflight, before model calls.
- `walk_checkpoint.py` copies all memory.db tables (including committed WAL),
  trader histories, local indexes and variants, plus exposure history, pending
  signals, original learning-window start and measured review context. Input
  corpora and membership data are pinned and hashed; the manifest is published
  last. Code, lockfile and effective runtime/model identities must match.
- `walk_branch.py` restores private writable state per branch and starts at the
  exact next session. Cash, invested value, market value, realized/unrealized
  profit, equity and position/order counts are reconciled before any decision.
  It neither reseeds the inherited account nor merges back to production.
  Parent model journals are not copied. Historical captures keep their original
  identity; measured learning queries the explicit parent/child lineage rather
  than forgetting parent samples or including unrelated runs.
- One optional exact knowledge intervention affects only the first open trade
  plan on the first continuation day. The frame retains its parent hash. Later
  fills, reviews and handbook rewrites follow each branch's own trajectory.
  This is NOT permanent rule removal or a new learning algorithm. An unused,
  missing or ambiguous intervention cannot be reported as a completed test.
- Every branch has an isolated subprocess, workspace, identity, journal, report
  and explicit deadline. Work is bounded by 16 branches, 30 sessions each, 200
  declared branch-sessions and two concurrent branches. Model calls require
  `--live`; mechanical child processes deny network access. The schedule seed
  is not a model seed and fresh trials need not produce matching random draws.
- Failures remain in the result set. Model-backed branches count missing/lost
  close reviews as incomplete learning, even when equity can be calculated.
  Interrupted usage is marked incomplete. Existing output directories are never
  silently reused and `walk_resume.py` refuses branch-directory tape splicing.

## Verification

See [m2b-verification.json](../../reviews/evidence/2026-09-25-trader-lifecycle/m2b-verification.json)
for exact commands, workflow, tested source, counts and source-blob matching.

The new tests cover real-engine uninterrupted-versus-resumed equity equality,
including actual orders, fills and exits; committed WAL and pending-settlement
copying; identity/private file isolation; manifest/content tampering; calendar,
mode and budget refusals; early account reconciliation; missing close reviews;
first-plan-only interventions; independent branch review/handbook generation
with deterministic stub responses and next-day retrieval; and measured learning
that includes only the parent/child lineage. Stub responses prove wiring and
isolation, not real-model learning effectiveness. Existing execution and M1/M2-A
regressions continue to run in the full suite.

The submitted code/tests and usage document are the exact verified blobs.
This completion record replaces the in-progress plan; audit-only workflows,
encoded staging and edit scripts are not submitted to main.

## Operation and limits

Usage: [walk_branch.md](../../walk_branch.md). Generate a fresh prefix on the
matching checkout and dependency environment, then start explicitly bounded
branches. Existing seven-run journals are not imported as restorable state.
No real-model calls or user historical-corpus experiments were performed for this
change; the engine equivalence check uses synthetic market fixtures.

Keep corpus/state writers quiescent during capture. SQLite backup and mutation
checks are not a distributed transaction across a live system. Pinned corpus
copies consume disk once per checkpoint. Private state is copied, not hardlinked;
read-only corpus links are application isolation, not an OS security sandbox or
protection against a hostile same-user process. Hashes establish content identity,
not data availability at historical decision time or provider authenticity.

This is the defined replay-runner state contract, not a live-process/whole-machine
snapshot. Daily information limits, current-only membership qualifications and
synthetic close execution remain explicit. Other model clients and interrupted
requests can be outside the observed agent journal; its usage is not total billing.
Historical prefix outcomes are inherited state, not fresh evidence. Suffix returns
are not automatically alpha or evidence of improvement, and no policy is promoted.

Remaining M2: full online/replay strategy convergence and scheduler/control-plane
isolation. M3: executable behavioral variants, independent forward evaluation and
reversible improvement-based promotion. These are not claimed by M2-B.
