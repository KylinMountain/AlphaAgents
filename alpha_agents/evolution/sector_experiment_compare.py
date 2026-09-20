"""Fail-closed comparison of preregistered sector-first experiment arms.

The comparator consumes completed replay artifacts. It does not run a model,
change a policy pointer or infer missing risk evidence.

Statistical uncertainty is computed on aligned *trading-day* returns using a
moving-block bootstrap. Adding more stocks to one day therefore does not
pretend to add independent samples.
"""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
import random
import statistics

from alpha_agents.evolution import performance, sector_experiment


class SectorCompareError(ValueError):
    pass


_ARM_ARCHITECTURES = {
    "A": "dual_rank_v0",
    "B": "sector_first_v0",
    "C": "sector_first_simple_selector",
    "D": "sector_first_no_flow",
}


def _read_json(path: Path) -> dict:
    if not path.exists():
        raise SectorCompareError(f"missing artifact: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        raise SectorCompareError(f"missing artifact: {path}")
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _float(value) -> float:
    if value in {None, ""}:
        raise SectorCompareError("numeric artifact value is missing")
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise SectorCompareError(
            f"numeric artifact value is invalid: {value!r}") from exc
    if not math.isfinite(out):
        raise SectorCompareError("numeric artifact value must be finite")
    return out


def _theme_exposure(run_dir: Path) -> float | None:
    path = run_dir / "theme_exposure.csv"
    if not path.exists():
        return None
    rows = _read_csv(path)
    if not rows:
        return None
    complete = {
        str(row.get("complete") or "").strip().lower()
        for row in rows
    }
    if not complete.issubset({"1", "true"}):
        return None
    values = [
        _float(row.get("max_theme_cluster_exposure_pct"))
        for row in rows
        if row.get("max_theme_cluster_exposure_pct") not in {None, ""}
    ]
    return max(values) if values else None


def load_arm(run_dir: Path, arm: str) -> dict:
    if arm not in _ARM_ARCHITECTURES:
        raise SectorCompareError(f"unknown arm {arm!r}")
    meta = _read_json(run_dir / "run.json")
    equity = _read_csv(run_dir / "equity.csv")
    fills = _read_csv(run_dir / "fills.csv")
    if not equity:
        raise SectorCompareError(f"arm {arm} has no equity rows")

    expected = _ARM_ARCHITECTURES[arm]
    actual = str(meta.get("selection_architecture") or "")
    if actual != expected:
        raise SectorCompareError(
            f"arm {arm} expected {expected}, got {actual or '<missing>'}")

    initial_account = meta.get("initial_account")
    if not isinstance(initial_account, dict):
        raise SectorCompareError(
            f"arm {arm} is missing the explicit initial_account mark")
    if initial_account.get("kind") != "initial_mark":
        raise SectorCompareError(
            f"arm {arm} initial_account.kind must be 'initial_mark'")
    try:
        metrics = performance.equity_metrics(
            initial_account.get("equity"), equity)
    except performance.PerformanceError as exc:
        raise SectorCompareError(
            f"arm {arm} has invalid equity history: {exc}") from exc

    window = meta.get("window") or {}
    try:
        declared_days = int(window.get("trading_days"))
    except (TypeError, ValueError) as exc:
        raise SectorCompareError(
            f"arm {arm} window.trading_days is missing or invalid") from exc
    if declared_days != metrics["trading_days"]:
        raise SectorCompareError(
            f"arm {arm} declares {declared_days} trading days but has "
            f"{metrics['trading_days']} unique ordered equity days")

    returns = metrics["daily_returns_pct"]
    traded = sum(abs(_float(row.get("amount"))) for row in fills)
    average_equity = metrics["average_daily_equity"]
    return {
        "arm": arm,
        "architecture": actual,
        "experiment_arm": meta.get("experiment_arm"),
        "experiment_manifest_hash": meta.get("experiment_manifest_hash"),
        "frozen_directions_hash": meta.get("frozen_directions_hash"),
        "window": window,
        "initial_account": initial_account,
        "observed_days": metrics["days"],
        "run_id": meta.get("run_id"),
        "errors": meta.get("errors") or [],
        "model_usage_ok": bool(meta.get("model_usage_ok")),
        "agent_tool_calls": meta.get("agent_tool_calls"),
        "model_calls_made": meta.get("model_calls_made"),
        "decider_counters": meta.get("decider_counters") or {},
        "capability_matrix": meta.get("capability_matrix") or {},
        "daily_returns_pct": returns,
        "portfolio": {
            "initial_equity": round(metrics["initial_equity"], 6),
            "final_equity": round(metrics["final_equity"], 6),
            "net_return_pct": round(metrics["net_return_pct"], 6),
            "max_drawdown_pct": round(metrics["max_drawdown_pct"], 6),
            "worst_day_return_pct": round(
                metrics["worst_day_return_pct"], 6),
            "turnover_ratio": (
                round(traded / average_equity, 6)
                if average_equity > 0 else None),
            "buy_fills": sum(
                str(row.get("side") or "").lower() == "buy" for row in fills),
            "sell_fills": sum(
                str(row.get("side") or "").lower() == "sell" for row in fills),
            "max_theme_cluster_exposure_pct": _theme_exposure(run_dir),
        },
        "layers": {
            "direction": (
                _read_json(run_dir / "direction_report.json")
                if (run_dir / "direction_report.json").exists() else None),
            "stock": (
                _read_json(run_dir / "stock_report.json")
                if (run_dir / "stock_report.json").exists() else None),
            "execution": {
                "fills": len(fills),
                "errors": len(meta.get("errors") or []),
            },
        },
    }


def _registered_window(manifest: dict, actual: dict) -> bool:
    start = str(actual.get("start") or "")[:10]
    end = str(actual.get("end") or "")[:10]
    return any(
        start == str(window.get("start") or "")[:10]
        and end == str(window.get("end") or "")[:10]
        for window in manifest.get("validation_windows") or []
    )


def _aligned(a: dict[str, float], b: dict[str, float]) -> list[tuple[str, float]]:
    days = sorted(set(a) & set(b))
    return [(day, b[day] - a[day]) for day in days]


def _moving_block_ci(values: list[float], *, block: int,
                     repetitions: int, seed: int) -> dict:
    n = len(values)
    if n < block or block <= 0 or repetitions <= 0:
        return {
            "n_days": n,
            "mean_delta_pct": (
                round(sum(values) / n, 6) if values else None),
            "ci95": None,
            "status": "insufficient",
        }

    starts = list(range(0, n - block + 1))
    rng = random.Random(seed)
    samples = []
    blocks_needed = (n + block - 1) // block
    for _ in range(repetitions):
        sample = []
        for _ in range(blocks_needed):
            start = rng.choice(starts)
            sample.extend(values[start:start + block])
        sample = sample[:n]
        samples.append(sum(sample) / len(sample))

    ordered = sorted(samples)
    lo = ordered[int(0.025 * (len(ordered) - 1))]
    hi = ordered[int(0.975 * (len(ordered) - 1))]
    return {
        "n_days": n,
        "mean_delta_pct": round(sum(values) / n, 6),
        "median_delta_pct": round(float(statistics.median(values)), 6),
        "ci95": [round(lo, 6), round(hi, 6)],
        "status": "ok",
    }


def _risk_check(arm: dict, manifest: dict) -> dict:
    risk = manifest["risk_boundaries"]
    p = arm["portfolio"]
    checks = {
        "max_drawdown_pct": (
            p["max_drawdown_pct"] <= float(risk["max_drawdown_pct"])),
        "max_tail_loss_pct": (
            p["worst_day_return_pct"] is not None
            and abs(min(0.0, p["worst_day_return_pct"]))
            <= float(risk["max_tail_loss_pct"])),
        "max_turnover_ratio": (
            p["turnover_ratio"] is not None
            and p["turnover_ratio"] <= float(risk["max_turnover_ratio"])),
        "max_theme_cluster_exposure_pct": (
            None if p["max_theme_cluster_exposure_pct"] is None
            else p["max_theme_cluster_exposure_pct"]
            <= float(risk["max_theme_cluster_exposure_pct"])),
    }
    return {
        "checks": checks,
        "passed": all(value is True for value in checks.values()),
        "unverified": [
            key for key, value in checks.items() if value is None],
    }


def compare(*, manifest: dict, arm_dirs: dict[str, Path]) -> dict:
    manifest_hash = sector_experiment.require_valid(manifest)
    missing = sorted(set(_ARM_ARCHITECTURES) - set(arm_dirs))
    if missing:
        raise SectorCompareError(f"missing arm directories: {missing}")

    arms = {
        arm: load_arm(Path(arm_dirs[arm]), arm)
        for arm in sorted(_ARM_ARCHITECTURES)
    }

    binding_errors = {}
    for arm, value in arms.items():
        problems = []
        if value["experiment_arm"] != arm:
            problems.append(
                f"experiment_arm={value['experiment_arm']!r}, expected {arm}")
        if value["experiment_manifest_hash"] != manifest_hash:
            problems.append("experiment_manifest_hash mismatch")
        if arm == "C" and not value["frozen_directions_hash"]:
            problems.append("C is missing frozen_directions_hash")
        if problems:
            binding_errors[arm] = problems
    if binding_errors:
        raise SectorCompareError(
            "experiment binding mismatch: " + json.dumps(
                binding_errors, ensure_ascii=False, sort_keys=True))

    windows = {
        (str(value["window"].get("start")),
         str(value["window"].get("end")))
        for value in arms.values()
    }
    if len(windows) != 1:
        raise SectorCompareError("A/B/C/D do not share one validation window")
    observed_calendars = {
        tuple(value["observed_days"]) for value in arms.values()
    }
    if len(observed_calendars) != 1:
        raise SectorCompareError(
            "A/B/C/D do not share the same observed trading-day calendar")
    actual_window = next(iter(arms.values()))["window"]
    if not _registered_window(manifest, actual_window):
        raise SectorCompareError(
            "run window is not one of the preregistered validation windows")

    training_end = str(
        (manifest.get("training_window") or {}).get("end") or "")[:10]
    actual_start = str(actual_window.get("start") or "")[:10]
    if training_end and actual_start <= training_end:
        raise SectorCompareError(
            "validation window overlaps or precedes the training window")

    bad_runs = {
        arm: {
            "errors": len(value["errors"]),
            "model_usage_ok": value["model_usage_ok"],
        }
        for arm, value in arms.items()
        if value["errors"] or not value["model_usage_ok"]
    }

    block = manifest["block_method"]
    seed = int(manifest_hash[:16], 16)
    pairings = {}
    for left, right in (("A", "B"), ("B", "C"), ("B", "D")):
        aligned = _aligned(
            arms[left]["daily_returns_pct"],
            arms[right]["daily_returns_pct"])
        result = _moving_block_ci(
            [value for _day, value in aligned],
            block=int(block["block_length_days"]),
            repetitions=int(block["repetitions"]),
            seed=seed + ord(left) + ord(right),
        )
        result["expected_trading_days"] = int(
            arms[left]["window"]["trading_days"])
        result["left_observed_days"] = len(arms[left]["observed_days"])
        result["right_observed_days"] = len(arms[right]["observed_days"])
        result["common_trading_days"] = len(aligned)
        floor = float(manifest["minimum_meaningful_improvement_pct"])
        ci = result.get("ci95")
        if result["status"] != "ok" or ci is None:
            evidence = "insufficient"
        elif ci[0] > floor:
            evidence = "positive_beyond_floor"
        elif ci[1] < -floor:
            evidence = "negative_beyond_floor"
        else:
            evidence = "inconclusive"
        result["evidence"] = evidence
        result["minimum_meaningful_improvement_pct"] = floor
        pairings[f"{right}_minus_{left}"] = result

    risk = {arm: _risk_check(value, manifest) for arm, value in arms.items()}
    required_layers = {
        "A": ("stock",),
        "B": ("direction", "stock"),
        "C": ("direction", "stock"),
        "D": ("direction", "stock"),
    }
    missing_layers = {
        arm: [
            layer for layer in required_layers[arm]
            if value["layers"].get(layer) is None
        ]
        for arm, value in arms.items()
    }

    minimum_days = int(manifest["minimum_days"])
    observed_days = len(next(iter(arms.values()))["observed_days"])
    execution_status = {
        arm: (
            "has_fills"
            if value["portfolio"]["buy_fills"] + value["portfolio"]["sell_fills"]
            else "no_fills"
        )
        for arm, value in arms.items()
    }
    statistical_status = {
        name: value["evidence"] for name, value in pairings.items()
    }
    data_status = {
        arm: (
            "incomplete"
            if missing_layers[arm] or risk[arm]["unverified"]
            else "complete"
        )
        for arm in arms
    }

    operational_summary = {
        arm: {
            "budget": {
                "agent_tool_calls": value["agent_tool_calls"],
                "model_calls_made": value["model_calls_made"],
            },
            "decision_counters": value["decider_counters"],
            "coverage": value["capability_matrix"],
            "errors": value["errors"],
        }
        for arm, value in arms.items()
    }

    reasons = []
    if observed_days < minimum_days:
        reasons.append(
            f"validation days {observed_days} < minimum {minimum_days}")
    if bad_runs:
        reasons.append("one or more arms have model-usage errors or run errors")
    if any(not value["passed"] for value in risk.values()):
        reasons.append("one or more arms fail or cannot verify risk boundaries")
    if any(missing_layers.values()):
        reasons.append("layer reports are incomplete")
    if any(
            value["status"] == "insufficient"
            for value in pairings.values()):
        reasons.append("one or more paired comparisons lack enough days for CI")
    if all(value == "no_fills" for value in execution_status.values()):
        reasons.append("no arm produced an executable fill sample")

    return {
        "manifest_hash": manifest_hash,
        "window": actual_window,
        "primary_metric": manifest["primary_metric"],
        "evidence_scope": "historical_architecture_comparison",
        "promotion_eligible": False,
        "status": "insufficient" if reasons else "ready_for_human_review",
        "reasons": reasons,
        "arms": {
            arm: {
                "architecture": value["architecture"],
                "run_id": value["run_id"],
                "experiment_arm": value["experiment_arm"],
                "experiment_manifest_hash":
                    value["experiment_manifest_hash"],
                "portfolio": value["portfolio"],
                "budget": operational_summary[arm]["budget"],
                "decision_counters":
                    operational_summary[arm]["decision_counters"],
                "coverage": operational_summary[arm]["coverage"],
                "layers": {
                    "direction": value["layers"]["direction"],
                    "stock": value["layers"]["stock"],
                    "execution": value["layers"]["execution"],
                    "portfolio": value["portfolio"],
                },
                # Keep the two established top-level fields for callers that
                # predate the explicit four-layer comparison contract.
                "execution": value["layers"]["execution"],
                "risk": risk[arm],
                "missing_layers": missing_layers[arm],
            }
            for arm, value in arms.items()
        },
        "paired_daily": pairings,
        "operational_summary": operational_summary,
        "evidence_status": {
            "data": data_status,
            "statistics": statistical_status,
            "execution": execution_status,
        },
        "measurement_contract": {
            "direction": "forward direction labels / selection diagnostics",
            "stock": "stock preselection and planner diagnostics",
            "execution": "orders/fills/errors from the shared execution path",
            "portfolio": "ledger equity, turnover and risk after execution",
        },
        "note": (
            "No arm is declared a winner here. Historical architecture "
            "comparison remains development evidence; promotion still requires "
            "forward evidence and human approval."),
    }
