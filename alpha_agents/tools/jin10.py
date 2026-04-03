"""Fetch real-time flash news from 金十数据 (Jin10).

Jin10 is one of the most popular real-time financial news flash sources in China,
covering macro-economic data releases, central bank decisions, and market events.
No authentication required.
"""

import json
import logging

from alpha_agents.http_client import fetch

logger = logging.getLogger(__name__)

API_URL = "https://flash-api.jin10.com/get_flash_list"
DEFAULT_PARAMS = {
    "channel": "-8200",
    "vip": "1",
    "max_time": "",
}

_EXTRA_HEADERS = {
    "Referer": "https://www.jin10.com/",
    "Origin": "https://www.jin10.com",
}


def _fetch_flash_list(limit: int = 30) -> list[dict]:
    """Fetch raw flash news items from Jin10 API."""
    resp = fetch(API_URL, params=DEFAULT_PARAMS, headers=_EXTRA_HEADERS)
    data = resp.json()
    items = data.get("data", [])
    return items[:limit]


def _parse_item(item: dict) -> dict:
    """Convert a raw Jin10 flash item into a standardised news dict."""
    inner = item.get("data", {}) or {}
    content = (inner.get("content") or "").strip()
    time_str = (item.get("time") or "").strip()
    title = content[:50] if content else ""

    return {
        "title": title,
        "summary": content[:300],
        "time": time_str,
        "source": "金十数据",
    }


def get_jin10_fn(limit: int = 30, keyword: str | None = None) -> str:
    """Fetch Jin10 real-time flash news.

    Args:
        limit: Maximum number of news items to return.
        keyword: Optional keyword to filter results on title and content.
    """
    try:
        raw_items = _fetch_flash_list(limit)
        news = [_parse_item(item) for item in raw_items]

        if keyword:
            kw = keyword.lower()
            news = [
                n for n in news
                if kw in n["title"].lower() or kw in n["summary"].lower()
            ]

        news = news[:limit]

        return json.dumps({"news": news, "count": len(news)}, ensure_ascii=False)
    except Exception as e:
        logger.error("Failed to fetch Jin10 flash news: %s", e)
        return json.dumps({"news": [], "count": 0, "error": str(e)}, ensure_ascii=False)
