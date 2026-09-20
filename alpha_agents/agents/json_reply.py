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
