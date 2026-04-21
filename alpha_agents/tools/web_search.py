"""Web search tool using DuckDuckGo (free, no API key needed)."""

import json
import logging

from ddgs import DDGS

logger = logging.getLogger(__name__)


def web_search_fn(query: str, max_results: int = 10) -> str:
    """Search the web using DuckDuckGo. Replay-aware.

    In replay mode, returns the captured result for the exact same query if
    one was logged before ``as_of``; otherwise returns empty + a ``replay_miss``
    flag so the caller's downstream can skip this signal rather than leak
    live Google results into a historical simulation.
    """
    # Replay short-circuit
    try:
        from alpha_agents.evolution.replay_mode import get_replay_as_of
        as_of = get_replay_as_of()
    except Exception:
        as_of = None
    if as_of:
        from alpha_agents.data.snapshot_store import read_web_search
        cached = read_web_search(query, as_of)
        if cached:
            return cached
        return json.dumps({
            "query": query, "results": [], "count": 0,
            "replay_miss": True,
            "note": "no captured web_search result for this query before as_of",
        }, ensure_ascii=False)

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
        payload = json.dumps({"query": query, "results": items, "count": len(items)}, ensure_ascii=False)
        try:
            from alpha_agents.data.snapshot_store import save_web_search
            save_web_search(query, payload)
        except Exception as e:
            logger.debug("web_search capture failed: %s", e)
        return payload
    except Exception as e:
        logger.error("Web search failed: %s", e)
        return json.dumps({"query": query, "results": [], "count": 0, "error": str(e)}, ensure_ascii=False)
