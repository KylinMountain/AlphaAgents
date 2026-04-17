"""Agent evolution layer — closes the learning loop.

Phase 1 (this module): L1 Feedback — inject data that's already written but
not read by any agent (cognition, sentiment, VPA signal confirmations).

Phase 2+ (not yet implemented): L2 Lessons/Principles, L3 Playbooks.
See docs/superpowers/specs/2026-04-16-agent-evolution-design.md.

Public API:
    build_morning_context, build_chat_context, build_vpa_context  — compose
    inject_sentiment, inject_cognition, inject_vpa_signal_history  — atoms
"""

from alpha_agents.evolution.feedback import (
    inject_cognition,
    inject_sentiment,
    inject_vpa_signal_history,
)
from alpha_agents.evolution.context_builder import (
    build_chat_context,
    build_morning_context,
    build_vpa_context,
)

__all__ = [
    "build_chat_context",
    "build_morning_context",
    "build_vpa_context",
    "inject_cognition",
    "inject_sentiment",
    "inject_vpa_signal_history",
]
