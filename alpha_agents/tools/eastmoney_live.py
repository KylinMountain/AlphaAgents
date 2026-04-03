"""东方财富7x24直播快讯数据源.

Fetches real-time 7x24 financial flash news from East Money's live API.
This is the same data feed shown on: https://kuaixun.eastmoney.com/

Unlike the akshare-based news source which fetches article headlines,
this source captures real-time flash news (快讯) streaming 24/7.
"""

import json
import logging
import time

from alpha_agents import http_client

logger = logging.getLogger(__name__)

# East Money 7x24 live feed API
LIVE_API_URL = "https://np-listapi.eastmoney.com/comm/wap/getListInfo"

DEFAULT_PARAMS = {
    "cb": "",
    "client": "wap",
    "type": "1",          # 7x24 快讯
    "mession": "lst",
    "pageSize": "50",
    "pageIndex": "1",
    "fields1": "title,summary,digest",
    "fields2": "title,summary,digest,showTime,source",
}


def _parse_response(text: str) -> list[dict]:
    """Parse API response, handling potential JSONP wrapper."""
    text = text.strip()
    # Strip JSONP callback if present
    if text.startswith("(") and text.endswith(");"):
        text = text[1:-2]
    elif text.startswith("(") and text.endswith(")"):
        text = text[1:-1]

    data = json.loads(text)
    items = []
    raw_list = data.get("data", {}).get("list", [])

    for item in raw_list:
        title = item.get("title", "").strip()
        digest = item.get("digest", "").strip()
        summary = item.get("summary", "").strip()
        show_time = item.get("showTime", "")
        source = item.get("source", "东方财富")

        # Use digest as title fallback; use summary as content
        display_title = title or digest[:80] or summary[:80]
        if not display_title:
            continue

        items.append({
            "title": display_title,
            "summary": (summary or digest)[:300],
            "time": show_time,
            "source": f"东方财富7x24/{source}" if source else "东方财富7x24",
        })

    return items


def get_eastmoney_live_fn(*, keyword: str = "", limit: int = 50) -> str:
    """Fetch East Money 7x24 live flash news.

    Args:
        keyword: Optional keyword filter.
        limit: Max items to return.

    Returns:
        JSON string with {"news": [...]} format.
    """
    try:
        params = {**DEFAULT_PARAMS, "pageSize": str(min(limit * 2, 100))}
        resp = http_client.fetch(
            LIVE_API_URL,
            params=params,
            headers={"Referer": "https://kuaixun.eastmoney.com/"},
        )
        items = _parse_response(resp.text)
    except Exception as e:
        logger.warning("Failed to fetch eastmoney live: %s", e)
        items = []

    if keyword:
        kw = keyword.lower()
        items = [i for i in items if kw in i["title"].lower() or kw in i["summary"].lower()]

    return json.dumps({"news": items[:limit]}, ensure_ascii=False)
