#!/usr/bin/env python3
"""Screen frozen policy variants in a bounded historical DreamWorld.

Example:
  uv run python scripts/dream.py --parent 1 --variant 2 --variant 3 \
      --start 2026-06-01 --end 2026-08-31 --report-type morning

Dream evidence is historical development evidence only. A survivor may enter
forward shadow; this command has no install, approve or promote action.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from alpha_agents.evolution.dream_agent import DreamAgent  # noqa: E402
from alpha_agents.evolution.dream_world import build_prediction_world  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent", type=int, required=True)
    parser.add_argument("--variant", type=int, action="append", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--report-type", default="morning")
    parser.add_argument("--trader-id", default=None)
    parser.add_argument("--minimum-samples", type=int, default=20)
    parser.add_argument("--tolerance", type=float, default=0.0)
    args = parser.parse_args(argv)

    world = build_prediction_world(
        start=args.start, end=args.end, report_type=args.report_type,
        trader_id=args.trader_id)
    result = DreamAgent().screen(
        world=world,
        parent_version_id=args.parent,
        variant_version_ids=args.variant,
        minimum_samples=args.minimum_samples,
        tolerance=args.tolerance)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
