# Trader lifecycle M2-A: frozen decision laboratory

Date: 2026-09-25
Status: implemented; offline regressions verified
Base: main@dea9c519ba1d61233bb81c2b719a527acde9e7cc
Scope: the first M2 increment, not all of M2 or M3

## Delivered

A T+1 trade plan can now be captured before the model runs, exported without
writing the source database, and sampled again through the same execution and
parsing seam with one explicitly specified knowledge change. This is a local
behavior experiment, not a continuous account backtest.

- Immutable canonical DecisionFrame and displayed TraderState separate the
  initial input, changing trader context, planner build and enclosing capture
  identities. Planner source, template and dependency lock are fingerprinted.
  Nested callers cannot mutate a sealed frame. A changed source hash refuses
  the experiment before a provider call rather than silently using new code.
- Real walk-forward T+1 calls bind run/trader identities. The additive
  decision_capture_events stream stores pre-call inputs and distinct output,
  failure or missing-output observations. Captures are one-shot, append-only,
  and do not commit unrelated business transactions. Failed inserts roll back
  their own transaction. A parse failure is not an intentional abstention.
- Normal and frozen T+1 decisions share one model runner and the original
  parse_orders implementation. Existing prompt text, tools, trading rules and
  ordinary retry defaults are preserved. No forced orders or sizing change.
- Online intraday entry pricing uses the same capture envelope but retains its
  existing strategy and tools. Its initial-input frame is observation-only:
  this lab refuses to invent or query missing historical tool results.
- scripts/decision_lab.py provides list/export and bounded comparison runs.
  Source access is read-only/query-only. An intervention must name one exact,
  unique knowledge fragment; only that block is replaced and the parent frame
  hash is retained. Missing/ambiguous/no-op edits fail before sampling.
- Arms are interleaved, failed samples remain in the denominator, and every
  sample has a separate subprocess, private storage and journal. The hard
  limits are 200 logical samples, four workers and an explicit deadline.
  A schedule seed is not a deterministic model seed.
- --live explicitly enables new provider samples. Declared model identity must
  match; client failover and SDK transport retries are disabled for experiments.
  --response-file is a no-model parser check, not a new behavioral observation.
  The source account is not advanced and no order or learning rule is written.
- Logs retain exact inputs, raw replies, verdicts, interventions and hashes.
  Usage reports count journaled requests only. Interrupted calls can be absent
  from that journal, so failed live samples mark usage incomplete, not free.
  Existing experiment directories are never silently overwritten or resumed.

## Evidence

Validation run 36096258524, job 107949043476, tested source commit
0d4dc51e85d49d1b6890a5baf07f708ad3809440. Locked dependencies were installed with
uv sync --frozen --extra dev.

Targeted regressions: 204 passed. Full suite: 3528 passed, 20 skipped, 2 warnings.
This is 57 additional passing cases over M1's 3471-pass baseline. Existing
fork warnings remain; skipped cases are not counted as passing. Harness,
documentation and policy checks passed with unchanged existing lint debt.

The tests cover exact normal/frozen input and parser equivalence, pre-call
persistence, tamper detection, identity isolation, transaction rollback,
independent parser subprocesses, explicit mode/budget enforcement, refusal of
unsupported frames, failed-sample retention and incomplete usage reporting.
They do not call a real model or establish a trading edge.

See [verification](../../reviews/evidence/2026-09-25-trader-lifecycle/m2a-verification.json)
and [usage](../../decision_lab.md). The code/test blobs and usage document
submitted to main are the exact ones verified on the audit branch; this
completion record replaces the tested in-progress plan. Audit-only workflows,
patch staging and edit scripts are excluded from main.

## Operational and evidence boundaries

Schema initialization adds a new table and triggers; existing trades and M1
records are not rewritten. Back up the database and stop old workers before
starting a new deployment. Retain private account/knowledge snapshots under an
appropriate retention policy; application isolation is not an OS security sandbox.

Captures start with this version. Old seven-run journals are not backfilled into
complete frames. Historical-journal import remains separate work. Planner hashes
establish content identity, not point-in-time truth or provider authenticity.
The close-phase frame remains explicitly synthetic, not strict 14:55 information.
Gateway-side routing and defaults cannot be guaranteed by the client.

TraderState is the state shown to one decision, not a complete restore point for
holdings, settlement, learning or tool-side effects. The planner build hash is
not a complete identity of every learning algorithm in the repository. Changed
account trajectories cannot reuse the old run's downstream model answers.

## Remaining M2 / M3

M2 still needs continuous account/learning forks, full online/replay strategy
alignment and scheduler/control-plane isolation. Initial tool-bearing captures
are not full trajectory replay. M3's executable behavioral variants, independent
forward evaluation and reversible improvement-based promotion are unchanged.
Neither the user's original seven runs nor real-model behavior, profitability,
drawdown improvement or out-of-sample alpha was verified in this increment.
