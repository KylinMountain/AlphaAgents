# Dream RSI — Opportunity World & Selection Evaluation

Status: **Opportunity RSI core implemented; event-aware Dream integration pending**

This document extends the first bounded Dream Agent. The current implementation
can replay one narrow class of intervention — confidence-label → probability
mapping — on historical scored predictions. That is a real Dream loop, but not
the whole RSI system.

The next world is built from **opportunities**, not only trades.

## 1. Current boundary

Implemented today:

```text
scored champion predictions
        ↓
Prediction DreamWorld
        ↓
frozen policy variants
        ↓
confidence_brier_v1
        ↓
survivor / retire / insufficient
        ↓
forward shadow only
```

Hard properties already in place:

- historical Dream evidence is never promotion-eligible;
- variants and worlds are hashed/frozen;
- changed policy genes must be observable by the evaluator;
- a survivor may pay the cost of forward shadow, but Dream never moves the
  active policy pointer.

Implemented after the original design:

- the full Opportunity Journal surface: selected / researched-not-selected /
  offered-not-researched / execution refusal / unreadable decision;
- immutable 1/3/5-session forward market labels for every offered name;
- OpportunityDreamWorld and selection-skill diagnostics;
- deterministic ranking baselines;
- PIT theme-gate counterfactuals on real selected opportunities;
- a real live gene, `selection_rank.change_share`, executed by the T1 panel builder;
- Dream counterfactuals for that same selection gene;
- strictly-forward selection shadow with a preregistered sample floor;
- a selection gate persisted into the same human approval / promotion boundary.

Still pending in this track:

- provider-backed Event Expectations ingestion;
- event snapshots included in the DreamWorld hash;
- event/surprise policy genes, only after their live and evaluator paths exist;
- richer selection genes beyond the first change-vs-turnover lane mix.

## 2. Why Opportunity World is necessary

Closed trades answer:

> Did the selected trade make money?

They cannot answer:

> Was it the best available choice?

A day may look like:

```text
A — researched → selected
B — researched → not selected
C — offered → not researched
D — offered → not researched
```

If A returns +2% but B returns +14%, the trade is profitable while selection
has high opportunity cost. Conversely, A losing -1% can still be a strong
selection if the rest of the panel lost -8%.

Therefore RSI needs:

```text
Opportunity Journal
      ↓
historical outcome labels for every offered code
      ↓
Opportunity DreamWorld
      ↓
selection diagnostics / deterministic ranking baselines
      ↓
later: frozen ranking/theme policy variants
```

## 3. Opportunity outcome contract

Outcome is a **market label**, not simulated P&L.

For every opportunity item, label only after the maximum requested horizon has
matured. v1 uses 1 / 3 / 5 future trading sessions.

Entry mark:

- open decision: actual entry-session **open**;
- close decision: actual entry-session **close**.

Forward label for horizon H:

```text
return_H = close(entry session + H trading sessions) / entry_mark - 1
```

Also record:

- MFE over the next 5 sessions;
- MAE over the next 5 sessions;
- exact target dates;
- source row hash / label version.

These labels must not be confused with what the account could actually fill.
They answer selection quality on a common price convention; execution quality
remains in the broker/ledger evidence.

A label is written once, append-only, only when the full horizon exists.
Partially matured rows stay unlabeled rather than being updated every day.

## 4. Opportunity DreamWorld

The world groups immutable opportunity sets and matured outcomes:

```text
OpportunitySet
├── information_cutoff
├── phase
├── policy_ref
├── context
│   ├── run_theme
│   ├── ranking_day
│   └── market facts available at decision time
└── items
    ├── panel facts
    ├── research status
    ├── selected / not selected
    └── forward outcome labels
```

World hash includes both the decision-time facts and the matured outcome labels.
Changing a label source or feature definition creates a different world.

## 5. Three evaluators, in order

### A. Selection skill — implement first

No counterfactual policy claim. Diagnose the behaviour that actually happened.

Metrics per horizon:

- selected mean / median forward return;
- researched-not-selected mean;
- offered-not-researched mean;
- selected lift versus panel mean/median;
- per-set regret = best offered return - best selected return;
- selected-above-panel-median rate;
- coverage: sets with at least one selected name, fully matured sets/items.

This is observational evidence. It can discover "the Agent researches useful
names but chooses the wrong one" without pretending to know why.

### B. Ranking baselines — implement second

Replay deterministic, non-LLM scorers over exactly the same panel:

- previous-session change;
- turnover;
- fund flow;
- simple registered weighted formulas.

The purpose is not to promote a hand-written factor. It gives the LLM selector
a fixed baseline and tells us whether complexity bought anything.

A ranking scorer must declare the panel fields it observes. Missing fields cause
refusal/coverage loss; zero is never substituted for unknown data.

### C. Theme-gate policy — only when context is replayable

Theme-gate evaluation needs the decision-time theme and a point-in-time theme
score. It must not infer theme state from later outcomes.

For each selected intent:

```text
same opportunity set
+ same theme score snapshot
+ parent gate parameters
+ variant gate parameters
        ↓
admit/refuse flip
        ↓
join to the selected opportunity's matured outcome
```

Only then can Dream answer:

> Would this gate have filtered winners or losers?

If theme_score_history or run theme is unavailable, the evaluator refuses and
the manifest reports missing capability.

## 6. Ranking/selection policy variants

Do **not** add new mutable policy genes just to make Dream look complete.

A gene becomes evolvable only when:

1. the live/replay trading path actually reads it;
2. the Dream evaluator reads the same semantics;
3. one-gene intervention can change behaviour on at least one fixed world;
4. the forward-shadow producer can observe it.

So first build diagnostics + deterministic baselines. Only after a ranking
configuration is wired into the real Trader should it enter PolicyVersion.

This keeps the invariant:

> Gene ↔ live intervention ↔ Dream evaluator ↔ forward evaluator

## 7. Event/expectation data is a capability, not an assumption

The Event Expectations layer should not say "historical consensus does not
exist" before source discovery.

Provider discovery has to distinguish:

- event history;
- announcement timestamp;
- historical consensus values;
- **historical consensus vintages/revisions**;
- local permission/points;
- symbol/period coverage.

Candidate probes include Tushare financial/forecast datasets, Tushare sell-side
earnings forecasts, AKShare equivalents, and existing local archives.

Official Tushare documentation currently advertises historical forecast data
and sell-side earnings forecast data extending back many years. That is a lead,
not proof that every required vintage field is available to this account.
Local probe decides.

## 8. Source Probe contract

A probe must be read-only by default and emit:

```text
provider
dataset
sdk_available
credential_available
permission
history_start
time_field
revision/vintage_field
sample_fields
point_in_time_grade
notes
```

Grades:

- **A** — historical vintages/revisions with knowable capture/publication time;
- **B** — historical observations with announcement time, but revision semantics
  incomplete;
- **C** — current/latest snapshot only;
- **U** — unknown until local probe succeeds.

Dream/Replay manifests consume the grade; they never silently upgrade C/U to A.

## 9. Delivery order

```text
M1  Opportunity Journal                                  DONE
M2  Opportunity Outcomes                                DONE
M3  Opportunity DreamWorld + selection skill            DONE
M4  deterministic ranking baselines                     DONE
M5  theme context + theme-gate Dream evaluator          DONE
M6a event source probe                                   DONE
M6b provider adapters / normalized ingest               IN PROGRESS
M7  Event Expectations in Opportunity/DreamWorld hash   DONE
M8a first live selection gene                           DONE
M8b forward selection shadow + sealed gate              DONE
M9  event/surprise policy genes                         BLOCKED ON M6b/M7
```

## 10. Acceptance criteria for this iteration

- all opportunity items can receive immutable 1/3/5-session market outcomes;
- selected / researched-not-selected / not-researched remain distinct;
- Dream can report selection lift and regret on the same opportunity sets;
- no outcome label can directly promote a policy;
- incomplete horizons are reported as coverage, not treated as zero;
- event design no longer assumes source history is absent;
- local source probe can tell us what Tushare / AKShare / local archives
  actually provide before we write an ingest adapter.


## 11. Current governed selection loop

The first selection gene now has one semantic path end to end:

```text
Evidence about T-1 ranking
        ↓
Candidate
        ↓
selection_rank.change_share
        ↓
Live T1 panel builder
        ↓
Opportunity Journal
        ↓
Opportunity Outcomes
        ↓
Dream panel-policy counterfactual
        ↓
Forward Selection Shadow
        ↓
sealed preregistered sample
        ↓
Selection Gate
        ↓
Human Approve
        ↓
PolicyRegistry Promote
```

Important: Dream evidence is still non-promotable. The gate only becomes
candidate-policy evidence after the strictly-forward selection shadow seals.
The human approval boundary remains unchanged.
