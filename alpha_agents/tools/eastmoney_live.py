"""东方财富7x24直播快讯数据源.

Fetches real-time 7x24 financial flash news from East Money's live API.
This is the same data feed shown on: https://kuaixun.eastmoney.com/

Unlike the akshare-based news source which fetches article headlines,
this source captures real-time flash news (快讯) streaming 24/7.
"""

import json
import logging
import re

from alpha_agents import http_client

logger = logging.getLogger(__name__)

# East Money 7x24 live feed API
# Format: getlist_{column}_ajaxResult_{pageSize}_{pageIndex}_.html
# Column 102 = 7x24 快讯
LIVE_API_TEMPLATE = "https://newsapi.eastmoney.com/kuaixun/v1/getlist_102_ajaxResult_{size}_{page}_.html"


def _parse_response(text: str) -> list[dict]:
    """Parse API response — strips JSONP wrapper `var ajaxResult=...`."""
    text = text.strip()

    # Strip `var ajaxResult=` prefix
    match = re.match(r"var\s+\w+\s*=\s*", text)
    if match:
        text = text[match.end():]

    # Strip JSONP callback if present
    if text.startswith("(") and text.endswith(");"):
        text = text[1:-2]
    elif text.startswith("(") and text.endswith(")"):
        text = text[1:-1]

    data = json.loads(text)
    items = []
    raw_list = data.get("LivesList", [])

    for item in raw_list:
        title = item.get("title", "").strip()
        digest = item.get("digest", "").strip()
        show_time = item.get("showtime", "")
        source = "东方财富7x24"

        display_title = title or digest[:80]
        if not display_title:
            continue

        items.append({
            "title": display_title,
            "summary": (digest or title)[:300],
            "time": show_time,
            "source": source,
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
        url = LIVE_API_TEMPLATE.format(size=min(limit * 2, 100), page=1)
        resp = http_client.fetch(
            url,
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
