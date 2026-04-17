"""Compose L1 feedback atoms into unified agent contexts."""

from __future__ import annotations

from alpha_agents.evolution.feedback import (
    inject_cognition,
    inject_sentiment,
    inject_vpa_signal_history,
)


def build_morning_context(themes: list[dict], stats: str) -> str:
    """Build the enriched context injected into the morning agent.

    Phase 1: sentiment + cognition (both currently lost by morning_scan) +
    the existing stats string.
    Phase 2 hook: will also include principles + recent daily_lessons.
    Phase 3 hook: will also include active playbooks.

    The ``themes`` list is currently not used here (morning_scan passes it
    in a separate ``themes_ctx`` argument to run_morning_analysis). Kept in
    the signature so callers have a single entry point and future phases
    can leverage it (e.g., filtering lessons by active theme).
    """
    sections = []
    sentiment = inject_sentiment()
    if sentiment:
        sections.append(sentiment)
    cognition = inject_cognition()
    if cognition:
        sections.append(cognition)
    if stats:
        sections.append(stats)
    return "\n\n".join(sections)


def build_chat_context(portfolio_summary: str, themes_summary: str, stats_summary: str) -> str:
    """Stub — filled in Task 16."""
    return ""


def build_vpa_context(code: str) -> str:
    """Build the context block injected before VPA LLM analysis.

    Phase 1: only VPA signal history (L1 feedback).
    Phase 3 hook: will also include matched playbook info (see spec §VPA build).
    """
    signal_history = inject_vpa_signal_history(code)
    sections = [s for s in (signal_history,) if s]
    return "\n\n".join(sections)
