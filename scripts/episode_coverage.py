#!/usr/bin/env python3
"""What did we decide, and what came of it. Read-only.

Three parts of design §9's "selection and coverage" and §10's quarantine.

*Decisions.* The number the book could not produce before the ``episodes``
table: how many were made, how many were refused before an order was
written, how many orders were cancelled without ever trading, and how many
actually filled. Before this, "we decided 40 times and filled 6" was
unanswerable — the only rows that survived were the ones a decision
produced, so a low fill rate and a quiet week looked identical.

* Results.* The live outcome labels by kind and state, the count still
awaiting a result, and whether the label store is structurally sound. The
three kinds are reported side by side and never merged: a decision can be
a correct forecast and a losing trade at once, and one "performance"
figure would hide exactly that.

*Candidates.* The quarantine: how many proposals sit in each lifecycle
state, which ones are not statements §10 could evaluate, and which cite an
experience that does not exist. ``validated`` is printed beside the other
states and means no more than they do — reaching it activates nothing. The
supporting/opposing split is shown per candidate because a proposal citing
only the cases that agree with it is the failure §10 names, and it is
invisible in a single count.

*Approvals.* The other half of §10's line: which knowledge a person has
actually put in force, and whether the version they approved is still the
version on disk. Drift is reported, never treated as a fault — knowledge is
expected to keep moving — but "we approved this and it has since changed"
is the one thing an approval record exists to be able to say.

It reads. It writes nothing, and it has no exit-code contract: this is a
report an operator reads, not a gate a cron pages on (for that, see
``scripts/reconcile.py``).

One caveat, because this script says "writes nothing" above:
``learning_candidates`` owns its own schema and initialises it on every
entry point, so the first read against a pre-T3 database runs that
module's migration (add four columns, rebuild the table to relax
``CHECK(status = 'candidate')``). It is idempotent and it preserves every
row; it is not a data write, but it is not a pure read either.

    uv run python scripts/episode_coverage.py
    uv run python scripts/episode_coverage.py --trader slow --open 20
    uv run python scripts/episode_coverage.py --candidates
    uv run python scripts/episode_coverage.py --status testing
    uv run python scripts/episode_coverage.py --history 3
    uv run python scripts/episode_coverage.py --cites 12
    uv run python scripts/episode_coverage.py --approvals
    uv run python scripts/episode_coverage.py --is-approved 7
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
from alpha_agents.data import knowledge_snapshots as approvals  # noqa: E402
from alpha_agents.data import learning_candidates as candidates  # noqa: E402
from alpha_agents.data import outcomes  # noqa: E402

#: Report order. Alphabetical puts "censored" first, which reads as if
#: giving up were the normal end of a label.
_STATE_ORDER = (outcomes.PENDING, outcomes.MATURED, outcomes.CENSORED,
                outcomes.REVISED)

#: The lifecycle in the order a proposal walks it, not alphabetically.
_LIFECYCLE_ORDER = (candidates.OBSERVATION, candidates.HYPOTHESIS,
                    candidates.TESTING, candidates.VALIDATED,
                    candidates.RETIRED)

#: Long enough for the shape of a claim, short enough for one line.
_CLAIM_WIDTH = 58


def _short(text: str | None, width: int = _CLAIM_WIDTH) -> str:
    if not text:
        return "(no claim stated)"
    flat = " ".join(str(text).split())
    return flat if len(flat) <= width else flat[:width - 1] + "…"


def _citation_tally(citations_json: str | None) -> str:
    """``'2 supporting / 1 opposing'``.

    Both numbers, never their sum: a candidate citing only the cases that
    agree with it is exactly the failure §10 names, and it hides inside a
    total.
    """
    try:
        buckets = json.loads(citations_json) if citations_json else {}
    except (TypeError, ValueError):
        return "unreadable citations"
    return " / ".join(
        f"{len(buckets.get(key) or [])} {key}"
        for key in (candidates.SUPPORTING, candidates.OPPOSING))


def _candidate_rows(rows: list[dict]) -> list[dict]:
    return [{"id": r["id"], "status": r["status"],
             "entity_type": r["entity_type"], "operation": r["operation"],
             "claim": r["claim"],
             "citations": _citation_tally(r["evidence_episode_ids"])}
            for r in rows]


def _report(conn, trader_id: str | None, limit: int, *,
            list_candidates: bool = False, status: str | None = None,
            history_id: int | None = None, cites: int | None = None,
            list_approvals: bool = False,
            is_approved: int | None = None) -> dict:
    out = {"coverage": episodes.coverage(conn, trader_id=trader_id),
           "outcomes": outcomes.counts(conn),
           "awaiting_result": len(outcomes.pending_labels(conn)),
           "complaints": outcomes.integrity(conn),
           "candidates": candidates.counts(),
           "candidate_complaints": candidates.integrity(),
           "approvals": approvals.counts(),
           "approval_complaints": approvals.integrity(),
           "approval_drift": approvals.drifted(),
           "open_episodes": []}
    for ep in episodes.open_episodes(conn, trader_id=trader_id, limit=limit):
        kinds = [e["kind"] for e in episodes.events_for(conn, ep["id"])]
        out["open_episodes"].append({
            "id": ep["id"], "trader_id": ep["trader_id"], "code": ep["code"],
            "order_id": ep["order_id"], "position_id": ep["position_id"],
            "opened_at": ep["opened_at"],
            "boundary": ep["decision_snapshot_id"], "kinds": kinds,
        })
    if list_candidates:
        out["candidate_list"] = _candidate_rows(
            candidates.candidates_by_status(status))
    if history_id is not None:
        out["history"] = {"candidate_id": history_id,
                          "moves": candidates.transitions_for(history_id)}
    if cites is not None:
        out["cited_by"] = {
            "episode_id": cites,
            "candidates": [
                {"id": c["id"], "status": c["status"], "claim": c["claim"],
                 "cited_as": c["cited_as"]}
                for c in candidates.candidates_citing(cites)],
        }
    if list_approvals:
        out["approval_list"] = [
            {"id": s["id"], "approved_by": s["approved_by"],
             "approved_at": s["approved_at"], "reason": s["reason"],
             "notes": s["notes"], "verifies": approvals.verify_snapshot(s["id"]),
             "items": approvals.items_for(s["id"])}
            for s in approvals.all_snapshots()]
    if is_approved is not None:
        found = approvals.snapshots_for_candidate(is_approved)
        out["is_approved"] = {
            "candidate_id": is_approved,
            "approved": bool(found),
            "snapshot_ids": [s["id"] for s in found],
        }
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

    lines.append("")
    lines.append("Outcome labels — the three kinds stay apart")
    tally = data["outcomes"]
    width = max(len(k) for k in tally)
    for kind in sorted(tally):
        cells = "  ".join(f"{state} {tally[kind].get(state, 0):<4}"
                          for state in _STATE_ORDER)
        lines.append(f"  {kind:<{width}}  {cells}")
    lines.append(f"  awaiting a result   {data['awaiting_result']}")
    problems = data["complaints"]
    if problems:
        lines.append(f"  integrity           {len(problems)} problem(s):")
        for p in problems:
            lines.append(f"    - {p}")
    else:
        lines.append("  integrity           clean")

    lines.append("")
    lines.append("Candidate knowledge — proposals, not knowledge")
    states = data["candidates"]
    width = max(len(s) for s in _LIFECYCLE_ORDER)
    lines.append("  " + "   ".join(
        f"{status:<{width}} {states.get(status, 0)}"
        for status in _LIFECYCLE_ORDER))
    problems = data["candidate_complaints"]
    if problems:
        lines.append(f"  integrity           {len(problems)} problem(s):")
        for p in problems:
            lines.append(f"    - {p}")
    else:
        lines.append("  integrity           clean")

    for row in data.get("candidate_list", []):
        lines.append(
            f"  #{row['id']:<5} {row['status']:<{width}} "
            f"{row['entity_type']}/{row['operation']:<9} "
            f"[{row['citations']}]  {_short(row['claim'])}")

    history = data.get("history")
    if history is not None:
        lines.append("")
        lines.append(f"Lifecycle moves of candidate #{history['candidate_id']}"
                     " — who moved it, and why")
        if not history["moves"]:
            lines.append("  (never moved: still where it was written)")
        for m in history["moves"]:
            lines.append(f"  {m['at']}  {m['from_status']} → {m['to_status']}"
                         f"  by {m['actor']}: {m['reason']}")

    cited = data.get("cited_by")
    if cited is not None:
        lines.append("")
        lines.append(f"Candidates citing episode #{cited['episode_id']}")
        if not cited["candidates"]:
            lines.append("  (none — no candidate claims this experience)")
        for c in cited["candidates"]:
            lines.append(f"  #{c['id']:<5} {c['status']:<{width}} "
                         f"as {'+'.join(c['cited_as'])}  {_short(c['claim'])}")

    lines.append("")
    lines.append("Approved knowledge — what a person put in force")
    record = data["approvals"]
    lines.append(f"  snapshots   {record['snapshots']}   "
                 f"items {record['items']}")
    drift = data["approval_drift"]
    if drift:
        lines.append(f"  drift       {len(drift)} approved item(s) changed "
                     "since approval:")
        for d in drift:
            lines.append(
                f"    - {d['entity_type']} #{d['entity_id']} "
                f"(approved {d['version_hash'][:12]}, now "
                f"{(d['current_version_hash'] or 'gone')[:12]})")
    else:
        lines.append("  drift       none — every approved version is current")
    problems = data["approval_complaints"]
    if problems:
        lines.append(f"  integrity   {len(problems)} problem(s):")
        for p in problems:
            lines.append(f"    - {p}")
    else:
        lines.append("  integrity   clean")

    for snap in data.get("approval_list", []):
        lines.append(
            f"  #{snap['id']:<5} {snap['approved_at']}  "
            f"by {snap['approved_by']}  "
            f"{'verified' if snap['verifies'] else 'HASH MISMATCH'}  "
            f"{_short(snap['reason'])}")
        for item in snap["items"]:
            cited = (f" from candidate #{item['candidate_id']}"
                     if item["candidate_id"] else "")
            lines.append(f"        {item['entity_type']} #{item['entity_id']}"
                         f"{cited}  version {item['version_hash'][:12]}")

    verdict = data.get("is_approved")
    if verdict is not None:
        lines.append("")
        if verdict["approved"]:
            ids = ", ".join(f"#{i}" for i in verdict["snapshot_ids"])
            lines.append(f"Candidate #{verdict['candidate_id']} is in an "
                         f"approved snapshot ({ids})")
        else:
            lines.append(f"Candidate #{verdict['candidate_id']} is in no "
                         "approved snapshot")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--trader", default=None,
                   help="count one book only (default: all of them).")
    p.add_argument("--open", type=int, default=10, dest="open_limit",
                   help="how many live episodes to list (default 10).")
    p.add_argument("--candidates", action="store_true",
                   help="list candidates, oldest first.")
    p.add_argument("--status", default=None, choices=candidates.STATUSES,
                   help="only this lifecycle state (implies --candidates).")
    p.add_argument("--history", type=int, default=None, dest="history_id",
                   metavar="ID",
                   help="print the lifecycle moves of one candidate.")
    p.add_argument("--cites", type=int, default=None, metavar="EPISODE_ID",
                   help="list candidates that cite this episode.")
    p.add_argument("--approvals", action="store_true",
                   help="list approved knowledge snapshots.")
    p.add_argument("--is-approved", type=int, default=None,
                   dest="is_approved", metavar="CANDIDATE_ID",
                   help="is this candidate inside an approved snapshot?")
    p.add_argument("--json", action="store_true",
                   help="machine-readable output.")
    args = p.parse_args(argv)

    data = _report(
        memory_store._get_conn(), args.trader, args.open_limit,
        list_candidates=args.candidates or args.status is not None,
        status=args.status, history_id=args.history_id, cites=args.cites,
        list_approvals=args.approvals, is_approved=args.is_approved)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(_render(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
