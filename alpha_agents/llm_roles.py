"""Purpose → model. One table for every LLM this system calls.

Five schemes had grown up side by side — ``AGENT_*``, ``DIGEST_*``,
``EMBEDDING_*``, ``MIMO_*`` and ``VPA_LLM_*`` — and two of them were read by
nothing at all. The cost is not tidiness: pointing a call site at a cheaper
model meant guessing which of five names it read, and the guess was silent
when wrong. ``MIMO_*`` is the worked example — configured, never read.

The scheme is one shape, repeated per purpose::

    <ROLE>_API_KEY / <ROLE>_BASE_URL / <ROLE>_MODEL

Every role has a tier. The tier is advice for whoever fills in ``.env``
rather than a constraint the code enforces — a cheap summariser and an
expensive decider are the same mechanism with different credentials.

A role that is not configured inherits from its parent, so ``AGENT_*`` alone
still gives every purpose a working model, and ``SUMMARY_*`` alone moves the
summarisers onto a cheaper one without touching the deciders.
"""

import logging
import os

logger = logging.getLogger(__name__)

#: The decision-making agents. Smart tier: these spend tool-call turns
#: reasoning about money.
AGENT = "agent"
#: News filtering and event linking. Cheap tier: one call per batch, and a
#: wrong answer costs a re-run rather than a position.
DIGEST = "digest"
#: Weekly report and daily review. Cheap tier: the input is prose the
#: pipeline already produced, and the job is to shorten it.
SUMMARY = "summary"
#: Concept and news vectors. A different API shape, but the same question —
#: which endpoint and which model.
EMBEDDING = "embedding"
#: Volume-price analysis.
VPA = "vpa"

ROLES = (AGENT, DIGEST, SUMMARY, EMBEDDING, VPA)

#: role → (what it is for, which tier it belongs to).
PURPOSE = {
    AGENT: ("决策与分析 Agent", "smart"),
    DIGEST: ("新闻过滤与事件链接", "cheap"),
    SUMMARY: ("周报 / 复盘等长文总结", "cheap"),
    EMBEDDING: ("概念与新闻向量", "embedding"),
    VPA: ("量价分析", "smart"),
}

#: A role with no env names of its own falls back to its parent. ``summary``
#: inherits the cheap tier, which is the whole point of having it: not
#: configuring it must not silently put the summarisers on the expensive
#: model.
PARENT = {
    SUMMARY: DIGEST,
    VPA: AGENT,
}


def _env(role: str, field: str) -> str:
    return (os.environ.get(f"{role.upper()}_{field}") or "").strip()


def _config_legacy(role: str) -> tuple[str, str, str]:
    """The legacy constants for ``role``, read off ``config`` at call time.

    Read late rather than imported once so a test that monkeypatches
    ``alpha_agents.config.AGENT_API_KEY`` is still observed — the VPA suite
    does exactly that.
    """
    from alpha_agents import config as C
    if role == AGENT:
        return C.AGENT_API_KEY, C.AGENT_BASE_URL, C.AGENT_MODEL
    if role == DIGEST:
        return C.DIGEST_API_KEY, C.DIGEST_BASE_URL, C.DIGEST_MODEL
    if role == EMBEDDING:
        return C.EMBEDDING_API_KEY, C.EMBEDDING_BASE_URL, C.EMBEDDING_MODEL
    return "", "", ""


def resolve(role: str, *,
            legacy: "tuple[str, str, str] | None" = None) -> tuple[str, str, str]:
    """``(api_key, base_url, model)`` for one purpose.

    Two calling conventions, and the difference matters:

    * ``legacy`` given — the caller already knows its configuration and is
      handing it over. This is the module global, which is both what
      ``config`` derived from ``.env`` at import time and what a test
      replaces when it patches, say, ``digest.DIGEST_MODEL``. It is
      authoritative; re-reading ``os.environ`` for the same name would
      ignore the patch and make the two disagree.
    * ``legacy`` omitted — resolve from ``<ROLE>_*`` env, then from the
      role's constants in ``config``.

    Either way a role that is still incomplete inherits from its parent, so
    a partial override adds a model without blanking the endpoint. A
    resolver that treated a partial override as complete would turn "use a
    cheaper model" into "stop calling the provider", and the failure would
    look like a network fault.
    """
    if role not in ROLES:
        raise ValueError(
            f"unknown LLM role {role!r}; known roles: {', '.join(ROLES)}")

    if legacy is not None:
        key, url, model = legacy[0] or "", legacy[1] or "", legacy[2] or ""
    else:
        base = _config_legacy(role)
        key = _env(role, "API_KEY") or (base[0] or "")
        url = _env(role, "BASE_URL") or (base[1] or "")
        model = _env(role, "MODEL") or (base[2] or "")

    if not (key and url and model):
        parent = PARENT.get(role)
        if parent:
            pkey, purl, pmodel = resolve(parent)
            key = key or pkey
            url = url or purl
            model = model or pmodel
    return key, url, model


def table() -> list[dict]:
    """Every role with its resolved endpoint. The audit surface.

    Exists so ``python main.py llm-roles`` can answer "which model actually
    answers for the summariser" without reading five env names by hand —
    which is the failure this module was written to end.
    """
    rows = []
    for role in ROLES:
        purpose, tier = PURPOSE[role]
        key, url, model = resolve(role)
        rows.append({
            "role": role,
            "purpose": purpose,
            "tier": tier,
            "model": model,
            "base_url": url,
            "configured": bool(key),
        })
    return rows


def describe(role: str) -> str:
    """One log line naming the endpoint a role resolved to, key excluded."""
    _, url, model = resolve(role)
    return f"{role}={model or '?'} @ {url or '?'}"
