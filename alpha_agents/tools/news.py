import json
import logging

import akshare as ak
import pandas as pd

logger = logging.getLogger(__name__)


def _fetch_news(**kwargs) -> pd.DataFrame:
    return ak.stock_news_em()


def get_news_fn(limit: int = 50, keyword: str | None = None) -> str:
    try:
        df = _fetch_news()

        if keyword:
            mask = df["标题"].str.contains(keyword, na=False) | df["内容"].str.contains(keyword, na=False)
            df = df[mask]

        df = df.head(limit)

        news = []
        for _, row in df.iterrows():
            news.append({
                "title": str(row.get("标题", "")),
                "summary": str(row.get("内容", ""))[:200],
                "time": str(row.get("发布时间", "")),
                "source": str(row.get("文章来源", "")),
            })

        return json.dumps({"news": news, "count": len(news)}, ensure_ascii=False)
    except Exception as e:
        logger.error("Failed to fetch news: %s", e)
        return json.dumps({"news": [], "count": 0, "error": str(e)}, ensure_ascii=False)
