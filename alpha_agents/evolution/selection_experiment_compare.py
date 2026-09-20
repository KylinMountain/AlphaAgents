"""Comparator for the minimal no-flow discovery experiment.

The primary statistic is the mean difference in 30-trading-day portfolio net
return across four preregistered reset windows, measured in percentage points.
Bootstrap samples paired daily-return blocks *inside each window*, recomputes
each compounded window return, then recomputes the same four-window statistic.
"""

from __future__ import annotations

import random
import statistics


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
