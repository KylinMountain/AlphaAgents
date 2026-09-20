"""Closed preregistration contract for minimal selection experiments.

This is deliberately separate from the legacy A/B/C/D contract. A new question
gets a new identity instead of silently changing what an old arm means.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


SCHEMA_VERSION = 1
FAMILY = "nf_discovery_v1"
PRIMARY_METRIC = "portfolio_net_return_pp"
PRIMARY_METRIC_UNIT = "percentage_points"

ARMS = {
    "CONTROL": {
        "architecture": "dual_rank_price_v1",
        "discovery": "whole_market_price_rank",
        "stock_selection": "transparent_prefix_v1",
        "planner": "shared_t1_planner_v1",
        "fund_flow_visible": False,
        "news_visible": False,
        "tools_visible": False,
        "learning_input": "frozen",
    },
    "SECTOR": {
        "architecture": "sector_rank_price_v1",
        "discovery": "transparent_sector_then_stock",
        "stock_selection": "transparent_prefix_v1",
        "planner": "shared_t1_planner_v1",
        "fund_flow_visible": False,
        "news_visible": False,
        "tools_visible": False,
        "learning_input": "frozen",
    },
}

DECISION_KEYS = frozenset({
    "trader", "picks_per_day", "panel_size", "participation",
    "max_turns_per_decision", "model_timeout_seconds", "pace_seconds",
    "news_limit", "trader_tools_enabled", "direction_limit",
    "learning_input", "run_theme",
})

RISK_KEYS = (
    "max_drawdown_pct",
    "max_tail_loss_pct",
    "max_turnover_ratio",
    "max_theme_cluster_exposure_pct",
)


class SelectionExperimentError(ValueError):
    pass


def _dump(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False)


def manifest_hash(manifest: dict) -> str:
    return hashlib.sha256(_dump(manifest).encode("utf-8")).hexdigest()


def template(*, capabilities_hash: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "family": FAMILY,
        "capabilities_hash": capabilities_hash,
        "arms": ARMS,
        "baseline_identity": {
            "code_ref": None,
            "policy_ref": None,
            "input_hash": None,
        },
        "decision_config": None,
        "model": None,
        "cost_model": None,
        "exit_policy": None,
        "training_window": {"start": None, "end": None},
        "validation_windows": [],
        "expected_days_per_window": 30,
        "minimum_total_days": 120,
        "primary_metric": PRIMARY_METRIC,
        "primary_metric_unit": PRIMARY_METRIC_UNIT,
        "minimum_meaningful_improvement_pp": None,
        "risk_boundaries": {key: None for key in RISK_KEYS},
        "block_method": {
            "method": "moving_block",
            "block_length_days": None,
            "repetitions": None,
            "cross_window_blocks": False,
        },
        "stopping_rule": {
            "validation_windows": 4,
            "then": "freeze",
        },
    }


def validate(manifest: dict) -> list[str]:
    errors: list[str] = []
    if manifest.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if manifest.get("family") != FAMILY:
        errors.append(f"family must be {FAMILY}")
    if manifest.get("arms") != ARMS:
        errors.append("arms must match the closed no-flow discovery contract")
    if manifest.get("primary_metric") != PRIMARY_METRIC:
        errors.append(f"primary_metric must be {PRIMARY_METRIC}")
    if manifest.get("primary_metric_unit") != PRIMARY_METRIC_UNIT:
        errors.append(
            f"primary_metric_unit must be {PRIMARY_METRIC_UNIT}")

    for field in (
            "capabilities_hash", "decision_config", "model",
            "cost_model", "exit_policy"):
        if not manifest.get(field):
            errors.append(f"{field} is required")

    decision = manifest.get("decision_config") or {}
    if set(decision) != DECISION_KEYS:
        errors.append(
            "decision_config keys must equal the closed no-flow contract")
    else:
        if decision.get("news_limit") != 0:
            errors.append("decision_config.news_limit must be 0")
        if decision.get("trader_tools_enabled") is not False:
            errors.append(
                "decision_config.trader_tools_enabled must be false")
        if decision.get("learning_input") != "frozen":
            errors.append("decision_config.learning_input must be frozen")
        if decision.get("direction_limit") != 3:
            errors.append("decision_config.direction_limit must be 3")

    identity = manifest.get("baseline_identity") or {}
    for key in ("code_ref", "policy_ref", "input_hash"):
        if not str(identity.get(key) or "").strip():
            errors.append(f"baseline_identity.{key} is required")

    training = manifest.get("training_window") or {}
    if not training.get("start") or not training.get("end"):
        errors.append("training_window.start/end are required")

    windows = manifest.get("validation_windows")
    if not isinstance(windows, list) or len(windows) != 4:
        errors.append("validation_windows must contain exactly 4 windows")
    else:
        seen = set()
        last_end = None
        for index, window in enumerate(windows):
            start = str(window.get("start") or "")[:10]
            end = str(window.get("end") or "")[:10]
            if not start or not end:
                errors.append(
                    f"validation_windows[{index}].start/end are required")
                continue
            key = (start, end)
            if key in seen:
                errors.append("validation windows must be unique")
            seen.add(key)
            if start > end:
                errors.append(
                    f"validation_windows[{index}] start must be <= end")
            if last_end is not None and start <= last_end:
                errors.append("validation windows must be non-overlapping")
            last_end = end

    expected_per_window = manifest.get("expected_days_per_window")
    if expected_per_window != 30:
        errors.append("expected_days_per_window must be 30")

    minimum_days = manifest.get("minimum_total_days")
    if not isinstance(minimum_days, int) or isinstance(minimum_days, bool) or (
            minimum_days < 120):
        errors.append("minimum_total_days must be an integer >= 120")

    floor = manifest.get("minimum_meaningful_improvement_pp")
    if not isinstance(floor, (int, float)) or isinstance(floor, bool):
        errors.append(
            "minimum_meaningful_improvement_pp must be numeric")
    elif floor < 0:
        errors.append(
            "minimum_meaningful_improvement_pp must be >= 0")

    risk = manifest.get("risk_boundaries") or {}
    for key in RISK_KEYS:
        if not isinstance(risk.get(key), (int, float)) or isinstance(
                risk.get(key), bool):
            errors.append(f"risk_boundaries.{key} must be numeric")

    block = manifest.get("block_method") or {}
    if block.get("method") != "moving_block":
        errors.append("block_method.method must be moving_block")
    if block.get("cross_window_blocks") is not False:
        errors.append("block_method.cross_window_blocks must be false")
    for key in ("block_length_days", "repetitions"):
        value = block.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            errors.append(f"block_method.{key} must be positive integer")

    stop = manifest.get("stopping_rule") or {}
    if stop != {"validation_windows": 4, "then": "freeze"}:
        errors.append("stopping_rule must freeze after 4 validation windows")
    return errors


def capability_errors(report: dict) -> list[str]:
    """Closed data requirements for the source-closed no-flow protocol.

    Fund flow, free news and event text are intentionally absent: requiring
    them would make an ablated source a hidden prerequisite.
    """
    errors: list[str] = []
    capabilities = report.get("capabilities") or {}

    price = capabilities.get("daily_price") or {}
    if price.get("status") != "available":
        errors.append("daily_price capability must be available")

    security = capabilities.get("security_status") or {}
    if security.get("status") != "available":
        errors.append("security_status capability must be available")
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


def require_valid(manifest: dict) -> str:
    errors = validate(manifest)
    if errors:
        raise SelectionExperimentError(
            "invalid selection experiment manifest: " + "; ".join(errors))
    return manifest_hash(manifest)


def register(manifest: dict, root: Path) -> Path:
    digest = require_valid(manifest)
    directory = Path(root) / "selection-manifests"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{digest}.json"
    payload = _dump(manifest) + "\n"
    try:
        with target.open("x", encoding="utf-8") as fh:
            fh.write(payload)
    except FileExistsError:
        existing = json.loads(target.read_text(encoding="utf-8"))
        if existing != manifest or manifest_hash(existing) != digest:
            raise SelectionExperimentError(
                f"registered manifest collision at {target}")
    return target
