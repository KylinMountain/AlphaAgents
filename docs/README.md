# Documentation map

Which document is authoritative for what. Read the one that matches your
question; the rest is context you do not need.

A document's **role** matters more than its date. A normative design is
correct to describe a target that does not exist yet; a status record is
wrong the moment it lags the code.

## Authoritative — read these before acting

| Question | Document | Role |
|---|---|---|
| What is this system supposed to become? | [`TRADER_CORE_DESIGN.md`](TRADER_CORE_DESIGN.md) | Normative target. Asserts nothing about what is built. |
| **What actually exists in the code?** | [`TRADER_CORE_IMPLEMENTATION.md`](TRADER_CORE_IMPLEMENTATION.md) | Status record. Code is the source of truth; this file follows it. |
| What are the non-negotiable invariants, and what enforces them? | [`GOLDEN_PRINCIPLES.md`](GOLDEN_PRINCIPLES.md) | Invariants + the mechanism that checks each. |
| How is the code laid out, and what may depend on what? | [`../ARCHITECTURE.md`](../ARCHITECTURE.md) | Layer map, databases, the day. |
| How do I work in this repo? | [`../AGENTS.md`](../AGENTS.md) | Instructions, acceptance criteria, evidence rules. |
| How do I review code / architecture / backtest / prompt? | [`REVIEW.md`](REVIEW.md) | Review mode and the required output format. |
| What is in flight, what was decided, what do we owe? | [`exec-plans/`](exec-plans/) | `active/` in flight · `completed/` records · `tech-debt-tracker.md` debt |
| How healthy is each area? | [`QUALITY_SCORE.md`](QUALITY_SCORE.md) | Graded on evidence, with the gap that caps each grade. |

**The phase model lives in one place:** `TRADER_CORE_DESIGN.md` §14. It lists
five phases — 1 trustworthy minimum slice, 2 complete trading kernel,
3 episode learning, **4 controlled evolution**, 5 product consolidation — and
says what each phase's completion means. Nothing else defines the phases.

## Research inputs — the evidence behind the design

Studies and design arguments. They are **inputs, not specifications**, their
numbers are dated, and they do not describe current behaviour. Read them to
understand *why* a decision was made, never to find out what the system does.

| Document | Dated | Answers |
|---|---|---|
| [`strategy_evaluation_2026-09.md`](strategy_evaluation_2026-09.md) | 2026-09-07 | Does the news-driven entry/exit strategy survive contact with market history? Largely negative results. |
| [`self_improvement_roadmap.md`](self_improvement_roadmap.md) | 2026-09-07 | G1–G6: which self-improvement mechanisms have external evidence behind them, and which are deliberately deferred. |
| [`thesis_design.md`](thesis_design.md) | 2026-09-08 | Why a position's unit is a falsifiable thesis rather than a ticker. |
| [`multi_trader.md`](multi_trader.md) | 2026-09-09 | Why variation has to be a file, and how two traders are kept independent. |
| [`trader_toolkit.md`](trader_toolkit.md) | 2026-09-10 | What a trader should *ask*, instead of hunting for signals. |
| [`DESIGN_REVIEW.md`](DESIGN_REVIEW.md) | 2026-09-11 | Point-in-time brief written for an external reviewer. §7's state queries are a 2026-09-11 snapshot and are stale; §6's measured negatives still stand. |

## Process

| Document | Answers |
|---|---|
| [`exec-plans/README.md`](exec-plans/README.md) | How to write, check and archive a plan. |
| [`references/README.md`](references/README.md) | When to vendor a dependency's docs (currently empty). |

## Archived

[`exec-plans/completed/`](exec-plans/completed/) is a historical record. Its
links and module references are **not maintained** and are exempt from the doc
linter — it described the world at the time, and rewriting history to satisfy a
linter would destroy the only thing an archive is for.

## Keeping this honest

Two rules, both cheap to break:

- **A status claim needs a measurement.** If a document says a mechanism works,
  it should say what checks it. `TRADER_CORE_IMPLEMENTATION.md` §9 records the
  commands and their output for this reason.
- **A gap goes in the document, not in someone's head.** When something is
  deliberately not done, it belongs in `TRADER_CORE_IMPLEMENTATION.md` §7 —
  that section is the list of things this project has decided *not* to pretend.
