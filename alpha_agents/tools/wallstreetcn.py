"""Fetch financial news from 华尔街见闻 (Wall Street CN).

Source: https://wallstreetcn.com — no API key required.
"""

import json
import logging
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

API_URL = "https://api-one-wscn.awtmt.com/apiv1/content/lives"
TIMEOUT = 15


def _parse_items(data: dict) -> list[dict]:
    """Extract news items from the API response."""
    items = []
    raw_items = data.get("data", {}).get("items", [])
    for item in raw_items:
        title = (item.get("title") or "").strip()
        summary = (item.get("content_text") or "").strip()
        display_time = item.get("display_time")
        uri = item.get("uri", "")

        time_str = ""
        if display_time:
            try:
                dt = datetime.fromtimestamp(display_time, tz=timezone.utc)
                time_str = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
            except (OSError, ValueError, TypeError):
                time_str = str(display_time)

        items.append({
            "title": title or summary[:80],
            "summary": summary[:300],
            "time": time_str,
            "source": "华尔街见闻",
            "uri": uri,
        })
    return items


def get_wallstreetcn_fn(limit: int = 30, keyword: str | None = None) -> str:
    """Fetch financial news from 华尔街见闻 (Wall Street CN).

    Args:
        limit: Maximum number of news items to return.
        keyword: Optional keyword to filter results (case-insensitive).
    """
    all_news: list[dict] = []

    try:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
            resp = client.get(API_URL, params={"channel": "global-channel", "limit": 20})
            resp.raise_for_status()
            data = resp.json()
            items = _parse_items(data)
            all_news.extend(items)
            logger.debug("Fetched %d items from 华尔街见闻", len(items))
    except Exception as e:
        logger.warning("Failed to fetch 华尔街见闻: %s", e)

    if keyword:
        kw = keyword.lower()
        all_news = [
            n for n in all_news
            if kw in n["title"].lower() or kw in n["summary"].lower()
        ]

    all_news = all_news[:limit]

    return json.dumps({"news": all_news, "count": len(all_news)}, ensure_ascii=False)
