"""Fetch Trump/political social media content.

Three data channels (tried in order):
1. Twitter Syndication API — direct tweet text, no API key, public endpoint
2. Google News RSS search — media reports of social posts, minutes delay
3. US political RSS feeds — The Hill, Politico, Fox News

No API key required for any channel.
"""

import json
import logging
import re
import xml.etree.ElementTree as ET

import httpx

from alpha_agents.config import no_proxy
from alpha_agents.http_client import fetch

logger = logging.getLogger(__name__)

# Twitter Syndication API — direct tweet content, no auth needed
# This is the public API used by Twitter embed widgets
TWITTER_ACCOUNTS = [
    ("realDonaldTrump", "Trump"),
    ("POTUS", "POTUS"),
    ("elonmusk", "Musk"),
]

SYNDICATION_URL = "https://syndication.twitter.com/srv/timeline-profile/screen-name/{username}"


def _fetch_tweets(username: str, label: str, limit: int = 10) -> list[dict]:
    """Fetch tweets via Twitter Syndication API (public, no auth)."""
    try:
        with no_proxy():
            r = httpx.get(
                SYNDICATION_URL.format(username=username),
                headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"},
                timeout=15,
                follow_redirects=True,
            )
        if r.status_code != 200:
            return []

        match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text)
        if not match:
            return []

        data = json.loads(match.group(1))
        entries = data.get("props", {}).get("pageProps", {}).get("timeline", {}).get("entries", [])

        items = []
        for e in entries[:limit]:
            tweet = e.get("content", {}).get("tweet", {})
            text = tweet.get("full_text", tweet.get("text", ""))
            created = tweet.get("created_at", "")
            if text:
                items.append({
                    "title": f"@{username}: {text[:80]}",
                    "summary": text[:300],
                    "time": created,
                    "source": f"X/@{username}",
                    "author": label,
                })
        return items
    except Exception as e:
        logger.debug("Twitter Syndication failed for @%s: %s", username, e)
        return []


# Google News RSS search queries — captures Trump Truth Social posts via media reports
GOOGLE_NEWS_FEEDS = [
    ("Trump Social Media", "https://news.google.com/rss/search?q=trump+truth+social+post&hl=en-US&gl=US&ceid=US:en"),
    ("Trump Policy", "https://news.google.com/rss/search?q=trump+tariff+OR+trade+OR+executive+order&when=1d&hl=en-US&gl=US&ceid=US:en"),
    ("Musk X Posts", "https://news.google.com/rss/search?q=elon+musk+posted+OR+tweeted+OR+announced&when=1d&hl=en-US&gl=US&ceid=US:en"),
]

# US political RSS feeds — high signal for market-moving policy news
POLITICAL_FEEDS = [
    ("The Hill", "https://thehill.com/feed/"),
    ("Politico", "https://rss.politico.com/politics-news.xml"),
    ("Fox Politics", "https://moxie.foxnews.com/google-publisher/politics.xml"),
]


def _parse_google_rss(xml_text: str, source_label: str) -> list[dict]:
    """Parse Google News RSS search results."""
    items: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        logger.warning("Failed to parse Google News RSS XML for %s", source_label)
        return items

    for item in root.iter("item"):
        title = item.findtext("title", "").strip()
        desc = item.findtext("description", "").strip()
        pub_date = item.findtext("pubDate", "").strip()
        link = item.findtext("link", "").strip()
        # Google News includes the original source in <source> tag
        media_source = item.findtext("source", "").strip()
        if title:
            items.append({
                "title": title,
                "summary": desc[:300],
                "time": pub_date,
                "source": media_source or source_label,
                "link": link,
                "author": source_label,
            })

    return items


def _parse_rss(xml_text: str, source: str) -> list[dict]:
    """Parse standard RSS 2.0 feed."""
    items: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        logger.warning("Failed to parse %s RSS XML", source)
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
            })

    return items


def _fetch_feed(url: str, source: str, is_google: bool = False) -> list[dict]:
    """Fetch and parse a single feed."""
    try:
        resp = fetch(url, max_retries=1)
        if is_google:
            return _parse_google_rss(resp.text, source)
        return _parse_rss(resp.text, source)
    except Exception as e:
        logger.debug("Feed unavailable (%s): %s", source, e)
        return []


def get_social_media_fn(limit: int = 20, keyword: str | None = None) -> str:
    """Fetch Trump/Musk social media content and US political news.

    Uses Google News RSS search to capture Truth Social posts reported by media,
    plus US political feeds for policy announcements.

    Args:
        limit: Maximum number of posts to return.
        keyword: Optional keyword to filter results (case-insensitive).
    """
    all_posts: list[dict] = []

    # 1. Twitter Syndication — direct tweets (highest priority)
    for username, label in TWITTER_ACCOUNTS:
        tweets = _fetch_tweets(username, label, limit=10)
        all_posts.extend(tweets)

    # 2. Google News search feeds — captures social posts reported by media
    for name, url in GOOGLE_NEWS_FEEDS:
        items = _fetch_feed(url, name, is_google=True)
        all_posts.extend(items)

    # US political RSS feeds
    for name, url in POLITICAL_FEEDS:
        items = _fetch_feed(url, name)
        all_posts.extend(items)

    if keyword:
        kw = keyword.lower()
        all_posts = [
            p for p in all_posts
            if kw in p["title"].lower() or kw in p["summary"].lower()
        ]

    all_posts = all_posts[:limit]

    return json.dumps({"news": all_posts, "count": len(all_posts)}, ensure_ascii=False)
