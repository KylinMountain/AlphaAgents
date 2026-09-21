"""One definition of "the model returned nothing usable".

Why this exists. A reasoning model spends output tokens on thinking before it
writes a single character of the answer, and the two share one budget. Measured
2026-09-21 against ``cn:deepseek-v4.1-flash``: an analytical prompt capped at
``max_tokens=800`` came back **HTTP 200, ``finish_reason="length"``, 800 tokens
of reasoning and ``content == ""``**. The same prompt uncapped answered in 778
characters. So a cap that was generous for a non-reasoning model is now a
silent mute button, and every call site that does ``(content or "").strip()``
turns that into an empty string it treats as an answer:

- ``evolution/playbook.py`` asked for a 30-character sentence at
  ``max_tokens=80`` and returned ``""`` — not even its own fallback, because
  nothing raised.
- ``agents/context_compressor.py`` would have stored an empty summary.
- ``evolution/lessons.py`` would have logged "Invalid JSON" for an empty body,
  which reads like a model that wrote malformed output rather than one that was
  cut off before it wrote anything.

The caps are gone (the model is free to use what it needs), but a ceiling is
not the only way to get an empty body — a refusal, a truncated stream or a
provider hiccup all look the same at the call site. So the check stays, and it
is here rather than repeated six times.

This module is cross-cutting: it depends on nothing in the package and may be
imported from any layer.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def content_or_none(response, *, where: str) -> str | None:
    """The message text, or ``None`` when the model said nothing usable.

    ``where`` names the call site and appears in the log, because "empty
    response" without it sends a reader looking through every LLM call in the
    process.
    """
    choices = getattr(response, "choices", None) or []
    if not choices:
        logger.warning("%s: model returned no choices", where)
        return None
    choice = choices[0]
    text = (getattr(choice.message, "content", None) or "").strip()
    reason = getattr(choice, "finish_reason", None)
    if text:
        if reason == "length":
            # Salvageable but incomplete: json_repair will happily turn a
            # half-written array into a valid short one, which is how three of
            # three digest batches were once accepted silently.
            logger.warning("%s: response hit the token ceiling (%d chars kept)",
                           where, len(text))
        return text
    if reason == "length":
        logger.error(
            "%s: empty content with finish_reason=length — the whole budget "
            "went to reasoning. Remove max_tokens at this call site, or send "
            "thinking={'type': 'disabled'} if the answer is meant to be short",
            where)
    else:
        logger.error("%s: empty content (finish_reason=%s)", where, reason)
    return None


def reasoning_tokens(response) -> int | None:
    """Reasoning tokens this response spent, when the provider reports them.

    Returned rather than logged so a caller can put it in its own line; not
    every provider carries the field, and ``None`` means "not reported", never
    "zero".
    """
    usage = getattr(response, "usage", None)
    details = getattr(usage, "completion_tokens_details", None)
    return getattr(details, "reasoning_tokens", None)
