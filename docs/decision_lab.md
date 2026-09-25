# Frozen decision laboratory (M2-A)

Scope: isolate one tool-free T+1 trade-plan decision. This is not a continuous
portfolio backtest or evidence that a strategy is profitable.

## Captured state and identities

Before awaiting the provider, the actual system instructions and rendered user
message are sealed with run/trader/session/cutoff, panel codes, declared model,
capability names, planner source/template/dependency-lock identity and displayed trader context.
The original text is retained, not reassembled later from today's databases.

`policy_build_hash` describes the planner adapter, instructions, template, model
and execution options, not the full project's learning algorithm. The displayed
book/knowledge/note are `state_snapshot_hash`, not a complete restorable account.
`input_hash` identifies the exact initial model input; `frame_hash` additionally
covers identity and provenance. Hashes are integrity checks, not proof of source
truth, authenticity or point-in-time correctness.

Walk-forward's real T+1 calls supply run/trader identities and record input and
output in the additive append-only `decision_capture_events` table. The M1
session table is untouched. Repeated identical inputs have distinct invocation
IDs. Failure, cancellation and missing output are not intentional abstention.
Capture requires an idle database transaction: it refuses to commit unrelated
account writes merely to save an audit record.

Online intraday pricing shares this envelope but retains its existing tools and
parser. Its tool-bearing initial input is **observation-only** in this laboratory.
No tool responses are invented from current market data. Morning analysis,
exits and online selection have not been converted to the T+1 strategy.

## Read-only list and export

From the checkout that produced the recorded planner:

```bash
uv run python scripts/decision_lab.py list \
  --database /path/to/replay/memory.db --run-id clean --trader-id default

uv run python scripts/decision_lab.py export \
  --database /path/to/replay/memory.db --invocation-id ACTUAL_ID_FROM_LIST \
  --output /tmp/decision-frame.json
```

The connection is read-only/query-only. Export verifies event and frame hashes,
selects an exact invocation ID and never overwrites a file. Lists are bounded
by --limit (100 by default, at most 10000); narrow them by run/trader.

Captures begin with this version. Old seven-run journals are not silently
reconstructed as complete frames; importing them remains unimplemented. Frames
contain private account and knowledge context: keep them out of public repos and
apply an appropriate retention policy. Experiment directories are private to the
owner by default; this is application isolation, not an OS security sandbox.

## Compare one exact knowledge change

Arms are a JSON array. Copy `old` exactly from the exported `state.knowledge`;
it must occur once, and the full knowledge block must occur once in the input.
Missing, ambiguous and no-op edits fail. Only knowledge changes; model, prices,
candidates, account, instructions and upstream research remain fixed.

```json
[
  {"name": "without_R2", "old": "COPY THE EXACT R2 TEXT HERE", "new": ""}
]
```

Control is added automatically. More --frame arguments add cases. Duplicate
frames are rejected rather than counted as additional independent cases.

```bash
uv run python scripts/decision_lab.py run \
  --frame /tmp/decision-frame.json --arms /tmp/arms.json \
  --trials 3 --max-samples 6 --jobs 2 --timeout 180 \
  --output /tmp/decision-experiment-01 --live
```

One case x two arms x three trials = six logical samples. The maximum budget
is checked before launching; hard limits are 200 samples and four workers.
Schedule order is shuffled with --seed; it is not an LLM seed. Each worker has
a new process, private storage and journal. No source account/order/learning
writer is invoked. Existing experiment directories cannot be reused or resumed.

--live explicitly permits provider calls. The configured model name must match
the recorded declared model. The client disables model-family failover and SDK
transport retries and makes one logical attempt per sample; failures remain in
the denominator. Provider-side routing/defaults cannot be verified by this client.
Recorded request/token counts and response model names are reported. An interrupted
request may have no terminal journal entry; failed live samples mark usage incomplete,
not zero-cost. The dependency lock is included in the planner fingerprint. Counts are
not interpreted as proof the remote server's implementation was identical.

## No-model parser check

```bash
uv run python scripts/decision_lab.py run \
  --frame /tmp/decision-frame.json --response-file /tmp/original-response.txt \
  --trials 1 --max-samples 1 --output /tmp/parser-check-01
```

One explicit mode is required. Omitting both never calls a provider. Reusing an
old answer is labelled `parser_check`, not an independent model sample. It cannot
establish how the model would react to an altered prompt.

## Reading results

manifest.json freezes cases, interventions, sample order and budgets. Every
sample retains its frame, raw output, verdict, errors, hashes, worker log and
private journal. summary.json retains all samples plus per-arm status counts,
ordered-code counts, elapsed time and recorded request counts. Prices, proposed
sizes, reasons and candidate rejections remain in each verdict. No new rules
are promoted and no trading orders are placed.

The loaded planner source must match the captured code hash before a provider
call; use the recorded revision for strict reruns. Even harmless source edits
can invalidate a strict hash. Comparing different parser implementations is a
separate experiment, not implicit compatibility.

More orders is not automatically better. Failure-selected cases diagnose those
failures, not general efficacy; include justified abstention, normal entries and
missing-data cases. These are behavior/contract diagnostics, not profitability
estimates or independent market samples.

## Remaining M2/M3

Continuous account/learning forks, full online/replay strategy convergence,
scheduler/control-plane isolation and historical-journal import are deferred.
Tool-bearing snapshots do not capture a full tool trajectory. M3 behavioral
promotion is unchanged. Offline tests do not establish real-model improvement,
PIT correctness of the original seven runs or out-of-sample alpha.
