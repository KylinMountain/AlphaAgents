#!/usr/bin/env python3
"""Cross-table reconciliation: read the book, write only the audit log.

Run the eight invariants across virtual_portfolio, position_exits and
reservations. The check itself is read-only against the production
tables; the only writes are to reconciliation_runs and
reconciliation_diffs, which is the audit trail an operator reads when
the report flags something.

Exit codes:
    0  — clean run, or run produced only minor diffs.
    1  — at least one major or critical diff.
    2  — internal error (the runner itself failed; see logs).

A non-zero exit makes the script safe to chain into a cron that pages
on failure. --no-fail disables this for dashboards that want to keep
reporting even when the book is dirty.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from alpha_agents.data import reconciliation


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Run the cross-table reconciliation invariants.",
    )
    p.add_argument(
        "--json", action="store_true",
        help="machine-readable JSON output, one object per run.",
    )
    p.add_argument(
        "--trader", default=None,
        help="filter the printed diffs to one trader (cosmetic only).",
    )
    p.add_argument(
        "--fail-on-major", dest="fail_on_major",
        action="store_true", default=True,
        help="exit 1 on any major or critical diff (default).",
    )
    p.add_argument(
        "--no-fail-on-major", dest="fail_on_major",
        action="store_false",
        help="exit 0 even on major diffs; critical diffs still fail.",
    )
    p.add_argument(
        "--no-fail", action="store_true",
        help="never exit non-zero (use for dashboards, not for cron).",
    )
    args = p.parse_args(argv)

    try:
        result = reconciliation.reconcile()
    except Exception as e:
        print(f"reconciliation runner failed: {e}", file=sys.stderr)
        return 2

    diffs = result.diffs
    if args.trader:
        diffs = [d for d in diffs if d.trader_id == args.trader]
    n_critical = sum(1 for d in diffs if d.severity == reconciliation.CRITICAL)
    n_major = sum(1 for d in diffs if d.severity == reconciliation.MAJOR)
    n_minor = sum(1 for d in diffs if d.severity == reconciliation.MINOR)

    if args.json:
        out = {
            "run_id": result.run_id,
            "status": result.status,
            "diff_count": len(diffs),
            "critical": n_critical,
            "major": n_major,
            "minor": n_minor,
            "summary": result.summary,
            "diffs": [reconciliation.diff_to_dict(d) for d in diffs],
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(f"Reconciliation run #{result.run_id}: {result.status}")
        print(f"  diffs: {len(diffs)} "
              f"(critical={n_critical} major={n_major} minor={n_minor})")
        if result.summary:
            print("  per-trader derived totals:")
            for tid in sorted(result.summary):
                s = result.summary[tid]
                invested = s.get("invested_capital", 0.0)
                realized = s.get("realized_legs", 0.0)
                held = s.get("reservation_held", 0.0)
                consumed_rem = s.get("reservation_consumed_remaining", 0.0)
                print(f"    {tid or '(default)':10} "
                      f"n_open={s.get('n_open', 0)} "
                      f"n_pending={s.get('n_pending', 0)} "
                      f"n_closed={s.get('n_closed', 0)} "
                      f"invested={invested:.2f} "
                      f"realized={realized:.2f} "
                      f"reserved_held={held:.2f} "
                      f"reserved_consumed_remaining={consumed_rem:.2f}")
        for d in diffs:
            print(f"  [{d.severity:8}] "
                  f"{d.trader_id or '(unknown)':10} "
                  f"{d.invariant}: {d.detail}")

    if args.no_fail:
        return 0
    if n_critical:
        return 1
    if args.fail_on_major and n_major:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())