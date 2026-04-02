"""Fetch international news from public RSS feeds.

Sources: Reuters, AP, BBC, CNBC — no API key required.
"""

import json
import logging
import xml.etree.ElementTree as ET
from datetime import datetime

import httpx

logger = logging.getLogger(__name__)

RSS_FEEDS = [
    ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml"),
    ("CNBC World", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100727362"),
    ("CNBC Economy", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258"),
    ("Google News World", "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRGx1YlY4U0FtVnVHZ0pWVXlnQVAB?hl=en-US&gl=US&ceid=US:en"),
    ("Google News Business", "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRGx6TVdZU0FtVnVHZ0pWVXlnQVAB?hl=en-US&gl=US&ceid=US:en"),
]

TIMEOUT = 15


def _parse_rss(xml_text: str, source: str) -> list[dict]:
    """Parse RSS 2.0 or Atom feed into a list of news items."""
    items = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return items

    # RSS 2.0
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
                "source": source,
                "link": link,
            })

    # Atom (if no RSS items found)
    if not items:
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.findall(".//atom:entry", ns):
            title = (entry.findtext("atom:title", "", ns) or "").strip()
            summary = (entry.findtext("atom:summary", "", ns) or "").strip()
            updated = (entry.findtext("atom:updated", "", ns) or "").strip()
            link_el = entry.find("atom:link", ns)
            link = link_el.get("href", "") if link_el is not None else ""
            if title:
                items.append({
                    "title": title,
                    "summary": summary[:300],
                    "time": updated,
                    "source": source,
                    "link": link,
                })

    return items


def get_world_news_fn(limit: int = 30, keyword: str | None = None) -> str:
    """Fetch international news from multiple RSS feeds.

    Args:
        limit: Maximum number of news items to return.
        keyword: Optional keyword to filter results (case-insensitive).
    """
    all_news: list[dict] = []

    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
        for source_name, url in RSS_FEEDS:
            try:
                resp = client.get(url)
                resp.raise_for_status()
                items = _parse_rss(resp.text, source_name)
                all_news.extend(items)
                logger.debug("Fetched %d items from %s", len(items), source_name)
            except Exception as e:
                logger.warning("Failed to fetch %s: %s", source_name, e)

    if keyword:
        kw = keyword.lower()
        all_news = [
            n for n in all_news
            if kw in n["title"].lower() or kw in n["summary"].lower()
        ]

    all_news = all_news[:limit]

    return json.dumps({"news": all_news, "count": len(all_news)}, ensure_ascii=False)
