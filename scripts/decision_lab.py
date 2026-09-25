#!/usr/bin/env python3
"""Export a decision and compare knowledge interventions without trading.

--live explicitly allows fresh model samples. --response-file only rechecks the
parser: reusing an answer after a prompt edit does not measure model behavior.
Each sample has its own process, private storage and journal. This is not a
continuous portfolio replay or a strategy profitability experiment.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import random
import re
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from alpha_agents.data.decision_frame import DecisionFrame, FrameError  # noqa: E402


def write_json(path: Path, value) -> None:
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def load_frame(path: Path) -> DecisionFrame:
    return DecisionFrame.from_dict(json.loads(path.read_text(encoding="utf-8")))


def source_records(args) -> list[dict]:
    from alpha_agents.data.decision_capture import read_inputs
    database = args.database.expanduser().resolve(strict=True)
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("PRAGMA query_only=ON")
        return read_inputs(conn, run_id=args.run_id, trader_id=args.trader_id,
                           invocation_id=getattr(args, "invocation_id", None), limit=args.limit)


def arms_for(frame: DecisionFrame, interventions: list[dict]) -> list[tuple[str, DecisionFrame]]:
    if not isinstance(interventions, list):
        raise FrameError("Arms must be a JSON array of {name, old, new}")
    out, names = [("control", frame)], {"control"}
    for arm in interventions:
        if not isinstance(arm, dict) or set(arm) != {"name", "old", "new"}:
            raise FrameError("Each arm must contain exactly name, old and new")
        name = arm["name"]
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,48}", name):
            raise FrameError("Arm names must be ASCII identifiers")
        if name in names:
            raise FrameError("Duplicate/reserved arm name")
        names.add(name)
        out.append((name, frame.change_knowledge(old=arm["old"], new=arm["new"], name=name)))
    return out


def prepare(args) -> tuple[dict, list[dict]]:
    """Validate before creating any experiment output or contacting a provider."""
    if not 1 <= args.trials <= 20 or not 1 <= args.max_samples <= 200:
        raise FrameError("trials must be 1..20 and max-samples 1..200")
    if not 1 <= args.jobs <= 4 or not 1 <= args.timeout <= 600:
        raise FrameError("jobs must be 1..4 and timeout 1..600 seconds")
    frames = [load_frame(p) for p in args.frame]
    if len({f.frame_hash for f in frames}) != len(frames):
        raise FrameError("Duplicate source frame: it is not an independent case")
    changes = json.loads(args.arms.read_text(encoding="utf-8")) if args.arms else []
    expanded = [(frame, arms_for(frame, changes)) for frame in frames]
    total = sum(len(arms) * args.trials for _, arms in expanded)
    if total > args.max_samples:
        raise FrameError(f"{total} samples exceed declared budget {args.max_samples}")
    tasks = []
    for source, arms in expanded:
        for name, frame in arms:
            value = frame.as_dict()
            if value["producer"]["contract"] != "t1_orders.v1" or value["request"]["tools"]:
                raise FrameError("A tool-bearing/non-T+1 frame is observation-only")
            if value["request"]["model_settings"] is not None:
                raise FrameError("Custom model-settings frames are not supported")
            if value["identity"]["trader_id"] == "unbound":
                raise FrameError("A real trader identity is required")
            if args.live and value["request"]["model"] == "unbound":
                raise FrameError("Live samples require a known declared model")
            for trial in range(args.trials):
                tasks.append({"source_frame_hash": source.frame_hash, "arm": name,
                              "trial": trial + 1, "frame": value})
    # This is a scheduling seed, not a claim of deterministic model sampling.
    random.Random(args.seed).shuffle(tasks)
    for index, task in enumerate(tasks):
        task["sample_id"] = f"sample-{index + 1:04d}"
    manifest = {"schema_version": 1, "experiment_id": uuid4().hex,
                "mode": "model_samples" if args.live else "parser_check",
                "planned_samples": total, "max_samples": args.max_samples,
                "trials_per_case_arm": args.trials, "schedule_seed": args.seed,
                "worker_timeout_seconds": args.timeout, "jobs": args.jobs,
                "client_policy": {"logical_attempts": 1, "transport_retries": 0,
                                  "model_failover": False, "tools": False},
                "limits": ["Not a profit estimate or a continuous account fork",
                           "No point-in-time certification of source content",
                           "A schedule seed is not a model seed",
                           "All failures count; parser checks are not independent samples"],
                "tasks": tasks}
    return manifest, tasks


def journal_usage(path: Path) -> dict:
    out = {"provider_requests": 0, "prompt_tokens": 0, "completion_tokens": 0,
           "response_models": [], "usage_complete": True}
    for file in sorted(path.glob("llm_journal/*.jsonl")):
        for line in file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                out["usage_complete"] = False
                out["unreadable_journal_lines"] = out.get("unreadable_journal_lines", 0) + 1
                continue
            if row.get("kind") not in ("llm_call", "llm_error"):
                continue
            out["provider_requests"] += 1
            if row.get("response_model") and row["response_model"] not in out["response_models"]:
                out["response_models"].append(row["response_model"])
            usage = (row.get("response_json") or {}).get("usage")
            if not isinstance(usage, dict):
                out["usage_complete"] = False
                continue
            for key in ("prompt_tokens", "completion_tokens"):
                val = usage.get(key)
                if type(val) is int and val >= 0:
                    out[key] += val
                else:
                    out["usage_complete"] = False
    return out


def worker(args) -> int:
    """Fresh subprocess only: set private paths before importing application config."""
    started = time.monotonic()
    workspace = args.workspace.resolve(strict=True)
    storage = workspace / "storage"
    storage.mkdir(mode=0o700, exist_ok=False)
    os.environ["ALPHAAGENTS_DATA_DIR"] = str(storage)
    os.environ["ALPHAAGENTS_RUN_ID"] = "decision-lab"
    os.environ["ALPHAAGENTS_LLM_MODE"] = "record" if args.live else "live"
    os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"
    os.environ["LLM_ATTEMPTS"] = "1"
    result = {"mode": "model_sample" if args.live else "parser_check", "status": "error"}
    try:
        from alpha_agents.env_file import load_env
        load_env()
        from alpha_agents import config
        if Path(config.DATA_DIR).resolve() != storage:
            raise FrameError("Experiment storage escaped its private workspace")
        from alpha_agents.agents import t1_decider
        frame = load_frame(workspace / "frame.json")
        t1_decider.require_frozen_plan(frame)
        if args.live:
            import asyncio
            from alpha_agents.model_factory import create_model
            model = create_model(timeout=args.timeout, allow_failover=False, max_retries=0)
            verdict = asyncio.run(t1_decider.propose_frame(frame, model=model, timeout=args.timeout))
        else:
            raw = (workspace / "response.txt").read_text(encoding="utf-8")
            verdict = t1_decider.parse_frozen_plan(frame, raw)
        status = verdict.get("decision_status") or (
            "incomplete" if verdict.get("parse_error") else
            "ordered" if verdict.get("orders") else "refused" if verdict.get("refused") else "abstained")
        result.update(status=status, verdict=verdict)
    except Exception as exc:
        result.update(error_type=type(exc).__name__,
                      error_detail=str(exc) if isinstance(exc, FrameError) else "See isolated worker log")
        if not isinstance(exc, FrameError):
            import traceback
            traceback.print_exc()
    result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    result["usage"] = journal_usage(storage)
    write_json(workspace / "result.json", result)
    return 0 if result["status"] != "error" else 1


def _launch(task: dict, args, root: Path, raw: str | None) -> dict:
    started = time.monotonic()
    workspace = root / task["sample_id"]
    workspace.mkdir(mode=0o700)
    write_json(workspace / "frame.json", task["frame"])
    if raw is not None:
        (workspace / "response.txt").write_text(raw, encoding="utf-8")
    cmd = [sys.executable, str(Path(__file__).resolve()), "_worker",
           "--workspace", str(workspace), "--timeout", str(args.timeout)]
    if args.live:
        cmd.append("--live")
    with (workspace / "worker.log").open("x", encoding="utf-8") as log:
        try:
            proc = subprocess.run(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                  timeout=args.timeout + 30, check=False)
            output = workspace / "result.json"
            result = (json.loads(output.read_text(encoding="utf-8")) if output.exists()
                      else {"status": "error", "error_type": "WorkerExit", "returncode": proc.returncode})
        except subprocess.TimeoutExpired:
            result = {"status": "error", "error_type": "WorkerDeadline"}
        except (OSError, ValueError) as exc:
            result = {"status": "error", "error_type": type(exc).__name__}
    result.setdefault("elapsed_ms", int((time.monotonic() - started) * 1000))
    result.setdefault("usage", journal_usage(workspace / "storage"))
    result.update({k: task[k] for k in ("sample_id", "source_frame_hash", "arm", "trial")})
    result.update({key: task["frame"][key] for key in (
        "frame_hash", "input_hash", "state_snapshot_hash", "policy_build_hash")})
    write_json(workspace / "sample.json", result)
    return result


def summarize(manifest: dict, results: list[dict]) -> dict:
    groups = {}
    for row in results:
        bucket = groups.setdefault(row["arm"], {"n": 0, "status_counts": {}, "ordered_samples": 0,
                                              "ordered_codes": {}, "elapsed_ms": 0,
                                              "provider_requests": 0})
        bucket["n"] += 1
        status = row["status"]
        bucket["status_counts"][status] = bucket["status_counts"].get(status, 0) + 1
        codes = [v["code"] for v in (row.get("verdict") or {}).get("orders") or []]
        bucket["ordered_samples"] += bool(codes)
        for code in set(codes):
            bucket["ordered_codes"][code] = bucket["ordered_codes"].get(code, 0) + 1
        bucket["elapsed_ms"] += row.get("elapsed_ms", 0)
        bucket["provider_requests"] += (row.get("usage") or {}).get("provider_requests", 0)
    return {"experiment_id": manifest["experiment_id"], "mode": manifest["mode"],
            "planned_samples": manifest["planned_samples"], "finished_samples": len(results),
            "groups": groups, "results": results,
            "interpretation": "Behavior/contract diagnostics only. More orders is not necessarily better.",
            "limits": manifest["limits"]}


def run(args) -> dict:
    manifest, tasks = prepare(args)
    raw = args.response_file.read_text(encoding="utf-8") if args.response_file else None
    destination = args.output.expanduser().resolve()
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    write_json(destination / "manifest.json", manifest)
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        results = list(pool.map(lambda task: _launch(task, args, destination, raw), tasks))
    summary = summarize(manifest, results)
    write_json(destination / "summary.json", summary)
    return summary


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("list", "export"):
        q = sub.add_parser(name)
        q.add_argument("--database", type=Path, required=True)
        q.add_argument("--run-id")
        q.add_argument("--trader-id")
        q.add_argument("--limit", type=int, default=100)
        if name == "export":
            q.add_argument("--invocation-id", required=True)
            q.add_argument("--output", type=Path, required=True)
    q = sub.add_parser("run")
    q.add_argument("--frame", type=Path, action="append", required=True)
    q.add_argument("--arms", type=Path)
    q.add_argument("--trials", type=int, default=3)
    q.add_argument("--max-samples", type=int, default=24)
    q.add_argument("--jobs", type=int, default=1)
    q.add_argument("--seed", type=int, default=0)
    q.add_argument("--timeout", type=int, default=180)
    q.add_argument("--output", type=Path, required=True)
    mode = q.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--response-file", type=Path)
    q = sub.add_parser("_worker", help=argparse.SUPPRESS)
    q.add_argument("--workspace", type=Path, required=True)
    q.add_argument("--timeout", type=int, required=True)
    q.add_argument("--live", action="store_true")
    return p


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "_worker":
            return worker(args)
        if args.command == "run":
            summary = run(args)
            print(json.dumps({k: v for k, v in summary.items() if k != "results"},
                             ensure_ascii=False, indent=2))
            return 1 if any(r["status"] == "error" for r in summary["results"]) else 0
        records = source_records(args)
        if args.command == "export":
            if len(records) != 1:
                raise FrameError("The requested invocation was not found uniquely")
            write_json(args.output, records[0]["frame"])
            print(str(args.output))
        else:
            print(json.dumps([{"invocation_id": r["invocation_id"],
                               "observed_at": r["observed_at"],
                               "identity": r["frame"]["identity"],
                               "frame_hash": r["frame"]["frame_hash"],
                               "tools": r["frame"]["request"]["tools"]} for r in records],
                             ensure_ascii=False, indent=2))
        return 0
    except (FrameError, ValueError, OSError, sqlite3.Error) as exc:
        print(f"decision_lab: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
