#!/usr/bin/env python3
"""Move the active policy, or refuse and say why. One entry point, audited.

§14 ends Phase 4 with "evidence controls future policy changes through one
audited entry point". This is that entry point: the only thing in the repo a
person runs to change which policy is in force. Every move goes through
``policy_registry.promote`` / ``rollback``, and both are a compare-and-swap on
``active_policy.version_seq`` — so a change that raced another loses without
writing rather than overwriting it.

Three verbs, and the difference between them is the whole of §11:

* ``approve`` — a person authorises a version, citing the gate verdict that
  justifies it. Writes one approval row. **Moves no pointer.** §11 is explicit
  that a successful automatic evaluation is not a licence to promote, so this
  is a separate act from promoting and leaves a separate record.
* ``promote`` — move the pointer to an approved version. Refuses unless a
  person approved it *and* the live configuration still hashes to it.
* ``rollback`` — restore a version that was in force before. Needs no verdict
  (the evidence is the transition trail), and deletes nothing.

``status`` changes nothing and is the intended way to look before you leap.

    uv run python scripts/policy.py status
    uv run python scripts/policy.py approve --version 4 --by kylin \\
        --reason "beat the baseline over 25 paired days"
    uv run python scripts/policy.py promote --version 4 --by kylin \\
        --reason "approved" --dry-run
    uv run python scripts/policy.py rollback --to-version 3 --by kylin \\
        --reason "drawdown breach"

Exit 0 on success, 1 on refusal. A refusal prints which condition failed —
someone running this by hand needs the reason, not just the verdict.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from alpha_agents.config import MEMORY_DB_PATH  # noqa: E402
from alpha_agents.data import policy_registry as registry  # noqa: E402
from alpha_agents.evolution import holdout_gate, policy_sources  # noqa: E402


def _sources(version_id):
    """Stage only the target snapshot; every non-knowledge source stays live.

    Changing the pointer is what switches knowledge. Comparing a candidate
    against the incumbent's snapshot (or the newest unrelated approval) would
    make both promotion and rollback impossible for knowledge-only changes.
    """
    sources = registry.sources_of(version_id)
    if sources is None:
        raise ValueError(f"No policy version #{version_id}")
    return policy_sources.collect(
        knowledge_snapshot_id=sources["knowledge"].get("snapshot_id"))


def _eligible_verdict(version_id: int, decision_id: int | None):
    """The verdict an approval would cite, or raise.

    Prefers an explicit ``--gate-decision``; otherwise takes the newest
    eligible verdict for the version. ``eligible_decisions`` already excludes
    abstentions and rejections, so "no eligible verdict" is the honest answer
    rather than "the newest row, whatever it says".
    """
    rows = holdout_gate.get_gate_decisions(policy_version_id=version_id)
    if decision_id is not None:
        for row in rows:
            if row["id"] == decision_id:
                return row
        raise ValueError(
            f"gate decision #{decision_id} is not a verdict about policy "
            f"version #{version_id}")
    eligible = holdout_gate.eligible_decisions(version_id)
    if not eligible:
        raise ValueError(
            f"policy version #{version_id} has no eligible gate verdict: "
            f"{len(rows)} verdict(s) on record, none of them a promote over "
            "at least one day of paired forward evidence. There is nothing "
            "for a person to approve — §16 forbids inventing the evidence.")
    return eligible[0]


def _cmd_status(args) -> int:
    pointer = registry.active(args.policy_key)
    print(f"policy {args.policy_key!r}")
    if pointer is None:
        print("  nothing in force")
    else:
        version = registry.get_version(pointer["version_id"])
        print(f"  in force: version #{pointer['version_id']} at seq "
              f"{pointer['version_seq']} "
              f"(changed by {pointer['changed_by']}, {pointer['changed_at']})")
        if version is None:
            # Reported rather than raised. A pointer at a version that is not
            # on record is precisely the situation where an operator needs
            # this command to work, and crashing would hide the diagnosis
            # behind a traceback.
            print("  but that version is not on record — see integrity below")
        else:
            print(f"  frozen {version['frozen_at']} by "
                  f"{version['created_by']}: {version['reason']}")
            print(f"  content hash {version['content_hash'][:16]}")
            approval = registry.approval_for(pointer["version_id"],
                                             args.policy_key)
            print("  approved by "
                  + (f"{approval['approved_by']} on {approval['at']}"
                     if approval else "(an install — no approval needed)"))
            print("  live configuration still matches: "
                  f"{policy_sources.verify_live(pointer['version_id'])}")

    versions = registry.versions_for(args.policy_key)
    print(f"  {len(versions)} version(s) on record:")
    for version in versions:
        mark = " *" if pointer and pointer["version_id"] == version["id"] else ""
        print(f"    #{version['id']} frozen {version['frozen_at']} "
              f"{version['content_hash'][:12]}{mark}")

    print(f"  {len(registry.transitions_for(args.policy_key))} transition(s):")
    for step in registry.transitions_for(args.policy_key):
        source = (f"#{step['from_version_id']}"
                  if step["from_version_id"] else "(none)")
        print(f"    seq {step['version_seq']} {step['kind']:9} {source} -> "
              f"#{step['to_version_id']} by {step['actor']}")

    problems = registry.integrity()
    print("  integrity: " + ("clean" if not problems else ""))
    for problem in problems:
        print(f"    - {problem}")
    return 0


def _cmd_approve(args) -> int:
    verdict = _eligible_verdict(args.version, args.gate_decision)
    print(f"  citing gate decision #{verdict['id']}: {verdict['reason']}")
    print(f"    outcome={verdict['outcome']} "
          f"validation_days={verdict['validation_days']} n={verdict['n']}")
    if args.dry_run:
        print("dry run: nothing written.")
        return 0
    approval_id = registry.approve(
        version_id=args.version, approved_by=args.by, reason=args.reason,
        gate_decision=verdict, sources=_sources(args.version if args.verb != "rollback" else args.to_version),
        policy_key=args.policy_key, at=args.at)
    print(f"recorded approval #{approval_id}. The pointer did not move: "
          "promotion is a separate act.")
    return 0


def _cmd_promote(args) -> int:
    if args.dry_run:
        approval = registry.approval_for(args.version, args.policy_key)
        print("  approved by "
              + (f"{approval['approved_by']} on {approval['at']}"
                 if approval else "nobody — this promotion would be refused"))
        print("dry run: nothing written.")
        return 0
    seq = registry.promote(
        version_id=args.version, actor=args.by, reason=args.reason,
        sources=_sources(args.version if args.verb != "rollback" else args.to_version), expected_seq=args.expect_seq,
        policy_key=args.policy_key, at=args.at)
    print(f"policy {args.policy_key!r} is now at version #{args.version}, "
          f"seq {seq}")
    return 0


def _cmd_rollback(args) -> int:
    if args.dry_run:
        print("dry run: nothing written.")
        return 0
    seq = registry.rollback(
        to_version_id=args.to_version, actor=args.by, reason=args.reason,
        sources=_sources(args.version if args.verb != "rollback" else args.to_version), expected_seq=args.expect_seq,
        policy_key=args.policy_key, at=args.at)
    print(f"policy {args.policy_key!r} restored to version "
          f"#{args.to_version}, seq {seq}. Nothing was deleted.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--policy-key", default=registry.POLICY_KEY_DEFAULT,
                        help="which governed policy (default: trader).")
    sub = parser.add_subparsers(dest="verb", required=True)

    status = sub.add_parser("status",
                            help="what is in force, and is it still clean.")
    status.set_defaults(func=_cmd_status)

    def _common(p):
        p.add_argument("--by", required=True, help="who is acting.")
        p.add_argument("--reason", required=True, help="why, in one sentence.")
        p.add_argument("--at", default=None, metavar="YYYY-MM-DD",
                       help="the date to record (default: the kernel clock).")
        p.add_argument("--dry-run", action="store_true",
                       help="print what would happen, then stop.")

    approve = sub.add_parser("approve", help="authorise a version.")
    approve.add_argument("--version", type=int, required=True)
    approve.add_argument("--gate-decision", type=int, default=None,
                         help="cite this gate decision instead of the newest "
                              "eligible one.")
    _common(approve)
    approve.set_defaults(func=_cmd_approve)

    promote = sub.add_parser("promote", help="move the pointer to a version.")
    promote.add_argument("--version", type=int, required=True)
    promote.add_argument("--expect-seq", type=int, default=None,
                         help="only act if the pointer is still at this seq.")
    _common(promote)
    promote.set_defaults(func=_cmd_promote)

    rollback = sub.add_parser("rollback", help="restore an earlier version.")
    rollback.add_argument("--to-version", type=int, required=True)
    rollback.add_argument("--expect-seq", type=int, default=None,
                          help="only act if the pointer is still at this seq.")
    _common(rollback)
    rollback.set_defaults(func=_cmd_rollback)

    args = parser.parse_args(argv)

    # The path is printed because it is a fixed location that does not read
    # TMPDIR: a person about to move the active policy should see which
    # database they are moving it in.
    print(f"policy target: {MEMORY_DB_PATH}")
    try:
        return args.func(args)
    except (registry.PolicyError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
