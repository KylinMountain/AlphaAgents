"""L1 Feedback — query-and-format helpers for data that exists but wasn't injected."""

from __future__ import annotations

from alpha_agents.data.sentiment_cycle import get_sentiment_cycle


def inject_sentiment() -> str:
    """Format today's sentiment cycle phase for agent context.

    Returns empty string if no cycle is saved yet (first-run edge case).
    """
    cycle = get_sentiment_cycle()
    if not cycle:
        return ""
    phase = cycle.get("phase", "")
    strategy = cycle.get("strategy", "")
    if not phase:
        return ""
    # Truncate strategy to stay within ~50 char budget
    if len(strategy) > 40:
        strategy = strategy[:40].rstrip() + "…"
    return f"【情绪周期】{phase}" + (f" — {strategy}" if strategy else "")


def inject_cognition() -> str:
    """Stub — filled in Task 7."""
    return ""


def inject_vpa_signal_history(code: str) -> str:
    """Stub — filled in Task 9."""
    return ""
