"""Fetch posts from Truth Social and X/Twitter via RSS bridge services.

Sources:
- Trump on Truth Social via RSSHub
- Elon Musk on X/Twitter via RSSHub
- Fallback: Nitter RSS feeds

No API key required.
"""

import json
import logging
import xml.etree.ElementTree as ET

from alpha_agents.http_client import fetch

logger = logging.getLogger(__name__)

# Primary feeds via RSSHub
TRUMP_RSSHUB_URL = "https://rsshub.app/truthsocial/user/realDonaldTrump"
MUSK_RSSHUB_URL = "https://rsshub.app/twitter/user/elonmusk"

# Fallback feeds via Nitter
TRUMP_NITTER_URL = "https://nitter.net/realDonaldTrump/rss"
MUSK_NITTER_URL = "https://nitter.net/elonmusk/rss"

FEED_CONFIG = [
    {
        "primary": TRUMP_RSSHUB_URL,
        "fallback": TRUMP_NITTER_URL,
        "source": "Truth Social",
        "author": "Donald Trump",
    },
    {
        "primary": MUSK_RSSHUB_URL,
        "fallback": MUSK_NITTER_URL,
        "source": "X/Twitter",
        "author": "Elon Musk",
    },
]


def _parse_rss(xml_text: str, source: str, author: str) -> list[dict]:
    """Parse an RSS feed into a list of post items."""
    items: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        logger.warning("Failed to parse %s RSS XML for %s", source, author)
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
                "source": source,
                "link": link,
                "author": author,
            })

    return items


def _fetch_feed(primary_url: str, fallback_url: str, source: str, author: str) -> list[dict]:
    """Try fetching from primary URL, fall back to secondary on failure."""
    for url in (primary_url, fallback_url):
        try:
            resp = fetch(url)
            items = _parse_rss(resp.text, source, author)
            if items:
                logger.debug("Fetched %d items from %s for %s", len(items), url, author)
                return items
        except Exception as e:
            logger.warning("Failed to fetch %s (%s): %s", url, author, e)
    return []


def get_social_media_fn(limit: int = 20, keyword: str | None = None) -> str:
    """Fetch posts from Truth Social and X/Twitter via RSS bridges.

    Args:
        limit: Maximum number of posts to return.
        keyword: Optional keyword to filter results (case-insensitive).
    """
    all_posts: list[dict] = []

    for feed in FEED_CONFIG:
        posts = _fetch_feed(
            feed["primary"],
            feed["fallback"],
            feed["source"],
            feed["author"],
        )
        all_posts.extend(posts)

    if keyword:
        kw = keyword.lower()
        all_posts = [
            p for p in all_posts
            if kw in p["title"].lower() or kw in p["summary"].lower()
        ]

    all_posts = all_posts[:limit]

    return json.dumps({"news": all_posts, "count": len(all_posts)}, ensure_ascii=False)
