# Technical debt

Debt is a high-interest loan: paid down continuously in small increments,
never in painful bursts. Each item says what it costs and how it is
recognised, so an agent can pick one up without asking.

Grandfathered violations live in `scripts/lint_baseline.txt`. A line is
removed from that file only by fixing the code — a baseline that can grow
is not a baseline.

_Last updated 2026-09-14. Eight items were paid the same day. D10 (the due test
counted calendar days) — a not-yet-ripe forecast now stays `pending` instead of
being censored. D11 (the evolve page's reachable branch had no render case) — one
case per branch now. D2 (layering violations in `daily_archive`) — split into a
store and a pipeline task, rather than moved as this file originally proposed.
D9 (the frontend checks ran only when someone remembered) — they are a CI job
now. D7 (the gate had no caller) — a scheduled task asks it once per experiment,
at the point the sample was declared sufficient. D3 (26 silent `except: pass`) —
every one now names the exception and logs it. D5 (the clustering dimension) —
measured, half the entry turned out false, and the dead dimension is live again.
D4 (four files over 1200 lines) — split into nine modules, none over the limit.
D12 was added the same day: `scripts/research/` holds 11 byte-identical copies of
`scripts/*.py`. **Open: D6, D8, D12.**_

_On 2026-09-12 four baseline lines had become dead — the code was fixed but the
exemption was never removed — and were deleted. Deleting a line is the point: the
exemption existed to keep CI honest while the code was broken, and once it is
fixed the line is a lie about the repo._

_2026-09-13: D6's recogniser became narrower and checkable (the scoring path is
wired; `brier` is NULL on a calendar, not on a missing call), which is the
difference between "wait" and "go look". D8 and D9 were added the same day — a
column with no writer, and a check that only runs when someone remembers._

**Current baseline: 7 entries covering 13 violations.** `lint_harness.py`
prints the 13, not the 7, because one entry can cover several occurrences in
one file. (Was 26 entries / 47 violations on 2026-09-13, 11 / 17 this morning.)

## Open

### D2 — `data/daily_archive.py` orchestrates fetches

**Paid 2026-09-14 — and the fix written here was wrong.** This entry said "Move
to `pipeline/tasks/`". The module had two responsibilities, and only one of them
was the violation: `run_daily_archive` pulls from four `tools/` modules (that is
the layering break), while `save_snapshot` / `get_snapshot` are a store that
`data/market_history.py`, `data/market_data.py` and `data/sentiment_cycle.py`
read. Moving the whole file up would have made three `data/` modules import
`pipeline/` — trading four violations for three worse ones, in the layer that is
supposed to have none.

It was split instead: the store is `data/daily_snapshots.py`, the orchestration
is `pipeline/tasks/daily_archive.py`. Recogniser met —
`alpha_agents/data/daily_archive.py::layering` is gone from the baseline, and the
grandfathered count fell 47 → 43.

**Cost.** 4 layering violations. It pulls from four `tools/` modules to
assemble a daily snapshot, which makes it a pipeline task wearing a data
module's name.
**Recognise.** `daily_archive.py::layering` gone from the baseline.

### D3 — 26 silent `except ... : pass`

**Paid 2026-09-14.** All 26 sites across 14 modules now bind the exception and
log at debug, one message written per site rather than one template. The 14
baseline lines were deleted with the code, and the grandfathered count fell
43 → 17.

Two things the work turned up, both worth keeping:

- **`data/memory_store.py` had no logger at all.** A 2100-line store where two
  migration skips and a duplicate-lesson conflict were the three silent handlers.
  It has one now, added with its reason.
- **The rule's own advice was wrong.** The message used to suggest "or write a
  comment above the `pass` explaining why the failure is ignorable" — but the
  check reads the AST (`len(node.body) == 1 and isinstance(node.body[0], Pass)`),
  and a comment is not in the AST. Following that advice left the violation
  standing. The message now says so, and says the `except` needs `as e` to name
  the exception in the log line.

**Cost.** The PBOC parser returned rows whose every headline was the
literal string `"true"` for months, because nothing on that path was
allowed to complain. Every silent handler is a place that can happen
again.
**Fix.** One module at a time: add `logger.debug("... %s", e)`, or a
comment above the `pass` explaining why the failure is genuinely
ignorable.
**Recognise.** `silent-except` count in the baseline falls.
**Progress.** 29 → 26 → **0**. The first three modules
(`data/embeddings.py`, `evolution/lessons.py`, `evolution/playbook.py`) were
cleared earlier; this pass took the remaining 14.

### D4 — 4 files over 1200 lines

**Paid 2026-09-14.** All four are under the limit, and none of the five new
modules is over it either — moving lines from one file into another is not a fix.

| was | now | cut |
|---|---|---|
| `data/memory_store.py` 2161 | **1063** | `_SCHEMA` (824) → `data/memory_schema.py`; the VPA + financials **caches** (275) → `data/vpa_store.py` |
| `tools/vpa/data.py` 1409 | **827** | the bars-and-derived-metrics group (598) → `tools/vpa/bars.py` |
| `data/snapshot_store.py` 1338 | **1082** | `_SCHEMA` (257) → `data/snapshot_schema.py` |
| `tools/vpa/llm.py` 1243 | **887** | the 343-line system prompt + its `PROMPT_VERSION` → `tools/vpa/prompts.py` |

Three of the five cuts needed **no caller changes at all**: `memory_store`,
`snapshot_store` and `vpa/data.py` re-export the moved names, so every existing
`from … import …` keeps working and the name cannot drift (it is the same object).
Only `vpa_store` needed its two callers updated — because it imports the
connection from `memory_store`, so `memory_store` cannot import it back.

Two cuts were **measured and rejected**: `snapshot_store`'s news group (~250
lines) has 28 callers — all fourteen `sources/` adapters — and the schema cut
alone already put the file under the limit; and moving the connection itself
(`_get_conn` / `MEMORY_DB_PATH`) would mean touching ~75 patch sites across 47
test files plus `conftest`, where one miss sends tests at the production
database. That is a separate slice, and it is not needed to satisfy this entry.

**Recognise.** `file-size` gone from the baseline — met: the four lines were
deleted and the grandfathered count fell 17 → 13.
**Note.** Was 5 files; the count moves as the code does, so check it rather
than trusting this line.
**Caught by the harness on the way in.** The new `bars.py` used `Optional`
without importing it, and `lint_harness`'s undefined-name rule failed the build
on it — the same rule whose job is to notice a `NameError` that only fires on a
production branch, doing exactly that on a refactor.

### D5 — Playbook clustering lost a dimension

**Paid 2026-09-14 — and half of this entry was measured false.**

`_query_hit_clusters` grouped on `vpa_verdict` and **no writer recorded that
key**: neither `intraday_monitor` nor `morning_scan` puts it in `features_json`,
so the expression was a constant NULL and the clustering had two live dimensions
while the SQL read as if it had three. The cost therefore is not "historical
rules broke" — it is that clusters were **coarser than intended**, so an
auto-created pattern described "a theme, with or without an institution" rather
than a kind of setup.

The entry's second claim ("every historical playbook carrying a `vpa_verdict`
condition can no longer match") **does not hold in production**: `playbooks`
holds exactly one row and its conditions are `(institutional, theme)`. Zero have
`vpa_verdict`, precisely because the dimension never worked. Measuring first is
what turned "retire the broken patterns" into "don't touch anything".

The dimension is now `change_pct_band`, **recorded at decision time** by
`intraday_monitor` and by the replay script, with the boundaries defined once in
`evolution.playbook.change_band`. Recording it rather than deriving it twice (a
SQL `CASE` for the grouping plus a Python rule for the matching) is the point:
two definitions drift, and an auto-created pattern then describes a cluster it
can never match. `vpa_verdict` no longer appears in the module.

**Cost.** Removing `vpa_verdict` from recorded features collapsed
clustering from three dimensions to two (`theme` × `institutional`).
**Recognise.** `_query_hit_clusters` groups on three or more dimensions — met,
and pinned behaviourally: six rows differing only in the band form two clusters
and one.
**Known cost of the fix, accepted.** A third live dimension makes clusters finer,
so auto-creation gets slower: production has 47 rows with a `prob` and the
threshold is 3 wins per cluster. Expect no new auto-created playbook for a while
— that is specificity being paid for, not a breakage.

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

### D7 — The champion/challenger gate has no caller (paid 2026-09-14)
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

**Paid 2026-09-14 — the gate is now asked by itself, once per experiment.**
`pipeline/tasks/shadow_run.py` (15:45 on trading days) asks the gate for a
version the day its paired count first reaches what the gate requires, and never
again: a verdict already at the bar is the stopping rule. That "once" is the
point, not an economy — §12 forbids repeated inspection until something passes,
and asking daily would be optional stopping *and* would write a near-identical
`insufficient` row every day until the count filled. A person looking early with
`scripts/policy.py gate` does not suppress the automatic question, because their
verdict's `validation_days` is short of the bar too.

So the title is finally false, and what remains is not a missing caller but a
missing sample plus one operator decision: **no shadow run has been opened**, so
no experiment is counting, so the gate has nothing to answer about. That is D6's
kind of wait, and the recogniser below is still unmet for the same reason it was
yesterday — there is no promotion to look for.

**Note.** This was the one debt item that was scheduled work rather than
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

**Paid 2026-09-14.** `harness.yml` gained a `frontend` job: checkout,
`setup-node@v4` pinned to 22 with an npm cache on `web/package-lock.json`,
`npm ci`, `npm run lint`, `npm run build`, `npm run check:render`, and
`node --test tests/test_report_markdown.mjs`. Both frontend checks now run on
every push and pull request. The analysis is kept because it says why the check
exists at all.

**Cost.** `cd web && npm run check:render` is the only thing that can catch a
workspace page rendering "never wired" as "no news" — `npm run lint` and
`npm run build` both pass on it, and it is invisible in a screenshot. It
caught three such bugs on the day it was written. It used to run only when
someone remembered.
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

**Paid 2026-09-14.** `render-check.jsx` now carries one evolve case per branch of
`Reachability`, asserting each branch's own copy. The analysis is kept because
the assertion choice is the interesting part.

**Cost.** `cd web && npm run check:render` is the only check that can fail on a
workspace rendering the wrong thing, and its evolve case feeds a hand-built
payload with `reachable: false`. On 2026-09-13 the build flipped to
`reachable: true` (a candidate producer was registered), so the branch the real
page now takes — the "可达" banner and the non-warning KPI note — had **no case
in the matrix**. The check still passed, because it asserts about its own
synthetic payload; this was a coverage gap rather than a wrong claim, and the
`reachable: false` case is worth keeping for the same reason.
**Fix.** A second evolve case with `candidate_producers: ['remap_confidence']`
and `reachable: true`, asserting the reachable copy appears **and** that
`本构建不可达` does not — the pair, since a "must appear" assertion is
satisfiable by a coincidentally correct neighbour string. In particular the
`want` list must not contain `可达`: it is a substring of the *other* branch's
`本构建不可达`, so it would be satisfied by the reachable branch being absent —
the exact failure mode the pair exists to prevent.
**Recognise.** Two evolve cases in `render-check.jsx`, one per branch of
`Reachability`, and a mutation probe (pin `open` to `false`) that turns the new
case red.

### D12 — `scripts/research/` is a byte-identical copy of `scripts/`

**Cost.** 21 `.py` files live under `scripts/research/` and **11 of them are
byte-identical** to a same-named file one level up: `replay_evolution.py`,
`backtest_vpa.py`, the `analyze_vpa_*` family, `reparse_vpa_cache.py`, and so on.
Found on 2026-09-14 while changing the replay script's recorded features — the
same edit had to be made twice, and **nothing would have failed if only one copy
had been fixed**. That is this repository's recurring "one question, two answers"
shape with a filesystem for a database, and the failure mode is silent: the two
copies drift a line at a time until one of them is wrong and nobody knows which.
**Fix.** Decide which tree owns them. If `scripts/research/` is a snapshot for
the research notes, keep it and make it a deliberate export (a header saying what
it is and how it is refreshed); otherwise delete the duplicates and keep one
copy. Deleting the 11 is the safer half — nothing imports them.
**Recognise.** No file under `scripts/research/` hashes equal to a same-named
file in `scripts/`.
**Note.** Not fixed in the same turn that found it: removing a tree is the
owner's call, and it is not on the path of any behavioural change. Listed here so
the next person does not "fix a bug" in one copy only.

## Paid

- Four files were past the 1200-line limit (D4). Nine modules now, none over it,
  and three of the five cuts changed no caller at all — the moved names are
  re-exported by the file they left. Two further cuts were measured and rejected:
  a 250-line news group with 28 callers, and moving the database connection, which
  would touch ~75 patch sites across 47 test files.
- Playbook clustering grouped on a dimension nothing wrote (D5). It is now
  `change_pct_band`, recorded at decision time with the boundaries defined once,
  so the grouping, the generated condition and the matcher read one value. The
  entry's "historical patterns broke" half was measured false: production has one
  playbook and it never had that condition.
- 26 silent `except ...: pass` (D3) across 14 modules now name the exception and
  log it. `data/memory_store.py` had no logger at all. The lint rule's own advice
  was also wrong — a comment above the `pass` cannot clear it, because the check
  reads the AST — and the message now says so.
- The champion/challenger gate had no caller (D7). A scheduled task now asks it
  once per experiment, at the point the sample was declared sufficient — one
  look, because §12 forbids repeated inspection until something passes. What
  remains is a missing sample and an operator decision, not a missing caller.
- The frontend checks ran only when someone remembered (D9). `harness.yml` has a
  `frontend` job that runs `npm run lint`, `npm run build`, `npm run check:render`
  and `node --test tests/test_report_markdown.mjs` on node 22.
- `data/daily_archive.py` broke the layering invariant four times (D2). Split
  into `data/daily_snapshots.py` (the store, where its data-layer readers are)
  and `pipeline/tasks/daily_archive.py` (the orchestration, which may import
  `tools/`). The entry's own proposed fix — move the file up — would have
  created three backwards imports.
- The evolve page's `reachable` branch had no case in `render-check.jsx` (D11),
  so the branch the real page takes after the build registered a candidate was
  never rendered by any check. Paid 2026-09-14 with one case per branch and a
  mutation probe that turns the new one red.
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
