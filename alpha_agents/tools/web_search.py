"""Web search tool using DuckDuckGo (free, no API key needed)."""

import json
import logging

from ddgs import DDGS

logger = logging.getLogger(__name__)


def web_search_fn(query: str, max_results: int = 10) -> str:
    """Search the web using DuckDuckGo.

    Args:
        query: Search query string.
        max_results: Maximum number of results to return.

    Returns:
        JSON string with search results.
    """
    try:
        results = DDGS().text(query, max_results=max_results)
        items = [
            {
                "title": r.get("title", ""),
                "url": r.get("href", ""),
                "snippet": r.get("body", ""),
            }
            for r in results
        ]
        return json.dumps({"query": query, "results": items, "count": len(items)}, ensure_ascii=False)
    except Exception as e:
        logger.error("Web search failed: %s", e)
        return json.dumps({"query": query, "results": [], "count": 0, "error": str(e)}, ensure_ascii=False)
