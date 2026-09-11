#!/usr/bin/env python3
"""What did we decide, and what came of it. Read-only.

The number design §9 calls "selection and coverage", and the one the book
could not produce before the ``episodes`` table: how many decisions were
made, how many were refused before an order was written, how many orders
were cancelled without ever trading, and how many actually filled. Before
this, "we decided 40 times and filled 6" was unanswerable — the only rows
that survived were the ones a decision produced, so a low fill rate and a
quiet week looked identical.

It reads. It writes nothing, and it has no exit-code contract: this is a
report an operator reads, not a gate a cron pages on (for that, see
``scripts/reconcile.py``).

    uv run python scripts/episode_coverage.py
    uv run python scripts/episode_coverage.py --trader slow --open 20
    uv run python scripts/episode_coverage.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from alpha_agents.data import episodes, memory_store  # noqa: E402


def _report(conn, trader_id: str | None, limit: int) -> dict:
    out = {"coverage": episodes.coverage(conn, trader_id=trader_id),
           "open_episodes": []}
    for ep in episodes.open_episodes(conn, trader_id=trader_id, limit=limit):
        kinds = [e["kind"] for e in episodes.events_for(conn, ep["id"])]
        out["open_episodes"].append({
            "id": ep["id"], "trader_id": ep["trader_id"], "code": ep["code"],
            "order_id": ep["order_id"], "position_id": ep["position_id"],
            "opened_at": ep["opened_at"],
            "boundary": ep["decision_snapshot_id"], "kinds": kinds,
        })
    return out


def _render(data: dict) -> str:
    cov = data["coverage"]
    rate = cov["fill_rate"]
    lines = [
        "Episode coverage — every decision, not only the ones that traded",
        f"  decisions   {cov['decisions']}",
        f"  answered    {cov['verdicts']}"
        + (f"   ({cov['no_verdict']} left mid-action)" if cov["no_verdict"]
           else ""),
        f"  refused     {cov['refused']}",
        f"  cancelled   {cov['cancelled']}",
        f"  traded      {cov['traded']}",
        f"  fill rate   {'n/a (no decisions)' if rate is None else f'{rate:.1%}'}",
    ]
    if data["open_episodes"]:
        lines.append("")
        lines.append("Live episodes (a decision still running):")
        for ep in data["open_episodes"]:
            where = ep["position_id"] or ep["order_id"]
            lines.append(
                f"  #{ep['id']:<6} {ep['trader_id']:10} "
                f"{ep['code'] or '(no instrument)':12} "
                f"book={where if where is not None else '-':<6} "
                f"since {ep['opened_at']}  {'/'.join(ep['kinds'])}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--trader", default=None,
                   help="count one book only (default: all of them).")
    p.add_argument("--open", type=int, default=10, dest="open_limit",
                   help="how many live episodes to list (default 10).")
    p.add_argument("--json", action="store_true",
                   help="machine-readable output.")
    args = p.parse_args(argv)

    data = _report(memory_store._get_conn(), args.trader, args.open_limit)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(_render(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
