#!/usr/bin/env python3
"""Continue independent accounts from a runner-created sealed checkpoint.

A knowledge intervention perturbs the first open plan on the first branch day
only. It is NOT a permanent handbook edit: subsequent accounts and learning
advance on their own. The existing runner performs every fill and review.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import random
import re
import socket
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from scripts.walk_checkpoint import (CheckpointError, materialize, runtime_identity,
                             source_identity, verify, write_json)


def prepare(args) -> tuple[dict, list[dict]]:
    if not 1 <= args.days <= 30 or not 1 <= args.trials <= 8:
        raise CheckpointError("days must be 1..30 and trials 1..8")
    if not 1 <= args.max_branch_sessions <= 200 or not 1 <= args.jobs <= 2:
        raise CheckpointError("Budget must be 1..200 branch-sessions and jobs 1..2")
    if not 1 <= args.timeout <= 21600:
        raise CheckpointError("Each branch must have an explicit deadline in 1..21600 seconds")
    checkpoint = args.checkpoint.resolve(strict=True)
    value = verify(checkpoint)
    is_llm = value["args"]["decider"] == "llm"
    if bool(args.live) != is_llm:
        raise CheckpointError("Use --live for an LLM checkpoint; --mechanical for a placeholder")
    changes = json.loads(args.arms.read_text(encoding="utf-8")) if args.arms else []
    if not isinstance(changes, list):
        raise CheckpointError("Arms must be an array of {name, old, new}")
    if changes and not is_llm:
        raise CheckpointError("A mechanical trader cannot test a knowledge-text intervention")
    arms, names = [("control", None)], {"control"}
    for change in changes:
        if not isinstance(change, dict) or set(change) != {"name", "old", "new"}:
            raise CheckpointError("Each intervention must contain exactly name, old, new")
        name = change["name"]
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name)
                or name in names):
            raise CheckpointError("Invalid/duplicate/reserved arm name")
        if (not isinstance(change["old"], str) or not change["old"]
                or not isinstance(change["new"], str) or change["old"] == change["new"]):
            raise CheckpointError("Intervention must change a nonempty exact knowledge fragment")
        names.add(name)
        arms.append((name, change))
    count = len(arms) * args.trials
    if count > 16 or count * args.days > args.max_branch_sessions:
        raise CheckpointError("Experiment exceeds declared branch-session budget or 16 branches")
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect((checkpoint / 'corpus/market_history.db').as_uri()
                                 + '?mode=ro&immutable=1', uri=True)) as conn:
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM daily_kline WHERE date > ? ORDER BY date LIMIT ?",
            (value["completed_through"], args.days))]
    if len(dates) != args.days or dates[0] != value["next_session"]:
        raise CheckpointError("Pinned corpus does not cover the full requested branch window")
    experiment_id = uuid4().hex
    tasks = [{"arm": arm, "trial": trial + 1, "intervention": change,
              "branch_id": f"fork-{experiment_id[:12]}-{arm}-{trial + 1}"}
             for trial in range(args.trials) for arm, change in arms]
    random.Random(args.seed).shuffle(tasks)
    manifest = {"version": 1, "experiment_id": experiment_id,
                "checkpoint": str(checkpoint), "checkpoint_id": value["checkpoint_id"],
                "source_hash": value["source_hash"], "runtime_hash": value["runtime_hash"],
                "mode": "model_trajectories" if is_llm else "mechanical_regression",
                "sessions": dates, "days": args.days, "planned_branches": count,
                "planned_branch_sessions": count * args.days,
                "budget": args.max_branch_sessions, "timeout": args.timeout,
                "schedule_seed": args.seed, "jobs": args.jobs, "tasks": tasks,
                "intervention_scope": "First open plan on first continuation day only",
                "limits": ["Independent model trajectories, not matched random model seeds",
                           "No automatic rule promotion; more orders is not a success metric",
                           "An initial knowledge perturbation is not permanent rule removal",
                           "No historical corpus or training-data point-in-time certification",
                           "Failed branches stay in the report; interrupted usage may be incomplete"]}
    return manifest, tasks


def _deny_network(event, args):
    if event in ("socket.getaddrinfo", "socket.gethostbyname", "socket.getnameinfo"):
        raise RuntimeError("Mechanical branch network access is disabled")
    if event in ("socket.connect", "socket.bind", "socket.sendto", "socket.sendmsg"):
        if args[0].family in (socket.AF_INET, socket.AF_INET6):
            raise RuntimeError("Mechanical branch network access is disabled")


def worker(args) -> int:
    workspace = args.workspace.resolve(strict=True)
    task = json.loads((workspace / "task.json").read_text())
    value = verify(args.checkpoint)
    storage = workspace / "storage"
    os.environ["ALPHAAGENTS_DATA_DIR"] = str(storage)
    os.environ["ALPHAAGENTS_RUN_ID"] = task["branch_id"]
    os.environ["ALPHAAGENTS_LLM_MODE"] = "record" if args.live else "live"
    os.environ["LLM_ATTEMPTS"] = "1"
    os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"
    os.environ["WF_STALL_DUMP_SECONDS"] = "0"
    if not args.live:
        sys.addaudithook(_deny_network)
    result = {"status": "error", "branch_id": task["branch_id"],
              "checkpoint_id": value["checkpoint_id"]}
    try:
        from alpha_agents.env_file import load_env
        load_env()
        from alpha_agents import config
        if Path(config.DATA_DIR).resolve() != storage.resolve():
            raise CheckpointError("Branch storage escaped private workspace")
        if runtime_identity() != value["runtime_hash"]:
            raise CheckpointError("Branch runtime/model configuration differs from prefix")
        materialize(args.checkpoint, storage, branch_id=task["branch_id"])
        from scripts import walk_forward as WF
        from alpha_agents.evolution.replay_mode import mark_replay_process
        from alpha_agents.evolution.performance import equity_metrics
        mark_replay_process()
        WF._REPLAY_DIR = storage
        saved = dict(value["args"])
        if saved.get("sector_membership"):
            saved["sector_membership"] = args.checkpoint / "inputs" / saved["sector_membership"]
        saved.update(target=storage, start=value["next_session"], days=args.days,
                     run_id=task["branch_id"], out=workspace / "report", checkpoint_out=None,
                     no_keep_notes=True, keep_going=False, continuation=value,
                     branch_intervention=task["intervention"])
        run_args = argparse.Namespace(**saved)
        got = WF.run(run_args)
        report = WF.write_report(got, run_args.out)
        health = WF.integrity(got)
        applied = getattr(got["ctx"], "branch_intervention_used", False)
        full_dates = [r["date"] for r in got["equity"]] == task["sessions"]
        if abs(got["initial_account"]["equity"] - value["end_account"]["equity"]) > 0.01:
            raise CheckpointError("Branch starting equity differs from checkpoint ending equity")
        complete = (health["complete"] and not got["errors"] and full_dates
                    and report["meta"]["model_usage_ok"]
                    and report["meta"]["production_db_unchanged"]
                    and report["meta"]["corpus_read_only"]
                    and not got["corpus"]["changes"]
                    and (task["intervention"] is None or applied))
        result.update(status="complete" if complete else "incomplete", integrity=health,
                      actual_sessions=[r["date"] for r in got["equity"]],
                      initial_account=got["initial_account"], end_account=got["equity"][-1],
                      metrics=equity_metrics(got["initial_account"]["equity"], got["equity"]),
                      counters=dict(got["ctx"].counters), errors=got["errors"],
                      intervention_applied=applied,
                      intervention=task["intervention"],
                      note="Metrics cover the branch suffix only; parent history is not new evidence")
        if task["intervention"] and not applied:
            result["incomplete_reason"] = "No eligible first-day open plan consumed the intervention"
        # State/learning remains in this storage; WF.main's production merge is never called.
    except (Exception, SystemExit) as exc:
        import traceback
        traceback.print_exc()
        result.update(error_type=type(exc).__name__, error_detail=str(exc)[:1000])
    from scripts.decision_lab import journal_usage
    result["usage"] = journal_usage(storage)
    if args.live and result["status"] != "complete":
        result["usage"]["usage_complete"] = False
    write_json(workspace / "result.json", result)
    return 0 if result["status"] == "complete" else 1


def launch(task: dict, args, root: Path, sessions: list[str]) -> dict:
    workspace = root / task["branch_id"]
    workspace.mkdir(mode=0o700)
    write_json(workspace / "task.json", {**task, "sessions": sessions})
    cmd = [sys.executable, str(Path(__file__).resolve()), "_worker",
           "--workspace", str(workspace), "--checkpoint", str(args.checkpoint.resolve()),
           "--days", str(args.days)]
    if args.live:
        cmd.append("--live")
    started = time.monotonic()
    with (workspace / "worker.log").open("x", encoding="utf-8") as log:
        try:
            proc = subprocess.run(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                  timeout=args.timeout, check=False)
            path = workspace / "result.json"
            result = json.loads(path.read_text()) if path.exists() else {
                "status": "error", "error_type": "WorkerExit", "returncode": proc.returncode}
            if proc.returncode and result.get("status") == "complete":
                result.update(status="error", error_type="WorkerExit", returncode=proc.returncode)
        except subprocess.TimeoutExpired:
            result = {"status": "error", "error_type": "BranchDeadline"}
        except (OSError, ValueError) as exc:
            result = {"status": "error", "error_type": type(exc).__name__}
    from scripts.decision_lab import journal_usage
    result.setdefault("usage", journal_usage(workspace / "storage"))
    if args.live and result["status"] != "complete":
        result["usage"]["usage_complete"] = False
    result.update({k: task[k] for k in ("branch_id", "arm", "trial")})
    result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    write_json(workspace / "branch.json", result)
    return result


def run(args) -> dict:
    manifest, tasks = prepare(args)
    destination = args.output.expanduser().resolve()
    checkpoint = args.checkpoint.resolve()
    if destination.is_relative_to(checkpoint) or checkpoint.is_relative_to(destination):
        raise CheckpointError("Experiment output may not contain/overwrite the checkpoint")
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    write_json(destination / "manifest.json", manifest)
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        results = list(pool.map(lambda task: launch(task, args, destination, manifest["sessions"]), tasks))
    unchanged = True
    try:
        verify(checkpoint)
    except (CheckpointError, OSError, ValueError):
        unchanged = False
    summary = {"experiment_id": manifest["experiment_id"], "mode": manifest["mode"],
               "checkpoint_id": manifest["checkpoint_id"], "checkpoint_unchanged": unchanged,
               "planned_branches": len(tasks), "reported_branches": len(results),
               "complete_branches": sum(r["status"] == "complete" for r in results),
               "results": results, "limits": manifest["limits"],
               "interpretation": "Suffix trajectories, not proof of alpha or improvement"}
    if not unchanged:
        summary["invalid_reason"] = "Source/code/checkpoint verification failed after experiment"
    write_json(destination / "summary.json", summary)
    return summary


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    q = sub.add_parser("run")
    q.add_argument("--checkpoint", type=Path, required=True)
    q.add_argument("--output", type=Path, required=True)
    q.add_argument("--days", type=int, required=True)
    q.add_argument("--trials", type=int, default=1)
    q.add_argument("--max-branch-sessions", type=int, required=True)
    q.add_argument("--timeout", type=int, required=True)
    q.add_argument("--jobs", type=int, default=1)
    q.add_argument("--seed", type=int, default=0)
    q.add_argument("--arms", type=Path)
    mode = q.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--mechanical", action="store_true")
    q = sub.add_parser("_worker", help=argparse.SUPPRESS)
    q.add_argument("--workspace", type=Path, required=True)
    q.add_argument("--checkpoint", type=Path, required=True)
    q.add_argument("--days", type=int, required=True)
    q.add_argument("--live", action="store_true")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "_worker":
            return worker(args)
        result = run(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if (result["checkpoint_unchanged"] and
                     result["complete_branches"] == result["planned_branches"]) else 1
    except (CheckpointError, OSError, ValueError) as exc:
        print(f"Branch experiment refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
