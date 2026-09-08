"""Scheduled tasks, and helpers shared between them.

The decision-context helpers live here rather than in
data/decision_context.py because they read from tools/, which the data
layer may not import. The linter caught that after the first attempt at
deduplicating them.
"""

import logging

logger = logging.getLogger(__name__)

def safe_market_regime() -> str | None:
    """Market regime for the decision context; never raises.

    Context documents a decision, it is not part of one — failing to
    record it must never stop a recommendation being saved. Shared so the
    morning and intraday tasks cannot drift apart on what they record.
    """
    try:
        from alpha_agents.tools.exit_signals import get_market_regime
        regime, _pct = get_market_regime()
        return regime if regime != "unknown" else None
    except Exception as e:
        logger.debug("Decision context: regime unavailable: %s", e)
        return None


def safe_sentiment_phase() -> str | None:
    """Sentiment-cycle label for the decision context; never raises."""
    try:
        from alpha_agents.data.sentiment_cycle import get_sentiment_cycle
        return get_sentiment_cycle().get("phase") or None
    except Exception as e:
        logger.debug("Decision context: sentiment unavailable: %s", e)
        return None


def safe_active_themes() -> list[dict] | None:
    """Active themes for the decision context; never raises.

    Here rather than local to a task because both the morning and intraday
    paths need it: morning_scan reached for a ``themes`` local belonging to
    a different function and raised NameError at the point it saved its
    recommendations — after the agent had already done all its work.
    """
    try:
        from alpha_agents.data.memory_store import get_active_themes
        return get_active_themes()
    except Exception as e:
        logger.debug("Decision context: themes unavailable: %s", e)
        return None


"""Scheduled analysis tasks for the trading day."""
