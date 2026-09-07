# Technical debt

Debt is a high-interest loan: paid down continuously in small increments,
never in painful bursts. Each item says what it costs and how it is
recognised, so an agent can pick one up without asking.

Grandfathered violations live in `scripts/lint_baseline.txt`. A line is
removed from that file only by fixing the code — a baseline that can grow
is not a baseline.

## Open

### D1 — `data/portfolio.py` is service code in the storage layer
**Cost.** 3 layering violations. It imports `tools.exit_signals` and
`tools.fund_flow` because it holds the position lifecycle — stop-loss,
holding caps, bearish signals. That is business logic, not storage.
**Fix.** Move the lifecycle to a service module that both `data` and
`pipeline` may call; leave the SQLite access behind in `data`.
**Recognise.** `lint_harness.py` stops reporting `portfolio.py::layering`.

### D2 — `data/daily_archive.py` orchestrates fetches
**Cost.** 4 layering violations. It pulls from four `tools/` modules to
assemble a daily snapshot, which makes it a pipeline task wearing a data
module's name.
**Fix.** Move to `pipeline/tasks/`.
**Recognise.** `daily_archive.py::layering` gone from the baseline.

### D3 — 29 silent `except ... : pass`
**Cost.** The PBOC parser returned rows whose every headline was the
literal string `"true"` for months, because nothing on that path was
allowed to complain. Every silent handler is a place that can happen
again.
**Fix.** One module at a time: add `logger.debug("... %s", e)`, or a
comment above the `pass` explaining why the failure is genuinely
ignorable.
**Recognise.** `silent-except` count in the baseline falls.

### D4 — 5 files over 1200 lines
`tools/vpa/data.py` (1408), `tools/vpa/llm.py` (1264), and three others.
**Cost.** A file an agent cannot hold in context gets edited blindly, and
blind edits are where duplication starts.
**Fix.** Split by responsibility, not by line count.
**Recognise.** `file-size` gone from the baseline.

### D5 — Playbook clustering lost a dimension
**Cost.** Removing `vpa_verdict` from recorded features collapsed
clustering from three dimensions to two (`theme` × `institutional`), and
every historical playbook carrying a `vpa_verdict` condition can no
longer match.
**Fix.** Add replacement buckets from features already recorded — score
band, change_pct band, anomaly category.
**Recognise.** `_query_hit_clusters` groups on three or more dimensions.

### D6 — Entry side is unevaluated
**Cost.** The largest gap in `docs/strategy_evaluation_2026-09.md`. G6
records decision context going forward, but there is no history to
replay, so no one can say whether news-driven picks beat the market.
**Fix.** Accumulate. Needs weeks of live running, not a code change.
**Recognise.** `replay_day` returns `replayable: true` for a month of
trading days.

## Paid

- News feeds consumed as snapshots rather than streams — a 06:30 scan saw
  one hour of the fifteen since the previous close. Fixed by
  `pipeline/tasks/news_ingest.py` plus windowed reads.
- `published_at` stored verbatim in mixed formats, silently breaking
  window queries. Normalised at write time; 470 rows rewritten.
- Predictions re-saved every intraday cycle, inflating playbook trade
  counts by an order of magnitude. `save_prediction` is idempotent per
  `(date, code, report_type)`.
