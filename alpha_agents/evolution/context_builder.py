"""Compose L1 feedback atoms into unified agent contexts."""

from __future__ import annotations

from alpha_agents.evolution.feedback import (
    inject_cognition,
    inject_sentiment,
    inject_vpa_signal_history,
    inject_portfolio,
    inject_principles,
    inject_recent_lessons,
    inject_playbooks,
)


def build_morning_context(themes: list[dict], stats: str,
                          mode: str = "full") -> str:
    """Build the enriched context injected into the morning agent.

    mode: "full" (default) includes all Phase 1/2/3 sections.
          "baseline" includes only Phase 1 (sentiment + cognition + stats) —
          used by Phase 4 A/B validation to compare against pre-evolution prompts.

    The ``themes`` list is currently not used here (morning_scan passes it
    in a separate ``themes_ctx`` argument to run_morning_analysis). Kept in
    the signature so callers have a single entry point and future phases
    can leverage it (e.g., filtering lessons by active theme).
    """
    sections = []
    # Portfolio first: what it already owns bounds what it should buy.
    # Recommending a stock already held, or adding risk to a theme that
    # just stopped it out, are the two mistakes an unseen book invites.
    for part in (inject_portfolio(), inject_sentiment(), inject_cognition()):
        if part:
            sections.append(part)
    # Calibration goes to the agent that states the probabilities. Without
    # this the agent writes a prob every morning and never learns anything
    # from having written it.
    from alpha_agents.evolution.calibration import inject_calibration
    cal = inject_calibration()
    if cal:
        sections.append(cal)
    if mode != "baseline":
        for part in (inject_principles(), inject_recent_lessons(),
                     inject_playbooks()):
            if part:
                sections.append(part)
    if stats:
        sections.append(stats)
    return "\n\n".join(sections)


def build_chat_context(portfolio_summary: str, themes_summary: str,
                       stats_summary: str) -> str:
    """Build the chat agent's system-prompt context string.

    Phase 1: adds sentiment on top of the existing portfolio / themes / stats.
    Phase 2: also injects principles + recent lessons (3-day window).
    """
    sections = []
    for part in (portfolio_summary, themes_summary, stats_summary,
                 inject_sentiment(), inject_principles(),
                 inject_recent_lessons(days=3)):
        if part:
            sections.append(part)
    return "\n\n".join(sections)


def build_vpa_context(code: str, as_of: str | None = None) -> str:
    """Build the context block injected before VPA LLM analysis.

    Phase 1: only VPA signal history (L1 feedback).
    Phase 3 hook: will also include matched playbook info (see spec §VPA build).
    """
    signal_history = inject_vpa_signal_history(code, as_of=as_of)
    sections = [s for s in (signal_history,) if s]
    return "\n\n".join(sections)
