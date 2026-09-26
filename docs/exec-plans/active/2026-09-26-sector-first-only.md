# Sector-First is the only selection path

状态：active（2026-09-26）

## Goal

The owner (2026-09-26): "去掉这个非 sector first 的路径，以 sector first 路径为主，
不加这个参数也是这个思路。" `e7efe1d` (09-21) already removed the change/turnover
gene from every trading path but left it inlined in `walk_forward` as the frozen
A-arm control, and left `--selection-architecture` defaulting to it. Every
Trader Runtime replay since 09-25 (t8-30d … horizon30, tle30) therefore ran the
control arm because nobody passed the flag. This removes the control so an
unflagged run is the target strategy.

## What changes

- `walk_forward`: architectures are `sector_first_v0` (default),
  `sector_first_simple_selector`, `sector_first_no_flow`, `sector_rank_price_v1`.
  `dual_rank_v0` / `dual_rank_price_v1`, `_build_panel`, `_legacy_lane_mix`,
  `_legacy_pool_rows` are deleted.
- `--decider` defaults to `llm`. The `placeholder` decider stays as an explicit
  ledger-kernel test harness (no model, not a strategy) and is exempt from the
  architecture requirement.
- Membership: without `--sector-membership`, the run uses today's
  `concept_stocks` (the stated lookahead `--allow-current-membership` used to
  opt into). The limitation line still prints. Preregistered experiments still
  refuse a non-PIT archive.
- `--close-buys` is deleted: the sector path is 09:00-only, so after the
  control is gone a close buy is unreachable (declared-but-unreachable).
- ABCD experiment → BCD (arm A removed, A/B comparison removed).
- nf_discovery_v1 (CONTROL=`dual_rank_price_v1` vs SECTOR) is deleted: a
  two-arm comparison with no control compares nothing. `sector_rank_price_v1`
  survives as an architecture.

## Acceptance criteria

1. `grep -rn "dual_rank" alpha_agents scripts tests` → 0 hits.
2. `walk_forward --start … --days N --trader default` with no other flag runs
   `sector_first_v0` with an LLM decider; `run-args`/report meta say so (test).
3. `uv run pytest tests/ -q`, `lint_harness.py`, `lint_docs.py` pass.
4. 2-day smoke on main with `--selection-architecture sector_first_v0
   --allow-current-membership` finishes with 0 errored day-phases and
   `机械 0 笔` (run before the refactor, isolated worktree).

## Evidence note (not a reason to keep the control)

`wf30-clean` (sector_first_v0, post-`ce83ce6`, pre-Runtime, 30d from
2026-01-05): −0.208%, excess −7.03pp, 6 buys. Switching architecture is not by
itself expected to fix returns; tle30/horizon30 (dual_rank, same window) remain
the recorded historical baseline.

## Decision log

- 2026-09-26: keep `placeholder` decider — it is the fixture that runs the
  whole ledger kernel without a model in `tests/test_walk_forward.py`; it never
  claims to be a strategy and its report says so.
