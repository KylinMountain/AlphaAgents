#!/usr/bin/env python3
"""Operate stock-layer Dream evidence.

Label matured stock opportunities:
  uv run python scripts/dream_stock.py sweep

Write one stock-layer report:
  uv run python scripts/dream_stock.py report \
      --start 2026-01-01 --end 2026-03-31 --run-id B-run \
      --out-file /tmp/B/stock_report.json

The report keeps stock preselection separate from final orders. Historical
Dream evidence is never promotion-eligible.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from alpha_agents.data import opportunity_outcomes  # noqa: E402
from alpha_agents.evolution import dream_selection  # noqa: E402
from alpha_agents.evolution.dream_world import build_opportunity_world  # noqa: E402


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sweep = sub.add_parser("sweep")
    sweep.add_argument("--limit", type=int, default=5000)

    report = sub.add_parser("report")
    report.add_argument("--start", required=True)
    report.add_argument("--end", required=True)
    report.add_argument("--trader", default=None)
    report.add_argument("--run-id", default=None)
    report.add_argument("--horizon", type=int, default=5)
    report.add_argument("--out-file", type=Path, default=None)

    args = parser.parse_args(argv)
    if args.cmd == "sweep":
        result = opportunity_outcomes.sweep(limit=args.limit)
    else:
        world = build_opportunity_world(
            start=args.start,
            end=args.end,
            trader_id=args.trader,
            run_id=args.run_id,
        )
        result = dream_selection.stock_layer_report(
            world, horizon=args.horizon)
        if args.out_file is not None:
            _write(args.out_file, result)

    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
