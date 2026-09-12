# Golden principles

Opinionated, mechanical rules that keep this codebase legible to the next
agent run. They constrain **invariants**, not implementations: what must
hold, never which library gets you there.

Each one says how it is enforced. A principle nothing checks is a wish,
and it rots the same way a stale instruction file does.

---

## 1. No LLM grades its own output

**Rule.** The utility of a memory, principle, playbook or recommendation
comes from market data. A model may *propose*; only prices *judge*.

**Why.** A model asked to judge its own memories accepts its own wrong
ones 31–54% of the time. Swapping in a stronger judge does not fix it —
the errors stay correlated. Only verification grounded in something
outside the model breaks the loop.

**Enforced by.** `lint_harness.py` flags any call that feeds an LLM
response into a status/weight/score write, inside `evolution/` — the layer
where memory utility is decided. Two modules are exempt there:
`evolution/principle_scoring.py` and `evolution/holdout_gate.py`, because both
grade from market data rather than from a model. `data/scoring.py` computes the
scores themselves (excess, residual, Brier) and sits outside that check's scope.

**Corollary.** Where a model must assess something, it states its own
answer first, then compares. See `evolution/two_stage_judge.py`.

---

## 2. Parse at the boundary

**Rule.** Every external payload — HTTP, LLM, SQLite row — is shaped
before it reaches business logic. Never probe a dict three layers deep
and hope.

**Why.** Guessed shapes fail far from where they were guessed. The stack
trace lands in the consumer, not at the boundary that let it in.

**Enforced by.** Structural test: modules under `pipeline/` and `agents/`
may not index raw `json.loads` output. Not prescriptive about how —
dataclass, TypedDict or explicit `.get` with defaults all pass.

---

## 3. Shared utilities over hand-rolled helpers

**Rule.** A helper needed twice moves into a shared module. Invariants
belong in one place.

**Why.** Agents replicate what they find. A helper copied three times
becomes four subtly different helpers, and the fix has to be made four
times.

**Enforced by.** `lint_harness.py` reports near-duplicate function bodies
across modules.

---

## 4. Layer direction is one-way

**Rule.** `data → sources → tools → evolution → pipeline → agents →
server`. Cross-cutting (`config`, `http_client`, `notify`) is importable
anywhere. Nothing else crosses backwards.

The order is declared once, in `scripts/lint_harness.py`; the storage
layer comes first because a source's job includes persisting what it
fetched, and `evolution` sits below `pipeline` because the review task
drives lesson extraction rather than the other way round.

**Why.** This is the constraint that keeps the repo navigable as it
grows. Usually postponed until a team is large; with agents writing the
code it is a prerequisite, because drift compounds daily.

**Enforced by.** `lint_harness.py`, hard failure.

---

## 5. Compare median to median

**Rule.** When measuring anything cross-sectional, benchmark a median
against a median. Never a group median against a market mean.

**Why.** A-share returns are right-skewed, so that mismatch manufactures
a negative edge out of nothing. It has already produced one wrong
conclusion in this repo — the first exit-signal study — which reversed
entirely once fixed.

**Enforced by.** Review checklist; not mechanically checkable. Cite the
comparison basis in the docstring of any function that computes an edge.

---

## 6. Forward validation only

**Rule.** A rule is never evaluated on days it was distilled from.
Validation is what happened *after* the candidate existed.

**Why.** A proportional split of history leaks: the recent slice is
exactly what reflection fitted to. Reflection cannot reach forward, which
is what makes a creation-date boundary sound.

**Enforced by.** `evolution/holdout_gate.py` splits on creation date;
tests assert the creation day itself is training.

---

## 7. Small samples do not ship

**Rule.** No conclusion with n < 50 reaches production code. State n and
the number of independent windows next to any claimed effect.

**Why.** A first pass here on six sparse windows (n = 7–17) ranked three
exit signals in the exact reverse of the dense run.

**Enforced by.** Review checklist; `lint_docs.py` requires research docs
to state sample sizes.

---

## 8. Failures are loud

**Rule.** No bare `except:`. No `except Exception: pass` on a path that
can lose data. A degraded source reports itself.

**Why.** The PBOC parser returned eight rows whose every headline was the
literal string `"true"` for months — worse than an outage, because
nothing looked wrong.

**Enforced by.** `lint_harness.py`.

---

## 9. Structured logging

**Rule.** `logger.info("msg %s", value)` — never f-strings in log calls.

**Why.** Lazy formatting, and messages stay greppable when values differ.

**Enforced by.** `lint_harness.py`.

---

## 10. Files stay under 1200 lines

**Rule.** Past the limit, split by responsibility.

**Why.** A file an agent cannot hold in context gets edited blindly, and
blind edits are where duplication starts.

**Enforced by.** `lint_harness.py`.
