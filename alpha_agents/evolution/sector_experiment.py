"""Preregistered contract for sector-first architecture experiments."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


SCHEMA_VERSION = 2

ARMS = {
    "A": {
        "name": "dual_rank_v0",
        "direction_selection": "incumbent",
        "stock_selection": "same_trader",
        "fund_flow_visible": True,
    },
    "B": {
        "name": "sector_first_v0",
        "direction_selection": "sector_first",
        "stock_selection": "same_trader",
        "fund_flow_visible": True,
    },
    "C": {
        "name": "sector_first_simple_selector",
        "direction_selection": "frozen_from_B",
        "stock_selection": "preregistered_simple",
        "fund_flow_visible": True,
    },
    "D": {
        "name": "sector_first_no_flow",
        "direction_selection": "sector_first",
        "stock_selection": "same_trader",
        "fund_flow_visible": False,
    },
}

RISK_KEYS = (
    "max_drawdown_pct",
    "max_tail_loss_pct",
    "max_turnover_ratio",
    "max_theme_cluster_exposure_pct",
)

IDENTITY_KEYS = (
    "code_ref",
    "policy_ref",
    "input_hash",
)


class SectorExperimentError(ValueError):
    pass


def _dump(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False)


def manifest_hash(manifest: dict) -> str:
    return hashlib.sha256(_dump(manifest).encode("utf-8")).hexdigest()


def template(*, capabilities_hash: str) -> dict:
    """Return an intentionally incomplete preregistration template."""
    return {
        "schema_version": SCHEMA_VERSION,
        "capabilities_hash": capabilities_hash,
        "architecture_arms": ARMS,
        "baseline_identity": {key: None for key in IDENTITY_KEYS},
        "decision_config": None,
        "model": None,
        "research_budget": None,
        "cost_model": None,
        "exit_policy": None,
        "training_window": {"start": None, "end": None},
        "validation_windows": [],
        "forward_start": None,
        "primary_metric": None,
        "minimum_meaningful_improvement_pct": None,
        "minimum_days": 50,
        "stopping_rule": None,
        "risk_boundaries": {key: None for key in RISK_KEYS},
        "block_method": {
            "method": None,
            "block_length_days": None,
            "repetitions": None,
        },
        "notes": (
            "Fill and verify before comparing arms. A template is not a "
            "registered experiment."),
    }


def _empty(value) -> bool:
    return value is None or value == "" or value == [] or value == {}


def validate(manifest: dict) -> list[str]:
    errors = []
    if manifest.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if manifest.get("architecture_arms") != ARMS:
        errors.append("architecture_arms must match the frozen A/B/C/D contract")

    for field in (
            "capabilities_hash", "decision_config", "model",
            "research_budget", "cost_model", "exit_policy", "forward_start",
            "primary_metric", "stopping_rule"):
        if _empty(manifest.get(field)):
            errors.append(f"{field} is required")

    identity = manifest.get("baseline_identity") or {}
    for key in IDENTITY_KEYS:
        if _empty(identity.get(key)):
            errors.append(f"baseline_identity.{key} is required")

    minimum_improvement = manifest.get("minimum_meaningful_improvement_pct")
    if not isinstance(minimum_improvement, (int, float)):
        errors.append("minimum_meaningful_improvement_pct must be numeric")
    elif minimum_improvement < 0:
        errors.append(
            "minimum_meaningful_improvement_pct must be >= 0")

    training = manifest.get("training_window") or {}
    if _empty(training.get("start")) or _empty(training.get("end")):
        errors.append("training_window.start/end are required")

    windows = manifest.get("validation_windows")
    if not isinstance(windows, list) or len(windows) != 4:
        errors.append("validation_windows must contain exactly 4 windows")
    else:
        for index, window in enumerate(windows):
            if _empty(window.get("start")) or _empty(window.get("end")):
                errors.append(
                    f"validation_windows[{index}].start/end are required")

    minimum_days = manifest.get("minimum_days")
    if not isinstance(minimum_days, int) or minimum_days < 50:
        errors.append("minimum_days must be an integer >= 50")

    risk = manifest.get("risk_boundaries") or {}
    for key in RISK_KEYS:
        value = risk.get(key)
        if not isinstance(value, (int, float)):
            errors.append(f"risk_boundaries.{key} must be numeric")

    block = manifest.get("block_method") or {}
    for key in ("method", "block_length_days", "repetitions"):
        if _empty(block.get(key)):
            errors.append(f"block_method.{key} is required")
    if block.get("block_length_days") is not None:
        if not isinstance(block["block_length_days"], int) or (
                block["block_length_days"] <= 0):
            errors.append("block_method.block_length_days must be positive int")
    if block.get("repetitions") is not None:
        if not isinstance(block["repetitions"], int) or (
                block["repetitions"] <= 0):
            errors.append("block_method.repetitions must be positive int")

    return errors


def require_valid(manifest: dict) -> str:
    errors = validate(manifest)
    if errors:
        raise SectorExperimentError(
            "invalid sector experiment manifest: " + "; ".join(errors))
    return manifest_hash(manifest)


def register(manifest: dict, root: Path) -> Path:
    """Archive one validated manifest under its content hash.

    Registration is content-addressed and never overwrites an existing file.
    Re-registering byte-equivalent content is idempotent; any changed protocol
    necessarily receives a different path and hash.
    """
    digest = require_valid(manifest)
    directory = Path(root) / "manifests"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{digest}.json"
    payload = _dump(manifest) + "\n"
    try:
        with target.open("x", encoding="utf-8") as fh:
            fh.write(payload)
    except FileExistsError:
        existing = json.loads(target.read_text(encoding="utf-8"))
        if existing != manifest or manifest_hash(existing) != digest:
            raise SectorExperimentError(
                f"registered manifest collision at {target}")
    return target
