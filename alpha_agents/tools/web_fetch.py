"""Web page fetching tool — extract clean content from a URL.

Uses trafilatura for main content extraction (local, free, no API).
Falls back to BeautifulSoup if trafilatura can't extract content.
"""

import json
import logging
import re

import httpx
import trafilatura

from alpha_agents.config import no_proxy

logger = logging.getLogger(__name__)

MAX_CONTENT_LENGTH = 8000


def _extract_with_bs4(html: str) -> str:
    """Fallback: extract text with BeautifulSoup."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()
        text = soup.get_text(separator="\n")
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html)

    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def web_fetch_fn(url: str) -> str:
    """Fetch a web page and extract its main text content.

    Tries trafilatura first (best quality), falls back to BeautifulSoup.
    Fully local, no external API needed.
    """
    try:
        with no_proxy():
            response = httpx.get(url, timeout=15, follow_redirects=True, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            })
            response.raise_for_status()

        html = response.text

        # Try trafilatura first (best at extracting article content)
        content = trafilatura.extract(html, include_links=False, include_comments=False)
        if not content:
            content = trafilatura.extract(html, favor_recall=True)
        if not content:
            # Fallback to BeautifulSoup
            content = _extract_with_bs4(html)

        content = content[:MAX_CONTENT_LENGTH] if content else ""

        return json.dumps({
            "url": url,
            "content": content,
            "error": None,
        }, ensure_ascii=False)
    except Exception as e:
        logger.error("Failed to fetch %s: %s", url, e)
        return json.dumps({
            "url": url,
            "content": "",
            "error": str(e),
        }, ensure_ascii=False)
