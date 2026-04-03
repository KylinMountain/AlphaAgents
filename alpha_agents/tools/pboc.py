"""Fetch monetary policy announcements from the People's Bank of China (PBOC).

Source: 中国人民银行官网 — no API key required.
"""

import json
import logging
import re
from html.parser import HTMLParser

from alpha_agents.http_client import fetch as http_fetch

logger = logging.getLogger(__name__)

PBOC_URL = "http://www.pbc.gov.cn/goutongjiaoliu/113456/113469/index.html"
PBOC_BASE = "http://www.pbc.gov.cn"


class _PBOCHTMLParser(HTMLParser):
    """Simple HTML parser to extract news list items from PBOC page.

    The PBOC news page uses a pattern where each list item contains an <a> tag
    with a title attribute and href, accompanied by a date in a <span> tag.
    """

    def __init__(self) -> None:
        super().__init__()
        self.items: list[dict] = []
        self._in_list_item = False
        self._current_title = ""
        self._current_link = ""
        self._current_date = ""
        self._capture_date = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict = dict(attrs)
        if tag == "a" and attr_dict.get("href", ""):
            href = attr_dict["href"] or ""
            title = attr_dict.get("title", "")
            # PBOC links to news articles typically contain a numeric path
            if title and ("/goutongjiaoliu/" in href or href.startswith("/")):
                self._in_list_item = True
                self._current_title = title
                if href.startswith("http"):
                    self._current_link = href
                else:
                    self._current_link = PBOC_BASE + href
        if tag == "span":
            self._capture_date = True

    def handle_data(self, data: str) -> None:
        if self._capture_date and self._in_list_item:
            stripped = data.strip()
            # Match date patterns like 2026-04-01 or 2026.04.01
            if re.match(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}", stripped):
                self._current_date = stripped

    def handle_endtag(self, tag: str) -> None:
        if tag == "span":
            self._capture_date = False
        # Finalize item when we have enough data
        if self._in_list_item and self._current_title and tag in ("li", "tr", "div"):
            self.items.append({
                "title": self._current_title,
                "summary": self._current_title,
                "time": self._current_date,
                "source": "中国人民银行",
                "link": self._current_link,
            })
            self._in_list_item = False
            self._current_title = ""
            self._current_link = ""
            self._current_date = ""


def _parse_pboc_html(html_text: str) -> list[dict]:
    """Parse PBOC HTML page to extract news items."""
    # Try regex-based extraction first (more reliable for PBOC's structure)
    items: list[dict] = []

    # Pattern: <a> tags with title attribute followed by date spans
    # PBOC uses patterns like: <a href="..." title="新闻标题">新闻标题</a> ... <span>2026-04-01</span>
    link_pattern = re.compile(
        r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*title=["\']([^"\']+)["\']'
        r'|<a\s+[^>]*title=["\']([^"\']+)["\'][^>]*href=["\']([^"\']+)["\']'
    )
    date_pattern = re.compile(r"(\d{4}[-./]\d{1,2}[-./]\d{1,2})")

    # Split by list item boundaries
    segments = re.split(r"<(?:li|tr)[^>]*>", html_text)

    for segment in segments:
        link_match = link_pattern.search(segment)
        if not link_match:
            continue

        if link_match.group(1) and link_match.group(2):
            href, title = link_match.group(1), link_match.group(2)
        else:
            title, href = link_match.group(3), link_match.group(4)

        if not title or not href:
            continue

        date_match = date_pattern.search(segment)
        date_str = date_match.group(1) if date_match else ""

        if href.startswith("http"):
            full_link = href
        elif href.startswith("/"):
            full_link = PBOC_BASE + href
        else:
            full_link = href

        items.append({
            "title": title.strip(),
            "summary": title.strip(),
            "time": date_str,
            "source": "中国人民银行",
            "link": full_link,
        })

    # Fallback to HTML parser if regex found nothing
    if not items:
        parser = _PBOCHTMLParser()
        try:
            parser.feed(html_text)
        except Exception:
            logger.warning("HTML parser failed for PBOC page")
        items = parser.items

    return items


def get_pboc_news_fn(limit: int = 20, keyword: str | None = None) -> str:
    """Fetch monetary policy news from the People's Bank of China.

    Args:
        limit: Maximum number of news items to return.
        keyword: Optional keyword to filter results (case-insensitive).
    """
    all_news: list[dict] = []

    try:
        resp = http_fetch(PBOC_URL)
        # PBOC pages are typically encoded in UTF-8 or GBK
        content = resp.text
        if not content or "charset" in resp.headers.get("content-type", ""):
            encoding = resp.encoding or "utf-8"
            content = resp.content.decode(encoding, errors="replace")
        all_news = _parse_pboc_html(content)
        logger.debug("Fetched %d items from PBOC", len(all_news))
    except Exception as e:
        logger.warning("Failed to fetch PBOC news: %s", e)

    if keyword:
        kw = keyword.lower()
        all_news = [
            n for n in all_news
            if kw in n["title"].lower() or kw in n["summary"].lower()
        ]

    all_news = all_news[:limit]

    return json.dumps({"news": all_news, "count": len(all_news)}, ensure_ascii=False)
