#!/usr/bin/env python3
"""Operate direction-level Dream evidence.

Label matured direction opportunities:
  uv run python scripts/dream_direction.py sweep \
      --membership /path/to/pit_membership.json

Report direction selection skill:
  uv run python scripts/dream_direction.py report \
      --start 2026-01-01 --end 2026-06-30

Compare the Agent to the transparent sector rank:
  uv run python scripts/dream_direction.py baseline \
      --start 2026-01-01 --end 2026-06-30 --top-k 3
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from alpha_agents.data import sector_membership  # noqa: E402
from alpha_agents.data import theme_opportunity_outcomes as O  # noqa: E402
from alpha_agents.evolution import dream_direction as D  # noqa: E402


def _world(args):
    return D.build_world(
        start=args.start,
        end=args.end,
        trader_id=args.trader,
        run_id=args.run_id,
        architecture=args.architecture,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sweep = sub.add_parser("sweep")
    sweep.add_argument("--membership", type=Path, required=True)
    sweep.add_argument("--limit", type=int, default=5000)

    report = sub.add_parser("report")
    report.add_argument("--start", required=True)
    report.add_argument("--end", required=True)
    report.add_argument("--trader", default=None)
    report.add_argument("--run-id", default=None)
    report.add_argument("--horizon", type=int, default=5)
    report.add_argument(
        "--architecture", default="sector_first_v0",
        choices=(
            "sector_first_v0", "sector_first_simple_selector",
            "sector_first_no_flow"))
    report.add_argument("--min-coverage", type=float, default=0.5)

    baseline = sub.add_parser("baseline")
    baseline.add_argument("--start", required=True)
    baseline.add_argument("--end", required=True)
    baseline.add_argument("--trader", default=None)
    baseline.add_argument("--run-id", default=None)
    baseline.add_argument("--horizon", type=int, default=5)
    baseline.add_argument(
        "--architecture", default="sector_first_v0",
        choices=(
            "sector_first_v0", "sector_first_simple_selector",
            "sector_first_no_flow"))
    baseline.add_argument("--top-k", type=int, default=3)
    baseline.add_argument("--min-coverage", type=float, default=0.5)

    args = parser.parse_args(argv)
    if args.cmd == "sweep":
        archive = sector_membership.load(args.membership)
        result = O.sweep(
            membership_archive=archive,
            limit=args.limit,
        )
    elif args.cmd == "report":
        result = D.selection_skill(
            _world(args),
            horizon=args.horizon,
            min_coverage=args.min_coverage,
        )
    else:
        result = D.transparent_rank_baseline(
            _world(args),
            horizon=args.horizon,
            top_k=args.top_k,
            min_coverage=args.min_coverage,
        )

    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
