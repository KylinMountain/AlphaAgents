# Quality score

Each area graded on the evidence available, tracked over time. A grade is
a claim about how much of the area is verified — not how much code exists.

Scale: **A** covered by tests and validated against data · **B** tested,
not validated · **C** works, thinly tested · **D** known gaps · **F** not
trustworthy.

_Last updated 2026-09-07._

| Area | Grade | Basis | Gap |
|---|---|---|---|
| `data/scoring.py` | **A** | 27 tests; verified end to end on real history | — |
| `evolution/holdout_gate.py` | **A** | 24 tests incl. leak boundary | No live validation data yet |
| `evolution/principle_scoring.py` | **A** | 16 tests incl. decay weighting | Needs graded predictions to act on |
| `data/decision_context.py` | **A** | 19 tests; replay verified | No history to replay |
| `tools/exit_signals.py` | **B** | 19 tests; regime rule forward-tested over 24 windows | Patterns kept for display are known-useless |
| `evolution/playbook.py` | **B** | 13 capacity tests | Clustering lost a dimension (D5) |
| `sources/` | **B** | 16 structural tests; all 13 feeds probed live | CLS down upstream; 2 feeds degraded |
| `pipeline/tasks/news_ingest.py` | **B** | Window reads tested; ingest verified live | No test for a multi-hour outage |
| `data/portfolio.py` | **C** | Lifecycle exercised indirectly | Service code in the storage layer (D1) |
| `pipeline/tasks/morning_scan.py` | **C** | Imports and window logic tested | The scan itself is not run in tests |
| `pipeline/tasks/intraday_monitor.py` | **C** | Same | Same |
| `tools/vpa/` | **C** | 13 test files inherited | Not on the main line; two files oversized (D4) |
| `agents/` | **D** | Almost no tests | LLM calls untested end to end |
| `server/` | **D** | API returns verified by hand | No request tests |
| Entry strategy | **F** | — | Never evaluated (D6) |

## How to move a grade

Raising a grade requires evidence, not effort. State what changed and
what now checks it. Lowering one is equally valid — a grade that only
goes up is decoration.
