#!/usr/bin/env python3
"""Audit and verify sector-first experiment inputs.

Audit is read-only:
  uv run python scripts/sector_first.py audit --as-of 2026-09-19 --out /tmp/sf

Verify refuses incomplete preregistration:
  uv run python scripts/sector_first.py verify \
      --manifest /tmp/sf/experiment_manifest.json --out /tmp/sf

Register a validated source-capability report after semantic review:
  uv run python scripts/sector_first.py register-capabilities \
      --capabilities /tmp/sf/capabilities.json --out /tmp/sf

Register a validated protocol without overwriting an older version:
  uv run python scripts/sector_first.py register \
      --manifest /tmp/sf/experiment_manifest.json --out /tmp/sf

Run one preregistered validation window across all four isolated arms:
  uv run python scripts/sector_first.py run-matrix \
      --manifest /tmp/sf/manifests/<hash>.json \
      --capabilities /tmp/sf/capabilities/<hash>.json \
      --sector-membership /path/to/pit-membership.json \
      --corpus data --window-index 0 --out /tmp/sf/window-0
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from alpha_agents.report_io import write_json  # noqa: E402
from alpha_agents.config import DATA_DIR  # noqa: E402
from alpha_agents.data import frozen_direction_archive  # noqa: E402
from alpha_agents.data import policy_registry, sector_source_probe  # noqa: E402
from alpha_agents.evolution import sector_experiment  # noqa: E402
from alpha_agents.evolution import sector_experiment_compare  # noqa: E402
from alpha_agents.evolution import selection_experiment  # noqa: E402
from alpha_agents.evolution import selection_experiment_compare  # noqa: E402
from alpha_agents.evolution import world_read_set  # noqa: E402



def _picks_flag(decision: dict) -> list[str]:
    """``--picks`` only when the manifest names a cap.

    ``picks_per_day: null`` is a real setting — "no cap, the agent decides
    how many ideas to take" — and the honest way to pass it is to pass
    nothing, letting the runner's own default stand. ``str(None)`` produced
    the literal "None", which argparse rejects as an int; substituting a
    number would run an arm the manifest did not preregister.
    """
    picks = decision.get("picks_per_day")
    return [] if picks is None else ["--picks", str(int(picks))]

def _git_code_ref() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO, check=False, capture_output=True, text=True)
    ref = completed.stdout.strip()
    if completed.returncode != 0 or len(ref) != 40:
        raise sector_experiment.SectorExperimentError(
            "formal experiment needs a measurable git code ref")
    return ref


def _source_identity(corpus: Path, membership: Path) -> dict:
    if not membership.exists():
        raise FileNotFoundError(membership)
    return world_read_set.file_identity({
        "market_history.db": corpus / "market_history.db",
        "market_snapshots.db": corpus / "market_snapshots.db",
        "stocks.db": corpus / "stocks.db",
        "sector_membership": membership,
    })


def _audit(args) -> int:
    capabilities = sector_source_probe.probe_all(args.corpus)
    identity = _source_identity(args.corpus, args.sector_membership)
    report = sector_source_probe.with_content_hash({
        "as_of": args.as_of,
        "data_dir": str(args.corpus),
        "capabilities": capabilities,
        "input_identity": identity,
    })

    out = args.out
    write_json(out / "capabilities.json", report)
    manifest = sector_experiment.template(
        capabilities_hash=report["content_hash"])
    manifest["baseline_identity"] = {
        "code_ref": _git_code_ref(),
        "policy_ref": policy_registry.active_ref(),
        "input_hash": identity["input_hash"],
    }
    write_json(out / "experiment_manifest.json", manifest)

    print(json.dumps({
        "capabilities": str(out / "capabilities.json"),
        "manifest": str(out / "experiment_manifest.json"),
        "capabilities_hash": report["content_hash"],
        "manifest_ready": False,
        "note": (
            "experiment_manifest.json is a preregistration template; "
            "verify must fail until all required fields are frozen."),
    }, ensure_ascii=False, indent=2))
    return 0


def _register(args) -> int:
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    path = sector_experiment.register(manifest, args.out)
    print(json.dumps({
        "registered_manifest": str(path),
        "manifest_hash": sector_experiment.require_valid(manifest),
    }, ensure_ascii=False, indent=2))
    return 0


def _register_selection(args) -> int:
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    path = selection_experiment.register(manifest, args.out)
    print(json.dumps({
        "registered_manifest": str(path),
        "manifest_hash": selection_experiment.require_valid(manifest),
        "family": selection_experiment.FAMILY,
    }, ensure_ascii=False, indent=2))
    return 0


def _register_capabilities(args) -> int:
    report = json.loads(args.capabilities.read_text(encoding="utf-8"))
    path = sector_source_probe.register(report, args.out)
    sealed = json.loads(path.read_text(encoding="utf-8"))
    print(json.dumps({
        "registered_capabilities": str(path),
        "capabilities_hash": sealed["content_hash"],
        "formal_errors": sector_source_probe.formal_errors(sealed),
    }, ensure_ascii=False, indent=2))
    return 0


def _freeze_directions(args) -> int:
    payload = frozen_direction_archive.export(run_id=args.run_id)
    frozen_direction_archive.write(args.out_file, payload)
    print(json.dumps({
        "out_file": str(args.out_file),
        "source_run_id": payload["source_run_id"],
        "days": len(payload["days"]),
        "archive_hash": payload["archive_hash"],
    }, ensure_ascii=False, indent=2))
    return 0


_ARM_ARCHITECTURES = {
    "A": "dual_rank_v0",
    "B": "sector_first_v0",
    "C": "sector_first_simple_selector",
    "D": "sector_first_no_flow",
}


_SELECTION_ARCHITECTURES = {
    "CONTROL": "dual_rank_price_v1",
    "SECTOR": "sector_rank_price_v1",
}


def _registered_selection_manifest(path: Path) -> tuple[dict, str]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    digest = selection_experiment.require_valid(manifest)
    if (
            path.name != f"{digest}.json"
            or path.parent.name != "selection-manifests"):
        raise selection_experiment.SelectionExperimentError(
            "selection matrix requires the content-addressed manifest "
            "produced by sector_first.py register-selection")
    return manifest, digest


def _selection_capability_errors(report: dict) -> list[str]:
    """Capabilities actually consumed by nf_discovery_v1.

    Fund flow and free-form event/news vintages are intentionally excluded:
    this protocol forbids reading them, so their absence cannot invalidate it.
    """
    caps = report.get("capabilities") or {}
    errors = []
    price = caps.get("daily_price") or {}
    if price.get("status") != "available":
        errors.append("daily_price capability is not available")

    security = caps.get("security_status") or {}
    if security.get("status") != "available":
        errors.append("security_status capability is not available")
    if security.get("point_in_time_grade") != "A":
        errors.append(
            "security_status point_in_time_grade must be A")
    if security.get("strict_replay_eligible") is not True:
        errors.append(
            "security_status must be strict_replay_eligible")
    verification = security.get("verification") or {}
    for field in ("verified_by", "verified_at", "evidence"):
        if not str(verification.get(field) or "").strip():
            errors.append(
                f"security_status grade A requires verification.{field}")
    return errors


def _registered_selection_capabilities(
        path: Path, manifest: dict) -> tuple[dict, str]:
    report = json.loads(path.read_text(encoding="utf-8"))
    digest = sector_source_probe.report_hash(report)
    if report.get("content_hash") != digest:
        raise selection_experiment.SelectionExperimentError(
            "capability report content hash does not match its payload")
    if path.name != f"{digest}.json" or path.parent.name != "capabilities":
        raise selection_experiment.SelectionExperimentError(
            "selection matrix requires a content-addressed capability report")
    if manifest.get("capabilities_hash") != digest:
        raise selection_experiment.SelectionExperimentError(
            "selection manifest capabilities_hash mismatch")
    errors = _selection_capability_errors(report)
    if errors:
        raise selection_experiment.SelectionExperimentError(
            "nf_discovery_v1 capability gate failed: " + "; ".join(errors))
    return report, digest


def _registered_manifest(path: Path) -> tuple[dict, str]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    digest = sector_experiment.require_valid(manifest)
    if path.name != f"{digest}.json" or path.parent.name != "manifests":
        raise sector_experiment.SectorExperimentError(
            "run-matrix requires the content-addressed manifest produced by "
            "sector_first.py register; an editable working copy is not a "
            "preregistered protocol")
    return manifest, digest


def _registered_capabilities(path: Path, manifest: dict) -> tuple[dict, str]:
    report = json.loads(path.read_text(encoding="utf-8"))
    digest = sector_source_probe.report_hash(report)
    if report.get("content_hash") != digest:
        raise sector_experiment.SectorExperimentError(
            "capability report content hash does not match its payload")
    if path.name != f"{digest}.json" or path.parent.name != "capabilities":
        raise sector_experiment.SectorExperimentError(
            "run-matrix requires the content-addressed capability report "
            "produced by sector_first.py register-capabilities")
    if manifest.get("capabilities_hash") != digest:
        raise sector_experiment.SectorExperimentError(
            "manifest capabilities_hash does not match the registered "
            "capability report")
    errors = sector_source_probe.formal_errors(report)
    if errors:
        raise sector_experiment.SectorExperimentError(
            "formal A/B/C/D capability gate failed: " + "; ".join(errors))
    return report, digest


def _window_session_count(corpus: Path, start: str, end: str) -> int:
    db = corpus / "market_history.db"
    if not db.exists():
        raise FileNotFoundError(f"market history not found: {db}")
    conn = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT DISTINCT date FROM daily_kline "
            "WHERE date >= ? AND date <= ? ORDER BY date",
            (start, end),
        ).fetchall()
    finally:
        conn.close()
    days = [str(row[0]) for row in rows]
    if not days or days[0] != start or days[-1] != end:
        raise sector_experiment.SectorExperimentError(
            f"registered window {start}..{end} is not fully present in "
            f"{db}; observed endpoints are "
            f"{days[0] if days else None}..{days[-1] if days else None}")
    return len(days)


def _walk_command(*, manifest: dict, manifest_path: Path,
                  membership_path: Path, target: Path, out: Path,
                  start: str, days: int, arm: str, run_id: str,
                  frozen_directions: Path | None = None) -> list[str]:
    architecture = _ARM_ARCHITECTURES[arm]
    decision = manifest["decision_config"]
    exit_policy = manifest["exit_policy"]
    cmd = [
        sys.executable, str(REPO / "scripts" / "walk_forward.py"),
        "--target", str(target),
        "--start", start,
        "--days", str(days),
        "--decider", "llm",
        "--selection-architecture", architecture,
        "--sector-membership", str(membership_path),
        "--experiment-manifest", str(manifest_path),
        "--experiment-arm", arm,
        "--run-id", run_id,
        "--out", str(out),
        "--trader", str(decision["trader"]),
        *_picks_flag(decision),
        "--panel-size", str(decision["panel_size"]),
        "--participation", str(decision["participation"]),
        "--news-limit", str(decision["news_limit"]),
        "--model-timeout", str(decision["model_timeout_seconds"]),
        "--pace-seconds", str(decision["pace_seconds"]),
    ]
    if not decision["trader_tools_enabled"]:
        cmd.append("--no-trader-tools")
    if decision["max_turns_per_decision"] is not None:
        cmd.extend([
            "--max-turns", str(decision["max_turns_per_decision"])])
    if not exit_policy["mechanical_stop"]:
        cmd.append("--no-stop-loss")
    if not exit_policy["mechanical_target"]:
        cmd.append("--no-take-profit")
    if exit_policy["agent_exits"]:
        cmd.append("--agent-exits")
    if arm == "C":
        if frozen_directions is None:
            raise sector_experiment.SectorExperimentError(
                "C requires B's frozen direction archive")
        cmd.extend(["--frozen-directions", str(frozen_directions)])
    elif frozen_directions is not None:
        raise sector_experiment.SectorExperimentError(
            "only C may receive --frozen-directions")
    return cmd


def _selection_walk_command(
        *, manifest: dict, manifest_path: Path, membership_path: Path,
        target: Path, out: Path, start: str, days: int,
        arm: str, run_id: str) -> list[str]:
    architecture = _SELECTION_ARCHITECTURES[arm]
    decision = manifest["decision_config"]
    exit_policy = manifest["exit_policy"]
    cmd = [
        sys.executable, str(REPO / "scripts" / "walk_forward.py"),
        "--target", str(target),
        "--start", start,
        "--days", str(days),
        "--decider", "llm",
        "--selection-architecture", architecture,
        "--sector-membership", str(membership_path),
        "--selection-experiment-manifest", str(manifest_path),
        "--selection-experiment-arm", arm,
        "--run-id", run_id,
        "--out", str(out),
        "--trader", str(decision["trader"]),
        "--theme", str(decision["run_theme"]),
        *_picks_flag(decision),
        "--panel-size", str(decision["panel_size"]),
        "--participation", str(decision["participation"]),
        "--news-limit", "0",
        "--model-timeout", str(decision["model_timeout_seconds"]),
        "--pace-seconds", str(decision["pace_seconds"]),
        "--no-trader-tools",
    ]
    if not exit_policy["mechanical_stop"]:
        cmd.append("--no-stop-loss")
    if not exit_policy["mechanical_target"]:
        cmd.append("--no-take-profit")
    if exit_policy["agent_exits"]:
        cmd.append("--agent-exits")
    if exit_policy.get("close_buys"):
        cmd.append("--close-buys")
    return cmd


def _run_child(cmd: list[str], *, env: dict[str, str]) -> None:
    print("+ " + " ".join(cmd), flush=True)
    completed = subprocess.run(cmd, env=env, check=False)
    if completed.returncode != 0:
        raise sector_experiment.SectorExperimentError(
            f"child command failed with exit {completed.returncode}: "
            + " ".join(cmd))


def _verify_matrix_identity(
        manifest: dict, corpus: Path, membership: Path) -> tuple[dict, str]:
    measured_identity = _source_identity(corpus, membership)
    frozen_identity = manifest["baseline_identity"]
    identity_errors = {}
    actual_code = _git_code_ref()
    if frozen_identity.get("code_ref") != actual_code:
        identity_errors["code_ref"] = {
            "manifest": frozen_identity.get("code_ref"),
            "runtime": actual_code,
        }
    if frozen_identity.get("input_hash") != measured_identity["input_hash"]:
        identity_errors["input_hash"] = {
            "manifest": frozen_identity.get("input_hash"),
            "runtime": measured_identity["input_hash"],
        }
    if identity_errors:
        raise sector_experiment.SectorExperimentError(
            "formal experiment identity mismatch: "
            + json.dumps(identity_errors, ensure_ascii=False, sort_keys=True))
    return measured_identity, actual_code


def _run_matrix(args) -> int:
    manifest, digest = _registered_manifest(args.manifest)
    _capabilities, capabilities_digest = _registered_capabilities(
        args.capabilities, manifest)

    measured_identity, actual_code = _verify_matrix_identity(
        manifest, args.corpus, args.sector_membership)
    windows = manifest["validation_windows"]
    if not 0 <= args.window_index < len(windows):
        raise sector_experiment.SectorExperimentError(
            f"window-index must be 0..{len(windows) - 1}")
    if not args.sector_membership.exists():
        raise FileNotFoundError(args.sector_membership)
    if manifest["exit_policy"].get("agent_exits"):
        raise sector_experiment.SectorExperimentError(
            "Sector-First formal arms do not support agent_exits yet; "
            "freeze agent_exits=false until the close-buy path is wired")

    window = windows[args.window_index]
    start = str(window["start"])[:10]
    end = str(window["end"])[:10]
    days = _window_session_count(args.corpus, start, end)

    root = args.out
    state_root = root / "state"
    arms_root = root / "arms"
    frozen = root / "frozen_b_directions.json"
    prefix = args.run_prefix or (
        f"sector-{digest[:10]}-w{args.window_index}")
    record_path = root / "matrix_run.json"
    if record_path.exists():
        raise sector_experiment.SectorExperimentError(
            f"{record_path} already exists; formal matrix runs are append-by-"
            "directory, not overwrite-in-place")

    root.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["ALPHAAGENTS_LLM_MODE"] = "record"
    completed_arms = []

    def bootstrap(arm: str) -> Path:
        target = state_root / arm
        _run_child([
            sys.executable, str(REPO / "scripts" / "walk_bootstrap.py"),
            "--target", str(target), "--corpus", str(args.corpus),
        ], env=env)
        return target

    def replay(arm: str, *, frozen_directions: Path | None = None) -> None:
        target = bootstrap(arm)
        run_id = f"{prefix}-{arm}"
        _run_child(_walk_command(
            manifest=manifest,
            manifest_path=args.manifest,
            membership_path=args.sector_membership,
            target=target,
            out=arms_root / arm,
            start=start,
            days=days,
            arm=arm,
            run_id=run_id,
            frozen_directions=frozen_directions,
        ), env=env)
        completed_arms.append(arm)

    status = {
        "manifest_hash": digest,
        "manifest": str(args.manifest),
        "capabilities_hash": capabilities_digest,
        "capabilities": str(args.capabilities),
        "membership": str(args.sector_membership),
        "input_identity": measured_identity,
        "code_ref": actual_code,
        "window_index": args.window_index,
        "window": {"start": start, "end": end, "trading_days": days},
        "completed_arms": completed_arms,
        "status": "running",
    }
    write_json(record_path, status)
    try:
        replay("A")
        write_json(record_path, status)
        replay("B")
        write_json(record_path, status)

        freeze_env = dict(env)
        freeze_env["ALPHAAGENTS_DATA_DIR"] = str(state_root / "B")
        _run_child([
            sys.executable, str(REPO / "scripts" / "sector_first.py"),
            "freeze-directions",
            "--run-id", f"{prefix}-B",
            "--out-file", str(frozen),
        ], env=freeze_env)
        if not frozen.exists():
            raise sector_experiment.SectorExperimentError(
                "B direction freeze command returned success but produced no archive")

        replay("C", frozen_directions=frozen)
        write_json(record_path, status)
        replay("D")
        status["status"] = "arms_complete"
        write_json(record_path, status)

        report = sector_experiment_compare.compare(
            manifest=manifest,
            arm_dirs={arm: arms_root / arm for arm in _ARM_ARCHITECTURES},
        )
        write_json(root / "comparison.json", report)
        status["status"] = report["status"]
        status["comparison"] = str(root / "comparison.json")
        write_json(record_path, status)
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "ready_for_human_review" else 3
    except Exception:
        status["status"] = "failed"
        write_json(record_path, status)
        raise


def _run_selection_matrix(args) -> int:
    manifest, digest = _registered_selection_manifest(args.manifest)
    _caps, capabilities_digest = _registered_selection_capabilities(
        args.capabilities, manifest)
    measured_identity, actual_code = _verify_matrix_identity(
        manifest, args.corpus, args.sector_membership)

    root = args.out
    record_path = root / "selection_matrix_run.json"
    if record_path.exists():
        raise selection_experiment.SelectionExperimentError(
            f"{record_path} already exists; use a new output directory")
    root.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["ALPHAAGENTS_LLM_MODE"] = "record"
    completed = []
    status = {
        "family": selection_experiment.FAMILY,
        "manifest_hash": digest,
        "manifest": str(args.manifest),
        "capabilities_hash": capabilities_digest,
        "capabilities": str(args.capabilities),
        "membership": str(args.sector_membership),
        "input_identity": measured_identity,
        "code_ref": actual_code,
        "completed": completed,
        "status": "running",
    }
    write_json(record_path, status)

    try:
        for index, window in enumerate(manifest["validation_windows"]):
            start = str(window["start"])[:10]
            end = str(window["end"])[:10]
            days = _window_session_count(args.corpus, start, end)
            expected = int(manifest["expected_days_per_window"])
            if days != expected:
                raise selection_experiment.SelectionExperimentError(
                    f"window {index} resolved to {days} sessions; "
                    f"expected exactly {expected}")
            window_root = root / "windows" / str(index)
            for arm in ("CONTROL", "SECTOR"):
                target = root / "state" / str(index) / arm
                _run_child([
                    sys.executable, str(REPO / "scripts" / "walk_bootstrap.py"),
                    "--target", str(target), "--corpus", str(args.corpus),
                ], env=env)
                run_id = (
                    f"{args.run_prefix or 'nf'}-{digest[:10]}-"
                    f"w{index}-{arm}")
                _run_child(_selection_walk_command(
                    manifest=manifest,
                    manifest_path=args.manifest,
                    membership_path=args.sector_membership,
                    target=target,
                    out=window_root / arm,
                    start=start,
                    days=days,
                    arm=arm,
                    run_id=run_id,
                ), env=env)
                completed.append({"window": index, "arm": arm})
                write_json(record_path, status)

        report = selection_experiment_compare.compare_artifact_windows(
            manifest=manifest,
            window_dirs=[
                root / "windows" / str(index)
                for index in range(4)
            ],
            seed=int(args.seed),
        )
        write_json(root / "comparison.json", report)
        status["comparison"] = str(root / "comparison.json")
        status["status"] = (
            "ready_for_review"
            if report["status"] == "ok"
            else report["status"]
        )
        status["historical_only"] = True
        status["promotion_allowed"] = False
        write_json(record_path, status)
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "ok" else 3
    except Exception:
        status["status"] = "technical_invalid"
        write_json(record_path, status)
        raise


def _compare_selection(args) -> int:
    manifest, _digest = _registered_selection_manifest(args.manifest)
    report = selection_experiment_compare.compare_artifact_windows(
        manifest=manifest,
        window_dirs=[
            args.windows_dir / str(index) for index in range(4)
        ],
        seed=int(args.seed),
    )
    write_json(args.out / "comparison.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "ok" else 3


def _compare(args) -> int:
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    arm_dirs = {
        arm: args.arms_dir / arm for arm in ("A", "B", "C", "D")
    }
    report = sector_experiment_compare.compare(
        manifest=manifest, arm_dirs=arm_dirs)
    write_json(args.out / "comparison.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "ready_for_human_review" else 3


def _verify(args) -> int:
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    errors = sector_experiment.validate(manifest)
    result = {
        "valid": not errors,
        "errors": errors,
        "manifest_hash": (
            sector_experiment.manifest_hash(manifest) if not errors else None),
    }
    write_json(args.out / "manifest_verification.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    audit = sub.add_parser("audit")
    audit.add_argument("--as-of", required=True)
    audit.add_argument("--corpus", type=Path, default=DATA_DIR)
    audit.add_argument("--sector-membership", type=Path, required=True)
    audit.add_argument("--out", type=Path, required=True)

    register_caps = sub.add_parser("register-capabilities")
    register_caps.add_argument("--capabilities", type=Path, required=True)
    register_caps.add_argument("--out", type=Path, required=True)

    register = sub.add_parser("register")
    register.add_argument("--manifest", type=Path, required=True)
    register.add_argument("--out", type=Path, required=True)

    register_selection = sub.add_parser("register-selection")
    register_selection.add_argument("--manifest", type=Path, required=True)
    register_selection.add_argument("--out", type=Path, required=True)

    freeze = sub.add_parser("freeze-directions")
    freeze.add_argument("--run-id", required=True)
    freeze.add_argument("--out-file", type=Path, required=True)

    selection_matrix = sub.add_parser("run-selection-matrix")
    selection_matrix.add_argument("--manifest", type=Path, required=True)
    selection_matrix.add_argument("--capabilities", type=Path, required=True)
    selection_matrix.add_argument(
        "--sector-membership", type=Path, required=True)
    selection_matrix.add_argument("--corpus", type=Path, default=DATA_DIR)
    selection_matrix.add_argument("--run-prefix", default=None)
    selection_matrix.add_argument("--seed", type=int, default=1)
    selection_matrix.add_argument("--out", type=Path, required=True)

    matrix = sub.add_parser("run-matrix")
    matrix.add_argument("--manifest", type=Path, required=True)
    matrix.add_argument("--capabilities", type=Path, required=True)
    matrix.add_argument("--sector-membership", type=Path, required=True)
    matrix.add_argument("--corpus", type=Path, default=DATA_DIR)
    matrix.add_argument("--window-index", type=int, required=True)
    matrix.add_argument("--run-prefix", default=None)
    matrix.add_argument("--out", type=Path, required=True)

    compare_selection = sub.add_parser("compare-selection")
    compare_selection.add_argument("--manifest", type=Path, required=True)
    compare_selection.add_argument("--windows-dir", type=Path, required=True)
    compare_selection.add_argument("--seed", type=int, default=1)
    compare_selection.add_argument("--out", type=Path, required=True)

    compare = sub.add_parser("compare")
    compare.add_argument("--manifest", type=Path, required=True)
    compare.add_argument("--arms-dir", type=Path, required=True)
    compare.add_argument("--out", type=Path, required=True)

    verify = sub.add_parser("verify")
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--out", type=Path, required=True)

    args = parser.parse_args(argv)
    if args.cmd == "audit":
        return _audit(args)
    if args.cmd == "register-capabilities":
        return _register_capabilities(args)
    if args.cmd == "register":
        return _register(args)
    if args.cmd == "register-selection":
        return _register_selection(args)
    if args.cmd == "freeze-directions":
        return _freeze_directions(args)
    if args.cmd == "run-selection-matrix":
        return _run_selection_matrix(args)
    if args.cmd == "run-matrix":
        return _run_matrix(args)
    if args.cmd == "compare-selection":
        return _compare_selection(args)
    if args.cmd == "compare":
        return _compare(args)
    return _verify(args)


if __name__ == "__main__":
    raise SystemExit(main())
