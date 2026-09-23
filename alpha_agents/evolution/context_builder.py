"""Compose L1 feedback atoms into unified agent contexts."""

from __future__ import annotations

import logging

from alpha_agents.evolution.feedback import (
    inject_cognition,
    inject_sentiment,
    inject_vpa_signal_history,
    inject_portfolio,
    inject_principles,
    inject_recent_lessons,
    inject_playbooks,
)

logger = logging.getLogger(__name__)


def knowledge_in_force() -> str:
    """The rules the system has learned, rendered as the agent sees them.

    The knowledge half of the context — approved principles and playbooks —
    and nothing else. ``sentiment`` / ``cognition`` are the market, the
    calibration and entry blocks are the trader's own record; only these two
    are "knowledge" in the sense ``decision_snapshots`` records.

    Exists so the *rendered text* can be hashed by the caller that shows it.
    A hash is only worth anything if it covers the same bytes that reached
    the prompt, so this is the single place the block is composed and both
    the context builder and the recorder read it here.
    """
    parts = []
    for fn in (inject_principles, inject_playbooks):
        try:
            part = fn()
        except Exception:                              # noqa: BLE001
            # A retrieval failure is an empty block, not a broken decision:
            # the same posture ``feedback`` takes everywhere else. The hash
            # then records "nothing was in force", which is the truth.
            part = ""
        if part:
            parts.append(part)
    return "\n\n".join(parts)


def build_morning_context(themes: list[dict], stats: str,
                          mode: str = "full",
                          trader_id: str | None = None,
                          knowledge: str | None = None) -> str:
    """Build the enriched context injected into the morning agent.

    mode: "full" (default) includes Phase 1/2/3 sections except raw lessons.
          "baseline" includes only Phase 1 (sentiment + cognition + stats) —
          used by Phase 4 A/B validation to compare against pre-evolution prompts.

    ``trader_id`` splits the context in two. The market half — sentiment,
    cognition, principles, playbooks — is shared, because it is what the
    system has learned about the world. The self half — the book, the
    calibration curve, the process grades — belongs to one trader, and
    pooling it would show every trader an average nobody traded.

    The ``themes`` list is currently not used here (morning_scan passes it
    in a separate ``themes_ctx`` argument to run_morning_analysis). Kept in
    the signature so callers have a single entry point and future phases
    can leverage it (e.g., filtering lessons by active theme).
    """
    sections = []
    # Portfolio first: what it already owns bounds what it should buy.
    # Recommending a stock already held, or adding risk to a theme that
    # just stopped it out, are the two mistakes an unseen book invites.
    for part in (inject_portfolio(trader_id), inject_sentiment(),
                 inject_cognition()):
        if part:
            sections.append(part)
    # Calibration goes to the agent that states the probabilities. Without
    # this the agent writes a prob every morning and never learns anything
    # from having written it.
    from alpha_agents.evolution.calibration import inject_calibration
    from alpha_agents.evolution.process_quality import inject_process_quality
    # Calibration says how wrong its confidence is; process quality says
    # whether the thesis it is about to write will be gradeable at all.
    # Both go to the agent that writes them.
    from alpha_agents.data.portfolio_risk import (
        inject_entry_quality, inject_entry_side,
    )
    # Entry quality is the one number that separates a pullback book from
    # a breakout book: how often it was right and never got filled. Entry
    # side is the check that the book is the style it claims to be — the
    # prompt is the only thing enforcing that now, and a prompt can be
    # ignored.
    for part in (inject_calibration(trader_id=trader_id),
                 inject_process_quality(trader_id=trader_id),
                 inject_entry_quality(trader_id=trader_id),
                 inject_entry_side(trader_id=trader_id)):
        if part:
            sections.append(part)
    # What happened to this trader's own positions, in its own words. Not a
    # rule and not gated as one: the snapshot gate is about a note *becoming*
    # a rule, and this context picks stocks — it is the one that most needs
    # to remember. Until 2026-09-22 the review had this and the morning scan
    # did not, so the decision that could act on the memory was the one
    # without it.
    #
    # Since 2026-09-23 that memory is the trader's review of each trade it
    # closed (``trade_review``), not ``own_trade_notes`` — which was one
    # fixed-formula statistic restated daily, with nothing in it to act on.
    from alpha_agents.data.memory_store import _get_conn
    from alpha_agents.data.trader import DEFAULT_TRADER
    from alpha_agents.evolution import trade_review
    try:
        reviews = trade_review.inject(_get_conn(), trader_id or DEFAULT_TRADER)
    except Exception as e:                            # noqa: BLE001
        logger.warning("Trade reviews unavailable: %s", e)
        reviews = ""
    if reviews:
        sections.append(reviews)
    if mode != "baseline":
        # ``knowledge`` lets the caller render once and hand the *same string*
        # back for hashing. Without it the caller would have to re-render, and
        # the claim "the hash covers what the agent read" would rest on the
        # approved rows not having changed in between — an assumption this
        # module cannot check. Supplying it makes the identity structural.
        block = knowledge_in_force() if knowledge is None else knowledge
        if block:
            sections.append(block)
    if stats:
        sections.append(stats)
    return "\n\n".join(sections)


def build_chat_context(portfolio_summary: str, themes_summary: str,
                       stats_summary: str) -> str:
    """Build the chat agent's system-prompt context string.

    Phase 1: adds sentiment on top of the existing portfolio / themes / stats.
    Phase 2: also injects existing principles, never raw daily lessons.
    """
    sections = []
    for part in (portfolio_summary, themes_summary, stats_summary,
                 inject_sentiment(), inject_principles()):
        if part:
            sections.append(part)
    return "\n\n".join(sections)


def build_review_context(portfolio_summary: str = "",
                         trader_id: str | None = None) -> str:
    """What the review agent needs to avoid re-learning what it knows.

    It had none of this. post_review runs *after* the report is written —
    it extracts lessons — so the agent doing the writing could not see the
    principles and lessons it had already produced. Every session started
    from zero, which means it re-derives the same lesson and can never
    write the sentence that is actually worth reading: "I knew this and
    did it anyway."

    Raw lessons are explicitly unverified research material, not trading
    rules. Only this research context opts in to reading them.

    Calibration is here for the same reason. The review is where the agent
    grades itself, and grading without seeing your own track record is
    just narrating the day.
    """
    from alpha_agents.evolution.calibration import inject_calibration
    from alpha_agents.evolution.consistency import inject_consistency
    from alpha_agents.evolution.process_quality import inject_process_quality

    sections = []
    from alpha_agents.data.portfolio_risk import inject_entry_side

    for part in (portfolio_summary, inject_principles(),
                 inject_recent_lessons(days=30, purpose="research"),
                 inject_calibration(trader_id=trader_id),
                 inject_consistency(trader_id=trader_id),
                 inject_process_quality(trader_id=trader_id),
                 inject_entry_side(trader_id=trader_id)):
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
