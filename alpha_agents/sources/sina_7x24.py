"""Fetch the 新浪财经 7x24 live feed.

Worth having alongside the other flash sources for one reason the others
lack: Sina tags each item with the instruments it concerns, in
``ext.stocks`` —

    {"market": "cn", "symbol": "sh603828", "key": "*ST利达"}
    {"market": "commodity", "symbol": "nf_cu0", "key": "铜"}

That is an editorial news→ticker mapping, so a company headline arrives
already linked to a code instead of needing an LLM to guess one. Items
also carry a category tag (公司 / 市场 / 国际 / 其他) and a precise
publication timestamp.

Endpoint is the live-blog feed behind https://finance.sina.com.cn/7x24/
(zhibo_id 152 = 财经). No auth.
"""

import json
import logging

from alpha_agents.http_client import fetch

logger = logging.getLogger(__name__)

FEED_URL = (
    "https://zhibo.sina.com.cn/api/zhibo/feed"
    "?page=1&page_size={page_size}&zhibo_id=152&tag_id=0&dire=f&dpc=1"
)

_EXTRA_HEADERS = {"Referer": "https://finance.sina.com.cn/7x24/"}

# Sina's own market codes → the 6-digit form the rest of the project uses.
_CN_PREFIXES = ("sh", "sz", "bj")


def _normalise_symbol(stock: dict) -> dict | None:
    """Turn one ext.stocks entry into {code, name, market}.

    A-share symbols arrive as sh603828 / sz300308; strip the exchange
    prefix so they join with everything else keyed on the 6-digit code.
    Commodity and overseas entries are kept but not renumbered.
    """
    symbol = (stock.get("symbol") or "").strip()
    name = (stock.get("key") or "").strip()
    market = (stock.get("market") or "").strip()
    if not symbol:
        return None

    if market == "cn" and symbol[:2] in _CN_PREFIXES and symbol[2:].isdigit():
        return {"code": symbol[2:], "name": name, "market": "cn"}
    return {"code": symbol, "name": name, "market": market or "other"}


def _parse_item(item: dict) -> dict:
    """Convert one feed row into the project's standard news dict."""
    text = (item.get("rich_text") or "").strip()

    ext = item.get("ext")
    if isinstance(ext, str) and ext:
        try:
            ext = json.loads(ext)
        except json.JSONDecodeError:
            ext = {}
    if not isinstance(ext, dict):
        ext = {}

    stocks = []
    for s in ext.get("stocks") or []:
        if isinstance(s, dict):
            norm = _normalise_symbol(s)
            if norm:
                stocks.append(norm)

    tags = [
        t.get("name") for t in (item.get("tag") or [])
        if isinstance(t, dict) and t.get("name")
    ]

    # Headlines are wrapped in 【】; use that as the title when present.
    title = text
    if text.startswith("【") and "】" in text:
        title = text[1:text.index("】")]
    else:
        title = text[:50]

    return {
        "title": title,
        "summary": text[:300],
        "time": (item.get("create_time") or "").strip(),
        "source": "新浪7x24",
        # Extras beyond the common schema — consumers that do not know
        # about them simply ignore these keys.
        "stocks": stocks,
        "tags": tags,
        "link": ext.get("docurl") or "",
    }


def _fetch_feed(limit: int = 30) -> list[dict]:
    resp = fetch(FEED_URL.format(page_size=min(limit, 100)),
                 headers=_EXTRA_HEADERS)
    payload = json.loads(resp.text)
    rows = (
        payload.get("result", {})
        .get("data", {})
        .get("feed", {})
        .get("list", [])
    )
    if not isinstance(rows, list):
        logger.warning("Sina 7x24 returned unexpected shape: %s", type(rows))
        return []
    return [_parse_item(r) for r in rows]


def get_sina_7x24_fn(limit: int = 30, keyword: str | None = None,
                     stocks_only: bool = False) -> str:
    """Fetch the Sina 7x24 live feed. Replay-aware.

    Args:
        limit: maximum items to return.
        keyword: filter on title and summary.
        stocks_only: keep only items Sina linked to an instrument — the
            subset where the news→ticker mapping is already resolved.
    """
    from alpha_agents.data.snapshot_store import replay_news_response, save_news
    replay = replay_news_response(["新浪7x24"], limit, keyword)
    if replay is not None:
        return replay

    try:
        news = _fetch_feed(limit)

        try:
            save_news("新浪7x24", news)
        except Exception as e:
            logger.debug("sina 7x24 capture failed: %s", e)

        if stocks_only:
            news = [n for n in news if n.get("stocks")]

        if keyword:
            kw = keyword.lower()
            news = [
                n for n in news
                if kw in n["title"].lower() or kw in n["summary"].lower()
            ]

        news = news[:limit]
        linked = sum(1 for n in news if n.get("stocks"))
        return json.dumps(
            {"news": news, "count": len(news), "linked_count": linked},
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error("Failed to fetch Sina 7x24: %s", e)
        return json.dumps({"news": [], "count": 0, "error": str(e)},
                          ensure_ascii=False)
