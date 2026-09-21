"""Small shared helpers for strict JSON-only agent replies."""

from __future__ import annotations

import re


_TICKS = chr(96) * 3
_FENCE = re.compile(
    re.escape(_TICKS) + r"(?:json)?\s*(.*?)" + re.escape(_TICKS),
    re.IGNORECASE | re.DOTALL,
)


def json_body(text: str) -> str:
    """Return the most likely JSON object without inventing missing content."""
    text = (text or "").strip()
    if not text:
        return ""
    blocks = _FENCE.findall(text)
    if blocks:
        return blocks[-1].strip()
    if text.startswith("{"):
        return text
    start, end = text.rfind("{"), text.rfind("}")
    return text[start:end + 1] if start >= 0 and end > start else text

def squeeze_name(name: str) -> str:
    """A concept name with whitespace removed, for comparison only.

    THS ships names like "中国AI 50", "同花顺漂亮100" and "共封装光学(CPO)".
    A model writing the same concept back drops the space about as often as
    it keeps it, and both selectors compared the two by exact string. Measured
    on the 2026-01-06 session: the stock selector answered "中国AI50" for the
    offered "中国AI 50", both picks were refused as ``invalid_primary_theme``,
    and the day placed no orders — with its direction, stocks and written
    reasoning all correct.

    Only whitespace is ignored. The squeezed form is for the comparison; what
    gets stored is always the offered spelling, because everything downstream
    joins on the concept name.
    """
    return "".join(str(name or "").split())


def match_offered(written: str, offered) -> str | None:
    """The offered name this one means, ignoring whitespace — or ``None``."""
    target = squeeze_name(written)
    if not target:
        return None
    return next((name for name in offered
                 if squeeze_name(name) == target), None)
