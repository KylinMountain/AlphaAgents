"""One place that builds the agent model.

Seven modules had their own two-line copy of this. Small enough to slip
under the duplication linter, big enough that adding provider fallbacks
meant editing seven files — which is the shape of duplication that
actually costs something.

Free-tier providers fail transiently: a real morning scan died on
``{'message': 'Upstream error from Nvidia: Service temporarily
overloaded', 'code': 502}`` after the agent had already done its tool
calls. OpenRouter accepts a ``models`` array and moves to the next entry
when one is overloaded or rate-limited, so a single provider hiccup no
longer loses the whole run.
"""

import logging

from agents import ModelSettings, OpenAIChatCompletionsModel
from openai import AsyncOpenAI

from alpha_agents import llm_journal
from alpha_agents.config import AGENT_API_KEY, AGENT_BASE_URL, AGENT_MODEL

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "qwen-plus"

# Tried in order when the primary is overloaded. OpenRouter-specific:
# other providers ignore the extra body field.
#
# Ordered by measured latency on one tool-calling round trip, because the
# agents run a tool loop up to max_turns=100 under a 600s ceiling — a slow
# model does not degrade the run, it kills it. Measured on the box:
# ling 1.6s, dots 2.2s, nemotron-3-super 6.1s. Two were dropped rather
# than demoted: nemotron-3.5-lightning at 57s exhausts the timeout in ten
# turns, and thinkingmachines/inkling:free returns 403 ("only available on
# agentic harnesses" — a whitelist we are not on).
_OPENROUTER_FALLBACKS = [
    "inclusionai/ling-3.0-flash-fin:free",
    "dots-studio/dots-3-note-preview:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
]


def _is_openrouter() -> bool:
    return "openrouter.ai" in (AGENT_BASE_URL or "")


def _fallback_models() -> list[str]:
    """Fallback chain for the configured primary, primary excluded."""
    if not _is_openrouter():
        return []
    primary = AGENT_MODEL or DEFAULT_MODEL
    return [m for m in _OPENROUTER_FALLBACKS if m != primary]


def model_identity() -> dict:
    """The declared model identity, as a fingerprintable dict.

    Read by ``evolution.policy_sources``: which model answers is behaviour, so
    a policy version has to name it. Assembled here rather than by the reader
    so that adding a field to the model configuration is a change in one
    place — a reader that reached into this module's constants would keep
    reporting the old set after the next one is added.

    The fallback chain is part of the identity, not a detail: under load the
    primary is bypassed and a different model answers, which is a different
    trader.
    """
    return {
        "agent_model": AGENT_MODEL or DEFAULT_MODEL,
        "agent_base_url": AGENT_BASE_URL,
        "default_model": DEFAULT_MODEL,
        "fallbacks": _fallback_models(),
    }


def create_model() -> OpenAIChatCompletionsModel:
    """The chat model every agent runs on."""
    # Not instrumented for *usage*: this client is handed to an Agent, and
    # the tracing hook already counts every generation the SDK makes
    # through it. Wrapping it too would bill each agent turn twice.
    #
    # Instrumented for the *journal*, which is a different question. Usage is
    # a number for a dashboard; the journal is what makes a walk-forward
    # replayable, because an LLM is sampled and two runs of the same 2020
    # window otherwise disagree for reasons that have nothing to do with 2020.
    # In the default ``live`` mode this hands back the very object below —
    # same identity, no proxy, no file — so the production path is unchanged.
    client = AsyncOpenAI(api_key=AGENT_API_KEY, base_url=AGENT_BASE_URL)
    return OpenAIChatCompletionsModel(
        model=AGENT_MODEL or DEFAULT_MODEL,
        openai_client=llm_journal.journaled(client),
    )


def create_model_settings() -> ModelSettings:
    """Run settings carrying the provider fallback chain.

    OpenRouter reads a ``models`` array from the request body and moves
    to the next entry when the primary is overloaded or rate-limited.
    The agents SDK forwards ModelSettings.extra_body into the request,
    which is the only hook for a non-standard field like this.

    Verified against the live API: the array is accepted alongside a
    valid primary, and providers that do not know the field ignore it —
    so DashScope, DeepSeek and the rest take the same path with an empty
    extra_body.
    """
    fallbacks = _fallback_models()
    if not fallbacks:
        return ModelSettings()
    logger.debug("Model %s with %d fallbacks", AGENT_MODEL, len(fallbacks))
    return ModelSettings(extra_body={"models": fallbacks})
