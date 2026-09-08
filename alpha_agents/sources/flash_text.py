"""Shared text handling for the 7x24 flash sources.

CLS and Jin10 wrap headlines the same way (【headline】body) and both fall
back to a slice of the body when there is no wrapper. That slice was a raw
``content[:50]``, which cuts mid-word — "Total turnover on the Shanghai and
Shenzhen exchan" is what the dashboard actually rendered.
"""

import re

# Any CJK ideograph. Used to tell a Chinese flash from its English twin.
_CJK = re.compile(r"[一-鿿]")

# Sentence enders worth cutting a headline at, Chinese and Latin both.
_BREAK = re.compile(r"[。！？；!?;]|\.\s|\n")

_HEADLINE_LIMIT = 60


def headline(content: str, limit: int = _HEADLINE_LIMIT) -> str:
    """First clause of a flash body, as a headline.

    Prefers the wrapper, then the first sentence, then a word boundary —
    only slicing mid-token when a single unbroken run is longer than the
    limit, which for Chinese text is the normal case and reads fine.
    """
    content = (content or "").strip()
    if not content:
        return ""

    if content.startswith("【") and "】" in content:
        return content[1:content.index("】")].strip()

    if len(content) <= limit:
        return content

    head = content[:limit]
    m = _BREAK.search(head)
    if m and m.start() >= 8:
        return head[:m.start()].strip()

    # Latin text: back off to the last space rather than splitting a word.
    space = head.rfind(" ")
    if space >= limit // 2:
        return head[:space].strip() + "…"
    return head.strip() + "…"


def has_chinese(text: str) -> bool:
    return bool(_CJK.search(text or ""))


def drop_translation_twins(items: list[dict], window_seconds: int = 120,
                           source: str = "金十数据") -> list[dict]:
    """Remove Jin10's English re-posts of a Chinese flash.

    Jin10 publishes each flash twice: the Chinese original, then an English
    translation four to six seconds later. Half the feed on a Chinese
    dashboard was the same news read twice.

    An English item is dropped only when a Chinese item sits within
    ``window_seconds`` of it — an English-only flash, which the feed does
    occasionally carry, has no twin and survives.

    Scoped to one source, because the mixed feed also carries BBC, CNBC and
    the rest of the international RSS in English. Chinese flashes arrive
    every few seconds, so an unscoped filter would find a "twin" for every
    foreign headline and delete the entire international feed.
    """
    from datetime import datetime

    def stamp(item: dict):
        try:
            return datetime.strptime(item.get("time", ""), "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            return None

    def in_scope(item: dict) -> bool:
        return source is None or item.get("source") == source

    scoped = [i for i in items if in_scope(i)]
    chinese_times = [
        t for t in (stamp(i) for i in scoped
                    if has_chinese(i.get("summary") or i.get("title", "")))
        if t is not None
    ]
    if not chinese_times:
        return items

    kept = []
    for item in items:
        text = item.get("summary") or item.get("title", "")
        if not in_scope(item) or has_chinese(text):
            kept.append(item)
            continue
        ts = stamp(item)
        if ts is None:
            kept.append(item)
            continue
        twin = any(abs((ts - c).total_seconds()) <= window_seconds
                   for c in chinese_times)
        if not twin:
            kept.append(item)
    return kept
