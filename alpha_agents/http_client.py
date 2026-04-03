"""Shared HTTP client with anti-scraping measures.

Provides a configured httpx.Client with:
- Rotating User-Agent headers
- Per-domain rate limiting
- Automatic retry with exponential backoff
- Random request jitter
- Optional Cloudflare Worker proxy for IP rotation
"""

import json as _json
import logging
import os
import random
import time
import threading
from collections import defaultdict
from contextlib import contextmanager

import httpx

logger = logging.getLogger(__name__)

# Realistic browser User-Agent strings
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0",
]

# Common browser headers
_BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Cache-Control": "no-cache",
}

# Default settings
DEFAULT_TIMEOUT = 15
MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.0  # seconds
MIN_REQUEST_INTERVAL = 1.0  # min seconds between requests to same domain
JITTER_RANGE = (0.3, 1.5)  # random delay range in seconds


def random_ua() -> str:
    """Return a random User-Agent string."""
    return random.choice(_USER_AGENTS)


def get_headers(extra: dict | None = None) -> dict:
    """Build request headers with a random User-Agent."""
    headers = {**_BASE_HEADERS, "User-Agent": random_ua()}
    if extra:
        headers.update(extra)
    return headers


class _DomainThrottle:
    """Thread-safe per-domain rate limiter."""

    def __init__(self, min_interval: float = MIN_REQUEST_INTERVAL):
        self._min_interval = min_interval
        self._last_request: dict[str, float] = defaultdict(float)
        self._lock = threading.Lock()

    def wait(self, domain: str) -> None:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_request[domain]
            if elapsed < self._min_interval:
                sleep_time = self._min_interval - elapsed + random.uniform(*JITTER_RANGE)
                time.sleep(sleep_time)
            else:
                # Small random jitter even when not throttled
                time.sleep(random.uniform(0.1, 0.5))
            self._last_request[domain] = time.monotonic()


_throttle = _DomainThrottle()


def _extract_domain(url: str) -> str:
    """Extract domain from URL for rate limiting."""
    from urllib.parse import urlparse
    return urlparse(url).netloc


def fetch(
    url: str,
    *,
    method: str = "GET",
    headers: dict | None = None,
    params: dict | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_retries: int = MAX_RETRIES,
    throttle: bool = True,
    follow_redirects: bool = True,
    **kwargs,
) -> httpx.Response:
    """Make an HTTP request with anti-scraping protections.

    Features:
    - Random User-Agent on each request
    - Per-domain rate limiting with jitter
    - Retry with exponential backoff on transient errors
    - Proper browser-like headers

    Args:
        url: Target URL.
        method: HTTP method.
        headers: Extra headers (merged with defaults).
        params: Query parameters.
        timeout: Request timeout in seconds.
        max_retries: Max retry attempts on failure.
        throttle: Whether to apply per-domain rate limiting.
        follow_redirects: Follow HTTP redirects.
        **kwargs: Passed to httpx.Client.request().

    Returns:
        httpx.Response

    Raises:
        httpx.HTTPStatusError: On non-retryable HTTP errors.
        httpx.ConnectError: After all retries exhausted.
    """
    domain = _extract_domain(url)
    req_headers = get_headers(headers)

    last_exc = None
    for attempt in range(max_retries + 1):
        if throttle:
            _throttle.wait(domain)

        try:
            with httpx.Client(
                timeout=timeout,
                follow_redirects=follow_redirects,
            ) as client:
                resp = client.request(
                    method, url, headers=req_headers, params=params, **kwargs
                )
                # Retry on server errors and rate limits
                if resp.status_code in (429, 500, 502, 503, 504):
                    if attempt < max_retries:
                        delay = RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 1)
                        logger.warning(
                            "HTTP %d from %s, retrying in %.1fs (attempt %d/%d)",
                            resp.status_code, domain, delay, attempt + 1, max_retries,
                        )
                        time.sleep(delay)
                        # Rotate UA on retry
                        req_headers["User-Agent"] = random_ua()
                        continue

                resp.raise_for_status()
                return resp

        except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout) as e:
            last_exc = e
            if attempt < max_retries:
                delay = RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 1)
                logger.warning(
                    "%s for %s, retrying in %.1fs (attempt %d/%d)",
                    type(e).__name__, domain, delay, attempt + 1, max_retries,
                )
                time.sleep(delay)
                req_headers["User-Agent"] = random_ua()
            else:
                raise

    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Cloudflare Worker proxy for IP rotation
# ---------------------------------------------------------------------------

# Set these env vars to enable CF Worker proxy:
#   CF_WORKER_URL=https://your-worker.your-subdomain.workers.dev
#   CF_WORKER_AUTH_TOKEN=your-secret-token  (optional, but recommended)

_CF_WORKER_URL = os.environ.get("CF_WORKER_URL", "")
_CF_WORKER_AUTH_TOKEN = os.environ.get("CF_WORKER_AUTH_TOKEN", "")


def cf_worker_available() -> bool:
    """Check if Cloudflare Worker proxy is configured."""
    return bool(_CF_WORKER_URL)


def fetch_via_worker(
    url: str,
    *,
    method: str = "GET",
    headers: dict | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> httpx.Response:
    """Fetch a URL through the Cloudflare Worker proxy.

    The Worker runs on CF edge nodes with rotating IPs, making it
    much harder for targets to block by IP. Free tier = 100k req/day.

    Falls back to direct fetch() if Worker is not configured.

    Returns:
        An httpx.Response-like object. The .text property contains
        the response body, .status_code contains the HTTP status.
    """
    if not _CF_WORKER_URL:
        logger.debug("CF Worker not configured, falling back to direct fetch")
        return fetch(url, method=method, headers=headers, timeout=timeout)

    req_headers = get_headers(headers)
    worker_headers = {"Content-Type": "application/json"}
    if _CF_WORKER_AUTH_TOKEN:
        worker_headers["Authorization"] = f"Bearer {_CF_WORKER_AUTH_TOKEN}"

    payload = {
        "url": url,
        "method": method,
        "headers": req_headers,
        "timeout": timeout * 1000,  # Worker expects milliseconds
    }

    try:
        with httpx.Client(timeout=timeout + 5) as client:
            resp = client.post(
                _CF_WORKER_URL,
                headers=worker_headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        if "error" in data:
            raise httpx.HTTPError(f"Worker error: {data['error']}")

        cf_info = data.get("cf", {})
        logger.debug(
            "CF Worker fetch: %s → %d (colo=%s, country=%s)",
            url, data.get("status", 0),
            cf_info.get("colo", "?"), cf_info.get("country", "?"),
        )

        # Construct a minimal Response-like object
        proxied = httpx.Response(
            status_code=data.get("status", 200),
            text=data.get("body", ""),
        )
        return proxied

    except Exception as e:
        logger.warning("CF Worker fetch failed for %s: %s, falling back to direct", url, e)
        return fetch(url, method=method, headers=headers, timeout=timeout)


@contextmanager
def client_session(
    *,
    timeout: int = DEFAULT_TIMEOUT,
    follow_redirects: bool = True,
    extra_headers: dict | None = None,
):
    """Context manager for a pre-configured httpx.Client with random UA.

    Use this when you need to make multiple requests in a session
    (e.g., iterating RSS feeds).
    """
    headers = get_headers(extra_headers)
    with httpx.Client(
        timeout=timeout,
        follow_redirects=follow_redirects,
        headers=headers,
    ) as client:
        yield client
