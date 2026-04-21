import json
import logging

import akshare as ak
import pandas as pd

from alpha_agents.config import no_proxy

logger = logging.getLogger(__name__)


def _fetch_news(**kwargs) -> pd.DataFrame:
    with no_proxy():
        return ak.stock_news_em()


def get_news_fn(limit: int = 50, keyword: str | None = None) -> str:
    """Fetch Eastmoney news. Proxy-aware: replay reads ``news_items`` snapshot."""
    # Replay short-circuit
    try:
        from alpha_agents.evolution.replay_mode import get_replay_as_of
        as_of = get_replay_as_of()
    except Exception:
        as_of = None
    if as_of:
        from alpha_agents.data.snapshot_store import read_news
        news = read_news(sources=["东方财富7x24", "东方财富"], as_of=as_of,
                         keyword=keyword, limit=limit)
        return json.dumps({"news": news, "count": len(news)}, ensure_ascii=False)

    try:
        df = _fetch_news()

        # Capture full corpus BEFORE filtering
        full_items = []
        for _, row in df.iterrows():
            full_items.append({
                "title": str(row.get("新闻标题", "")),
                "summary": str(row.get("新闻内容", ""))[:500],
                "time": str(row.get("发布时间", "")),
                "source": str(row.get("文章来源", "")),
            })
        try:
            from alpha_agents.data.snapshot_store import save_news
            save_news("东方财富", full_items)
        except Exception as e:
            logger.debug("eastmoney news capture failed: %s", e)

        if keyword:
            mask = df["新闻标题"].str.contains(keyword, na=False) | df["新闻内容"].str.contains(keyword, na=False)
            df = df[mask]

        df = df.head(limit)

        news = []
        for _, row in df.iterrows():
            news.append({
                "title": str(row.get("新闻标题", "")),
                "summary": str(row.get("新闻内容", ""))[:200],
                "time": str(row.get("发布时间", "")),
                "source": str(row.get("文章来源", "")),
            })

        return json.dumps({"news": news, "count": len(news)}, ensure_ascii=False)
    except Exception as e:
        logger.error("Failed to fetch news: %s", e)
        return json.dumps({"news": [], "count": 0, "error": str(e)}, ensure_ascii=False)
