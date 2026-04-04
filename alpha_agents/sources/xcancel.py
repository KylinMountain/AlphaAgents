"""Fetch latest tweets from X/Twitter via xcancel.com mirror.

xcancel.com is a public Nitter-like frontend that renders Twitter profiles
as plain HTML — no API key or authentication needed.

Not all accounts are available (some return 503), so the fetcher silently
skips unavailable profiles and reports what it could get.
"""

import json
import logging
import re
from html import unescape

from alpha_agents.http_client import fetch

logger = logging.getLogger(__name__)

# Accounts to follow — (display_name, handle)
# Easy to extend: just add a tuple here.
ACCOUNTS: list[tuple[str, str]] = [
    ("Elon Musk", "elonmusk"),
    ("Donald Trump", "realDonaldTrump"),
    ("JD Vance", "JDVance"),
    ("POTUS", "POTUS"),
    ("White House", "WhiteHouse"),
    ("Reuters", "Reuters"),
    ("Bloomberg", "business"),
    ("WSJ", "WSJ"),
    ("CNBC", "CNBC"),
    ("Fed Reserve", "federalreserve"),
    ("SEC Gov", "SECGov"),
    ("China Xinhua News", "XHNews"),
]

BASE_URL = "https://xcancel.com"


def _clean_html(text: str) -> str:
    """Strip HTML tags and decode entities."""
    text = re.sub(r"<[^>]+>", "", text)
    return unescape(text).strip()


def _parse_tweets(html: str, handle: str, display_name: str) -> list[dict]:
    """Extract tweets from xcancel profile HTML.

    xcancel renders tweets in <div class="tweet-content ..."> blocks,
    with timestamps in <span class="tweet-date"><a ...>time</a></span>.
    """
    tweets: list[dict] = []

    # Each tweet is in a timeline-item div containing tweet-content and tweet-date
    # Match tweet content blocks
    content_pattern = re.compile(
        r'class="tweet-content[^"]*"[^>]*>(.*?)</div>',
        re.DOTALL,
    )
    # Match tweet dates (inside <a> with href containing status id)
    date_pattern = re.compile(
        r'class="tweet-date"[^>]*>\s*<a[^>]*title="([^"]*)"',
        re.DOTALL,
    )
    # Match tweet links for permalink
    link_pattern = re.compile(
        r'class="tweet-link"[^>]*href="([^"]*)"',
    )

    contents = content_pattern.findall(html)
    dates = date_pattern.findall(html)
    links = link_pattern.findall(html)

    for i, raw_content in enumerate(contents):
        text = _clean_html(raw_content)
        if not text:
            continue

        tweet = {
            "title": f"@{handle}: {text[:80]}{'...' if len(text) > 80 else ''}",
            "summary": text[:300],
            "source": f"X/@{handle}",
            "author": display_name,
        }

        if i < len(dates):
            tweet["time"] = dates[i]
        if i < len(links):
            tweet["link"] = f"https://x.com{links[i]}"

        tweets.append(tweet)

    return tweets


def _fetch_account(handle: str, display_name: str) -> list[dict]:
    """Fetch tweets for a single account. Returns empty list on failure."""
    url = f"{BASE_URL}/{handle}"
    try:
        resp = fetch(url, max_retries=1, timeout=10)
        tweets = _parse_tweets(resp.text, handle, display_name)
        if tweets:
            logger.debug("xcancel: fetched %d tweets from @%s", len(tweets), handle)
        return tweets
    except Exception as e:
        logger.debug("xcancel: @%s unavailable: %s", handle, e)
        return []


def get_xcancel_fn(limit: int = 30, accounts: list[tuple[str, str]] | None = None) -> str:
    """Fetch latest tweets from X/Twitter via xcancel mirror.

    Args:
        limit: Maximum total tweets to return.
        accounts: Optional override list of (display_name, handle) tuples.
                  Defaults to ACCOUNTS.
    """
    target_accounts = accounts or ACCOUNTS
    all_tweets: list[dict] = []

    for display_name, handle in target_accounts:
        tweets = _fetch_account(handle, display_name)
        all_tweets.extend(tweets)

    all_tweets = all_tweets[:limit]

    return json.dumps({"news": all_tweets, "count": len(all_tweets)}, ensure_ascii=False)
