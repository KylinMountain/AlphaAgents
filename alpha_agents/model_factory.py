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

from alpha_agents import llm_journal, llm_roles
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


def _credentials(role: str) -> tuple[str, str, str]:
    """The resolved triple for ``role``, honouring this module's globals.

    Only the agent role has globals here, and they are passed as the legacy
    triple rather than read from ``config`` inside ``llm_roles``: tests patch
    ``model_factory.AGENT_MODEL`` and friends, and only a reader of this
    module's own global sees that. Other roles have no globals to honour, so
    they resolve from their own ``<ROLE>_*`` config.
    """
    if role == llm_roles.AGENT:
        return llm_roles.resolve(
            role, legacy=(AGENT_API_KEY, AGENT_BASE_URL, AGENT_MODEL))
    return llm_roles.resolve(role)


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
    _, base_url, model = _credentials(llm_roles.AGENT)
    return {
        "agent_model": model or DEFAULT_MODEL,
        "agent_base_url": base_url,
        "default_model": DEFAULT_MODEL,
        "fallbacks": _fallback_models(),
    }


def create_model(timeout: float | None = None, *,
                 role: str = llm_roles.AGENT) -> OpenAIChatCompletionsModel:
    """The chat model a role runs on.

    ``role`` names the purpose, not the provider: ``agent`` for the deciders,
    ``summary`` for the report summarisers, ``digest`` for news filtering.
    See ``llm_roles`` for the table and ``.env.example`` for the names. The
    default keeps every existing caller on exactly the credentials it had.

    ``timeout`` is opt-in and defaults to the SDK's own, so the production
    path is byte-for-byte what it was. It exists because a walk-forward
    against a rate-limited provider produced the failure it prevents: when a
    free tier's TPM budget is spent, some providers **queue** the request
    instead of answering ``429``, and with no client timeout the SDK waits its
    default ten minutes and then retries twice — thirty minutes for one
    session, with nothing in the log to say why. Bounding the wait turns that
    into a named failure the caller already knows how to survive.
    """
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
    api_key, base_url, model = _credentials(role)
    extra = {"timeout": timeout} if timeout else {}
    client = AsyncOpenAI(api_key=api_key, base_url=base_url, **extra)
    return OpenAIChatCompletionsModel(
        model=model or DEFAULT_MODEL,
        openai_client=llm_journal.journaled(client),
    )


def create_model_settings(role: str = llm_roles.AGENT) -> ModelSettings:
    """Run settings carrying the provider fallback chain.

    OpenRouter reads a ``models`` array from the request body and moves
    to the next entry when the primary is overloaded or rate-limited.
    The agents SDK forwards ModelSettings.extra_body into the request,
    which is the only hook for a non-standard field like this.

    Verified against the live API: the array is accepted alongside a
    valid primary, and providers that do not know the field ignore it —
    so DashScope, DeepSeek and the rest take the same path with an empty
    extra_body.

    The chain is the *agent* role's: it was measured for the tool loop that
    reaches ``max_turns=100``. A summariser has no such loop, so handing it
    the agent's fallbacks would be a claim about latency nobody measured.
    """
    if role != llm_roles.AGENT:
        return ModelSettings()
    fallbacks = _fallback_models()
    if not fallbacks:
        return ModelSettings()
    logger.debug("Model %s with %d fallbacks", AGENT_MODEL, len(fallbacks))
    return ModelSettings(extra_body={"models": fallbacks})
