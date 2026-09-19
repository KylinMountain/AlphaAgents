#!/usr/bin/env python3
"""Operate forward selection shadow experiments.

Open:
  uv run python scripts/selection_shadow.py open --parent 1 --variant 4

Process newly matured Opportunity Journal sets:
  uv run python scripts/selection_shadow.py process --run 1

Status:
  uv run python scripts/selection_shadow.py status --run 1
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from alpha_agents.config import DATA_DIR  # noqa: E402
from alpha_agents.evolution import selection_shadow as S  # noqa: E402


def _history():
    conn = sqlite3.connect(
        f"file:{DATA_DIR / 'market_history.db'}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    op = sub.add_parser("open")
    op.add_argument("--parent", type=int, required=True)
    op.add_argument("--variant", type=int, required=True)
    op.add_argument("--opened-on", default=None)
    op.add_argument("--horizon", type=int, default=5)
    op.add_argument("--minimum-sets", type=int, default=20)

    pr = sub.add_parser("process")
    pr.add_argument("--run", type=int, required=True)

    st = sub.add_parser("status")
    st.add_argument("--run", type=int, required=True)

    args = parser.parse_args(argv)
    if args.cmd == "open":
        run_id = S.open_run(
            parent_version_id=args.parent,
            variant_version_id=args.variant,
            opened_on=args.opened_on,
            horizon=args.horizon,
            minimum_sets=args.minimum_sets)
        result = S.summary(run_id)
    elif args.cmd == "process":
        hist = _history()
        try:
            S.process(run_id=args.run, history_conn=hist)
        finally:
            hist.close()
        result = S.summary(args.run)
    else:
        result = S.summary(args.run)

    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
