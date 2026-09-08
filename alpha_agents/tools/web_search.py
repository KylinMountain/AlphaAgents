"""Web search tool using DuckDuckGo (free, no API key needed)."""

import concurrent.futures as _cf
import json
import logging
import os

from ddgs import DDGS

logger = logging.getLogger(__name__)

# DDGS().__init__ can hang indefinitely on DNS/TLS failures (no internal
# timeout). We isolate it in a single-shot executor so the caller is bounded.
#
# 12s was tuned against a direct connection. Behind a proxy — which is the
# only way this reaches DuckDuckGo from a mainland host — the same query
# measured 12.7s to 16.3s, so the old budget cut off every successful
# search and reported it as a timeout.
_SEARCH_TIMEOUT_SECONDS = int(os.environ.get("WEB_SEARCH_TIMEOUT", "30"))


def _ddgs_search(query: str, max_results: int):
    return DDGS().text(query, max_results=max_results)


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

    ex = _cf.ThreadPoolExecutor(max_workers=1)
    try:
        fut = ex.submit(_ddgs_search, query, max_results)
        try:
            results = fut.result(timeout=_SEARCH_TIMEOUT_SECONDS)
        except _cf.TimeoutError:
            # DDGS is wedged in DNS/TLS — abandon the worker (wait=False so
            # we return immediately; the thread exits when its socket finally
            # errors out) and report timeout to the caller.
            logger.warning("Web search timed out after %ds: %s",
                           _SEARCH_TIMEOUT_SECONDS, query[:80])
            return json.dumps({"query": query, "results": [], "count": 0,
                               "error": "search_timeout"}, ensure_ascii=False)
    finally:
        ex.shutdown(wait=False)
    try:
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
