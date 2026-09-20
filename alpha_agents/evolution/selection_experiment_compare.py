"""Comparator for the minimal no-flow discovery experiment.

The primary statistic is the mean difference in 30-trading-day portfolio net
return across four preregistered reset windows, measured in percentage points.
Bootstrap samples paired daily-return blocks *inside each window*, recomputes
each compounded window return, then recomputes the same four-window statistic.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
import random
import statistics

from alpha_agents.evolution import performance, selection_experiment


class SelectionCompareError(ValueError):
    pass


def compound_return_pct(values: list[float]) -> float:
    wealth = 1.0
    for value in values:
        wealth *= 1.0 + float(value) / 100.0
    return (wealth - 1.0) * 100.0


def window_delta_pp(control: list[float], sector: list[float]) -> float:
    if len(control) != len(sector) or not control:
        raise SelectionCompareError(
            "paired window returns must be non-empty and equal length")
    return compound_return_pct(sector) - compound_return_pct(control)


def _sample_indices(n: int, *, block: int, rng: random.Random) -> list[int]:
    if block <= 0 or n < block:
        raise SelectionCompareError(
            "block length must be positive and <= window days")
    starts = list(range(n - block + 1))
    out: list[int] = []
    while len(out) < n:
        start = rng.choice(starts)
        out.extend(range(start, min(start + block, n)))
    return out[:n]


def compare_windows(*, windows: list[dict], block_length_days: int,
                    repetitions: int, seed: int,
                    minimum_total_days: int,
                    minimum_meaningful_improvement_pp: float) -> dict:
    """Compare four reset windows without crossing reset boundaries."""
    if len(windows) != 4:
        raise SelectionCompareError("exactly four validation windows required")
    if repetitions <= 0:
        raise SelectionCompareError("repetitions must be positive")

    observed_deltas: list[float] = []
    total_days = 0
    normalized = []
    all_days = set()

    for index, window in enumerate(windows):
        control = list(window.get("control_daily_returns_pct") or [])
        sector = list(window.get("sector_daily_returns_pct") or [])
        days = list(window.get("days") or [])
        if len(days) != len(control) or len(days) != len(sector):
            raise SelectionCompareError(
                f"window {index} day/return lengths differ")
        if len(days) != 30:
            raise SelectionCompareError(
                f"window {index} must contain exactly 30 trading days")
        if len(set(days)) != len(days):
            raise SelectionCompareError(
                f"window {index} contains duplicate trading days")
        if any(day in all_days for day in days):
            raise SelectionCompareError(
                "validation windows overlap in observed trading days")
        all_days.update(days)
        total_days += len(days)
        observed_deltas.append(window_delta_pp(control, sector))
        normalized.append((control, sector))

    if total_days < minimum_total_days:
        return {
            "status": "insufficient_data",
            "total_unique_days": total_days,
            "window_delta_pp": observed_deltas,
            "mean_delta_pp": statistics.mean(observed_deltas),
            "ci95_pp": None,
            "evidence": "insufficient",
        }

    rng = random.Random(seed)
    boot = []
    for _ in range(repetitions):
        deltas = []
        for control, sector in normalized:
            indices = _sample_indices(
                len(control), block=block_length_days, rng=rng)
            c = [control[i] for i in indices]
            s = [sector[i] for i in indices]
            deltas.append(window_delta_pp(c, s))
        boot.append(statistics.mean(deltas))

    ordered = sorted(boot)
    lo = ordered[int(0.025 * (len(ordered) - 1))]
    hi = ordered[int(0.975 * (len(ordered) - 1))]
    observed = statistics.mean(observed_deltas)
    floor = float(minimum_meaningful_improvement_pp)

    if lo > floor:
        evidence = "positive_beyond_floor"
    elif hi < -floor:
        evidence = "negative_beyond_floor"
    else:
        evidence = "inconclusive"

    return {
        "status": "ok",
        "total_unique_days": total_days,
        "window_delta_pp": [round(value, 6) for value in observed_deltas],
        "mean_delta_pp": round(observed, 6),
        "median_window_delta_pp": round(
            float(statistics.median(observed_deltas)), 6),
        "ci95_pp": [round(lo, 6), round(hi, 6)],
        "minimum_meaningful_improvement_pp": floor,
        "evidence": evidence,
        "unit": "percentage_points",
        "bootstrap": {
            "method": "paired_moving_block_within_window",
            "block_length_days": block_length_days,
            "repetitions": repetitions,
            "cross_window_blocks": False,
        },
    }


_ARM_ARCHITECTURES = {
    "CONTROL": "dual_rank_price_v1",
    "SECTOR": "sector_rank_price_v1",
}


def _read_json(path: Path) -> dict:
    if not path.exists():
        raise SelectionCompareError(f"missing artifact: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        raise SelectionCompareError(f"missing artifact: {path}")
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def load_run(run_dir: Path, *, arm: str, manifest: dict) -> dict:
    """Load one formal no-flow run and re-prove its identity."""
    if arm not in _ARM_ARCHITECTURES:
        raise SelectionCompareError(f"unknown selection arm {arm!r}")
    digest = selection_experiment.require_valid(manifest)
    meta = _read_json(Path(run_dir) / "run.json")
    equity = _read_csv(Path(run_dir) / "equity.csv")

    expected_arch = _ARM_ARCHITECTURES[arm]
    if meta.get("selection_architecture") != expected_arch:
        raise SelectionCompareError(
            f"{arm} expected {expected_arch}, got "
            f"{meta.get('selection_architecture')!r}")
    if meta.get("experiment_arm") != arm:
        raise SelectionCompareError(
            f"{arm} artifact experiment_arm mismatch")
    if meta.get("experiment_manifest_hash") != digest:
        raise SelectionCompareError(
            f"{arm} experiment_manifest_hash mismatch")

    identity = manifest.get("baseline_identity") or {}
    if meta.get("code_ref") != identity.get("code_ref"):
        raise SelectionCompareError(f"{arm} code_ref mismatch")
    if meta.get("policy_ref") != identity.get("policy_ref"):
        raise SelectionCompareError(f"{arm} policy_ref mismatch")
    if (meta.get("input_identity") or {}).get(
            "input_hash") != identity.get("input_hash"):
        raise SelectionCompareError(f"{arm} input_hash mismatch")

    world_hashes = list(meta.get("world_read_set_hashes") or [])
    if not world_hashes:
        raise SelectionCompareError(
            f"{arm} missing world_read_set_hashes")
    if meta.get("errors"):
        raise SelectionCompareError(
            f"{arm} contains technical run errors")

    initial = meta.get("initial_account")
    if not isinstance(initial, dict) or initial.get("kind") != "initial_mark":
        raise SelectionCompareError(
            f"{arm} missing explicit initial_account mark")
    try:
        metrics = performance.equity_metrics(
            initial.get("equity"), equity)
    except performance.PerformanceError as exc:
        raise SelectionCompareError(
            f"{arm} invalid equity history: {exc}") from exc

    declared = meta.get("window") or {}
    try:
        declared_days = int(declared.get("trading_days"))
    except (TypeError, ValueError) as exc:
        raise SelectionCompareError(
            f"{arm} invalid window.trading_days") from exc
    expected_days = int(manifest.get("expected_days_per_window") or 0)
    if declared_days != expected_days or metrics["trading_days"] != expected_days:
        raise SelectionCompareError(
            f"{arm} must contain exactly {expected_days} trading days")

    return {
        "arm": arm,
        "run_id": meta.get("run_id"),
        "days": metrics["days"],
        "daily_returns_pct": [
            metrics["daily_returns_pct"][day] for day in metrics["days"]
        ],
        "net_return_pct": metrics["net_return_pct"],
        "max_drawdown_pct": metrics["max_drawdown_pct"],
        "world_read_set_hashes": world_hashes,
        "window": declared,
    }


def compare_artifact_windows(*, manifest: dict,
                             window_dirs: list[Path],
                             seed: int = 1) -> dict:
    """Load four reset windows from disk and compare their two formal arms."""
    selection_experiment.require_valid(manifest)
    if len(window_dirs) != 4:
        raise SelectionCompareError(
            "exactly four window directories are required")

    windows = []
    evidence = []
    for index, root in enumerate(window_dirs):
        control = load_run(root / "CONTROL", arm="CONTROL", manifest=manifest)
        sector = load_run(root / "SECTOR", arm="SECTOR", manifest=manifest)
        if control["days"] != sector["days"]:
            raise SelectionCompareError(
                f"window {index} CONTROL/SECTOR calendars differ")
        registered = manifest["validation_windows"][index]
        expected = {
            "start": str(registered.get("start") or "")[:10],
            "end": str(registered.get("end") or "")[:10],
        }
        actual = {
            "start": control["days"][0],
            "end": control["days"][-1],
        }
        if actual != expected:
            raise SelectionCompareError(
                f"window {index} artifact dates {actual} do not match "
                f"registered {expected}")
        windows.append({
            "days": control["days"],
            "control_daily_returns_pct": control["daily_returns_pct"],
            "sector_daily_returns_pct": sector["daily_returns_pct"],
        })
        evidence.append({
            "window_index": index,
            "control_run_id": control["run_id"],
            "sector_run_id": sector["run_id"],
            "control_net_return_pct": round(
                control["net_return_pct"], 6),
            "sector_net_return_pct": round(
                sector["net_return_pct"], 6),
            "control_max_drawdown_pct": round(
                control["max_drawdown_pct"], 6),
            "sector_max_drawdown_pct": round(
                sector["max_drawdown_pct"], 6),
        })

    block = manifest["block_method"]
    report = compare_windows(
        windows=windows,
        block_length_days=int(block["block_length_days"]),
        repetitions=int(block["repetitions"]),
        seed=seed,
        minimum_total_days=int(manifest["minimum_total_days"]),
        minimum_meaningful_improvement_pp=float(
            manifest["minimum_meaningful_improvement_pp"]),
    )
    report["family"] = selection_experiment.FAMILY
    report["manifest_hash"] = selection_experiment.manifest_hash(manifest)
    report["windows"] = evidence
    report["historical_only"] = True
    report["promotion_allowed"] = False
    return report
