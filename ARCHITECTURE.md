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
| `data/` | SQLite schemas and access, scoring, decision context | Call the network |
| `sources/` | 13 news feeds, each normalising to one shape | Decide anything |
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
| `data/memory.db` | Themes, predictions, principles, playbooks, virtual portfolio | **No** — back it up |
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
  holdout_gate        candidates promoted only if forward validation holds
```

Nothing in that chain asks a model whether it did well. See
`docs/GOLDEN_PRINCIPLES.md`.

## Frontend

`web/` is React + Vite, built into `web/dist` by the Docker image's first
stage and served by `server/app.py`. Styled to match TradingAgents-AShare
so it can be embedded there.

## Deployment

Images are built by GitHub Actions and published to
`ghcr.io/kylinmountain/alphaagents`. Nothing is built on the box. See
`deploy/README.md`.
