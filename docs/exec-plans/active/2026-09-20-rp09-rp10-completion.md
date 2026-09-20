# RP-09 / RP-10 completion: immutable forward evidence and controlled promotion

Status: IN_REVIEW — engineering acceptance complete; real forward `n >= 50`
remains a time/data gate  
Owner: engineering leader  
Base: `main@da179d98`

## Goal

Close the remaining engineering gaps in RP-09 and RP-10 without moving the
active policy or claiming strategy effectiveness. Preserve the existing
human-approval and compare-and-swap promotion boundary.

## Acceptance criteria

- Merge the isolated, append-only RP-09 observation store. Observation-only
  rows have no path into candidate-forward promotion evidence.
- Registration timestamps are writer-controlled. A caller cannot backdate an
  experiment, and an overfilled legacy run fails closed.
- A run with 49 accepted samples and 10 newly mature candidates stores only
  the next preregistered eligible sample and seals exactly 50.
- Duplicate delivery, concurrent processing and retry after interruption
  preserve the same ordered sample IDs and seal hash.
- Producer and evaluator compatibility use an explicit registry of exact leaf
  genes. Unknown genes fail closed; parent-prefix matching is forbidden.
- Sector-First has a dedicated forward manifest and evaluator bound to the
  frozen candidate, protocol, code, data/world and evaluator identities.
- A positive return delta cannot pass when preregistered drawdown, tail-loss or
  concentration limits are breached.
- A promotion-grade verdict requires a complete, successful CI artifact bound
  to the candidate and manifest hashes. Historical comparisons and
  observation-only rows remain ineligible.
- ADV capacity is enforced on the actual fill path, including the one-lot
  fallback.
- Targeted tests, full pytest and policy/harness/docs lint pass in a clean
  environment. Environment-only proxy failures are reported separately.

## Decision log

- Extend the existing policy registry, gate decision and CAS promotion path;
  do not create a second promotion service.
- Keep RP-09 observation storage and RP-10 candidate-forward evidence in
  separate schemas and APIs.
- Keep strategy evidence at `n=0` until real future samples arrive; synthetic
  tests prove mechanics only.

## Verification

- `uv run pytest tests/ -q`: `2948 passed, 20 skipped`, including the
  concurrent duplicate-delivery regression.
- `uv run python scripts/lint_harness.py`: pass, 222 files, 13 grandfathered
  findings.
- `uv run python scripts/lint_docs.py`: pass.
- `uv run python scripts/lint_policy.py`: pass, 14 grandfathered findings.
- Targeted RP-09/RP-10/ADV suite: pass, including exact-N, duplicate,
  out-of-order, concurrent delivery, risk veto and CI binding.

No active policy pointer moved. Strategy evidence remains `n=0`; the code is
mergeable, while RP-10's final evidence state remains blocked until 50 real
future samples mature under one preregistered manifest.
