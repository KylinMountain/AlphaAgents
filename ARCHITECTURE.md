# Architecture

A top-level map. Where to look, and what may depend on what.

## Layers

Dependencies run one way. `scripts/lint_harness.py` fails the build on a
backwards import.

```
data → sources → tools → evolution → pipeline → agents → server
```

| Layer | Owns | Does not |
|---|---|---|
| `data/` | SQLite schemas and access, scoring, decision context, the trader book (orders, exits, theses, decision snapshots), attribution | Call the network |
| `sources/` | 13 news feeds (`NEWS_SOURCES` in `pipeline/monitor.py`), each normalising to one shape | Decide anything |
| `tools/` | Market queries the agents can call — quotes, fund flow, breadth, exit signals | Hold state |
| `evolution/` | Memory utility, principles, playbooks, the holdout gate | Talk to an LLM to decide |
| `pipeline/` | The scheduler and its tasks; the news ingest loop | Contain strategy rules |
| `agents/` | LLM prompts and the runners around them | Write to storage directly |
| `server/` | FastAPI, WebSocket, the dashboard build | Contain logic worth testing |

Cross-cutting, importable anywhere: `config`, `http_client`, `notify`,
and `evolution.replay_mode` — a contextvar holding a global "as of"
instant so historical replay stays honest.

## Databases

Four, separate on purpose. A rebuildable cache should not share a file
with the one store that cannot be rebuilt.

| File | Holds | Rebuildable |
|---|---|---|
| `data/memory.db` | Themes, predictions, principles, playbooks, and the trader book — `virtual_portfolio`, `position_exits`, `theses`, `decision_snapshots`, `learning_candidates` | **No** — back it up |
| `data/market_snapshots.db` | News items, market snapshots | Yes, by re-ingesting |
| `data/market_history.db` | Full-market daily K-lines, ~1.1 GB | Yes, slowly (`init-history`) |
| `data/activity.db` | The live activity feed | Yes, it is a feed |
| `data/chroma/concepts.db` | Concept embedding vectors | Yes (`build-embeddings`) |

## The day

```
00:00–23:59  news_ingest      every 5 min, all week — keeps the store fed
09:00        morning_scan     reads the window back to yesterday's close
09:25        opening_reminder
09:30–15:00  intraday_monitor every 5 min; anomaly-gated
15:30        review           verify → score → learn → gate
20:00        night_scan
Sat 10:00    weekly_report
```

Ingestion is continuous because the feeds are; consumption is windowed
because the tasks are. They meet at `news_items`, keyed on a normalised
`published_at`.

## How a recommendation is graded

```
morning_scan / intraday
  └─ save_prediction(prob=…, features=… + decision context)
        │  prob = P(beats the market over 5 trading days)
        ▼
review (T+5)
  └─ scoring.score_prediction
        ├─ excess    = return − median stock
        ├─ residual  = excess with style exposure regressed out
        └─ brier     = (prob − outcome)²
        ▼
  principle_scoring   principles inherit the scores of what they cited
  holdout_gate        exists and is tested, but is NOT called: the daily gate
                      ran with a zero-length validation window and abstained
                      every time, so Phase 1 disconnected it rather than leave
                      a gate that could never fire. Reconnecting it needs a
                      candidate-bound forward protocol (Phase 4).
```

Nothing in that chain asks a model whether it did well. See
`docs/GOLDEN_PRINCIPLES.md`.

Promotion is the one link with no writer today. Phase 4 of
`docs/TRADER_CORE_DESIGN.md` §14 is what supplies it.

## The trader book

A forecast that becomes a trade: one execution chain, one information
boundary, three outcomes that never overwrite each other. The target state
is `docs/TRADER_CORE_DESIGN.md`; what actually exists is
`docs/TRADER_CORE_IMPLEMENTATION.md`.

**Ownership is stored, never re-derived.**

```
theses.id ──▸ virtual_portfolio.thesis_id ──▸ position_exits.thesis_id
```

`theses.position_id` also points back, but the two forward columns are the
ones that survive a position being re-opened. "The nearest live thesis with
this code" is not an ownership relation — with two traders on one stock it
books A's result against B's idea, which is a bug this repo has already
paid for. There is no separate fills table: the entry fill *is* the
position row, and the ledger entries *are* the exit legs, so the resolvable
chain is `thesis → order → exits`.

**The decision boundary is frozen when the decision is made.**
`decision_snapshots` records the declared inputs, `information_cutoff` (the
latest instant the decision was allowed to use — not the time the job ran)
and the producer refs. `BEFORE UPDATE` / `BEFORE DELETE` triggers
`RAISE(ABORT)`, so append-only is enforced by the database rather than by
every caller remembering; a revision is a new row naming the one it
supersedes. `attribution.verify_snapshot` recomputes the covered hash, so a
rewrite that got past a dropped trigger is still detectable.

**Three outcomes, three stores** (design §9):

| Outcome | Reads | Answers |
|---|---|---|
| `attribution.forecast_outcome` | `predictions` | did it beat the market over the declared horizon |
| `attribution.trade_outcome` | `position_exits` | what the position actually realised |
| `evolution.process_quality` | episode records | how the decision was made |

A correct forecast and a losing trade coexist, and so do a wrong forecast
and a win. Closing a position never writes the prediction's label. A thesis
that never filled reports `fills=0` with `return_pct=None` rather than a
zero return: "did not trade" and "traded flat" are different facts, and
only one of them is evidence about skill.

**Unvalidated experience is quarantined.** Candidates land in
`learning_candidates` and there is no promotion API to call. Decision
prompts carry rule fields only — never `hit_rate`, `wins` or
`total_trades` — and raw lessons are research-only, so an unapproved lesson
cannot reach a morning or chat decision context. Same principle as the
grading chain above: nothing asks a model to rate itself.

## Frontend

`web/` is React + Vite, built into `web/dist` by the Docker image's first
stage and served by `server/app.py`. Styled to match TradingAgents-AShare
so it can be embedded there.

## Deployment

Images are built by GitHub Actions and published to
`ghcr.io/kylinmountain/alphaagents`. Nothing is built on the box. See
`deploy/README.md`.
