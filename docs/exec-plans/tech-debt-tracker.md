# Technical debt

Debt is a high-interest loan: paid down continuously in small increments,
never in painful bursts. Each item says what it costs and how it is
recognised, so an agent can pick one up without asking.

Grandfathered violations live in `scripts/lint_baseline.txt`. A line is
removed from that file only by fixing the code — a baseline that can grow
is not a baseline.

_Last updated 2026-09-14 (evening). The pending-order round paid no numbered item
but added two: **D13** — an order with no live quote neither checks its thesis
nor expires (deliberate, and the hole is named here rather than hidden) — and
**D14** — test doubles keyed on a module's namespace break silently when code
moves, which is how a split that left every test green still moved a seam the
tests were leaning on. D10 (the due test
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
`scripts/*.py`. **Open: D6, D8, D12, D13, D14, D15, D16, D17, D18.**_

_Later the same evening, the round that removed a unit lie and a threshold that
was never compared: **D8 is half paid** — `predictions.horizon_days` has writers
at last — but the entry had said the column was empty because the callers wanted
the global default, and that was false; the correction is recorded in it.
**D15** was added: the repository declares a sample floor in three places
(golden principles §7, the design doc, the README) and the one automated path
that ships a conclusion is checked against a different number, read out of the
frozen version. The round made that gap visible rather than closing it, because
closing it drifts every version already frozen._

**D16** and **D17** came out of the same sweep, both found while answering "how
far along is the loop": the daily task's stopping rule compares paired samples
against validation days, so it asks the gate **every day** in between — the
anti-pattern its own docstring says it exists to avoid; and the version in force
**no longer matches the running configuration**, because the theme-gate round
added a decision-time number to the fingerprinted block three hours after the
install and nothing froze a successor for it. Neither is fixed in that round, for
the same reason D15 is not: both candidate fixes decide a threshold question.

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

### D13 — A pending order with no live quote neither checks its thesis nor expires

**Added 2026-09-14.** Deliberate during the pending-order round, and written
down rather than papered over: the expiry is asked only once a live price is
known, because the reason it writes — 未到价 — is a claim about the market, and
this repository does not let a missing input produce an assertion (the scoring
side refuses to close an evidence window while the kline store is absent, for
the same reason). The cost lands on the other side: a suspended stock's order
waits indefinitely, skipping both its thesis check and its own deadline, and
nothing in the system says so.
**Cost.** An order on a halted stock is unfinishable again — the defect this
round set out to remove, reached by a different route.
**Recognise.** A pending row whose `code` has had no `realtime_prices` entry for
more than `expire_days` consecutive cycles. `check_pending_orders` cannot
currently tell "the price is missing this cycle" from "the price is missing and
has been for a week"; the second one needs a rule.

### D14 — Test doubles keyed on a module's namespace break silently when code moves

**Added 2026-09-14, found the hard way.** Moving `get_open_positions` into
`portfolio_book` left `patch("alpha_agents.data.portfolio._get_conn")` still
succeeding and still doing nothing: the function binds `_get_conn` in its own
module globals, so the patch redirected only the write path and the assertion
failed somewhere else. Two `test_portfolio.py` cases needed a second patch, and
`test_intent.py`'s allow-list went red for a third reason — it had been passing
on an accident. The fill path's `UPDATE virtual_portfolio SET` is written as
adjacent string literals, which the old `[^"']*status` regex could not see
across, so `portfolio.py` was on the allow-list only because the cancel UPDATE
beside it happened to be spelled on one line. The guard reads the AST now.
**Cost.** A split that leaves every test green can still have moved a seam the
tests were silently leaning on; the failure surfaces later as an unrelated
assertion.
**Recognise.** Any `patch("...module.name")` whose function has moved. Cheap
probe after a split: grep the old module's name through `tests/` and check every
hit still lands. The durable fix — routing these through one fixture that
patches every namespace binding the name — is a bigger change than the debt is
worth today.

### D15 — The repository's sample floor is declared in three places and enforced against a fourth number

**Added 2026-09-14.** `GOLDEN_PRINCIPLES.md` §7 declares "No conclusion with
n < 50 reaches production code"; `TRADER_CORE_DESIGN.md` §12 calls it "the
repository's `n ≥ 50` requirement"; `README.md` says the repo's rule "把诚实
门槛定在 50". None of the three is checked by anything. The code has
`holdout_gate.MIN_VALIDATION_SAMPLES = 20`, and the promotion path re-checks a
verdict's `n` against the floor the **frozen version** declares, which today is
also 20 — so a verdict at n between 20 and 49 satisfies the gate, satisfies the
version in force, and contradicts §7, and nothing refuses it.
**Cost.** A rule the repository states as a governance floor is decorative on
the only path that ships a conclusion. Worse than missing: three documents
asserting it is enforced makes it the last thing anyone would go looking for.
**Recognise.**
`tests/test_holdout_gate.py::TestTheDeclaredFloorAgainstTheRepositorysRule::test_the_rule_is_a_quotation_and_not_a_threshold`
drives a real `promote` verdict at n = 25 — below the rule, above the gate.
**Made visible, not closed, 2026-09-14.** `GOVERNANCE_MIN_SAMPLES = 50` quotes
the number in code, `promotion_floor_gap()` names the difference, and
`scripts/policy.py status` prints all three values (version's floor, gate's
abstention line, the rule). The fix that would close it — raising the constant
to 50 — is deliberately not taken here: `MIN_VALIDATION_SAMPLES` is in
`policy_sources._RULE_SOURCES`, so changing it marks **every version already
frozen** as drifted, and the version in force is the only one there is.
A behaviour-changing edit after freezing is supposed to be a new candidate, so
that is an operator's call with a cost attached, not a patch.

### D16 — The daily stopping rule compares paired samples against validation days

**Added 2026-09-14.** `pipeline/tasks/shadow_run.py` decides whether to ask the
gate with two conditions in two units: `row["remaining"]` counts paired
`(date, code)` samples, and `asked_already` compares `verdict["validation_days"]`
against that same `needed`. An experiment therefore passes the sample bar long
before it passes the day bar, and the gate is asked **on every run in between**,
writing a near-identical row each day — precisely the "governance in shape only"
this module's docstring says it was written to avoid, and the reason the old
`run_gate("daily_playbook", today, today)` call was deleted. §12's stopping rule
is "one look, at the preregistered point"; as written this is many looks.
**Cost.** An experiment's audit fills with rows that look like verdicts, and the
first real verdict stops being distinguishable from the noise around it.
**Why the suite could not see it.** `tests/test_shadow_run_task.py`'s `_ready`
stub returns a verdict whose `n` and `validation_days` **both** equal `needed`,
so the two units coincide and the disagreement is invisible. The defect survived
a test class named `TestItAsksTheGateOnce`.
**Recognise.**
`test_the_stopping_rule_asks_again_while_the_two_units_disagree` asserts the
defect, and the probe "the stopping rule is made unit-consistent" turns it red —
so the test detects the disagreement rather than the fix, on purpose.
**Fix, and why it is not taken here.** Either compare `verdict["n"]` against
`needed` (pairs with pairs) or make the floor count days. Both *are* the D15
question — whether the preregistered sample is 20 samples or 20 days — and each
trades this anti-pattern for another: pairs alone lets one morning's picks end
the experiment, days alone needs the gate's own threshold to move, which drifts
every version already frozen. Decide D15 first; this follows from it.

### D17 — The version in force no longer matches the running configuration

**Added 2026-09-14, found by re-running `status`.** `scripts/policy.py status`
prints `live configuration still matches: False` for version #1. It matched when
`install` ran at `2026-09-14T03:01:06Z`; the theme-gate commit (`d927a0b`,
`2026-09-14T14:20:08+08:00`) then added `theme_gate` to
`scoring.DEFAULT_DECISION_PARAMS`, which the decision block covers — and
`decision_params_of` merges the defaults *under* a version's own values, so the
number the trading path reads now is one the frozen version never declared.
`policy_sources.changed_sources(1)` attributes it to exactly that one key.
**Cost.** The pointer names a policy that does not describe what is running, so
every "is the incumbent still the incumbent" question has a wrong answer, and §11
("a behaviour-changing edit after freezing creates a new candidate") has not been
honoured for a change that did go in.
**Recognise.** `scripts/policy.py status` → `live configuration still matches`.
**Fix.** `freeze --by <you> --reason "the theme gate, frozen after the fact"` —
moves no pointer, needs no evidence, and puts the configuration actually running
on record as a version. Deliberately not run in this round: it decides what the
next candidate is, which is the operator's call, and the pointer cannot move to
it until a verdict exists anyway.
**Not to be confused with** the drift the staging bug used to produce. That one
was every version except the incumbent reporting as drifted, because the
pointer-controlled sources were read from the system instead of from the version.
This one is one key, on one source, with a nameable reason, and it is real.

### D18 — Review grades before it archives, so a window that shut today is graded tomorrow

**Added 2026-09-15, found by asking when the first Brier lands.** Inside
`run_review`, `_score_due_predictions()` is step 0 (line 658) and
`market_history.update_daily()` — the call that writes *today's* bar into
`daily_kline` — is near the end (line 808). So the grading pass reads an archive
whose newest bar is *yesterday*: on 2026-09-15 the 09-08 batch still measures
`have=5, need=6`, and it can only be graded by the **next** day's review, after
tonight's tail end has written the 09-15 bar in.
**Cost.** Every forecast is graded one trading day later than the window it
declares, so the earliest possible Brier is the run *after* the close that shut
it. On its own that is latency, not wrongness — the score, when it lands, is for
the right window. The sharper cost is what it does to prediction: the Learn page
reports the market's own calendar ("还差 1 个交易日"), and reading that as "one
day from now" is off by one, because the arithmetic and the schedule are not the
same clock. It also means the D6 recogniser ("check `brier > 0` once the 09-15
data is in") is satisfied *after* tonight's archive, not tonight's grading.
**A second half, same shape.** `update_daily()` only ever asks its source for
**today** (`_fetch_batch(codes, today, today)`) and returns early if today
already has rows. There is no catch-up for yesterday: if the source does not
have today's bar at 15:30, or the fetch fails, that trading day is a permanent
hole in `daily_kline` until someone runs `scripts/backfill_tushare.py` by hand —
and a hole in the market calendar is what `evidence_window_closed` reads as
"not closed", so every window spanning it stays unripe for ever.
**Recognise.** `grep -n "_score_due_predictions\|update_daily" review.py` — the
first is at ~658, the second at ~808. Or: `SELECT MAX(date) FROM daily_kline`
right after a 15:30 review that just scored nothing, and compare with the date of
the run.
**Fix.** Move the two archive calls (`run_daily_archive`, `update_daily`) above
the scoring step, so one run can grade what its own close matured. Not done in
this round: it changes what the first grading run sees, and whether the EOD
source has a final bar at 15:30 sharp is a question about the upstream, not
about this repository — a fetch that lands early and writes a partial day is
worse than a day of latency. The catch-up half is a separate, smaller fix
(ask for the last N trading days, not just today) and does not depend on that
answer.

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
**Status 2026-09-15 (11:20).** Still 0 Brier, and the recogniser above is now
**one day early**: the 09-15 close does mature the 09-08 batch on the market's
calendar, but `run_review` grades before it archives today's bar (see **D18**),
so the first Brier can only land in the **2026-09-16** review. The narrower
condition to check is therefore `SUM(brier IS NOT NULL) > 0` after the 09-15 row
exists in `daily_kline` **and** a review has run since — i.e. on the evening of
2026-09-16. If it is still 0 then, it is a code bug, and D18's second half (a
hole in `daily_kline`, which `evidence_window_closed` reads as "not closed") is
the first place to look.

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

### D8 — A capability with no production user: per-forecast maturity (half paid 2026-09-14)
**Cost.** `predictions.horizon_days` and `predictions.deadline` are the T2
mechanism for letting each forecast declare how long it gave itself to be
right — the breakout book a 3-day horizon, the pullback book a 5-day one.
The mechanism is complete and tested at every layer: `_deadline_for` refuses
to assume a horizon the caller did not state, `save_prediction` writes both
columns on insert and update, the due query falls back to `date + 5 days`
while reporting `legacy_horizon`, and `outcome_labels` carries that
distinction into the label. **The callers never used it**: all 232 rows in
production carried NULL in both columns, and every one was graded on the global
5-day fallback. This is the fourth class this repo keeps finding (a column that
exists and is never written; `intents.order_id` was the third, fixed in S6).

**This entry was wrong about why, and the wrongness was load-bearing.** It said
"nothing is broken — 5 days is what those two callers want". It is not: the
`intraday_monitor` function hands `"horizon_days": 3` to the *thesis* it builds
from the very same pick, two paragraphs below its `save_prediction` call, and 37
of the 41 theses in production say three days. The callers had declared a
horizon; they had not passed it to the forecast. So the loop was spending its
paired samples grading each forecast against a deadline its own thesis disagreed
with — the one thing a "not yet a bug" reading can least afford to be wrong
about.

**Paid 2026-09-14.** `morning_scan` passes `r.get("horizon_days")` (the prompt
asks the model for one per pick) and `intraday_monitor` names the 3 once
(`INTRADAY_HORIZON_DAYS`) and feeds both the forecast and the thesis from it.
Pinned by `tests/test_declared_horizon_reaches_the_forecast.py`, which drives the
two real save paths and patches the constant to 4, so a reader still using a
literal fails instead of agreeing.

**What is left:** the *forecast* now declares what the decision declared, but
`_deadline_for` still returns `None` when the model states nothing, and the
thesis still takes `from_recommendation`'s own 5-day default in that case — so
one decision can still be recorded as "declared 5" (thesis) and "declared
nothing" (forecast). That difference is honest rather than broken, and it is
what `legacy_horizon` labels. Making the two agree by writing 5 over the silence
is the substitution this entry forbids.

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
