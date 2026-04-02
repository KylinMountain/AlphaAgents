"""Fetch official White House statements and briefings via RSS feed.

Source: White House Briefing Room — no API key required.
"""

import json
import logging
import xml.etree.ElementTree as ET

import httpx

logger = logging.getLogger(__name__)

RSS_URL = "https://www.whitehouse.gov/feed/"
TIMEOUT = 15


def _parse_rss(xml_text: str) -> list[dict]:
    """Parse White House RSS feed into a list of news items."""
    items: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        logger.warning("Failed to parse White House RSS XML")
        return items

    for item in root.iter("item"):
        title = item.findtext("title", "").strip()
        desc = item.findtext("description", "").strip()
        pub_date = item.findtext("pubDate", "").strip()
        link = item.findtext("link", "").strip()
        if title:
            items.append({
                "title": title,
                "summary": desc[:300],
                "time": pub_date,
                "source": "White House",
                "link": link,
            })

    return items


def get_whitehouse_fn(limit: int = 20, keyword: str | None = None) -> str:
    """Fetch official White House statements from the RSS feed.

    Args:
        limit: Maximum number of news items to return.
        keyword: Optional keyword to filter results (case-insensitive).
    """
    all_news: list[dict] = []

    try:
        with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
            resp = client.get(RSS_URL)
            resp.raise_for_status()
            all_news = _parse_rss(resp.text)
            logger.debug("Fetched %d items from White House RSS", len(all_news))
    except Exception as e:
        logger.warning("Failed to fetch White House RSS: %s", e)

    if keyword:
        kw = keyword.lower()
        all_news = [
            n for n in all_news
            if kw in n["title"].lower() or kw in n["summary"].lower()
        ]

    all_news = all_news[:limit]

    return json.dumps({"news": all_news, "count": len(all_news)}, ensure_ascii=False)
