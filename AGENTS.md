# AGENTS.md

A map, not a manual. Everything here points somewhere; nothing here is the
full story. Read the pointer that matches your task — loading all of it
crowds out the code you came to change.

## What this is

A simulated A-share trader: it trades, and it learns from its own results.

The delivery order is **trustworthy trading facts → attributable learning →
verifiable evolution**, and it is not reorderable — a system that learns from
results it cannot account for learns its own bugs. More reports, or more
agents, cannot substitute for that order.

The scheduled tasks are the trader's day, not the point of the system. Its
recommendations are graded against market data, never against a model's
opinion of itself.

Start with `docs/README.md` for which document is authoritative for what, and
`ARCHITECTURE.md` for the domain map and layering.

## Non-negotiable invariants

Enforced mechanically by `scripts/lint_harness.py` (run in CI). Violating
one fails the build; the error message tells you how to fix it.

1. **Layer direction.** `data → sources → tools → evolution → pipeline →
   agents → server`. Never import backwards. Cross-cutting (`config`,
   `http_client`, `notify`) may be imported from anywhere.
2. **No LLM grades its own output.** Utility of a memory, principle or
   playbook comes from market data only. See `docs/GOLDEN_PRINCIPLES.md`.
3. **Parse at the boundary.** Every external payload is shaped before it
   reaches business logic. How is up to you.
4. **File size.** 1200 lines. Past that, split by responsibility.
5. **Structured logging.** `logger.info("msg %s", x)`, never f-strings —
   lazy formatting and greppable messages.
6. **No bare `except:`** and no silent `except Exception: pass` on a path
   that can lose data.

## Where the truth lives

| Question | File |
|---|---|
| **Which document is authoritative for what?** | `docs/README.md` |
| How is the code laid out? | `ARCHITECTURE.md` |
| What rules keep it coherent? | `docs/GOLDEN_PRINCIPLES.md` |
| What is the strategy, and does it work? | `docs/strategy_evaluation_2026-09.md` |
| What is being built next, and why? | `docs/self_improvement_roadmap.md` |
| **What phase are we in, and what does each phase mean?** | `docs/TRADER_CORE_DESIGN.md` §14 |
| What is the trader core, and what of it exists? | `docs/TRADER_CORE_DESIGN.md` (target) · `docs/TRADER_CORE_IMPLEMENTATION.md` (built) |
| What is in flight? | `docs/exec-plans/active/` |
| What was decided and shipped? | `docs/exec-plans/completed/` |
| What do we owe? | `docs/exec-plans/tech-debt-tracker.md` |
| How healthy is each area? | `docs/QUALITY_SCORE.md` |
| How is it deployed? | `deploy/README.md` |

## Before you write code

- **Small change** — no plan file. Make it, test it, commit it.
- **Anything spanning files or changing behaviour** — write
  `docs/exec-plans/active/<slug>.md` first: goal, acceptance criteria
  that a machine can check, decision log. Move it to `completed/` when
  done.

State acceptance criteria as numbers wherever you can. "Improve the exit
rule" is not checkable; "median excess return over 24 windows does not
degrade" is.

## Verification

```bash
uv run pytest tests/ -q          # must pass before every commit
uv run python scripts/lint_harness.py   # invariants
uv run python scripts/lint_docs.py      # knowledge-base freshness
```

Tests are the contract. A change without a test that would have caught
its absence is not finished.

## Evidence rules

This repo makes claims about markets, and it is easy to fool yourself.

- Compare **median to median**. A-share cross-sections are right-skewed;
  a group median against a market mean manufactures a fake edge. This has
  already produced one wrong conclusion here — see the note in
  `alpha_agents/tools/exit_signals.py`.
- **n < 50 does not ship.** A first pass on six sparse windows gave the
  exact opposite ranking to the dense run.
- **Validation is forward.** Never evaluate on days the rule was distilled
  from.
- Report what the data said, including when it contradicts the reason the
  work was started.

## Review mode

When asked for a code, architecture, backtest or prompt review, use
`docs/REVIEW.md` — the Anna Coulling volume-price mindset applied to
engineering: *where is the volume behind this price?*
