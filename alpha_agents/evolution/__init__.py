"""Agent evolution layer — closes the learning loop.

Phase 1 (this module): L1 Feedback — inject data that's already written but
not read by any agent (cognition, sentiment, VPA signal confirmations).

Phase 2 (this module): L2 Lessons/Principles — extract lessons from review
reports, consolidate into reusable trading principles.

Phase 3 (this module): L3 Playbooks — pattern-based trade playbooks with
regime-aware matching and daily stat tracking.
See docs/superpowers/specs/2026-04-16-agent-evolution-design.md.

Public API:
    build_morning_context,
    build_review_context, build_chat_context, build_vpa_context  — compose
    inject_sentiment, inject_cognition, inject_vpa_signal_history  — atoms
    extract_daily_lessons, consolidate_principles, post_review  — L2 lessons
    match_playbook, update_playbook_stats  — L3 playbooks
"""

from alpha_agents.evolution.calibration import inject_calibration
from alpha_agents.evolution.consistency import inject_consistency
from alpha_agents.evolution.process_quality import inject_process_quality
from alpha_agents.evolution.feedback import (
    inject_cognition,
    inject_portfolio,
    inject_sentiment,
    inject_vpa_signal_history,
)
from alpha_agents.evolution.context_builder import (
    build_chat_context,
    build_morning_context,
    build_review_context,
    build_vpa_context,
)
from alpha_agents.evolution.lessons import (
    extract_daily_lessons,
    consolidate_principles,
    post_review,
)
from alpha_agents.evolution.playbook import match_playbook, update_playbook_stats
from alpha_agents.evolution.metrics import (
    compute_evolution_metrics,
    get_evolution_metrics_trend,
    format_metrics_trend,
)

__all__ = [
    "build_chat_context",
    "build_morning_context",
    "build_review_context",
    "build_vpa_context",
    "inject_calibration",
    "inject_consistency",
    "inject_process_quality",
    "inject_cognition",
    "inject_portfolio",
    "inject_sentiment",
    "inject_vpa_signal_history",
    "extract_daily_lessons",
    "consolidate_principles",
    "post_review",
    "match_playbook",
    "update_playbook_stats",
    "compute_evolution_metrics",
    "get_evolution_metrics_trend",
    "format_metrics_trend",
]
