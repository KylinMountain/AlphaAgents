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

    Proxy-aware: live fetch captures to ``news_items``; replay reads from
    the snapshot table filtered by ``published_at <= as_of`` + keyword.

    Args:
        limit: Maximum number of news items to return.
        keyword: Optional keyword to filter results on title and content.
    """
    # Replay short-circuit — read from snapshot, NEVER touch live API
    try:
        from alpha_agents.evolution.replay_mode import get_replay_as_of
        as_of = get_replay_as_of()
    except Exception:
        as_of = None
    if as_of:
        from alpha_agents.data.snapshot_store import read_news
        news = read_news(sources=["财联社电报"], as_of=as_of,
                         keyword=keyword, limit=limit)
        return json.dumps({"news": news, "count": len(news)}, ensure_ascii=False)

    try:
        news = _fetch_telegraph()

        # Capture to snapshot BEFORE filtering (so replay can keyword-search
        # the full corpus, not just what this particular call retrieved).
        try:
            from alpha_agents.data.snapshot_store import save_news
            save_news("财联社电报", news)
        except Exception as e:
            logger.debug("cls news capture failed: %s", e)

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
