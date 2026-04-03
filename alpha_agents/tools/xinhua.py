"""Fetch financial news from Xinhua News Agency (新华社) via RSS feed.

Source: Xinhua Finance — no API key required.
"""

import json
import logging
import re
import xml.etree.ElementTree as ET

from alpha_agents.http_client import fetch

logger = logging.getLogger(__name__)

RSS_URLS = [
    "http://www.news.cn/feed/caijing.xml",
    "http://www.news.cn/feed/toutiao.xml",
]

FALLBACK_URL = "http://www.news.cn/fortune/"


def _parse_rss(xml_text: str) -> list[dict]:
    """Parse Xinhua RSS feed into a list of news items."""
    items: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        logger.warning("Failed to parse Xinhua RSS XML")
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
                "source": "新华社",
                "link": link,
            })

    return items


def _parse_fallback_html(html_text: str) -> list[dict]:
    """Extract headlines from Xinhua fortune page as a fallback."""
    items: list[dict] = []
    # Match common headline link patterns on news.cn
    pattern = r'<a[^>]+href="(https?://www\.news\.cn/[^"]*)"[^>]*>([^<]+)</a>'
    matches = re.findall(pattern, html_text)
    seen_titles: set[str] = set()
    for link, title in matches:
        title = title.strip()
        if title and len(title) > 4 and title not in seen_titles:
            seen_titles.add(title)
            items.append({
                "title": title,
                "summary": "",
                "time": "",
                "source": "新华社",
                "link": link,
            })
    return items


def get_xinhua_fn(limit: int = 20, keyword: str | None = None) -> str:
    """Fetch financial news from Xinhua News Agency RSS feeds.

    Args:
        limit: Maximum number of news items to return.
        keyword: Optional keyword to filter results (case-insensitive).
    """
    all_news: list[dict] = []

    # Try each RSS URL in order
    for url in RSS_URLS:
        try:
            resp = fetch(url)
            all_news = _parse_rss(resp.text)
            if all_news:
                logger.debug("Fetched %d items from Xinhua RSS: %s", len(all_news), url)
                break
        except Exception as e:
            logger.warning("Failed to fetch Xinhua RSS (%s): %s", url, e)

    # Fallback to scraping the fortune page
    if not all_news:
        try:
            resp = fetch(FALLBACK_URL)
            all_news = _parse_fallback_html(resp.text)
            logger.debug("Fetched %d items from Xinhua fallback page", len(all_news))
        except Exception as e:
            logger.warning("Failed to fetch Xinhua fallback page: %s", e)

    if keyword:
        kw = keyword.lower()
        all_news = [
            n for n in all_news
            if kw in n["title"].lower() or kw in n["summary"].lower()
        ]

    all_news = all_news[:limit]

    return json.dumps({"news": all_news, "count": len(all_news)}, ensure_ascii=False)
