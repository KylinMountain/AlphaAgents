#!/usr/bin/env python3
"""Approve knowledge into an immutable snapshot. Writes one record, changes nothing else.

§10 puts approval in a person's hands: a candidate may be kept, argued
about and validated, and still not be in force until it is named in an
approved snapshot. This script is the human end of that sentence — the only
thing in the repo that writes the approval record.

It is **not** an activation path. Approving changes no prompt, no retrieval
weight and no ``_get_*`` return value; it records who put what in force and
when, and stops there. Nothing reads the record to change behaviour (see
``docs/TRADER_CORE_IMPLEMENTATION.md`` §7).

``--item`` is ``entity_type:entity_id[:candidate_id]``, repeated once per
knowledge row. ``--dry-run`` resolves the version hashes and prints exactly
what would be recorded, then exits without writing — the intended way to
look before you leap.

    uv run python scripts/approve_knowledge.py \\
        --by kylin --reason "shadow-tested for a quarter" \\
        --item principle:12:7 --item playbook:3

    uv run python scripts/approve_knowledge.py \\
        --by kylin --reason "checking first" --item principle:12 --dry-run

Exit 0 on success, 1 on refusal. A refusal prints which item was wrong:
someone running this by hand needs the reason, not just the verdict.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from alpha_agents.config import MEMORY_DB_PATH  # noqa: E402
from alpha_agents.data import knowledge_snapshots as snapshots  # noqa: E402


def _item(raw: str) -> dict:
    parts = raw.split(":")
    if len(parts) not in (2, 3) or not parts[0]:
        raise argparse.ArgumentTypeError(
            f"{raw!r} is not entity_type:entity_id[:candidate_id]")
    entry = {"entity_type": parts[0]}
    for key, value in zip(("entity_id", "candidate_id"), parts[1:]):
        try:
            entry[key] = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"{raw!r}: {key} must be an integer, got {value!r}")
    return entry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--by", required=True, help="who is approving.")
    parser.add_argument("--reason", required=True,
                        help="why, in one sentence.")
    parser.add_argument("--item", action="append", type=_item, required=True,
                        dest="items", metavar="TYPE:ID[:CANDIDATE]",
                        help="knowledge to approve; repeat once per row.")
    parser.add_argument("--notes", default=None, help="anything else worth keeping.")
    parser.add_argument("--at", default=None, metavar="YYYY-MM-DD",
                        help="the date the approval claims (default: the kernel clock).")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be recorded, then stop.")
    args = parser.parse_args(argv)

    # The path is printed because it is a fixed location that does not read
    # TMPDIR: a person about to write an approval should see which database
    # they are writing it into.
    print(f"approval target: {MEMORY_DB_PATH}")
    try:
        resolved = snapshots.plan(args.items)
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    for item in resolved:
        cited = (f" (from candidate #{item['candidate_id']})"
                 if item["candidate_id"] else "")
        print(f"  {item['entity_type']} #{item['entity_id']}{cited} "
              f"version {item['version_hash'][:16]}")
    if args.dry_run:
        print("dry run: nothing written.")
        return 0
    try:
        snapshot_id = snapshots.approve(
            approved_by=args.by, reason=args.reason, items=args.items,
            notes=args.notes, approved_at=args.at)
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(f"recorded knowledge snapshot #{snapshot_id}; "
          f"{snapshots.counts()['snapshots']} snapshot(s) on record")
    return 0


if __name__ == "__main__":
    sys.exit(main())
