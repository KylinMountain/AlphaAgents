"""Fetch news flashes from 财联社电报 (CLS Telegraph).

CLS Telegraph is one of the fastest A-share news flash sources in China.
Uses akshare's stock_info_global_cls() which handles CLS API auth internally.
"""

import json
import logging

from alpha_agents.config import no_proxy

logger = logging.getLogger(__name__)


def _fetch_telegraph() -> list[dict]:
    """Fetch telegraph items via akshare (handles CLS API auth)."""
    import akshare as ak
    with no_proxy():
        df = ak.stock_info_global_cls()

    items = []
    for _, row in df.iterrows():
        title = str(row.get("标题", "")).strip()
        content = str(row.get("内容", "")).strip()
        date_str = str(row.get("发布日期", "")).strip()
        time_str = str(row.get("发布时间", "")).strip()
        timestamp = f"{date_str} {time_str}".strip()

        # Use content as title if title is empty
        if not title and content:
            title = content[:50]

        if title:
            items.append({
                "title": title,
                "summary": content[:300] if content else title,
                "time": timestamp,
                "source": "财联社电报",
            })
    return items


def get_cls_telegraph_fn(limit: int = 30, keyword: str | None = None) -> str:
    """Fetch CLS Telegraph news flashes.

    Args:
        limit: Maximum number of news items to return.
        keyword: Optional keyword to filter results on title and content.
    """
    try:
        news = _fetch_telegraph()

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
