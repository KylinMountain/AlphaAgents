"""Fetch SEC press releases and enforcement actions via RSS feed.

Source: U.S. Securities and Exchange Commission — no API key required.
"""

import json
import logging
import xml.etree.ElementTree as ET

from alpha_agents.http_client import fetch

logger = logging.getLogger(__name__)

RSS_URL = "https://www.sec.gov/news/pressreleases.rss"


def _parse_rss(xml_text: str) -> list[dict]:
    """Parse SEC RSS feed into a list of news items."""
    items: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        logger.warning("Failed to parse SEC RSS XML")
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
                "source": "SEC",
                "link": link,
            })

    return items


def get_sec_news_fn(limit: int = 20, keyword: str | None = None) -> str:
    """Fetch SEC press releases from the RSS feed.

    Args:
        limit: Maximum number of news items to return.
        keyword: Optional keyword to filter results (case-insensitive).
    """
    all_news: list[dict] = []

    try:
        resp = fetch(RSS_URL)
        all_news = _parse_rss(resp.text)
        logger.debug("Fetched %d items from SEC RSS", len(all_news))
    except Exception as e:
        logger.warning("Failed to fetch SEC RSS: %s", e)

    if keyword:
        kw = keyword.lower()
        all_news = [
            n for n in all_news
            if kw in n["title"].lower() or kw in n["summary"].lower()
        ]

    all_news = all_news[:limit]

    return json.dumps({"news": all_news, "count": len(all_news)}, ensure_ascii=False)
