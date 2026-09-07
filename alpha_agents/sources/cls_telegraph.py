"""Fetch news flashes from 财联社电报 (CLS Telegraph).

CLS Telegraph is one of the fastest A-share news flash sources in China.
Uses akshare's stock_info_global_cls() which handles CLS API auth internally.

Known outage (verified 2026-09-07): the endpoint akshare 1.18.50 calls,
``https://www.cls.cn/nodeapi/telegraphList``, now returns 404, and CLS's
current API rejects unsigned requests with ``{"errno":"10012","msg":"签名
错误"}`` while the web page is client-rendered. There is no fetch path
left that does not involve defeating their request signing, so this source
stays down until akshare ships a working endpoint.

The practical hazard is not the failure but its shape: akshare wraps the
call in ``make_request_with_retry_json(max_retries=10)``, so a dead
endpoint presents as a multi-minute hang that stalls the whole scan.
_FETCH_TIMEOUT bounds it.
"""

import json
import logging
import threading

from alpha_agents.config import no_proxy

logger = logging.getLogger(__name__)

# akshare retries 10x internally; cap the total so one dead upstream can
# never hold up a morning scan or an intraday cycle.
_FETCH_TIMEOUT = 20


def _fetch_telegraph() -> list[dict]:
    """Fetch telegraph items via akshare (handles CLS API auth)."""
    # A raw daemon thread, not ThreadPoolExecutor: pool workers are
    # non-daemon and concurrent.futures registers an atexit hook that
    # joins them, so a thread stuck in akshare's retry loop would hold up
    # interpreter shutdown even after shutdown(wait=False). A daemon
    # thread is abandoned cleanly.
    result: dict = {}

    def _run() -> None:
        try:
            result["df"] = _fetch_telegraph_blocking()
        except Exception as exc:            # noqa: BLE001 — surfaced below
            result["error"] = exc

    worker = threading.Thread(target=_run, daemon=True, name="cls-telegraph")
    worker.start()
    worker.join(timeout=_FETCH_TIMEOUT)

    if worker.is_alive():
        raise TimeoutError(
            f"CLS telegraph fetch exceeded {_FETCH_TIMEOUT}s "
            "(akshare endpoint returns 404 and retries 10x)"
        )
    if "error" in result:
        raise result["error"]

    return _rows_to_items(result["df"])


def _fetch_telegraph_blocking():
    """The raw akshare call. Runs on a worker thread so it can be timed out."""
    import akshare as ak
    with no_proxy():
        return ak.stock_info_global_cls()


def _rows_to_items(df) -> list[dict]:
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
