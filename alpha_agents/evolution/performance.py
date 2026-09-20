"""Strict portfolio-performance accounting shared by replay and comparison.

The initial account mark is deliberately separate from daily equity rows: it is
the denominator for the first trading day's return and the first drawdown peak,
but it is not itself a trading day.
"""

from __future__ import annotations

from datetime import date
import math


class PerformanceError(ValueError):
    pass


def _number(value, *, field: str) -> float:
    if value in {None, ""} or isinstance(value, bool):
        raise PerformanceError(f"{field} is missing or not numeric")
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise PerformanceError(f"{field} is not numeric: {value!r}") from exc
    if not math.isfinite(out):
        raise PerformanceError(f"{field} must be finite")
    return out


def _day(value, *, index: int) -> str:
    raw = str(value or "")
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise PerformanceError(
            f"equity row {index} has invalid ISO date {raw!r}") from exc
    normalized = parsed.isoformat()
    if raw != normalized:
        raise PerformanceError(
            f"equity row {index} date must be YYYY-MM-DD, got {raw!r}")
    return normalized


def equity_metrics(initial_equity, equity_rows: list[dict]) -> dict:
    """Return one strict metric view over an initial mark plus trading days."""
    initial = _number(initial_equity, field="initial_equity")
    if initial <= 0:
        raise PerformanceError("initial_equity must be > 0")
    if not equity_rows:
        raise PerformanceError("equity rows are empty")

    days: list[str] = []
    values: list[float] = []
    daily: dict[str, float] = {}
    prior_day: str | None = None
    prior_equity = initial
    peak = initial
    worst = 0.0

    for index, row in enumerate(equity_rows):
        day = _day(row.get("date"), index=index)
        if prior_day is not None and day <= prior_day:
            kind = "duplicate" if day == prior_day else "out-of-order"
            raise PerformanceError(
                f"equity dates are {kind}: {prior_day!r} then {day!r}")

        value = _number(row.get("equity"), field=f"equity[{day}]")
        if value < 0:
            raise PerformanceError(f"equity[{day}] must be >= 0")
        if prior_equity <= 0:
            raise PerformanceError(
                f"cannot compute return for {day} after non-positive equity")

        daily[day] = (value / prior_equity - 1.0) * 100.0
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1.0)

        days.append(day)
        values.append(value)
        prior_day = day
        prior_equity = value

    final = values[-1]
    return {
        "initial_equity": initial,
        "final_equity": final,
        "trading_days": len(days),
        "days": days,
        "daily_returns_pct": daily,
        "net_return_pct": (final / initial - 1.0) * 100.0,
        "max_drawdown_pct": abs(worst) * 100.0,
        "worst_day_return_pct": min(daily.values()),
        "average_daily_equity": sum(values) / len(values),
    }
