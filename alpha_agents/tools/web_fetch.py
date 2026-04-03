"""Web page fetching tool — extract text content from a URL."""

import json
import logging
import re

from alpha_agents.http_client import fetch as http_fetch, get_headers

logger = logging.getLogger(__name__)

MAX_CONTENT_LENGTH = 8000  # Truncate to avoid blowing up LLM context


def _extract_text(html: str) -> str:
    """Extract readable text from HTML, stripping tags and excess whitespace."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        # Remove script/style elements
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separator="\n")
    except Exception:
        # Fallback: regex strip tags
        text = re.sub(r"<[^>]+>", " ", html)

    # Clean up whitespace
    lines = [line.strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line)
    return text[:MAX_CONTENT_LENGTH]


def web_fetch_fn(url: str) -> str:
    """Fetch a web page and extract its text content.

    Args:
        url: The URL to fetch.

    Returns:
        JSON string with extracted text content.
    """
    try:
        from alpha_agents.config import no_proxy
        with no_proxy():
            response = http_fetch(url, headers=get_headers())
        text = _extract_text(response.text)
        return json.dumps({
            "url": url,
            "content": text,
            "status": response.status_code,
            "error": None,
        }, ensure_ascii=False)
    except Exception as e:
        logger.error("Failed to fetch %s: %s", url, e)
        return json.dumps({
            "url": url,
            "content": "",
            "status": 0,
            "error": str(e),
        }, ensure_ascii=False)
