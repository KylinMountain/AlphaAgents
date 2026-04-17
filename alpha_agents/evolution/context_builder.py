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
    """Stub — filled in Task 12."""
    return ""
