"""Content-addressed identity of the data/policy world a formal replay consumes."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess

from alpha_agents.data import sector_membership, sector_source_probe


SCHEMA_VERSION = 1
READER_VERSION = 1
CORPUS_FILES = ("market_history.db", "market_snapshots.db", "stocks.db")


class WorldContractError(ValueError):
    pass


def _dump(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False)


def _hash(value) -> str:
    return hashlib.sha256(_dump(value).encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    """Hash one frozen file and refuse if it mutates while being read."""
    path = Path(path)
    if not path.exists():
        raise WorldContractError(f"world input is missing: {path}")
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (
            after.st_size, after.st_mtime_ns):
        raise WorldContractError(f"world input changed while hashing: {path}")
    return digest.hexdigest()


def code_ref(repo_root: Path) -> str:
    override = os.environ.get("ALPHAAGENTS_CODE_REF_OVERRIDE")
    if override:
        # Test/packaging escape hatch. Formal CLI refuses this env var.
        return override
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(repo_root), text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise WorldContractError("cannot resolve runtime git code ref") from exc


def live_policy_ref() -> str:
    """Hash the actual live policy sources, independent of replay memory."""
    from alpha_agents.data import policy_registry
    from alpha_agents.evolution import policy_sources

    sources = policy_sources.collect()
    return "live@" + policy_registry.content_hash_for(sources)


def _membership(path: Path) -> dict:
    archive = sector_membership.load(Path(path))
    return {
        "path_name": Path(path).name,
        "file_hash": file_hash(path),
        "snapshots": [
            {
                "snapshot_id": row.snapshot_id,
                "available_at": row.available_at,
                "content_hash": row.content_hash,
                "point_in_time": row.point_in_time,
            }
            for row in archive
        ],
    }


def _corpus(data_dir: Path) -> dict:
    out = {}
    for name in CORPUS_FILES:
        path = Path(data_dir) / name
        out[name] = {
            "sha256": file_hash(path),
            "size": path.stat().st_size,
        }
    return out


def build(*, repo_root: Path, corpus_dir: Path, membership_path: Path,
          capabilities: dict, capability_review: dict,
          policy_ref_value: str | None = None) -> dict:
    report_digest = sector_source_probe.report_hash(capabilities)
    if capabilities.get("content_hash") != report_digest:
        raise WorldContractError("capability report hash mismatch")
    try:
        review_digest = sector_source_probe.require_review(
            capability_review, report_hash_value=report_digest)
    except sector_source_probe.CapabilityError as exc:
        raise WorldContractError(str(exc)) from exc

    policy = policy_ref_value or live_policy_ref()
    inputs = {
        "reader_version": READER_VERSION,
        "corpus": _corpus(corpus_dir),
        "membership": _membership(membership_path),
        "capabilities_hash": report_digest,
        "capability_review_hash": review_digest,
    }
    contract = {
        "schema_version": SCHEMA_VERSION,
        "code_ref": code_ref(repo_root),
        "policy_ref": policy,
        "input_hash": _hash(inputs),
        "inputs": inputs,
    }
    contract["world_hash"] = _hash(contract)
    return contract


def contract_hash(contract: dict) -> str:
    body = {key: value for key, value in contract.items()
            if key != "world_hash"}
    return _hash(body)


def formal_errors(contract: dict, capabilities: dict) -> list[str]:
    errors = []
    if contract.get("schema_version") != SCHEMA_VERSION:
        errors.append("world schema version mismatch")
    if contract.get("world_hash") != contract_hash(contract):
        errors.append("world hash mismatch")
    if not str(contract.get("code_ref") or "").strip():
        errors.append("world code_ref is missing")
    if not str(contract.get("policy_ref") or "").strip():
        errors.append("world policy_ref is missing")
    if not str(contract.get("input_hash") or "").strip():
        errors.append("world input_hash is missing")

    status = (
        (capabilities.get("capabilities") or {})
        .get("security_status_history") or {}
    )
    if status.get("point_in_time_grade") != "A":
        errors.append("historical security status is not PIT grade A")
    if status.get("strict_replay_eligible") is not True:
        errors.append("historical security status is not strict replay eligible")

    snapshots = (
        ((contract.get("inputs") or {}).get("membership") or {})
        .get("snapshots") or []
    )
    if not snapshots:
        errors.append("world membership archive is empty")
    elif any(row.get("point_in_time") is not True for row in snapshots):
        errors.append("world membership contains non-PIT snapshots")
    return errors


def require_formal(contract: dict, capabilities: dict) -> str:
    errors = formal_errors(contract, capabilities)
    if errors:
        raise WorldContractError(
            "formal world contract failed: " + "; ".join(errors))
    return str(contract["world_hash"])


def register(contract: dict, root: Path, *, capabilities: dict) -> Path:
    require_formal(contract, capabilities)
    directory = Path(root) / "worlds"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{contract['world_hash']}.json"
    payload = _dump(contract) + "\n"
    try:
        with target.open("x", encoding="utf-8") as fh:
            fh.write(payload)
    except FileExistsError:
        existing = json.loads(target.read_text(encoding="utf-8"))
        if existing != contract:
            raise WorldContractError(f"world contract collision at {target}")
    return target


def verify_runtime(contract: dict, *, repo_root: Path, corpus_dir: Path,
                   membership_path: Path, capabilities: dict,
                   capability_review: dict) -> str:
    """Recompute the actual world instead of trusting the registered JSON."""
    expected = require_formal(contract, capabilities)
    actual = build(
        repo_root=repo_root,
        corpus_dir=corpus_dir,
        membership_path=membership_path,
        capabilities=capabilities,
        capability_review=capability_review,
    )
    require_formal(actual, capabilities)
    if actual["world_hash"] != expected:
        fields = [
            key for key in ("code_ref", "policy_ref", "input_hash")
            if actual.get(key) != contract.get(key)
        ]
        raise WorldContractError(
            "runtime world differs from registered world: "
            + ", ".join(fields or ["world_hash"]))
    return expected


def bind_manifest(contract: dict, manifest: dict) -> None:
    identity = manifest.get("baseline_identity") or {}
    mismatches = []
    for key in ("code_ref", "policy_ref", "input_hash"):
        if identity.get(key) != contract.get(key):
            mismatches.append(
                f"{key}: manifest={identity.get(key)!r}, "
                f"world={contract.get(key)!r}")
    if mismatches:
        raise WorldContractError(
            "manifest identity does not match runtime world: "
            + "; ".join(mismatches))
