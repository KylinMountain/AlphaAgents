"""Compose L1 feedback atoms into unified agent contexts."""

from __future__ import annotations

from alpha_agents.evolution.feedback import (
    inject_cognition,
    inject_sentiment,
    inject_vpa_signal_history,
)


def build_morning_context(themes: list[dict], stats: str) -> str:
    """Stub — filled in Task 14."""
    return ""


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
