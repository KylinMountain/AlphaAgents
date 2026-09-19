#!/usr/bin/env python3
"""Audit and verify sector-first experiment inputs.

Audit is read-only:
  uv run python scripts/sector_first.py audit --as-of 2026-09-19 --out /tmp/sf

Verify refuses incomplete preregistration:
  uv run python scripts/sector_first.py verify \
      --manifest /tmp/sf/experiment_manifest.json --out /tmp/sf
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from alpha_agents.config import DATA_DIR  # noqa: E402
from alpha_agents.data import frozen_direction_archive  # noqa: E402
from alpha_agents.data import sector_source_probe  # noqa: E402
from alpha_agents.evolution import sector_experiment  # noqa: E402
from alpha_agents.evolution import sector_experiment_compare  # noqa: E402


def _dump(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False)


def _hash(value) -> str:
    return hashlib.sha256(_dump(value).encode("utf-8")).hexdigest()


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8")


def _audit(args) -> int:
    capabilities = sector_source_probe.probe_all(DATA_DIR)
    report = {
        "as_of": args.as_of,
        "data_dir": str(DATA_DIR),
        "capabilities": capabilities,
    }
    report["content_hash"] = _hash(report)

    out = args.out
    _write(out / "capabilities.json", report)
    manifest = sector_experiment.template(
        capabilities_hash=report["content_hash"])
    _write(out / "experiment_manifest.json", manifest)

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


def _compare(args) -> int:
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    arm_dirs = {
        arm: args.arms_dir / arm for arm in ("A", "B", "C", "D")
    }
    report = sector_experiment_compare.compare(
        manifest=manifest, arm_dirs=arm_dirs)
    _write(args.out / "comparison.json", report)
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
    _write(args.out / "manifest_verification.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    audit = sub.add_parser("audit")
    audit.add_argument("--as-of", required=True)
    audit.add_argument("--out", type=Path, required=True)

    freeze = sub.add_parser("freeze-directions")
    freeze.add_argument("--run-id", required=True)
    freeze.add_argument("--out-file", type=Path, required=True)

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
    if args.cmd == "freeze-directions":
        return _freeze_directions(args)
    if args.cmd == "compare":
        return _compare(args)
    return _verify(args)


if __name__ == "__main__":
    raise SystemExit(main())
