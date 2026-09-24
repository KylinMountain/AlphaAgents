# Documentation map

Which document is authoritative for what. Read the one that matches your
question; the rest is context you do not need.

A document's **role** matters more than its date. A normative design is
correct to describe a target that does not exist yet; a status record is
wrong the moment it lags the code.

## Sector-First: current pre-validation review and implementation entry

**2026-09-20 验证前审查结论：REWORK。** 对 Sector-First 是否具备正式验证资格，
先读 [整体流程合伙人审查](reviews/2026-09-20-sector-first-partner-review.md)，
再按 [整改实施计划](exec-plans/active/2026-09-20-sector-first-review-remediation.md)
领取 RP-00 / RP-01。报告冻结基线证据，实施计划记录任务状态、依赖、验收与放行门；
原 Sector-First roadmap 保留目标设计和历史记录，不以旧复选框推断当前已验证。

当前明确分开四件事：文档提交、代码修复、工程验收、策略有效性。新报告与计划本身
不完成工程任务，不更改 active policy，也不把 n=0 改成有效策略样本。

## If you are reviewing this project from outside

**要一份可以直接转发的单文件交接包？** 用 [`REVIEW_PACKET.md`](REVIEW_PACKET.md) ——
它把下面这些打包成一封**自包含**的信：项目是什么、现在的实测状态、六个待评问题、
一个代表性失败、以及「不要看什么」。下面是权威原文，那份是它的导出。

Read these **in this order**. Each answers a different question, and reading
them out of order produces confident answers to the wrong question.

| # | Read | Why this one | Size |
|---|---|---|---|
| 1 | [`DESIGN_REVIEW.md`](DESIGN_REVIEW.md) — **start with its header** | Written for exactly this purpose: what we want reviewed, the honest negative results, the known defects, and the questions we cannot answer ourselves. Its header declares which sections are a dated snapshot and which still stand. | ~570 lines |
| 2 | [`exec-plans/active/`](exec-plans/) — the plans in flight | What is being worked on **right now**, with machine-checkable acceptance criteria. A review that skips these reviews yesterday's project. | ~200 lines each |
| 3 | [`TRADER_CORE_DESIGN.md`](TRADER_CORE_DESIGN.md) **§2 and §14 only** | §2 is the product definition (what Trade, Learn and Evolve each mean); §14 is the five phases and what each phase's completion means. Do **not** read the rest of the design as a description of what exists. | those two sections |
| 4 | [`TRADER_CORE_IMPLEMENTATION.md`](TRADER_CORE_IMPLEMENTATION.md) **§7 only** | The list of things this project decided **not** to pretend: what is deliberately unbuilt, and why. | that section |
| 5 | [`exec-plans/tech-debt-tracker.md`](exec-plans/tech-debt-tracker.md) `## Open` | Every open item with its cost and how it is recognised — so a reviewer can tell "known and priced" from "missed". | Open section |
| 6 | [`QUALITY_SCORE.md`](QUALITY_SCORE.md) | Where the evidence is thin, area by area, with the specific gap that caps each grade. Every Gap cell carries the date it was measured. | ~80 lines |
| 7 | [`GOLDEN_PRINCIPLES.md`](GOLDEN_PRINCIPLES.md) | The non-negotiable invariants — and, more usefully, **what checks each one**. | ~170 lines |
| 8 | [`REVIEW.md`](REVIEW.md) | The review mode this repository asks for and the output format it expects. | ~85 lines |

Only if the review touches code, add [`AGENTS.md`](../AGENTS.md) (how to work in
this repo, acceptance criteria, evidence rules) and
[`ARCHITECTURE.md`](../ARCHITECTURE.md) (layer map, databases, the trading day).

**If you only have time for three things:** 1, 2 and 4. The first is what we
want to know; the second is what is actually moving; the third is what we are
not pretending to have.

**What to ignore, and why.** `exec-plans/completed/` is an archive — its links
and module names are not maintained, and it described the world at the time.
The dated studies under *Research inputs* above are the evidence behind
decisions, not descriptions of current behaviour.
[`strategy_evaluation_2026-09.md`](strategy_evaluation_2026-09.md) is worth
reading for its **method and its negative results**, not for its state: it is
the one place the exit side was measured against market history.

## Authoritative — read these before acting

| Question | Document | Role |
|---|---|---|
| How do I install, configure and run it, and write my own trader? | [`INSTALL.md`](INSTALL.md) | Operator guide: setup, every env var, traders, replays. Follows the code. |
| What is this system supposed to become? | [`TRADER_CORE_DESIGN.md`](TRADER_CORE_DESIGN.md) | Normative target. Asserts nothing about what is built. |
| **What actually exists in the code?** | [`TRADER_CORE_IMPLEMENTATION.md`](TRADER_CORE_IMPLEMENTATION.md) | Status record. Code is the source of truth; this file follows it. |
| What are the non-negotiable invariants, and what enforces them? | [`GOLDEN_PRINCIPLES.md`](GOLDEN_PRINCIPLES.md) | Invariants + the mechanism that checks each. |
| How is the code laid out, and what may depend on what? | [`../ARCHITECTURE.md`](../ARCHITECTURE.md) | Layer map, databases, the day. |
| How do I work in this repo? | [`../AGENTS.md`](../AGENTS.md) | Instructions, acceptance criteria, evidence rules. |
| How do I review code / architecture / backtest / prompt? | [`REVIEW.md`](REVIEW.md) | Review mode and the required output format. |
| Is Sector-First ready for formal validation, and what should be fixed first? | [Partner review](reviews/2026-09-20-sector-first-partner-review.md) · [Remediation plan](exec-plans/active/2026-09-20-sector-first-review-remediation.md) | Dated evidence / active work packages and acceptance gates. Not a strategy-promotion approval. |
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
| [Sector-First partner review](reviews/2026-09-20-sector-first-partner-review.md) | 2026-09-20 | Quant / A-share / Agent review at main@eab3f1ec, with evidence strength, F01–F14 findings and isolated reproductions. |

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
