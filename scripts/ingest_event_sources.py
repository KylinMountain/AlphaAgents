#!/usr/bin/env python3
"""Ingest event facts after a provider has passed capability probing.

Examples:
  uv run python scripts/ingest_event_sources.py tushare \
      --dataset forecast --ts-code 600000.SH

  uv run python scripts/ingest_event_sources.py tushare \
      --dataset disclosure_date --ts-code 600000.SH

Company forecast rows are management guidance, not analyst consensus.
Date-only provider timestamps are conservatively visible at end-of-day.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from alpha_agents.data import event_ingest  # noqa: E402
from alpha_agents.data import tushare_event_adapter as tushare_adapter  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="provider", required=True)

    ts = sub.add_parser("tushare")
    ts.add_argument(
        "--dataset", required=True,
        choices=["forecast", "disclosure_date"])
    ts.add_argument("--ts-code", required=True)

    args = parser.parse_args(argv)
    if args.provider != "tushare":
        raise SystemExit("unsupported provider")

    frame = tushare_adapter.fetch(
        dataset=args.dataset, ts_code=args.ts_code)
    if args.dataset == "forecast":
        result = event_ingest.ingest_tushare_forecast(frame)
    else:
        result = event_ingest.ingest_tushare_disclosure(frame)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
