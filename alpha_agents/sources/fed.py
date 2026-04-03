"""Fetch press releases from the Federal Reserve (美联储) via RSS feed.

Source: Federal Reserve Press Releases — no API key required.
"""

import json
import logging
import xml.etree.ElementTree as ET

from alpha_agents.http_client import fetch

logger = logging.getLogger(__name__)

RSS_URL = "https://www.federalreserve.gov/feeds/press_all.xml"


def _parse_rss(xml_text: str) -> list[dict]:
    """Parse Federal Reserve RSS feed into a list of news items."""
    items: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        logger.warning("Failed to parse Federal Reserve RSS XML")
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
                "source": "Federal Reserve",
                "link": link,
            })

    return items


def get_fed_news_fn(limit: int = 20, keyword: str | None = None) -> str:
    """Fetch press releases from the Federal Reserve RSS feed.

    Args:
        limit: Maximum number of news items to return.
        keyword: Optional keyword to filter results (case-insensitive).
    """
    all_news: list[dict] = []

    try:
        resp = fetch(RSS_URL)
        all_news = _parse_rss(resp.text)
        logger.debug("Fetched %d items from Federal Reserve RSS", len(all_news))
    except Exception as e:
        logger.warning("Failed to fetch Federal Reserve RSS: %s", e)

    if keyword:
        kw = keyword.lower()
        all_news = [
            n for n in all_news
            if kw in n["title"].lower() or kw in n["summary"].lower()
        ]

    all_news = all_news[:limit]

    return json.dumps({"news": all_news, "count": len(all_news)}, ensure_ascii=False)
