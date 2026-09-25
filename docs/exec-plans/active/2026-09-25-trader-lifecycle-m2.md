# Trader lifecycle M2: isolate and compare a decision

Date: 2026-09-25
Base: main@dea9c519ba1d61233bb81c2b719a527acde9e7cc
Status: M2-A implementation under verification

## This increment: M2-A

Capture the actual model-bound input with explicit run/trader/time/capabilities;
separate the planner implementation identity from changing trader context; rerun
one tool-free T+1 plan through the original runner and parser. Online entry
pricing uses the same capture envelope, but its tool-bearing initial input is
not advertised as a complete replayable trajectory.

Acceptance:

1. Canonical immutable frames detect corruption and nested mutation cannot
   alter identities. Knowledge interventions specify one exact unique source
   fragment and retain the parent identity; no other input may change.
2. Save input before awaiting a provider. Normal response, invalid decision,
   timeout, cancellation and missing output remain distinct.
3. T+1 and online pricing share a capture contract without changing their
   prompts, tools, trading rules, ordinary provider retries or account logic.
4. Tool-free experiments use the same planner execution/parser seam. Tool-bearing
   frames, unknown identity and changed source code are refused rather than
   reconstructed from current market data.
5. Export is read-only. Each sample has a fresh process, private storage and
   journal; no source account/order/learning writer is invoked. Provider calls
   require --live. Failures are retained, not resampled until a trade appears.
6. Experiments fix non-knowledge input, interleave arms, cap logical samples and
   record complete input/output. Reusing raw responses is a parser check, not an
   independent model trial or a profit estimate.
7. Targeted and full offline regressions, architecture, documentation and policy
   checks pass. Real-model/corpus results are not inferred from unit tests.

## Deferred within M2

Continuous account/learning trajectory branching, complete online/replay strategy
convergence, scheduler/control-plane isolation, and historical-journal import.
M3 behavioral evolution/promotion is unchanged. This increment is the frozen
planning seam, not an assertion that one prompt is the entire trading agent.
