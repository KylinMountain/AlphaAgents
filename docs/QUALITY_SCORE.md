# Quality score

Each area graded on the evidence available, tracked over time. A grade is
a claim about how much of the area is verified — not how much code exists.

Scale: **A** covered by tests and validated against data · **B** tested,
not validated · **C** works, thinly tested · **D** known gaps · **F** not
trustworthy.

_Last updated 2026-09-14. The original rows were last reviewed 2026-09-07;
the trader-core rows were added 2026-09-12 and none of them has production
data yet. A grade can fall — see the note at the end._

| Area | Grade | Basis | Gap |
|---|---|---|---|
| `data/scoring.py` | **A** | 27 tests; verified end to end on real history | — |
| `evolution/holdout_gate.py` | **B** | `test_holdout_gate.py` (44 tests today) plus the candidate-bound and evidence-scope groups in `test_gate_candidate_bound.py` and `TestTheDeclaredFloorAgainstTheRepositorysRule` | Its floor and the repository's rule now name the same number: D15 was closed 2026-09-15 by **lowering the rule to 20**, not by raising the code. What stays thin is the evidence, not the gate — it *is* called now (`pipeline/tasks/shadow_run.py` asks it once per experiment), but every verdict so far is an abstention over an experiment that has not started. |
| `evolution/principle_scoring.py` | **A** | 16 tests incl. decay weighting | Needs graded predictions to act on |
| `data/decision_context.py` | **A** | 19 tests; replay verified | No history to replay |
| `tools/exit_signals.py` | **B** | 19 tests; regime rule forward-tested over 24 windows | Patterns kept for display are known-useless |
| `evolution/playbook.py` | **B** | 13 capacity tests | Clustering lost a dimension (D5) |
| `data/vector_store.py` | **A** | 21 tests incl. 3000-vector scale and a naive-numpy cross-check | — |
| `sources/` | **B** | 16 structural tests; all 13 feeds probed live | CLS down upstream; 2 feeds degraded |
| `pipeline/tasks/news_ingest.py` | **B** | Window reads tested; ingest verified live | No test for a multi-hour outage |
| `data/portfolio.py` + `portfolio_exit.py` + `portfolio_book.py` | **C** | Lifecycle exercised indirectly; the exit slice moved out in Phase 1, the order book's reads and cancels in the pending-order round (22 tests in `test_pending_orders_finishable.py`, six mutation probes) | Service code in the storage layer (D1); an order with no live quote neither checks its thesis nor expires (D13) |
| `pipeline/tasks/morning_scan.py` | **C** | Imports and window logic tested | The scan itself is not run in tests |
| `pipeline/tasks/intraday_monitor.py` | **C** | Same | Same |
| `tools/vpa/` | **C** | 13 test files inherited | Not on the main line; two files oversized (D4) |
| `agents/` | **D** | Almost no tests | LLM calls untested end to end |
| `server/` | **D** | API returns verified by hand | No request tests |
| Entry strategy | **F** | — | Never evaluated (D6) |

## Trader core (Phase 1–3) — graded 2026-09-12, gate area re-checked 2026-09-13

_Two rows were revisited on 2026-09-14 and nothing else here was re-derived._
_The `evolution/holdout_gate.py` row was re-checked after Phase 4 delivered U1–U5_
_and again when the round that made the sample floor reportable landed: the gate_
_is scheduled now, and its own floor is 20 against the repository's declared 50_
_(D15). The `data/portfolio*` row was revisited when the pending-order round_
_added a module and a test file to it. The rest of the grades are still_
_2026-09-12's._

**None of these can be A yet, and that is a statement about the sample, not
the code.** Every area below is tested; none has been validated against data,
because the tables are empty in `data/memory.db`. Test counts are collected,
not estimated.

| Area | Grade | Basis | Gap |
|---|---|---|---|
| `data/attribution.py` | **B** | 19 tests | `decision_snapshots`: 0 rows |
| `data/trade_ledger.py` | **B** | 27 tests | `position_exits`: 0 rows |
| `data/order_state.py` | **B** | 20 tests; "only two writers" asserted | `rejected` / `cancel_pending` still have no writer |
| `data/reservations.py` | **B** | 20 tests | 0 rows |
| `data/settlement.py` | **B** | 35 tests | 0 rows |
| `data/intent.py` | **B** | 27 tests | 0 rows |
| `data/clock.py` | **B** | 21 tests (`test_kernel_clock.py`) | never exercised on a real replay |
| `data/reconciliation.py` | **B** | 17 tests | only 2 runs recorded against the real book |
| `data/episodes.py` | **B** | 28 tests | 0 rows |
| `data/outcomes.py` | **B** | 40 tests | 0 rows — and `predictions.brier` is still NULL on all 202 rows, so calibration remains empty (§8.5 of `DESIGN_REVIEW.md` still stands) |
| `data/learning_candidates.py` | **B** | 59 tests | 0 rows; `evidence_episode_ids` is empty at all 5 production call sites |
| `data/knowledge_snapshots.py` | **B** | 53 tests | the table does not exist in `data/memory.db` yet — no production run since it landed |

## How to move a grade

Raising a grade requires evidence, not effort. State what changed and
what now checks it. Lowering one is equally valid — a grade that only
goes up is decoration.

`evolution/holdout_gate.py` is the worked example: it was **A** on 2026-09-07
on the strength of 24 tests including the leak boundary. It is **B** now, not
because a test was deleted but because its only caller was removed and nothing
promotes through it. The tests still pass; the mechanism is still correct; the
grade measures how much is *verified in operation*, and that fell.
