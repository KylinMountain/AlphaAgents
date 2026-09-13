# Technical debt

Debt is a high-interest loan: paid down continuously in small increments,
never in painful bursts. Each item says what it costs and how it is
recognised, so an agent can pick one up without asking.

Grandfathered violations live in `scripts/lint_baseline.txt`. A line is
removed from that file only by fixing the code — a baseline that can grow
is not a baseline.

_Last updated 2026-09-12. Four baseline lines had become dead — the code was
fixed but the exemption was never removed — and were deleted on that date.
Deleting a line is the point: the exemption existed to keep CI honest while the
code was broken, and once it is fixed the line is a lie about the repo._

**Current baseline: 26 entries covering 47 violations.** `lint_harness.py`
prints the 47, not the 26, because one entry can cover several occurrences in
one file.

## Open

### D2 — `data/daily_archive.py` orchestrates fetches
**Cost.** 4 layering violations. It pulls from four `tools/` modules to
assemble a daily snapshot, which makes it a pipeline task wearing a data
module's name.
**Fix.** Move to `pipeline/tasks/`.
**Recognise.** `daily_archive.py::layering` gone from the baseline.

### D3 — 26 silent `except ... : pass`
**Cost.** The PBOC parser returned rows whose every headline was the
literal string `"true"` for months, because nothing on that path was
allowed to complain. Every silent handler is a place that can happen
again.
**Fix.** One module at a time: add `logger.debug("... %s", e)`, or a
comment above the `pass` explaining why the failure is genuinely
ignorable.
**Recognise.** `silent-except` count in the baseline falls.
**Progress.** 29 → 26. `data/embeddings.py`, `evolution/lessons.py` and
`evolution/playbook.py` were cleared; their baseline lines were removed.

### D4 — 4 files over 1200 lines
`data/memory_store.py` (2145), `tools/vpa/data.py` (1410),
`data/snapshot_store.py` (1339), `tools/vpa/llm.py` (1244).
**Cost.** A file an agent cannot hold in context gets edited blindly, and
blind edits are where duplication starts.
**Fix.** Split by responsibility, not by line count.
**Recognise.** `file-size` gone from the baseline.
**Note.** Was 5 files; the count moves as the code does, so check it rather
than trusting this line.

### D5 — Playbook clustering lost a dimension
**Cost.** Removing `vpa_verdict` from recorded features collapsed
clustering from three dimensions to two (`theme` × `institutional`), and
every historical playbook carrying a `vpa_verdict` condition can no
longer match.
**Fix.** Add replacement buckets from features already recorded — score
band, change_pct band, anomaly category.
**Recognise.** `_query_hit_clusters` groups on three or more dimensions.

### D6 — Entry side is unevaluated
**Cost.** The largest gap in `docs/strategy_evaluation_2026-09.md`. G6
records decision context going forward, but there is no history to
replay, so no one can say whether news-driven picks beat the market.
**Fix.** Accumulate. Needs weeks of live running, not a code change.
**Recognise.** `replay_day` returns `replayable: true` for a month of
trading days.
**Status 2026-09-12.** Still open, and now measurable: `predictions` holds
202 rows with `brier` NULL on every one. The blocker is sample, not code.

### D7 — The champion/challenger gate has no caller
**Cost.** `evolution/holdout_gate.py` is tested (24 tests) and correct, but
nothing calls it. It *was* called — `lessons.post_review` ran it daily — with
`run_gate("daily_playbook", today, today)`, a zero-length validation window, so
all 3 recorded runs abstained (`gate_decisions`, every row
`validation_days: 0`). A gate that can never fire is worse than no gate: it
looks like governance. Phase 1 removed the call rather than leave it.
**Fix.** Candidate-bound forward validation, frozen policy registry, promotion
and rollback — i.e. Phase 4 of `docs/TRADER_CORE_DESIGN.md` §14.
**Recognise.** A promotion that changes the active policy pointer, with a
`gate_decisions` row whose `validation_days` is not 0 **and whose
`evidence_scope` is `candidate_policy`**. The second half was added 2026-09-13:
`validation_days > 0` alone is also satisfied by a comparison against the
no-skill baseline, which says the champion has skill and says nothing about
whether a policy is better.
**Status 2026-09-13.** The mechanism is delivered (Phase 4, U1–U5) and this
recogniser is still unmet: `gate_decisions` holds no non-abstain production
row. The blocker has become two things, and only one of them is data:

1. `predictions.brier` is still NULL on every row, so the champion half of any
   comparison is empty.
2. `shadow.PRODUCERS` registers the constant baseline only, so nothing in the
   build emits candidate-grade evidence and every verdict is refused on
   `evidence_scope` by design. Adding a candidate producer is a design
   decision — what the challenger *is* — not a missing function.

The original title still holds for a third reason: `run_gate` has no production
caller. Nothing in `pipeline/` or `server/` schedules it, so the gate went from
"can never fire" to "correct, candidate-bound and never asked" — progress, and
still not "running in production".
**Note.** This is the one debt item that is scheduled work rather than
housekeeping; it is listed here so it cannot be mistaken for "already handled"
by anyone reading the old claim that selection "早就有了".

## Paid

- `data/portfolio.py` had 3 layering violations — service code (stop-loss,
  holding caps, bearish signals) sitting in the storage layer. It no longer
  imports `tools/` at all, and the exit slice moved to `portfolio_exit.py` in
  Phase 1. **The baseline line is gone** (checked 2026-09-12: 0 violations).
- News feeds consumed as snapshots rather than streams — a 06:30 scan saw
  one hour of the fifteen since the previous close. Fixed by
  `pipeline/tasks/news_ingest.py` plus windowed reads.
- `published_at` stored verbatim in mixed formats, silently breaking
  window queries. Normalised at write time; 470 rows rewritten.
- Predictions re-saved every intraday cycle, inflating playbook trade
  counts by an order of magnitude. `save_prediction` is idempotent per
  `(date, code, report_type)`.
