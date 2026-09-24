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

import asyncio
import logging
import os
import re

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
    # Failover inside, journal outside: the journal records what the SDK
    # asked, so a resumed run matches its recording whichever model answered.
    if role == llm_roles.AGENT and not _is_openrouter():
        client = with_failover(client, base_url, api_key, model or DEFAULT_MODEL)
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


# ── transient failures ─────────────────────────────────────────

#: How many times a side-effect-free agent run is attempted. Override with
#: ``LLM_ATTEMPTS``.
ATTEMPTS = int(os.environ.get("LLM_ATTEMPTS", "3"))
#: Seconds to wait before the 2nd, 3rd, … attempt.
BACKOFF = (15, 45, 90)


def is_transient(exc: BaseException) -> bool:
    """A failure that is about the provider's moment, not the request.

    Measured 2026-09-24 on the pooled gateway this deployment uses: a
    four-character reply took 48 s once and 1.5 s the next time, and under
    three concurrent callers requests queued past a 120 s timeout or came
    back 502 ``no_healthy_account``. Waiting and asking again fixes those.
    A 400 or an unreadable reply does not, so they are not retried.
    """
    import openai
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, openai.APITimeoutError,
                        openai.APIConnectionError, openai.RateLimitError,
                        openai.InternalServerError)):
        return True
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and (status >= 500 or status == 429)


async def run_agent(agent, message, *, max_turns: int, timeout: float | None = None,
                    attempts: int | None = None, label: str = "", **kwargs):
    """``Runner.run`` with a per-attempt timeout and retries on transient failures.

    Only for runs with no side effects — a decision that is *returned*, not
    one a tool has already acted on. Retrying a run whose tools placed an
    order would place it twice.

    A day whose close review timed out used to lose its lesson silently: the
    2026-09-24 replay missed 4 market reviews, 4 handbook rewrites and 5
    whole decisions that way, while its report showed numbers as if nothing
    had happened.
    """
    from agents import Runner
    n = max(1, attempts or ATTEMPTS)
    for i in range(n):
        try:
            run = Runner.run(agent, message, max_turns=max_turns, **kwargs)
            return await (asyncio.wait_for(run, timeout=timeout) if timeout else run)
        except Exception as exc:                      # noqa: BLE001
            if i == n - 1 or not is_transient(exc):
                raise
            wait = BACKOFF[min(i, len(BACKOFF) - 1)]
            logger.warning("%s: transient model failure (%s: %s), attempt %d/%d, "
                           "retrying in %ds", label or agent.name, type(exc).__name__,
                           str(exc)[:200], i + 1, n, wait)
            await asyncio.sleep(wait)


# ── model unavailable: switch to a sibling ────────────────────

#: A failure that says *this model* cannot answer — gone, unfunded, its
#: account pool empty — as opposed to *this moment*. Waiting does not fix it;
#: another model of the same family on the same gateway might. Texts measured
#: on the gateway this deployment uses, 2026-09-24.
_UNAVAILABLE_TEXT = ("no_healthy_account", "service info not found", "model_not_found",
                     "does not exist", "not available", "insufficient", "quota",
                     "balance", "payment", "欠费", "余额")

#: Every answer a sibling gave instead of the configured model, for the
#: run's report: a run answered partly by another model is not the same run.
SWITCHES: list[dict] = []


class ModelsUnavailable(RuntimeError):
    """The configured model and every sibling were unavailable or unfunded."""


def is_unavailable(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None)
    if status in (402, 404):
        return True
    text = str(exc).lower()
    return any(t in text for t in _UNAVAILABLE_TEXT)


def family(model: str) -> str:
    """``global:deepseek-v4.1-flash-sg`` → ``deepseek-v4.1-flash``.

    The gateway names one model per region and route; the family is the
    name without the ``route:`` prefix and a trailing two-letter region.
    """
    name = model.rsplit(":", 1)[-1]
    return re.sub(r"-[a-z]{2}$", "", name)


def sibling_models(model: str, available: list[str]) -> list[str]:
    """The other models of ``model``'s family, ``AGENT_FALLBACK_MODELS`` first."""
    pinned = [m.strip() for m in os.environ.get("AGENT_FALLBACK_MODELS", "").split(",")
              if m.strip()]
    same = [m for m in available if m != model and family(m) == family(model)]
    out = []
    for m in pinned + same:
        if m != model and m not in out:
            out.append(m)
    return out


class _FailoverCompletions:
    def __init__(self, inner, state):
        self._inner = inner
        self._state = state

    async def create(self, **kwargs):
        s = self._state
        asked = kwargs.get("model")
        if asked != s["primary"]:
            return await self._inner.create(**kwargs)   # a specific model was asked for
        tried: list[str] = []
        last = None
        while True:
            if s["chain"] is None and tried:
                # Looked up only once something is unavailable: building a
                # model must not cost a request to the gateway.
                s["chain"] = await asyncio.to_thread(
                    _discover, s["base_url"], s["api_key"], s["primary"])
            order = [s["current"]] + [m for m in (s["chain"] or []) + [s["primary"]]
                                      if m != s["current"]]
            model = next((m for m in order if m not in tried), None)
            if model is None:
                break
            tried.append(model)
            try:
                resp = await self._inner.create(**dict(kwargs, model=model))
            except Exception as exc:          # noqa: BLE001
                if not is_unavailable(exc):
                    raise
                last = exc
                logger.warning("Model %s unavailable (%s: %s) — trying the next "
                               "of its family", model, type(exc).__name__, str(exc)[:160])
                continue
            if model != s["current"]:
                logger.warning("Model switched: %s → %s", s["current"], model)
                s["current"] = model
            if model != s["primary"]:
                SWITCHES.append({"asked": s["primary"], "answered": model})
            return resp
        raise ModelsUnavailable(
            f"{s['primary']} and its siblings {s['chain'] or []} are all unavailable: "
            f"{last}") from last


class _FailoverChat:
    def __init__(self, inner, state):
        self.completions = _FailoverCompletions(inner.completions, state)


class FailoverClient:
    """``AsyncOpenAI`` that moves to a sibling model when one is unavailable.

    Sits *inside* the journal: the journal records the request as the SDK
    made it, so a resumed run matches its recording whichever model answered.
    ``with_options`` is wrapped for the reason the journal gives — the SDK
    calls ``create`` on the clone.
    """

    def __init__(self, inner, state):
        self._inner = inner
        self._state = state

    @property
    def chat(self):
        # Built on first use: the SDK only ever reaches for ``chat``, and a
        # client that is never asked anything need not have one.
        return _FailoverChat(self._inner.chat, self._state)

    def __getattr__(self, name):
        if name in ("_inner", "_state"):
            raise AttributeError(name)
        return getattr(self._inner, name)

    def with_options(self, **kwargs):
        return FailoverClient(self._inner.with_options(**kwargs), self._state)


_SIBLINGS: dict[tuple[str, str], list[str]] = {}


def _discover(base_url: str, api_key: str, model: str) -> list[str]:
    """The gateway's other models of ``model``'s family, asked once per process."""
    key = (base_url, model)
    if key not in _SIBLINGS:
        available: list[str] = []
        try:
            import httpx
            r = httpx.get(base_url.rstrip("/") + "/models", timeout=15,
                          headers={"Authorization": f"Bearer {api_key}"})
            if r.status_code == 200:
                available = [m.get("id", "") for m in r.json().get("data", [])]
        except Exception as e:                        # noqa: BLE001
            logger.warning("Model list from %s unavailable: %s", base_url, e)
        _SIBLINGS[key] = sibling_models(model, available)
        if _SIBLINGS[key]:
            logger.info("Fallback models for %s: %s", model, ", ".join(_SIBLINGS[key]))
    return _SIBLINGS[key]


def with_failover(client, base_url: str, api_key: str, model: str):
    return FailoverClient(client, {"primary": model, "current": model, "chain": None,
                                   "base_url": base_url, "api_key": api_key})
