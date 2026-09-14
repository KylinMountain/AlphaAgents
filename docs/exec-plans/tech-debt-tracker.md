# Technical debt

Debt is a high-interest loan: paid down continuously in small increments,
never in painful bursts. Each item says what it costs and how it is
recognised, so an agent can pick one up without asking.

Grandfathered violations live in `scripts/lint_baseline.txt`. A line is
removed from that file only by fixing the code — a baseline that can grow
is not a baseline.

_Last updated 2026-09-13. On 2026-09-12 four baseline lines had become dead —
the code was fixed but the exemption was never removed — and were deleted.
Deleting a line is the point: the exemption existed to keep CI honest while the
code was broken, and once it is fixed the line is a lie about the repo._

_Also on 2026-09-13: D6's recogniser became narrower and checkable (the
scoring path is wired; `brier` is NULL on a calendar, not on a missing call),
which is the difference between "wait" and "go look". D8 and D9 were added the
same day — a column with no writer, and a check that only runs when someone
remembers._

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
**Status 2026-09-13.** Still open, and the blocker is *now* provably sample
rather than code — which is a narrower claim than the 2026-09-12 one. The
scoring path is wired and running: `review.py::_score_due_predictions` (whose
docstring names itself the G1 signal) calls `scoring.score_prediction`, which
regresses out style exposure and computes Brier, on the daily 15:30 review.
`prob` is filled on **47 of 202** rows, earliest 2026-09-08. `brier` is NULL
on all 202 because **no forecast had matured yet**.
**Recognise (narrower, check this before writing any scoring code).** Confirm
`SUM(brier IS NOT NULL) > 0` after the market data for **2026-09-15** is in
the database (see D10 — the calendar due date was one trading day early, which
is now fixed, but the *window* still closes on the 09-15 close, so the 09-08
batch is only gradeable once that row exists).
If it is still 0 at that point, this becomes a code bug in one of two places —
the market window not covering the horizon, or the due-date filter.
**Status 2026-09-14.** Unchanged, and the reason it is still open is now purely
the market's calendar: D10 was fixed on 2026-09-13, so the due test no longer
fires early and no forecast is censored for a window that is still open. What
remains is the wait for the 2026-09-15 close.

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
row. The blocker is now **one** thing, not two:

1. `predictions.brier` is still NULL on every row, so the champion half of any
   comparison is empty.

The second reason is gone: `shadow.PRODUCERS` now registers
`remap_confidence` with `kind="candidate"`, and `scripts/policy.py` gained the
`freeze` / `install` verbs, so the build can produce candidate-grade evidence
and an operator can create the first record. That also removes the "a promotion
would be a no-op" defect this entry did not know about: the decision parameters
are read from the version in force, so moving the pointer changes behaviour.

The original title still holds for a second reason: `run_gate` has no production
caller. Nothing in `pipeline/` or `server/` schedules it, and there is still no
verb that opens or advances a shadow run, so the gate went from "can never fire"
to "correct, candidate-bound and never asked" — progress, and still not
"running in production".
**Note.** This is the one debt item that is scheduled work rather than
housekeeping; it is listed here so it cannot be mistaken for "already handled"
by anyone reading the old claim that selection "早就有了".

### D8 — A capability with no production user: per-forecast maturity
**Cost.** `predictions.horizon_days` and `predictions.deadline` are the T2
mechanism for letting each forecast declare how long it gave itself to be
right — the breakout book a 3-day horizon, the pullback book a 5-day one.
The mechanism is complete and tested at every layer: `_deadline_for` refuses
to assume a horizon the caller did not state, `save_prediction` writes both
columns on insert and update, the due query falls back to `date + 5 days`
while reporting `legacy_horizon`, and `outcome_labels` carries that
distinction into the label. **The callers never use it.** Both production
call sites (`morning_scan.py`, `intraday_monitor.py`) pass `prob` and do not
pass `horizon_days`, so all 202 rows have NULL in both columns, every row is
graded on the global 5-day fallback, and every row is labelled undeclared.
Nothing is broken — 5 days is what those two callers want — but the column
reads as "declared per book" to anyone who does not check the data. This is
the fourth class this repo keeps finding (a column that exists and is never
written; `intents.order_id` was the third, fixed in S6).
**Fix.** Nothing, until a second trader actually runs a different horizon.
**Do not** pass `horizon_days=5` at the call sites to make the column
non-empty: that is precisely the substitution `_deadline_for` exists to
refuse, and it would forge a declaration on the caller's behalf.
**Recognise.** A production row with a non-NULL `deadline`, written by a
caller that stated its own horizon.

### D9 — The frontend render check runs locally only
**Cost.** `cd web && npm run check:render` is the only thing that can catch a
workspace page rendering "never wired" as "no news" — `npm run lint` and
`npm run build` both pass on it, and it is invisible in a screenshot. It
caught three such bugs on the day it was written. It runs only when someone
remembers, because `harness.yml` has no node environment; the same is true of
`tests/test_report_markdown.mjs`, the existing frontend test.
**Fix.** A node step in CI (setup-node + `npm ci` in `web/`) covering both
frontend checks. Deliberately deferred in Phase 5 rather than smuggled in with
the page work.
**Recognise.** `harness.yml` has a job that runs `npm run check:render` and
`node --test tests/test_report_markdown.mjs`.

### D10 — The due test counts calendar days; the horizon counts trading days

**Paid 2026-09-13 — kept here for the analysis, not as an open item.** The fix
is recorded at the end of this entry; the cost analysis is left intact because
it is the evidence that the recogniser below is the right one.

**Cost.** `scoring._forward_return` needs `horizon + 1` rows from
`daily_kline` — the entry close plus the close of the horizon-th *trading* day
after it. Maturity is decided by
`COALESCE(deadline, date(date, '+5 days')) <= as_of`, i.e. *calendar* days
(`_deadline_for` builds its deadline with `timedelta` too). Any six
consecutive calendar days contain at least one non-trading day, so at most
five trading dates fall inside the window and the sixth row is never there:
**no forecast can be scored on the day it is declared due.** The market DB
confirms it on real data — all four prediction dates carrying a `prob`
(2026-09-08…09-11) have 4, 3, 2 and 1 trading dates respectively inside their
calendar window, against 6 required.
`_score_due_predictions` then gets `None` from `score_prediction`, and
`label_forecast` writes `censored` with the reason "no score could be
computed" — which is the wrong fact, because the evidence was not late, it
was not due yet.
Two consequences, both measurable:
1. **`matured` is unreachable for forecast labels.** The first attempt always
   censors, so every graded forecast ends as `censored → revised`. The
   revision stream is supposed to mean "the number changed under a later
   price", and it instead carries a clock artifact.
2. **`brier` cannot populate on the calendar due date.** For the 2026-09-08
   batch (due 09-13) the window closes with the **2026-09-15** close; for
   2026-09-11 (due 09-16) it is the **2026-09-18** close.
**Why it survived.** `_score_due_predictions` has **no test at all** — grep
finds it only in `review.py`. The label tests call `label_forecast` with a
fabricated `scored=`, so they never travel through the due query,
`score_prediction` and `_forward_return`, which is the only path where the
two calendars meet. A function whose docstring names itself "the G1 signal"
is exactly the function an untested seam hides in.
**Fix (landed 2026-09-13).** The second option, taken — the two facts are now
separate. `scoring.evidence_window_closed(entry_date, horizon)` asks the
market's own calendar whether `horizon + 1` trading days have elapsed, and it is
the only source of that judgement: it is derived, never a boolean a caller
passes in, because a caller-supplied verdict is what lets a label decide what
the evidence says. `outcome_labels.label_forecast` consults it when no score
came back — a window still open leaves the label `pending` and writes nothing,
while a closed window with no price still censors, exactly as before.
`review._score_due_predictions` and `shadow.score_due` each report "graded /
window still open / censored" separately instead of folding three different
answers into one count. `deadline` keeps its calendar meaning on purpose: it is
the author's *declaration*, not the system's inference.
**Recognise.** A forecast whose first resolved label is `censored` while the
market had not traded `horizon + 1` days past its entry date. The old
recogniser (a first-resolved `matured`) still applies and is now reachable: the
2026-09-08 batch is graded on the **2026-09-15** close.
**Tests.** `tests/test_score_due_predictions.py` — six cases, the function's
first, running the real predicate and the real `score_prediction` over a
synthetic `daily_kline` rather than stubbing either.

### D11 — The evolve page's "reachable" branch has no render case
**Cost.** `cd web && npm run check:render` is the only check that can fail on a
workspace rendering the wrong thing, and its evolve case feeds a hand-built
payload with `reachable: false`. On 2026-09-13 the build flipped to
`reachable: true` (a candidate producer was registered), so the branch the real
page now takes — the "可达" banner and the non-warning KPI note — has **no case
in the matrix**. The check still passes, because it asserts about its own
synthetic payload; this is a coverage gap rather than a wrong claim, and the
`reachable: false` case is worth keeping for the same reason.
**Fix.** A second evolve case with `candidate_producers: ['remap_confidence']`
and `reachable: true`, asserting the reachable copy appears **and** that
`本构建不可达` does not — the pair, since a "must appear" assertion is
satisfiable by a coincidentally correct neighbour string.
**Recognise.** Two evolve cases in `render-check.jsx`, one per branch of
`Reachability`.

## Paid

- The forecast due test counted calendar days against a trading-day horizon
  (D10), so no forecast could be scored on its declared due date and the
  `matured` label was unreachable — every graded forecast went through
  `censored → revised`, turning an informative correction stream into clock
  noise. Fixed 2026-09-13 by separating "the clock reached the horizon" from
  "the evidence window has closed"; a not-yet-ripe forecast now stays
  `pending`.
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
