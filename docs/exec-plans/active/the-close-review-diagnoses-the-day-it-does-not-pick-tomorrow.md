# The close review diagnoses the day; it does not pick tomorrow

## What the 2026-01 replays said (2026-09-24)

Two 30-day runs (2026-01-05 on, equal-weight market +6.82%) fed each close
review's boards into the next morning — `close` through the prompt only,
`direction` also by adding them to the direction shortlist (up to 5).

| selected direction | close run | direction run |
|---|---|---|
| named by the previous close review | n=38, pct 31.2, fwd 5d median −0.7% | n=51, pct 34.6, fwd −1.4% |
| formula top 8 only | n=33, pct 43.8, fwd +0.1% | n=22, pct 43.0, fwd +0.2% |

Direction score overall 43.8 → 34.6. The morning theses said so in words:
"复盘点名……值得继续研究". The review names yesterday's biggest movers, so
feeding it forward as candidates is chasing yesterday. n=22 is below 50 —
exploratory, but it points the same way in both runs.

The user's framing: a review is for **why the morning did what it did** —
how we missed what rose, how we walked into what fell, whether the direction
turned under a position we kept or added to. Tomorrow's direction comes from
tomorrow's data.

## Change

1. The replay no longer adds review-named boards to the direction shortlist;
   the direction prompt no longer mentions them.
2. The review no longer writes a watchlist (it was tomorrow's picks by
   another name); its grading goes with it.
3. The close review is handed the day's own record: directions selected at
   the open with their thesis, those it saw and passed on, orders and fills,
   positions held with today's move, positions closed today.
4. Each board is diagnosed as 错过 / 踩坑 / 转向 / 做对 against that record,
   ending in a lesson for the handbook.
5. The morning reads it under a header that says it is yesterday's
   diagnosis, not today's list.

## Acceptance

1. `_build_direction_shortlist` output never carries `source="复盘点名"`
   (test).
2. `market_review._parse` drops `watchlist`; `inject` output contains
   "不是今天的买入名单" and no "观察" line (test).
3. The close review's message contains the day's record: a selected
   direction's thesis and an order's fill state (test).
4. pytest, lint_harness, lint_docs pass.
5. Measured after the next 30-day run: direction percentile back to ≥ 43.8
   (the formula-only level), exposure not below the `close` run's 8.2%.

## Decision log

- 2026-09-24: removed rather than graded. Grading the named boards would
  still put them in front of the direction stage as candidates; the user's
  point is that they are not candidates at all.
