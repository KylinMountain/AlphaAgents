"""Aggregate tech news from major English-language tech publications.

Covers product launches (NVIDIA chips, Tesla robots, Apple devices),
AI breakthroughs, semiconductor industry moves, and enterprise tech.

Sources: TechCrunch, VentureBeat, SiliconANGLE — all via public RSS feeds.
No API key required.
"""

import json
import logging
import xml.etree.ElementTree as ET

from alpha_agents.http_client import fetch

logger = logging.getLogger(__name__)

# (source_id, display_name, rss_url)
TECH_FEEDS: list[tuple[str, str, str]] = [
    ("techcrunch", "TechCrunch", "https://techcrunch.com/feed/"),
    ("venturebeat", "VentureBeat", "https://venturebeat.com/feed/"),
    ("siliconangle", "SiliconANGLE", "https://siliconangle.com/feed/"),
]


def _strip_cdata(text: str) -> str:
    """Remove CDATA wrapper if present."""
    if text.startswith("<![CDATA[") and text.endswith("]]>"):
        return text[9:-3]
    return text


def _parse_rss(xml_text: str, source: str) -> list[dict]:
    """Parse standard RSS 2.0 feed into news items."""
    items: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        logger.warning("Failed to parse %s RSS XML", source)
        return items

    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        title = _strip_cdata(title)
        if not title:
            continue

        desc = (item.findtext("description") or "").strip()
        desc = _strip_cdata(desc)
        # Strip HTML tags from description
        import re
        desc = re.sub(r"<[^>]+>", "", desc).strip()

        pub_date = (item.findtext("pubDate") or "").strip()
        link = (item.findtext("link") or "").strip()

        # Try to get categories/tags
        categories = [
            (c.text or "").strip()
            for c in item.findall("category")
            if c.text
        ]

        news_item: dict = {
            "title": title,
            "summary": desc[:300],
            "time": pub_date,
            "source": source,
            "link": link,
        }
        if categories:
            news_item["tags"] = categories[:5]

        items.append(news_item)

    return items


def _fetch_feed(source_id: str, source_name: str, url: str) -> list[dict]:
    """Fetch and parse a single tech RSS feed."""
    try:
        resp = fetch(url, max_retries=1, timeout=10)
        items = _parse_rss(resp.text, source_name)
        if items:
            logger.debug("tech_news: fetched %d items from %s", len(items), source_name)
        return items
    except Exception as e:
        logger.debug("tech_news: %s unavailable: %s", source_name, e)
        return []


def get_tech_news_fn(limit: int = 30, keyword: str | None = None) -> str:
    """Fetch latest tech news from TechCrunch, VentureBeat, SiliconANGLE.

    Covers product launches, AI/semiconductor developments, and enterprise tech.

    Args:
        limit: Maximum number of news items to return.
        keyword: Optional keyword to filter results (case-insensitive).
    """
    all_news: list[dict] = []

    for source_id, source_name, url in TECH_FEEDS:
        items = _fetch_feed(source_id, source_name, url)
        all_news.extend(items)

    if keyword:
        kw = keyword.lower()
        all_news = [
            n for n in all_news
            if kw in n["title"].lower() or kw in n["summary"].lower()
        ]

    all_news = all_news[:limit]

    return json.dumps({"news": all_news, "count": len(all_news)}, ensure_ascii=False)
