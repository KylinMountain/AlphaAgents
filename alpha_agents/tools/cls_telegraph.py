"""Fetch news flashes from 财联社电报 (CLS Telegraph).

CLS Telegraph is one of the fastest A-share news flash sources in China.
No authentication required.
"""

import json
import logging
from datetime import datetime

import httpx

logger = logging.getLogger(__name__)

API_URL = "https://www.cls.cn/nodeapi/updateTelegraph"
DEFAULT_PARAMS = {
    "app": "CailianpressWeb",
    "os": "web",
    "sv": "8.4.6",
}
TIMEOUT = 15


def _fetch_telegraph(limit: int = 30) -> list[dict]:
    """Fetch raw telegraph items from CLS API."""
    params = {**DEFAULT_PARAMS, "rn": str(limit)}
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
        resp = client.get(API_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
    return data.get("data", {}).get("roll_data", [])


def _parse_item(item: dict) -> dict:
    """Convert a raw CLS telegraph item into a standardised news dict."""
    title = (item.get("title") or "").strip()
    content = (item.get("content") or "").strip()
    ctime = item.get("ctime", 0)
    subjects = item.get("subjects") or []

    time_str = datetime.fromtimestamp(ctime).strftime("%Y-%m-%d %H:%M:%S") if ctime else ""
    tags = [s.get("subject_name", "") for s in subjects if s.get("subject_name")]

    return {
        "title": title,
        "summary": content[:300],
        "time": time_str,
        "source": "财联社电报",
        "tags": tags,
    }


def get_cls_telegraph_fn(limit: int = 30, keyword: str | None = None) -> str:
    """Fetch CLS Telegraph news flashes.

    Args:
        limit: Maximum number of news items to return.
        keyword: Optional keyword to filter results on title and content.
    """
    try:
        raw_items = _fetch_telegraph(limit)
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
        logger.error("Failed to fetch CLS telegraph: %s", e)
        return json.dumps({"news": [], "count": 0, "error": str(e)}, ensure_ascii=False)
