# Trader Core Design: Trade, Learn, Evolve

- Design date: 2026-09-11.
- Status: authorized target architecture; implementation has only begun Phase 1.
- Scope: a persistent simulated trader, trustworthy execution facts, attributable learning, and controlled policy evolution.
- This is a normative design, not a release checklist or evidence that the complete architecture exists.
- “Must” states a target invariant; proposed components below are responsibilities, not claims of existing modules.

## 1. Authority and implementation boundary

The design formalizes the local [Trade Learn Evolve proposal](../outputs/trade-learn-evolve-design.html).
The [Phase 1 plan](exec-plans/completed/2026-09-11-trader-core-phase1.md) governed the initial implementation scope; it closed on 2026-09-11 with each acceptance criterion mapped to a test, and the measured results are recorded in the [implementation status](TRADER_CORE_IMPLEMENTATION.md).
The [architecture map](../ARCHITECTURE.md), [repository instructions](../AGENTS.md), and [golden principles](GOLDEN_PRINCIPLES.md) provide engineering and evidence constraints.
The verified dependency order is `data → sources → tools → evolution → pipeline → agents → server`, as declared in [scripts/lint_harness.py:35](../scripts/lint_harness.py#L35).
Layer-order text in [AGENTS.md](../AGENTS.md) and [golden principles](GOLDEN_PRINCIPLES.md) has since been corrected to match that declaration; the order is stated once in the linter and restated, not redefined.
The HTML proposal's static observations are design inputs, not a fresh audit of concurrently changing code or a measurement of historical losses.
This document states the target and asserts nothing about what is built. For whether Phase 1 passed, read the implementation status document, which records the commands run, their output, and what remains unimplemented; no claim here should be read as a substitute for that.

## 2. Product definition and governing invariants

AlphaAgents is to become a system in which a persistent trader acts within an explicit mandate, owns the consequences, learns from evidence, and changes behavior through controlled experiments.
**Trade** means deciding to enter, add, hold, reduce, exit, or abstain under information, cash, inventory, and risk constraints.
**Learn** means separating observed outcomes from explanations and proposing falsifiable improvements with supporting and opposing evidence.
**Evolve** means replacing a frozen policy only after independent forward evaluation and authorized approval; insufficient evidence means retaining the incumbent.
The delivery order is trustworthy trading facts, attributable learning, then verifiable evolution; more reports or agents cannot substitute for that order.

1. Identity persists across jobs, restarts, policy changes, pauses, and losses.
2. Fills and ledger entries determine financial results; reports are rebuildable read models, never accounting inputs.
3. Shared facts retain provenance; shared inferences additionally retain their author, version, evidence, and creation time.
4. Every decision uses only information available at its decision time; later corrections do not rewrite that information boundary.
5. The model proposes decisions and hypotheses; deterministic services enforce constraints, execute, account, and measure outcomes.
6. An LLM cannot grade its own performance, approve its own candidate, relax its mandate, or modify historical results.
7. Unvalidated knowledge cannot silently alter active behavior, including through retrieval, weights, retirement, or prompt injection.
8. Simulation, forward shadow, historical replay, and legacy evidence remain distinguishable throughout storage and presentation.

## 3. Persistent domain model

`Trader = Identity + Mandate + ActivePolicy + Book + WorkingState + EvidenceHistory`.
This is a composition of bounded responsibilities, not a new all-purpose class.
The existing configuration model at [alpha_agents/data/trader.py:59](../alpha_agents/data/trader.py#L59) is an integration starting point, not the complete target aggregate.

| Object | Required responsibility and evidence | Mutation authority |
|---|---|---|
| TraderIdentity | Stable `trader_id`, name, lineage, creation time, lifecycle state | Authorized management commands |
| Mandate | Versioned capital authorization, permitted instruments, risk ceilings, resource budget, and strategy boundaries | External authorization only |
| PolicyVersion | Immutable behavioral snapshot, parent version, hashes, applicability, and approved knowledge dependencies | Candidate authoring; activation only through promotion |
| TraderRuntime | Durable event progress, working state, current information boundary, and references to orders and positions | Validated event transitions |
| Decision / Thesis | Action, forecast event and probability, horizon, evidence, counterevidence, limits, and invalidation conditions | Append-only decisions and explicit thesis revisions |
| DecisionEpisode | Related decisions, orders, fills, exits, abstentions, and outcome maturation | Deterministic association through explicit IDs |
| Order / Fill | Execution intent and state; individual simulated executions with price, quantity, time, and fees | Deterministic execution kernel |
| Ledger / Book | Economic postings, reservations, settlement lots, and derived cash, positions, and equity | Accounting kernel; projections are rebuildable |
| ExperienceCandidate | Falsifiable claim, behavioral delta, applicability, supporting episodes, alternatives, and uncertainty | Learner proposes; evidence service appends measurements |
| Experiment / Promotion | Frozen comparison protocol, isolated runs, evaluation decision, approval, activation, and rollback audit | Evaluator measures; authorized control plane approves |

A policy snapshot includes actual prompt contents, known model identity and configuration, tool contracts, selection, sizing, entry/exit rules, retrieval policy, calibrators, and approved knowledge snapshots.
Working memory may append observed facts and prior decisions, but cannot smuggle new executable rules into a frozen policy.
Capital changes are authorized external cash-flow events, not automatic rewards for recent performance.

## 4. Identity, provenance, and lifecycle

Every applicable record must resolve through `mode`, `run_id`, `trader_id`, `policy_version_id`, and the relevant snapshot, decision, and episode IDs.
The execution chain is `decision_id → order_id → fill_id → ledger_entry_id`; forecasts bind through `forecast_id`, and thesis ownership through `thesis_id`.
Foreign keys and ownership checks establish attribution; matching the same symbol, date, or “latest recommendation” is not a substitute.
An episode can contain multiple decisions, orders, and partial fills; an order with no fill can still produce execution evidence.
Different traders may hold the same instrument independently without sharing cash, positions, experiences, or policy approval state.
Lifecycle states include `active`, `paused`, `draining`, and `archived`; pausing blocks new risk but preserves management of existing exposure.
Archiving requires resolution or explicit transfer of outstanding responsibilities; removing a configuration file cannot orphan a position.
Unknown identity, corrupt state, or lost event ownership must enter a protective state, not silently fall back to another trader.
Policy changes retain trader identity; experimental branches have isolated account/run identities and explicit parent lineage.

## 5. Runtime and operating modes

The target is a modular monolith with serialized state transitions per account and shared read-only world snapshots.
The fast loop handles market updates, orders, protective exits, risk limits, reconciliation, and trading-day transitions without waiting for an LLM or report.
The slow loop requests structured decisions when information materially changes, matures outcomes, proposes learning candidates, and schedules justified experiments.
Schedules emit events; they must not embed parallel trading rules or directly mutate positions.

| Mode | Clock and input | State and evidence boundary |
|---|---|---|
| Regular paper trading | Incoming market stream and explicit decision clock | Main simulated account; fills are simulated, never real brokerage executions |
| Historical replay | Replay clock and point-in-time data adapter | Separate run and ledger; development evidence with declared data limitations |
| Forward shadow | Future common market inputs after candidate freeze | Independent candidate account, working memory, knowledge namespace, and evaluation records |

All modes use the same decision contract, risk checks, order transitions, execution assumptions, and accounting kernel; adapters change time and data access.
Replaying recorded model requests and responses can target deterministic ledger reproduction; asking the model again creates a new experiment.
A model alias, temperature, or seed alone does not guarantee reproducibility across provider updates; preserve request, response, known model identifiers, and tool results.
Use persistent idempotency keys, account event sequence numbers, transactional state changes, and durable outbox delivery for recoverable side effects.
One account writer or equivalent transactional serialization is required; multiple workers additionally require a lease or fencing mechanism rather than only an in-memory lock.

## 6. Trade: one execution path

The proposed execution flow is shared by entry, scale-in, reduction, liquidation, and automatic protective actions.

```text
MarketEvent → WorldSnapshot → DecisionIntent → SchemaValidation
  → PreTradeRisk → Cash/InventoryReservation → PaperBroker
  → FillEvent + Fees → LedgerTransaction + Outbox
  → Cash/Position/EquityProjections → Reconciliation
```

`PaperBroker` is a proposed responsibility, not a declaration that a complete broker module already exists.
Models and task adapters submit intents; they do not call a direct “change position” or “write return” operation.
An intent carries its action, evidence, information cutoff, source IDs, price/size constraints, expiry, and owning policy version.
Distinguish abstention, risk rejection, invalid schema, unavailable data, model timeout, cancellation, and unfilled orders; do not collapse them into a successful no-trade decision.
The order state machine supports `submitted`, `accepted`, `partially_filled`, `filled`, `rejected`, `cancel_pending`, `cancelled`, and `expired`, with explicit legal transitions.
A cancellation request is not a completed cancellation; preserve valid racing fills and release only the reservation for the confirmed cancelled remainder.
Submission reserves sufficient eligible cash or sellable inventory; partial fills consume reservations, and terminal remainders release them.
Validate dynamic constraints again before a fill, including freshness, price limits, liquidity, remaining authorization, and settlement eligibility.
A touched price does not guarantee a fill; participation, available volume, latency, suspensions, and declared matching assumptions constrain execution.
A stop is an exit intent, not a guaranteed execution price; blocked exits retain visible unresolved risk.
Global protection, trader pause, and degraded-data controls operate independently of model availability and report generation.
Market and fee rules must be versioned by instrument and effective date and verified during implementation; this design does not assert universal fixed market constants.

## 7. Ledger, valuation, and financial truth

Fills and explicit non-trade economic events produce balanced, append-only accounting transactions; positions and financial summaries are derived views.
Use fixed-precision amounts or integer monetary units with a declared rounding and cost-allocation policy.

```text
total_cash = opening_cash + net_external_cash_flows
             + sale_proceeds - purchase_payments - actual_fees + cash_entitlements
available_cash = cash_eligible_under_settlement_rules - reserved_cash
equity = total_cash + marked_inventory + receivables - payables
net_pnl = equity - opening_equity - net_external_contributions
```

Posting classifications must avoid counting the same entitlement in both cash and receivables; settlement eligibility is separate from economic ownership.
Realized P&L is derived from proceeds, allocated cost, and fees; it must not be added again on top of sale proceeds.
Partial exits and the final exit aggregate all corresponding fills and costs; the last exit must not overwrite earlier realized results.
Retain settlement lots, sellable times, fee assumptions, external flows, and applicable corporate actions rather than only an average position price.
A repeated command or fill must have one economic effect; recovery must reproduce the same account state without duplicate charges.
Correction entries reference the original posting and reason; they do not erase the original event or invent an execution.
Missing prices may retain the last reliable mark for operations only, with stale-quality flags; cost-price substitution cannot manufacture a valid flat return series.
Unresolved accounting differences or unsupported corporate actions block affected performance evidence from promotion evaluation.
Return percentages additionally require an explicit capital denominator and external-flow treatment; net P&L alone is not a cash-flow-adjusted rate of return.
Reports, dashboards, and learning explanations consume ledger-derived measures and cannot become alternative financial sources of truth.

## 8. Data time and shared-world contract

Record event occurrence, source publication, system receipt, availability, decision, order submission, and fill times separately.
Every decision input must satisfy `available_at ≤ decision_at`; labels additionally carry `label_available_at`.
Operational availability cannot precede actual receipt; replay adapters must declare how historical availability was established and flag unknown receipt history.
Store timezone-aware instants, original source timezone, exchange calendar version, and session identity; do not compare ambiguous local timestamps.
Snapshots freeze data versions, source IDs, revisions, freshness, missingness, and the admissible information cutoff.
Corrected data is appended with its own availability time; an as-of query must recover the version that was visible then.
Completed end-of-day bars cannot justify ideal fills earlier inside the same bar; ambiguous intrabar ordering uses a preregistered conservative rule.
Forecast deadlines follow the declared market calendar, not the next conveniently available price for an individual instrument.
Missingness, survivorship, corporate-action coverage, and data revisions must remain visible in replay and evaluation eligibility.
News and prices may be shared as sourced observations; extracted assertions and classifications retain their transformation provenance.
A theme state, causal attribution, or market-regime interpretation is an inference, not a neutral fact: retain author, policy/model version, creation time, and evidence references.
Shared inferences are optional attributed research inputs; an experiment freezes their producer and selection protocol to prevent cross-candidate contamination.
Historical LLM experiments cannot fully rule out pretrained knowledge of later events; replay supports development, not proof of uncontaminated forward skill.

## 9. Learn: three independent outcomes

The learning unit is the decision episode, not the review report or only a completed profitable position.
Record opportunities, trades, holds, abstentions, rejected intents, missing responses, cancellations, partial fills, and early exits to expose selection and coverage.

| Outcome | Measurement contract | Prohibited substitution |
|---|---|---|
| ForecastOutcome | Declared event, probability source, target, deadline, benchmark, Brier/log score, calibration, and coverage | A close-out return cannot overwrite the original fixed-horizon forecast label |
| TradeOutcome | Ledger-derived net P&L, costs, exposure duration, capital use, execution shortfall, and observable MFE/MAE | A correct forecast or hypothetical price move cannot be counted as earned profit |
| ProcessOutcome | Compliance with the then-frozen decision/risk rules, latency, missing data, system faults, and execution impediments | Profit does not excuse a violation; a compliant loss is not automatically a process failure |

One episode may simultaneously have a correct forecast, negative trading P&L, and compliant process; each result must remain independently inspectable.
A forecast may mature without any trade; an unfilled order has no invented trading return but retains process evidence.
Explicit model probabilities and confidence-to-probability mappings have distinct provenance and calibration versions.
Outcome states include `pending`, `matured`, `censored`, and `revised`, with evaluator version, evidence IDs, and availability time.
Unclosed positions retain interim valuations rather than a fabricated terminal result; missing terminal evidence remains censored or pending under the protocol.
Label corrections append revisions; later knowledge must not alter what an earlier learning run could access.
Deterministic services compute outcome metrics before a learner interprets them; narrative plausibility cannot establish causation.
Counterfactuals require a declared alternative policy, equal information and execution constraints, and a separate `CounterfactualOutcome` namespace.
A counterfactual is research evidence, never a historical fill, ledger correction, or “lost profit” measured by hindsight-optimal prices.

## 10. Candidate knowledge and behavioral isolation

An experience candidate records a falsifiable `claim`, `applicable_context`, `proposed_behavior_delta`, and explicit supporting and opposing `evidence_episode_ids`.
It also records alternative explanations, author and policy version, creation time, observed sample count, independent-window count, data quality, and known uncertainty.
Its knowledge lifecycle is `observation → hypothesis → testing → validated → retired`; validation does not itself activate knowledge.
A candidate can be retained with weak evidence, but must remain outside production decision retrieval until included in an approved policy snapshot.
New principles, playbooks, retrieval weights, calibrators, and retirement decisions that change behavior all require a candidate version.
Appending facts to working memory is permitted under the frozen retrieval contract; changing that contract is a policy change.
A cited experience appearing in a successful trade proves co-occurrence, not incremental value; compare frozen versions with and without it when attribution is required.
Prefer the smallest intelligible behavioral change; a dependent bundle may be evaluated together, but its benefit cannot be assigned precisely to one constituent rule.
Safety quarantine of corrupt inputs follows a separately authorized protective procedure and is recorded as an intervention, not validated strategy improvement.
The learner may propose and request evaluation; it has no permission to write active status, approve knowledge, alter measured results, or grant itself authority.

## 11. Evolve: forward shadow, promotion, and rollback

A candidate progresses through `draft → frozen → forward_shadow → insufficient / rejected / validated → approved → promoted`.
Post-promotion monitoring can lead to `rollback` or retirement; each transition records actor, time, evidence, and reason.
Freeze all behavioral dependencies, development-data cutoff, candidate hash, simulation/data protocol versions, and approval requirements before evaluation begins.
Any behavior-changing edit after freezing creates a new candidate and evaluation boundary, rather than silently continuing the old experiment.
Begin with one champion, one challenger, and an appropriate frozen simple non-LLM baseline, not an unbounded population of candidates.
Fork accounts from the same ledger and responsibility snapshot; preserve copied historical ownership and explicitly hand over future management to each branch's policy.
The branches then use separate cash, positions, orders, working memory, and knowledge namespaces under the same mandate and market-input protocol.
Shadow fills cannot reach the main account, and shadow-derived lessons cannot enter the champion's active knowledge.
Evaluation outcomes and derived reflections remain inaccessible to candidate authoring and adaptive rule retrieval while the experiment is active; frozen trading logic may observe its own operational state.
Once a holdout is released for research, it is no longer untouched validation data; subsequent candidates require new forward evidence.
The evaluator calculates measures from independent facts and a frozen protocol; it must not treat a model's confidence in itself as ground truth.
The promotion service checks eligibility and approval, then atomically changes one active-policy pointer with an auditable expected-version check.
The initial approval boundary is human authorization; automatic evaluation success is not permission to promote, and no LLM may approve its own candidate.
Rejected, insufficient, failed, or unavailable evaluations leave the active pointer and all behavioral dependency hashes unchanged.
Promotion and rollback specify handling of in-flight decisions, pending orders, stale snapshots, and positions before switching future behavior.
Existing positions remain with their originating policy by default; transferring management requires an explicit `handover` event while preserving original attribution.
Rollback restores an approved behavioral version for future actions; it does not delete fills, reset balances, erase losses, or rewrite prior outcomes.
Emergency risk suspension is independent of promotion and remains possible without claiming that a replacement strategy has been statistically validated.

## 12. Fair evaluation and evidence sufficiency

Integrity is the first gate: unreconciled ledgers, provenance gaps, information leakage, or broken isolation invalidate promotion evidence regardless of apparent return.
Freeze the hypothesis, primary metric, minimum useful effect, non-inferiority margin if applicable, risk limits, cost assumptions, evaluation horizon, and stopping rules.
Use comparable initial capital, mandates, market availability, execution rules, model/tool budgets, and resource-cost accounting; disclose any deliberate asymmetry.
Prediction comparison uses a common opportunity panel, target event, horizon, and declared treatment of abstentions and missing responses; report the full denominator and coverage.
Whole-trader comparison uses independently evolving books over aligned time windows; do not discard different selections by evaluating only an intersection of traded symbols.
Module-level experiments hold upstream dependencies fixed; whole-trader experiments may select different opportunities under the same opportunity and budget protocol.
For cross-sectional effects compare median with median; do not manufacture an edge by benchmarking a group median against a market mean.
Evaluate net equity performance, drawdown and tail risk, exposure, turnover, capacity assumptions, fill rate, and resource use; forecast scores are complementary, not substitutes.
Different mandates or capital scales require normalized protocols or separate cohorts, not an unqualified ranking by profit amount.
**The repository's `n ≥ 50` requirement is a minimum governance condition, not sufficient evidence of improvement or 50 independent observations.**
Define the sample unit and report observed n, independent windows, relevant environment coverage, and uncertainty beside every claimed effect.
Overlapping holding periods and same-day instruments are correlated; estimate uncertainty using appropriate time blocks or market clusters and remove overlapping-label leakage.
Control candidate count, repeated inspection, selection, and multiple comparisons through preregistered budgets and stopping rules; repeatedly trying until something passes is not evolution.
“No statistically detected deterioration” does not prove non-inferiority; improvement and non-inferiority require their own predeclared effect boundaries and uncertainty assessment.
Incomplete, censored, or low-power evidence produces `insufficient`, not an invented approval; continued learning does not require a policy release.
Historical replay is development evidence, and forward paper trading is necessary but still not proof of executable real-market returns.
No external research return, win rate, or numerical performance claim is adopted here; all numerical evidence thresholds in this design are governance requirements, not measured achievements.

## 13. Repository layering and integration boundaries

Arrows indicate layer ordering, not event flow: a module may depend on its own layer or an earlier layer, never a later one.
The following directories are existing repository paths; responsibilities marked as targets do not assert that their complete implementations exist.

| Existing layer | Target responsibility | Boundary |
|---|---|---|
| [alpha_agents/data/](../alpha_agents/data/) | Records, repositories, ledger persistence, deterministic accounting and base transitions | No network or Agent calls |
| [alpha_agents/sources/](../alpha_agents/sources/) | External-data normalization, publication/receipt provenance, ingestion | No trading decisions |
| [alpha_agents/tools/](../alpha_agents/tools/) | Market queries and reusable calculations | No persistent trader business state |
| [alpha_agents/evolution/](../alpha_agents/evolution/) | Outcome evaluation, candidate evidence, experiment and version-governance rules | No reverse dependency on pipeline or model self-grading |
| [alpha_agents/pipeline/](../alpha_agents/pipeline/) | Proposed runtime, event scheduling, execution workflows, and learning orchestration | No duplicate strategy rules or reverse Agent imports |
| [alpha_agents/agents/](../alpha_agents/agents/) | Structured decision-policy and learner model adapters | No direct storage writes or approval authority |
| [alpha_agents/server/](../alpha_agents/server/) | APIs, projections, management authorization, and high-level composition | No independent accounting or strategy implementation |

Lower layers accept structured policy/learner interfaces; a higher-level composition root injects model implementations without reverse imports.
Existing integration anchors include [portfolio](../alpha_agents/data/portfolio.py), [portfolio risk](../alpha_agents/data/portfolio_risk.py), and [forecast scoring](../alpha_agents/data/scoring.py).
Task consolidation starts from [book management](../alpha_agents/pipeline/tasks/book_manager.py), [morning scan](../alpha_agents/pipeline/tasks/morning_scan.py), and [intraday monitoring](../alpha_agents/pipeline/tasks/intraday_monitor.py).
Learning integration anchors are [lessons](../alpha_agents/evolution/lessons.py), [playbooks](../alpha_agents/evolution/playbook.py), [principle scoring](../alpha_agents/evolution/principle_scoring.py), and [holdout gating](../alpha_agents/evolution/holdout_gate.py).
TraderRuntime, PaperBroker, outcome repositories, version registry, shadow coordinator, and promotion service are **proposed components**, without invented file paths or new import-layer exemptions.
A future lowest-level contracts package requires an explicit architectural decision and dependency checks before introduction; this document creates no such package.

## 14. Delivery phases and historical migration

| Phase | Target boundary | Completion meaning |
|---|---|---|
| 1 — trustworthy minimum slice; begun, not declared complete | Cash and cumulative exit accounting, auditable per-trade records, explicit trader/source binding, separate trade outcomes, candidate-only learning boundary | A narrow trustworthy foundation, not a full ledger/broker/runtime/experiment system |
| 2 — complete trading kernel | Full fill/cash ledger, reservations, settlement lots, unified intents, runtime clocks, reconciliation, and replay | All trading paths share enforceable execution and accounting contracts |
| 3 — episode learning | Episode association, three outcome lifecycles, candidate evidence, and approved knowledge snapshots | Learning is attributable without changing active behavior implicitly |
| 4 — controlled evolution | Frozen policy registry, forward shadow accounts, fair evaluation, authorized promotion, and rollback | Evidence controls future policy changes through one audited entry point |
| 5 — product consolidation | Trader, episode, and experiment read models across APIs and the existing [frontend](../web/src/) | Trade workspace, Learn journal, and Evolve laboratory expose the same facts |

Phase 1 follows the active plan; full reservations, securities settlement, global event history, and forward version experiments are expressly later work.
Implementation completion and actual verification results belong in execution records, not assertions inferred from this target document.
Migrate incrementally around existing responsibilities; establish and reconcile new projections before changing readers, without permanent competing sources of truth.
Reconstruct history only when actual source records support it; never invent missing fills, fees, provenance IDs, approvals, or policy versions.
Unrecoverable history stays `legacy`; start a new accounting period from explicitly sourced opening balances and inventory with uncertainty disclosed.
An opening snapshot is a migration boundary, not fabricated historical execution, and cannot splice a new verified equity curve into unverified past performance.
Any later production-data migration requires its own authorization, backup, reconciliation, and recovery plan; none is performed by this documentation task.

## 15. Target acceptance contracts

These are future acceptance requirements, not executed tests or measured pass rates.
- Accounting reconciles to zero under declared precision; partial and final exits preserve cumulative P&L, and replaying a fill has one economic effect.
- Every new required provenance link resolves with matching trader, run, mode, and policy ownership; same-symbol concurrent traders do not cross-attribute results.
- Every actionable input satisfies the availability cutoff; corrected data and replay clocks cannot change previously recorded input snapshots.
- Entry, adjustment, protective exit, cancellation, and expiry all obey one execution contract; no business path directly fabricates a position or return.
- Forecast, trade, and process outcomes coexist without overwriting each other, including missing, censored, and revised evidence.
- Candidate rejection, insufficient n, evaluator failure, and unapproved knowledge changes leave active behavioral hashes unchanged.
- Shadow orders, capital, outcomes, and derived learning cannot mutate the main account or contaminate candidate development.
- Promotion and rollback preserve historical fills and ledger entries, retain complete approval audit, and resolve outstanding version ownership.
- Disabling reports or an LLM does not stop deterministic risk protection, order processing, accounting, reconciliation, or label maturation.
- Phase-specific implementation must later satisfy the repository's regression, architecture, and documentation checks without expanding lint exemptions; none were run for this document.

## 16. Non-goals

The initial scope is the existing A-share simulation domain, not real-money brokerage connectivity or claims of profitable deployment.
Online reinforcement learning, autonomous model-weight updates, and self-modifying production code are outside initial delivery; LLM self-approval remains prohibited throughout.
Unbounded memory growth, automatic capital escalation, persona/emotion scoring, role-count expansion, and short-window performance tournaments are not capability objectives.
Distributed actors, message brokers, microservice decomposition, and a wholesale rewrite are unnecessary until demonstrated operational needs justify them.
Market-rule verification, external return research, live network access, database inspection or migration, dependency installation, test execution, and commits are not part of this documentation-only change.
The governing outcome is a trader accountable to facts, learning expressed as hypotheses, and evolution constrained by evidence and authorization.
