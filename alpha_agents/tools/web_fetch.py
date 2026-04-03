"""Web page fetching tool — extract clean content from a URL via Jina Reader.

Jina Reader (r.jina.ai) is free, no API key needed, returns clean Markdown.
"""

import json
import logging

import httpx

logger = logging.getLogger(__name__)

MAX_CONTENT_LENGTH = 8000  # Truncate to avoid blowing up LLM context
JINA_PREFIX = "https://r.jina.ai/"


def web_fetch_fn(url: str) -> str:
    """Fetch a web page via Jina Reader and return clean Markdown content.

    Args:
        url: The URL to fetch.

    Returns:
        JSON string with extracted Markdown content.
    """
    try:
        jina_url = f"{JINA_PREFIX}{url}"
        response = httpx.get(jina_url, timeout=30, follow_redirects=True)
        response.raise_for_status()
        content = response.text[:MAX_CONTENT_LENGTH]
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
