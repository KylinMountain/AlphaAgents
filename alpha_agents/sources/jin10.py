"""Fetch real-time flash news from 金十数据 (Jin10).

Jin10 is one of the most popular real-time financial news flash sources in China,
covering macro-economic data releases, central bank decisions, and market events.
No authentication required.

Note: The original flash-api.jin10.com API endpoint returned 502 Bad Gateway.
We now use https://www.jin10.com/flash_newest.js which returns a JS file
containing the 50 most recent flash items as a JSON array.
"""

import json
import logging

from alpha_agents.http_client import fetch
from alpha_agents.sources.flash_text import drop_translation_twins, headline

logger = logging.getLogger(__name__)

# Public JS endpoint — no auth, no VIP required, returns 50 most recent items.
# The original flash-api.jin10.com is down (502 Bad Gateway).
FLASH_JS_URL = "https://www.jin10.com/flash_newest.js"

_EXTRA_HEADERS = {
    "Referer": "https://www.jin10.com/",
    "Origin": "https://www.jin10.com",
}


def _fetch_flash_list(limit: int = 30) -> list[dict]:
    """Fetch raw flash news items from Jin10's public JS endpoint."""
    resp = fetch(FLASH_JS_URL, headers=_EXTRA_HEADERS)
    text = resp.text.strip()
    # Strip JS variable wrapper: "var newest = [...]; "
    if text.startswith("var newest = "):
        text = text[len("var newest = "):]
    if text.endswith(";"):
        text = text[:-1]
    items = json.loads(text)
    if not isinstance(items, list):
        logger.warning("Jin10 flash_newest.js returned unexpected format: %s", type(items))
        return []
    return items[:limit]


def _parse_item(item: dict) -> dict:
    """Convert a raw Jin10 flash item into a standardised news dict.

    Keeps the editorial metadata the feed carries — Jin10 flags its own
    market-moving items with ``important``, and ``type`` separates a plain
    flash (0) from a 市场要闻 write-up (2). Dropping those threw away the
    only ranking signal in the payload, leaving every headline equal.
    """
    inner = item.get("data", {}) or {}
    # VIP items have an empty body and put the headline in vip_title.
    content = (inner.get("content") or inner.get("vip_title") or "").strip()
    time_str = (item.get("time") or "").strip()

    return {
        "title": headline(content),
        "summary": content[:300],
        "time": time_str,
        "source": "金十数据",
        # Extras beyond the common schema; unknown keys are ignored
        # downstream.
        "important": bool(item.get("important")),
        "type": item.get("type", 0),
        "origin": (inner.get("source") or "").strip(),
        "link": (inner.get("source_link") or inner.get("link") or "").strip(),
    }


def get_jin10_fn(limit: int = 30, keyword: str | None = None,
                 important_only: bool = False) -> str:
    """Fetch Jin10 real-time flash news. Replay-aware.

    Args:
        limit: maximum items to return.
        keyword: filter on title and summary.
        important_only: keep only what Jin10 itself flagged as
            market-moving — roughly 5% of the feed.
    """
    from alpha_agents.data.snapshot_store import replay_news_response, save_news
    replay = replay_news_response(["金十数据"], limit, keyword)
    if replay is not None:
        return replay

    try:
        raw_items = _fetch_flash_list(limit)
        news = [_parse_item(item) for item in raw_items]
        # Jin10 re-posts every flash in English a few seconds later.
        news = drop_translation_twins(news)

        try:
            save_news("金十数据", news)
        except Exception as e:
            logger.debug("jin10 capture failed: %s", e)

        if important_only:
            news = [n for n in news if n.get("important")]

        if keyword:
            kw = keyword.lower()
            news = [
                n for n in news
                if kw in n["title"].lower() or kw in n["summary"].lower()
            ]

        news = news[:limit]
        important_count = sum(1 for n in news if n.get("important"))

        return json.dumps(
            {"news": news, "count": len(news), "important_count": important_count},
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error("Failed to fetch Jin10 flash news: %s", e)
        return json.dumps({"news": [], "count": 0, "error": str(e)}, ensure_ascii=False)
