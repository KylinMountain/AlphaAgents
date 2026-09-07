"""Fetch international news from public RSS feeds.

Sources: BBC, CNBC, Google News, Al Jazeera, Bloomberg, FT,
France24, DW, RT, Middle East Eye, Haaretz — no API key required.
"""

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
import xml.etree.ElementTree as ET
from datetime import datetime

from alpha_agents.http_client import client_session

logger = logging.getLogger(__name__)

RSS_FEEDS = [
    # --- 英美主流 ---
    ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("BBC Business", "https://feeds.bbci.co.uk/news/business/rss.xml"),
    ("CNBC World", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100727362"),
    ("CNBC Economy", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258"),
    ("Google News World", "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRGx1YlY4U0FtVnVHZ0pWVXlnQVAB?hl=en-US&gl=US&ceid=US:en"),
    ("Google News Business", "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRGx6TVdZU0FtVnVHZ0pWVXlnQVAB?hl=en-US&gl=US&ceid=US:en"),
    # --- 财经专业 ---
    ("Bloomberg Markets", "https://feeds.bloomberg.com/markets/news.rss"),
    ("Financial Times", "https://www.ft.com/rss/home"),
    # --- 中东 ---
    ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
    ("Middle East Eye", "https://www.middleeasteye.net/rss"),
    ("Haaretz", "https://www.haaretz.com/srv/haaretz-latest-headlines"),
    # --- 欧洲/多视角 ---
    ("France24", "https://www.france24.com/en/rss"),
    ("DW News", "https://rss.dw.com/rdf/rss-en-world"),
    ("RT News", "https://www.rt.com/rss/news/"),
]

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

    # RDF 1.0 (e.g. DW News) — default namespace means iter("item") won't match
    if not items:
        rss10 = "http://purl.org/rss/1.0/"
        dc = "http://purl.org/dc/elements/1.1/"
        for item in root.iter(f"{{{rss10}}}item"):
            title = (item.findtext(f"{{{rss10}}}title", "") or "").strip()
            desc = (item.findtext(f"{{{rss10}}}description", "") or "").strip()
            pub_date = (item.findtext(f"{{{dc}}}date", "") or "").strip()
            link = (item.findtext(f"{{{rss10}}}link", "") or "").strip()
            if title:
                items.append({
                    "title": title,
                    "summary": desc[:300],
                    "time": pub_date,
                    "source": source,
                    "link": link,
                })

    # Atom (if still no items found)
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
    """Fetch international news from multiple RSS feeds. Replay-aware.

    Each RSS feed has its own source name (BBC World, Bloomberg, etc.).
    Replay reads all 15 known feed names from the snapshot.
    """
    from alpha_agents.data.snapshot_store import replay_news_response, save_news
    feed_names = [name for name, _ in RSS_FEEDS]
    replay = replay_news_response(feed_names, limit, keyword)
    if replay is not None:
        return replay

    all_news: list[dict] = []

    # Fetched in parallel, not in sequence. Five of these fourteen feeds
    # are dead and each burns its own connect timeout plus retries, so
    # serially the slow ones added up past the ingest task's ceiling and
    # cancelled the whole sweep — including the feeds that had already
    # answered. In parallel the cost is the slowest feed, not their sum.
    def _one(source_name: str, url: str) -> list[dict]:
        try:
            with client_session() as client:
                resp = client.get(url)
                resp.raise_for_status()
                items = _parse_rss(resp.text, source_name)
            logger.debug("Fetched %d items from %s", len(items), source_name)
            return items
        except Exception as e:
            logger.warning("Failed to fetch %s: %s", source_name, e)
            return []

    with ThreadPoolExecutor(max_workers=min(8, len(RSS_FEEDS))) as pool:
        futures = {
            pool.submit(_one, name, url): name
            for name, url in RSS_FEEDS
        }
        for fut in as_completed(futures):
            all_news.extend(fut.result())

    # Capture per-source to preserve source attribution in news_items
    for source_name in feed_names:
        src_items = [n for n in all_news if n.get("source") == source_name]
        if src_items:
            try:
                save_news(source_name, src_items)
            except Exception as e:
                logger.debug("world_news capture failed for %s: %s", source_name, e)

    if keyword:
        kw = keyword.lower()
        all_news = [
            n for n in all_news
            if kw in n["title"].lower() or kw in n["summary"].lower()
        ]

    all_news = all_news[:limit]

    return json.dumps({"news": all_news, "count": len(all_news)}, ensure_ascii=False)
