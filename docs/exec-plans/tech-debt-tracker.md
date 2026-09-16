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
`scripts/*.py`. **Open: D6, D8, D12, D13, D14, D18, D25, D30, D31** (D15, D16 and D17 were all opened and
paid on 2026-09-15, in one round. **D19–D24 were opened *and* paid on 2026-09-16**, all six
from an external review of the T+1 plan and all six fixed the same day: D19 was a
wrong market rule already in the kernel (the cash-side T+1 rule was the *withdrawal* rule
applied to buying power); D20 was the absence of date-versioned limit rules; D21 was that a
replay would have inherited the live trader's memory; D22 was that nothing stopped a replay
writing into the shared history; D23 was that nothing decided fills at all, so the first
walk would have improvised an intraday path and a capacity cap from the fill day's own
volume; and D24 was that nothing recorded a model exchange, so re-running the same window
re-sampled the model instead of reproducing it. Their entries below carry the payment
records; none of the six ever reached this list as debt. **D25 was opened the same day, out of the M3
design (§8 of that plan), and is *not* paid** — a candidate can reach `validated` on an empty evidence
list, and its fix belongs on the lifecycle transition, not on the five call sites that would be tempted
to fill the bucket with any id at hand. **D26 was found in the UI the same day and paid the same day**:
the command columns had entered the `position_exits` DDL without a migration, so every close intent on
an older database failed and the SQL error was recorded as the intent's rejection reason — a broken
exit path showing up in the UI as policy.)_

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

### D26 — Every close intent failed on a database the command columns never reached (paid 2026-09-16)

**Added 2026-09-16, from the UI.** The user sent a screenshot of the 改账审计 table in which 48
rejected intents carried the same rejection reason: `OperationalError: no such column: command_id`.
`command_id` and `request_json` had entered the `position_exits` DDL without a migration for tables
that already existed — `memory_store`'s ALTER list gained `thesis_id` but never the command pair —
so on any database created before them `find_command` raised on its first read and **every close
intent failed**. The audit trail recorded the outage as 48 ordinary policy rejections, each with a
SQL error for a reason.

**Cost.** No position could close anywhere the schema predated the columns, and nothing signalled
it: the failure mode was not an error page, it was plausible-looking data — a *rejection* is a
normal verdict, so nothing downstream ever asked why all of them said the same thing.

**Paid 2026-09-16** with a rebuild migration (`memory_store._migrate_position_exits_command`),
not a plain ALTER, and the difference is the point: the legacy table carries
`UNIQUE (position_id, exit_date, price, shares)`, which rejects two genuinely independent sales
that share terms — exactly the case the command ID exists to distinguish. The table is recreated
from the current DDL and legacy rows copied across (they hold NULL command IDs, which the new
unique constraint permits). Verified against a **copy** of the live database: the rebuild runs,
`record_exit` writes, and a same-command retry returns the original row. Three tests in
`test_trader_ledger.py`; the same-terms one **fails on an ALTER-shaped migration** — mutation
probe confirmed it dies on the legacy `UNIQUE` constraint, which is the distinction this entry
is about, not merely on a missing column.

**The code fix did not end the outage, and that is the second half of the lesson.** A migration
runs when a process *opens* the database, so a process that started before the migration was
committed never runs it. The two live processes started 09-15 11:02 and the fix was committed
09-16 13:13, so closes kept failing until they were restarted — the last `no such column` is
14:58:39. The tests were green the whole time: the migration is correct and covered, and nothing
re-ran it. **A migration is not paid until the running processes have been restarted**, and that
applies to every schema-shaped fix in this list. Applied to production 09-16 17:10 by restarting
both processes (`run-v2`, `web --no-monitor`); the columns now exist, the row counts are
unchanged, `quick_check` returns `ok`, and a close books — `position_exits` was empty before and
holds a `command_id` row after.

### D27 — The fill deadlocked against a lock it was already holding (paid 2026-09-16)

**Found by M1's first walk-forward fill, which hung.** `_fill_order` held `_write_lock` and
then asked for the market's exposure cap. The cap is computed by `get_sentiment_cycle`,
which on a cache miss *writes* the phase it just computed — and that write takes
`_write_lock`. `_write_lock` is a plain `threading.Lock`, not an `RLock`, so the frame
waited forever on a lock it already held: a self-deadlock with no error, no timeout and no
log line.

**Why production never showed it.** The 15:30 review pre-computes the next day's phase, so
in the live book the cache is always warm and the miss path never runs. A fresh walk-forward
directory has no phase, so M1's first fill took the branch the live system **cannot** reach.
The bug was reachable only from the replay side, and the live side had been hiding it —
which is the argument for the runner existing at all.

**Paid 2026-09-16** by hoisting the read above the lock. The placement is load-bearing and
the comment at `portfolio.py:748` says why: the phase is a fact about the market, not about
this book, so it needs no lock. **No test yet** — a deadlock test needs a timeout harness,
and the honest statement is that the fix is *unproven at the test layer*, not covered.
(Line numbers as committed in `3494559`, which paid D27–D29.)

### D28 — A negative budget aborted a trader's whole pending-order cycle (paid 2026-09-16)

**Found by the walk-forward on 2025-07-08.** The per-order budget was
`min(available, max_per_stock, max_for_theme, sentiment_room, cluster_cap)`, and **every term
in it can be negative**: `available` after a realized loss, `max_for_theme` once the theme
line is over its cap, `cluster_cap` once a correlated cluster is. `min()` over a set that
includes negatives is not a budget — it is the largest debt.

**The cost was out of proportion to the arithmetic.** The negative value reached
`_calc_shares`, produced a **negative share count**, and `consume_reservation` refused a
negative cost by **raising**. That raise aborted the entire pending-order cycle for the
trader, so one un-fundable order skipped every order behind it — a book-wide stall caused by
one negative number.

**Paid 2026-09-16** by flooring the *result* at `0.0` (`portfolio.py:796`), because "no room"
already has a spelling in this function: `shares == 0` is the refusal the code below already
knows how to say. A `max(0, ...)` on the theme term was already present and was not enough —
the floor has to be on the sum, since the sum is what goes negative. **No test yet**, same
caveat as D27.

### D29 — The trailing stop measured its own output and diverged geometrically (paid 2026-09-16)

**Found 2026-09-16**, reading the book during the D26 investigation: `000510 新金路`, entry
`16.31`, carried a stop of `24,705,832.43` — up from `15,352,643.13` earlier the same day. The
rule computed its distance as `(open_price - stop_loss) / open_price` and then wrote the result
back to `stop_loss`, so every cycle multiplied the stop by `peak / open`. The log has the factor
exactly: `35630.83 / 31635.21 = 1.126303 = 18.37 / 16.31`. Sixty-odd cycles of that is a
seven-figure stop, and a seven-figure stop on an `18.00` stock reads as *stopped* on every cycle.
The position could never be held again, and — once D26 was paid — it would have been sold on the
first cycle that could write an exit.

**Why the existing guards missed it.** They each covered one half, and the pair left a gap. The
ratchet only ever *raises* a stop, so a corrupted value was preserved by the very guard meant to
keep stops from loosening; and nothing checked that a stop is below the peak it claims to
protect. `trailing_stop_pct` was asserted against a hostile *policy* value but not against a
hostile *stored* value.

**Paid 2026-09-16.** Three changes, each paired with its own test: a frozen
`virtual_portfolio.initial_stop_loss` written once at the fill, so the distance is measured from
the entry stop rather than from the rule's last answer; a repair that recomputes a stop sitting
above the peak *before* the ratchet rather than inside it; and a `min(new_stop, peak_price)` clamp,
so no input can place a stop above the peak. The derivation was also collapsed into one function,
`portfolio_book.entry_stop` — the top-up carried its own copy and the two had already drifted.

**The second bug that fix uncovered.** The top-up's `SELECT` listed its columns explicitly and did
not include `initial_stop_loss`, while its guard read
`"initial_stop_loss" in pos.keys()` — which tests the *query's* column list, not the table's. The
guard answered False, the caller *declined*, and a decline is indistinguishable from a policy
decision: averaging down had quietly stopped moving the stop. The column is now selected, and
`entry_stop` **raises** when handed a row without it, because a NULL column and an absent one are
not the same thing. 8 tests in `tests/test_trailing_stop.py` plus one in `test_intent.py`; three
mutation probes (a silent `.get()`, a baked-in fallback, a removed clamp) each turn a specific
test red. Against the live row the repair takes `24,705,832.43 → 15.01 → 17.45` (peak `18.37`).

### D30 — The summary counts one vocabulary and the book records another, so its labels lead nowhere

**Added 2026-09-16, while fixing the walk-forward's cancel line.** `walk_forward` builds its
`挂单撤销` breakdown from the **alert** reasons `portfolio.check_pending_orders` returns, and an
alert's reason is a short paraphrase written for a human reading one line about one order. The
**ledger** records a different, longer string in `virtual_portfolio.close_reason`. The two agree
for some families and diverge for others, and nothing maps one onto the other.

**Measured** on the 2025-07-01 window (fresh replay directory, 30 sessions): **47** cancelled
rows and **44 distinct** `close_reason` strings — the book is deliberately per-instance. Asking
the book for each label the summary prints:

| summary label | count shown | rows in the book matching that string |
|---|---|---|
| `资金不足` | 36 | **36** ✓ (the book's string is a longer one beginning with it) |
| `价格涨走` | 6 | **0** ✗ (the book says `价格已涨走(…)`) |
| `到期未到价（5天）` | 3 | **0** ✗ (the book says `挂单到期未到价（挂5天，期限5天）`) |
| `到期未到价（7天）` | 2 | **0** ✗ (same shape, `挂7天`) |

**Cost.** The summary is the artifact a person audits from, and **three of its four labels cannot
be found in the book at all**; `挂单到期未到价` matches 5 rows, which merges the 5-day and 7-day
cases the summary separates. So the line cannot be reconciled against the ledger by its own
vocabulary — the detail survives (D30 is not data loss), but the index into it does not. This is
the same shape as the rest of this file: **a number and its evidence disagree, and neither
side is wrong on its own.**

**Not paid.** The fix is a design choice with two honest shapes — have the alert carry the
`close_reason` it wrote, or have the runner read the class back from the book — and either one
moves the summary onto the ledger's vocabulary. It is not a one-line change: the alert reason is
also what the live UI shows, so the two surfaces have to be reconciled rather than one renamed.
`_cancel_class`'s docstring names this gap at the point of use.

### D31 — Nothing mechanical detects a declared defence that no test would miss

**Added 2026-09-16, after the fourth instance.** This repository has a recurring defect class:
**a defence is written, documented and wired — and no test would go red if it were removed.**

| # | the defence | how it was found |
|---|---|---|
| 1 | the pending-order expiry path | dead code; nothing called it |
| 2 | `intents.policy_ref` | always empty; there was no registry to reference |
| 3 | `learning_candidates`' four proposal columns | written, never read |
| 4 | `_max_backtick_run`'s fence widening (**paid** 2026-09-16) | the acceptance list said "no tests"; the truth was narrower — the *ordering* was asserted, the *containment* was not |

**Cost.** Each instance reads as finished work. The function exists, the docstring explains what
it prevents, the call site is real — and deleting the call site leaves the suite green. The
failure is silent in the direction that matters: it makes the repo **look** defended.

Instance 4 is the clearest illustration of why the *record* matters as much as the code: the
acceptance list had it as *"零实现、零测试"* when two of its three parts were already asserted
and only the third was empty. **The list was wrong in the pessimistic direction** — and that is
its own kind of wrong, because it sends the next person to build something that already exists.

**Recognition rule** — this is the part worth keeping. For any function whose docstring names a
defence, ask: *"if I delete this, which test goes red?"* If the answer is "none", the defence is
a comment with a body. Instance 4 is now covered by
`tests/test_vpa_v10_helpers.py::TestAnAdversarialBodyCannotLeaveItsBlock` (6 cases; probes O and P).

**Not paid, and no fix proposed.** *"Does this function's stated purpose have an assertion?"* is
not a syntactic property, and the four instances share no shape — two are database columns, one
is a code path, one is a private helper. A lint rule is not obviously available. The recognition
rule above is what this entry can offer: a question to ask during review, not a check to run.

### D25 — A candidate can walk the whole lifecycle on an empty evidence list

**Added 2026-09-16, while designing M3** (§8 of the T+1 plan). `learning_candidates.save_candidate`
requires the four proposal fields, and `_episode_citations` insists both buckets be *stated* rather
than omitted — *"an empty list has to be stated, not omitted, or 'we looked and found none' cannot be
told from 'nobody looked'"*. That check enforces the **shape** of the citation. It does not enforce that
anything was looked at: `{"supporting": [], "opposing": []}` is a valid citation, and it is what **all
five production call sites** pass (`playbook.py` ×3, `lessons.py` ×2 — counted 2026-09-16).
`advance_candidate` then checks only that the *transition* is legal; it never reads
`evidence_episode_ids`. So a candidate that cites nothing can legally reach `validated`, and
`candidate_transitions` will record an actor and a reason for each step.

**Cost.** The module's own stated guarantee is that *"a proposal whose evidence is only the cases that
agree with it is not a hypothesis, it is an advertisement"*. The empty case is weaker than the
advertisement it guards against, and the empty case is the one every production call site produces.
This is `holdout_gate`'s `evidence_scope` lesson appearing a second time: *a guard whose missing case
is the permissive one is not a guard.*

**Why the fix is neither at the write boundary nor now.** Requiring a non-empty `supporting` bucket at
`save_candidate` would forbid a legitimate `observation` — the lifecycle's entry state exists for
exactly "we noticed something and have not attributed it yet". The guard belongs on the **transition**,
and a transition needs a reason to fire: something has to be able to name the decision an episode
records. Nothing can today, because both proposers read prose lessons rather than T1 episodes — which
is the gap M3 exists to close. **This entry is here so the gap is not rediscovered as a surprise, and
so the fix is not mistaken for "go fill in the five call sites".** Fill them with ids from anywhere and
the citation stops being empty and starts being wrong.

**How it is recognised.** M3's acceptance requires that a candidate cannot leave `observation` while
its evidence is empty — and, per the review of revision 3, non-empty ids are still not enough:
`supporting = [three ids picked at hand], opposing = []` is schema-legal and proves nothing about
whether a counter-example search ever ran. The evidence therefore becomes an **EvidenceBundle**
(declared `search_scope`, a replayable `matching_rule`, eligible/excluded counts, a cutoff — §8.2 of
the plan), so an empty opposing bucket means *searched the declared set and found none*, and the
evaluator replays the declared search to check the counts. Still a negative case with a mutation
probe, because a static assertion here would be satisfied by the very constants that make the bucket
empty.

### D24 — Nothing recorded the model exchanges, so a re-run of the same window was a re-sample (paid 2026-09-16)

**Added 2026-09-16, from the external review of the T+1 plan** — its P1-2, and M0 item 6. The
plan's own §6 had already drawn the line: saving the decision's input snapshot tells you what
the agent was shown, not that a second run reaches the same judgement. Nothing in the code
wrote down a model exchange, so M2's "same window, news filter on and off" would have compared
the filter *plus two samples*, and every walk-forward number would have been irreproducible by
construction.
**Paid 2026-09-16** with `alpha_agents/llm_journal.py` and 32 tests.
`ALPHAAGENTS_LLM_MODE` is `live` (the default; nothing written), `record`, or
`replay-recorded`. The seam is the client: `model_factory.create_model` passes its
`AsyncOpenAI` through `journaled()`, which hands back **the same object** in live mode and a
thin proxy otherwise. One JSON object per call lands in
`<DATA_DIR>/llm_journal/<run_id>.jsonl` — so it follows a `walk_bootstrap` replay directory
and cannot read the live journal — carrying `request_json` / `response_json`, both hashes,
`model_provider` / `model_id` **and** `response_model` (under OpenRouter's fallback chain a
different model answers, and a different model is a different trader), `policy_hash` /
`knowledge_snapshot_id` from `policy_sources.collect()`, plus `tool_calls` and `tool_results`.
Replay is positional and never falls through to the provider.
**Two traps found by reading the SDK, not by testing it.** `with_options` returns a *new*
client with new resource objects, and the retry loop calls `self._client.with_options(
max_retries=0)` and then `create` on the **result** — a plain delegating proxy would have
recorded nothing while every line of the run looked fine. The proxy re-wraps instead, and one
test drives that exact path through the SDK's own switch. Separately, the wrapper's resource
had to be named `chat` rather than `_chat`: named `_chat` it fell out of `__dict__`,
`__getattr__` handed back the inner client's resource, and the journal stayed empty. Twenty
tests caught it on the first run.
**Failures are records too.** A retried call is two exchanges, and a journal holding only the
successful one would hand attempt #2's answer to a replay asking for attempt #1. A raising
call is written as `llm_error` and re-raised on replay, **advancing the cursor**, because the
failure *is* the answer to that call.
**Where it refuses, and why each refusal is the point.** Streaming raises in both recording
modes rather than capturing a truncated answer (nothing in this repository streams). A request
that differs from the recording raises with the first differing key, instead of being answered
anyway. A truncated journal is a broken recording, not a shorter one. The first write of a
process **replaces** the run's journal and says how many lines it dropped — appending would let
a second run of the same window answer from the first run's recording, which is evidence that
is *wrong* rather than absent. And a value with no JSON form is named by its type rather than
passed through `repr`, which carries a memory address; the mutation probe prints the two
addresses that made the fingerprint differ.
**Not covered, named rather than implied:** the digest, embedding and VPA clients have their
own call paths and are not journaled.
**Not yet demonstrated:** that a whole replay reproduces intents, fills and episodes. The
mechanism is pinned call by call; the end-to-end claim needs M1's runner.

### D23 — A T+1 walk had no execution model, so nothing stopped it inventing an intraday path (paid 2026-09-16)

**Added 2026-09-16, from the external review of the T+1 plan** — its P0-3 and P0-4, and
M0 item 5. My plan's leakage table said "T+1 开到收的**全路径**" while the same plan argued
there is no minute path; both sentences were mine and only one could be true. And nothing in
the code decided fills, so the first walk would have improvised: a limit resolved from
high/low, and a capacity cap judged from the fill day's own volume.
**Cost.** Both errors flatter the strategy: a stop and a target touched in one session
become a profitable round trip instead of an unanswered question, and an order fills because
the day *later* turned out liquid enough.
**Paid 2026-09-16** with `alpha_agents/data/t1_execution.py` and 27 tests. Execution is
open-only. Market orders settle at the open and are **refused when the open is the limit**
in the direction with no counterparty (一字板 — the case A-share backtests get most
expensively wrong). Limits settle from the open and low/high, which *is* decidable for a
single resting order. **Two levels in one session come back `ambiguous`** rather than
resolved: the review's own example (`open=10.30, high=10.50, low=9.40`, entry 10.00, stop
9.50) is one of the tests.
**The fill day's volume is unreadable because it is unspellable.** `DayBar` has no volume
field; sizing reads `capacity_shares(adv20)` and `average_daily_volume` over the sessions
*before* the fill; a test asserts the field's absence, so adding it for convenience fails
and forces the conversation rather than passing quietly. `average_daily_volume` returns
`None` for a short history instead of a shorter average — the caller asked for ADV20.
**Found by its own tests, worth recording:** the first version compared the open against the
limit with one direction for both sides, so a **sell was refused whenever the open sat at or
above the down limit** — that is, almost always. The test covering a limit-up sell caught it,
and a mutation probe confirms it still would.

### D22 — Nothing stopped a replay from writing into the shared history (paid 2026-09-16)

**Added 2026-09-16, from the external review of the T+1 plan** — M0 item 1, and the
remaining half of its state-isolation P0. `walk_bootstrap.py` shares the corpus by symlink
and *prints* that it is read-only, but a symlink is writable: `market_history` and
`snapshot_store` opened their file read-write whatever it was, so a replay that ran an
ingest step would have appended 2020 rows to the corpus the live pipeline also reads.
**Cost.** The failure is silent and cumulative. Nothing in the run reports it, and the next
reader — the live pipeline included — trusts a corpus that has quietly acquired a replay's
rows. It also makes a replay indistinguishable from live capture after the fact, which is
the opposite of what the split was for.
**Paid 2026-09-16** with `alpha_agents/data/corpus_access.py`. A symlink **is** the marker
of "shared", and a shared file is opened `mode=ro`, so a write raises
`sqlite3.OperationalError: attempt to write a readonly database` — an enforced stop instead
of a convention. Both stores skip the WAL pragma **and** the schema script when the file is
shared (both write, and neither is a replay's business on history it does not own); a file
we own behaves exactly as before, so the live pipeline still ingests normally.
**Recognise.** `tests/test_corpus_access.py` pairs every refusal with a read that must
succeed — a read-only connection that also could not read would satisfy "writes are
refused" while being useless — and closes by asserting that the links `walk_bootstrap`
creates are exactly the paths this treats as shared, so the two mechanisms cannot drift
apart while both look correct in isolation.

### D21 — A replay would have inherited and overwritten the live trader's memory (paid 2026-09-16)

**Added 2026-09-16, from the external review of the T+1 plan** — its first P0, and the
first of its twelve required tests. The plan proposed starting history from a copy of the
production `data/`, which gives *file* isolation and not *temporal* isolation: the fresh
copy would still hold 2026's principles, lessons, frozen policy and candidates, so a run
dated 2020 would reason with a 2026 brain. And there was no override to avoid it —
`config.DATA_DIR` was `PROJECT_ROOT / "data"` with no environment exit — so a walk could
not be pointed elsewhere even deliberately.
**Cost.** The whole value of a historical walk is showing what a trader would have done
with what it knew. A brain from the future does not invalidate the run loudly; it makes it
look good. The same hardcoded path also sends a replay's ledger writes into the production
book.
**Paid 2026-09-16.** `config._data_dir()` resolves `ALPHAAGENTS_DATA_DIR` and otherwise
falls back to `data/`, with a test that a **blank** value falls back too rather than
resolving to `Path('')` — an empty variable in a shell profile must not point every store
at the working directory. `scripts/walk_bootstrap.py` materialises a replay directory as a
**split, not a snapshot**: `memory.db` is created empty from the live schema and is never
linked, while `market_history.db` / `market_snapshots.db` / `stocks.db` are **shared by
reference**, which is the reviewer's own model — corpus read-only, state fresh. It refuses
to bootstrap into a directory that already holds trader state without `--force`, and it
prints what it shared rather than leaving it implicit.
**The review's test #1 is now `tests/test_walk_bootstrap.py`**: a 2026 candidate and a
2026 position are seeded into the corpus, and the replay's state must contain neither —
with the corpus file checked byte-identical afterwards and the fixture proven first, so
the isolation claim cannot pass vacuously.
**Still open, and named rather than implied:** nothing yet *enforces* that a run treats
the shared corpus as read-only. That is M0 item 1 in the T+1 plan; today it is a
convention plus a printed list.

### D20 — Market rules are not versioned by date, so a 2020 replay would apply today's limits (paid 2026-09-16)

**Added 2026-09-16, from the external review of the T+1 plan.**
`config.TRADABLE_PREFIXES` defaults to `60,000,001,002,003,300,301` — main boards **and
ChiNext**. There is no *executable* price-limit rule anywhere in `data/` or `tools/`:
the only limit-related code counts what happened (`market_data.get_limit_up_pool`,
`market_history.compute_limit_up_stats`) rather than deciding what may happen.
**Cost.** 深交所 moved ChiNext's daily limit from 10% to **20% on 2020-08-24**, so a
walk-forward beginning in 2020 that assumes ±10% is wrong on one side of that date for
every `300`/`301` name — and those are in the default universe. The design already
requires this to be a service ("market and fee rules must be versioned by instrument
and effective date"); the code does not have one.
**Paid 2026-09-16** with `alpha_agents/data/market_rules.py` and 22 tests in
`tests/test_market_rules.py`. The entry's own sketch was wrong in one place and the
exchange explainer corrected it: ChiNext risk-warned names are **5% before the reform
and 20% after it** — they follow their board, they do not inherit the main board's 5%.
`market_rules(code, date, *, name=…, listed_trading_days=…)` returns a
`MarketRule(price_limit_pct, lot_size, reason, unchecked)`:
main boards ±10% (±5% risk-warned); ChiNext ±10% before 2020-08-24 then ±20%;
ChiNext's first five sessions uncapped; unknown boards **raise** rather than default.
What it cannot determine it *names* in `unchecked` — `st_status` when no name is passed
(risk-warning lives in the name, not the code) and `listing_day_exemption` when no
listing age is passed — because a silent 10% for a risk-warned name is exactly the kind
of guess this debt was about.
**Not encoded, on purpose:** a 2026-07-06 change of the main-board ST limit to ±10% is
reported by secondary sources only; it needs an exchange notice. STAR, BSE and funds
have different rules and are refused rather than guessed.

### D19 — The cash-side T+1 rule is the withdrawal rule applied to buying power (paid 2026-09-16)

**Added 2026-09-16. Found by the external reviewer of the T+1 plan; confirmed here in
the code and against the market rule.**
A-share settlement separates **可用** from **可取**: the proceeds of a sale on T are
**usable immediately** — spendable on a further purchase the same day, repeatedly — and
become **withdrawable** on T+1. This repository applies the withdrawal rule to buying
power: `portfolio.get_available_capital` subtracts
`settlement.unreleased_pending_total`, and `settlement.record_pending` stamps the row with
`next_settle_date(exit_date)`, so sale proceeds are withheld from available capital for a
day.
**Cost.** On a paper trader that never transfers cash out, buying power is understated on
**every day with a sale**: selling one name and buying another the same day is forbidden,
so turnover, exposure, the cash curve and drawdown are all distorted. It is also exactly
the kind of error a "one execution path" reproduces faithfully — which is why it belongs
here rather than as a replay-only workaround.
**Sources checked 2026-09-16.** 中国证监会河南证监局 investor-protection case
("投资者卖出股票成交后T+1日方可取出资金" — the dispute was precisely this confusion,
read as 可用 when it is 可取) and broker material summarising 沪深 as
"T日卖出资金T日可用，T+1日可提现".
**Recognise.** `tests/test_cash_settlement_semantics.py` — three tests that **assert the
defect**, named so they cannot be read as approval, with the citations in the module
docstring.
**Paid 2026-09-16.** The share side was left alone — shares bought on T are sellable on
T+1, and that part was right. The cash side was split: `get_available_capital` no longer
subtracts the pending total, so a sale's proceeds are spendable the day they arrive, and
`get_total_capital`'s documented identity loses its `pending` term
(`total = available + invested + reservations`).
The in-transit figure was **kept**, because it still answers a different question — how
much of the cash cannot yet leave the account — and it turned out to have no reporting
consumer at all, so the trade read model's settlement section now reports it as
`cash_in_transit` (with `pending_settlements` added to that section's declared needs).
**Three tests encoded the old rule and were flipped, not deleted**; the one that mattered
asserted *"released proceeds must raise available cash"*, and now asserts that release
changes withdrawability and not buying power — proving the fixture first, so the equality
is not vacuous. `tests/test_cash_settlement_semantics.py` replaced its three
assert-the-defect cases with the contract, and the docstrings that described the old rule
(`portfolio`, `memory_schema`, `test_trader_ledger`) now describe the correct one.
Full suite after the change: **2030 passed / 18 skipped**.

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

### D15 — The repository's sample floor is declared in three places and enforced against a fourth number (paid 2026-09-15)

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
`tests/test_holdout_gate.py::TestTheDeclaredFloorAgainstTheRepositorysRule`
drives a real `promote` verdict at n = 25 — below the old rule, above the gate.
**Paid 2026-09-15, by operator decision, in the direction this entry did not
anticipate.** The operator set the floor at **20** — the code's number — so the
rule was **lowered to meet the code** rather than the code raised to meet the
rule. `GOVERNANCE_MIN_SAMPLES` went 50 → 20; it is deliberately absent from
`policy_sources._RULE_SOURCES`, so this drifted no version, and the cost this
entry priced ("every version already frozen becomes drifted", plus the lost
rollback target that follows from it) never had to be paid at all.
`GOLDEN_PRINCIPLES` §7, `TRADER_CORE_DESIGN` §12 and `README` now all say 20, and
§7 states plainly that the honesty bar was lowered and what that costs.
`promotion_floor_gap` survives for the case that is still real: a **version**
declaring a floor below the rule. The demonstrating test was renamed to
`test_the_band_the_old_gap_left_open_is_gone` and now asserts the closure.

### D16 — The daily stopping rule compares paired samples against validation days (paid 2026-09-15)

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
`test_the_stopping_rule_asks_again_while_the_two_units_disagree` asserted the
defect, and the probe "the stopping rule is made unit-consistent" turned it red —
so the test detected the disagreement rather than the fix, on purpose.
**Paid 2026-09-15, by following D15.** Both candidate fixes *were* the D15
question — whether the preregistered sample is 20 samples or 20 days — and the
operator answered it: **20 paired samples**. Pairs therefore compare with pairs.
`asked_already` now reads `verdict["n"]`, and so does `_position_line`, so a
verdict resting on twenty pairs from a single morning ends the asking instead of
looking short of a twenty-*day* bar. The demonstrating test was flipped, as its
own docstring instructed, to
`test_a_verdict_that_reaches_the_sample_bar_ends_the_asking`; two sibling cases
that had been leaning on the mixed unit now state `n` explicitly instead of
leaving it absent (absent read as zero, which happened to give the right answer
for the wrong reason). Evidence: putting `validation_days` back on the
right-hand side turns exactly two cases red —
`test_it_does_not_ask_again_the_next_day` and the flipped one.

### D17 — The version in force no longer matches the running configuration (paid 2026-09-15)

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
**Paid 2026-09-15** with `freeze --by evilkylin --reason "the theme gate, frozen
after the fact"`, after backing the database up (`data/memory.db.bak-20260915-192259`)
and checking `--dry-run` first. It moved no pointer, as designed. Version #2
(`d9c28e312eef55e9`) now declares the full decision block **including
`theme_gate`**, while #1's block simply does not contain that key — so the thing
this entry was about is now visible in the record instead of only in prose.
Verified: `verify_live(2) is True`, `verify_live(1) is False`, `integrity: clean`,
and `status` still prints `live configuration still matches: False` because the
pointer is still on #1 — which is the correct state until a verdict exists.
`drifted()` returns `[1]`, so **#1 can no longer be rolled back to**; #2 is the
first clean version and becomes the rollback target once promoted. Freezing it
also decided what the next candidate is, which this entry said was the
operator's call.
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

- **A market rule the repository got wrong, and a rule it never had (D19, D20)** — both
  found by an external review of the T+1 plan on 2026-09-16 and paid the same day. Sale
  proceeds were withheld from buying power for a day: the *withdrawal* rule applied to
  *spending*, which forbade selling one name and buying another the same day, where the
  A-share rule is 可用 on T and 可取 on T+1. And no executable price-limit rule existed at
  all, while the default universe includes ChiNext, whose limit moved 10% → 20% on
  2020-08-24. The second is now `data/market_rules.py`, sourced from the exchange's own
  explainer — which also corrected the entry's sketch, since ChiNext risk-warned names
  follow their board (5% before the reform, 20% after) rather than the main board's 5%.
  Both are errors a walk-forward would otherwise have reproduced faithfully.
- The version in force no longer matched the running configuration (D17): a
  theme-gate commit added a key to the decision block three hours after `install`
  ran, so every "is the incumbent still the incumbent" question had a wrong
  answer. Paid 2026-09-15 by freezing the configuration that is actually running
  as version #2 — no pointer move, no evidence needed. #1's block simply lacks
  the `theme_gate` key while #2's carries it, so the gap is now visible in the
  record; and because #1 reports drifted, #2 is the first clean version and the
  rollback target once promoted.
- The daily stopping rule compared a count of paired samples against a count of
  validation days (D16), so an experiment passed the sample bar long before the
  day bar and the gate was asked every run in between — the "near-identical
  insufficient row every day" the module was written to avoid. Paid 2026-09-15
  once D15 settled the unit: both sides compare **paired samples** now, and
  reverting that turns exactly two cases red.
- The repository's sample floor was declared in three places and enforced against
  a fourth (D15). Closed 2026-09-15 by **lowering the rule from 50 to 20** to meet
  the code, not by raising the code to meet the rule: the gate's constant is a
  behaviour source, so raising it would have drifted every version already frozen
  — and taken the rollback target with it — while the rule constant moves nothing
  a trader does. `GOLDEN_PRINCIPLES` §7, `TRADER_CORE_DESIGN` §12, `README` and
  `GOVERNANCE_MIN_SAMPLES` now all say 20, and §7 states plainly that the honesty
  bar was lowered and what that costs.
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
