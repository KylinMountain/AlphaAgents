#!/usr/bin/env python3
"""Probe local/Tushare/AKShare event data capabilities.

Default is read-only and offline:
  uv run python scripts/probe_event_sources.py

Explicit Tushare network sample:
  uv run python scripts/probe_event_sources.py --live-tushare \
      --ts-code 600000.SH
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from alpha_agents.data.event_source_probe import probe_all  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--live-tushare", action="store_true")
    parser.add_argument("--ts-code", default="600000.SH")
    args = parser.parse_args(argv)

    kwargs = {
        "live_tushare": args.live_tushare,
        "ts_code": args.ts_code,
    }
    if args.data_dir is not None:
        kwargs["data_dir"] = args.data_dir
    print(json.dumps(probe_all(**kwargs), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
