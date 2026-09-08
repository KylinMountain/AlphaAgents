"""Fetch news flashes from 财联社电报 (CLS Telegraph).

One of the fastest A-share flash sources.

Endpoint history: akshare's ``stock_info_global_cls`` calls
``cls.cn/nodeapi/telegraphList``, which CLS retired around 2026-05 and now
404s — and akshare wraps that in a ten-attempt retry, so a dead endpoint
presented as a multi-minute hang rather than an error. This module calls
the current ``/v1/roll/get_roll_list`` directly.

Signing: CLS requires a ``sign`` parameter, computed locally as
``md5(sha1(query sorted by key))``. No secret is involved — it is a
request-shape check, not authentication, so nothing here works around an
access control.
"""

import hashlib
import json
import logging
import time
from urllib.parse import urlencode

from alpha_agents.http_client import fetch
from alpha_agents.sources.flash_text import headline

logger = logging.getLogger(__name__)

ROLL_URL = "https://www.cls.cn/v1/roll/get_roll_list"

_EXTRA_HEADERS = {"Referer": "https://www.cls.cn/telegraph"}

# Bump if CLS starts rejecting the version; it is sent as-is.
_CLIENT_VERSION = "8.4.6"

FETCH_TIMEOUT = 20


def _sign(params: dict) -> str:
    """md5(sha1(query sorted by key)) — CLS's request-shape check.

    Values are stringified first. urlencode turns 20 into "20" on the
    way out, so signing the int and sending the string would produce a
    signature nobody — including us — can reproduce from the sent URL.
    """
    query = urlencode(sorted((k, str(v)) for k, v in params.items()))
    return hashlib.md5(
        hashlib.sha1(query.encode()).hexdigest().encode()
    ).hexdigest()


def _build_url(limit: int) -> str:
    params = {
        "app": "CailianpressWeb",
        "os": "web",
        "sv": _CLIENT_VERSION,
        "last_time": int(time.time()),
        "refresh_type": 1,
        "rn": max(1, min(limit, 50)),
        "category": "",
    }
    params["sign"] = _sign(params)
    return f"{ROLL_URL}?{urlencode(params)}"


def _parse_item(item: dict) -> dict | None:
    """One roll_data row into the project's standard news dict."""
    content = (item.get("content") or item.get("brief") or "").strip()
    title = (item.get("title") or "").strip()
    if not title and content:
        title = headline(content)
    if not title:
        return None

    ctime = item.get("ctime")
    try:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ctime)))
    except (TypeError, ValueError):
        stamp = ""

    return {
        "title": title,
        "summary": content[:300] or title,
        "time": stamp,
        "source": "财联社电报",
        # CLS marks the items it considers market-moving.
        "important": bool(item.get("level") in ("A", "B")
                          or item.get("is_ad") == 0 and item.get("level") == "A"),
        "link": (item.get("shareurl") or "").strip(),
    }


def _fetch_telegraph(limit: int = 30) -> list[dict]:
    resp = fetch(_build_url(limit), headers=_EXTRA_HEADERS, timeout=FETCH_TIMEOUT)
    payload = json.loads(resp.text)

    if payload.get("errno"):
        raise RuntimeError(
            f"CLS errno={payload.get('errno')} msg={payload.get('msg', '')}"
        )

    rows = (payload.get("data") or {}).get("roll_data") or []
    items = [_parse_item(r) for r in rows if isinstance(r, dict)]
    return [i for i in items if i]


def get_cls_telegraph_fn(limit: int = 30, keyword: str | None = None,
                         important_only: bool = False) -> str:
    """Fetch CLS Telegraph news flashes.

    Proxy-aware: live fetch captures to ``news_items``; replay reads from
    the snapshot table filtered by ``published_at <= as_of`` + keyword.

    Args:
        limit: Maximum number of news items to return.
        keyword: Optional keyword to filter results on title and content.
        important_only: Keep only what CLS flagged as market-moving.
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
        news = _fetch_telegraph(limit)

        # Capture to snapshot BEFORE filtering (so replay can keyword-search
        # the full corpus, not just what this particular call retrieved).
        try:
            from alpha_agents.data.snapshot_store import save_news
            save_news("财联社电报", news)
        except Exception as e:
            logger.debug("cls news capture failed: %s", e)

        if important_only:
            news = [n for n in news if n.get("important")]

        if keyword:
            kw = keyword.lower()
            news = [
                n for n in news
                if kw in n["title"].lower() or kw in n["summary"].lower()
            ]

        news = news[:limit]
        return json.dumps(
            {"news": news, "count": len(news),
             "important_count": sum(1 for n in news if n.get("important"))},
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error("Failed to fetch CLS telegraph: %s", e)
        return json.dumps({"news": [], "count": 0, "error": str(e)},
                          ensure_ascii=False)
