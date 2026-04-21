"""Shared Tushare Pro client.

Uses the shared community endpoint (http://121.40.135.59:8010/) via a token
supplied either by env var ``TUSHARE_TOKEN`` or a documented default.

The custom ``__http_url`` MUST be set — without it the shared token is
rejected by the official api.tushare.pro endpoint.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

# Default to the shared community token if env not set
_DEFAULT_TOKEN = "gLtNTVsLQTjuToBauxRcUfHaSIMPGLBFAqDPmsWcNmIzLTczKPgonWkcEhadcQMO"
_DEFAULT_URL = "http://121.40.135.59:8010/"

_pro = None


def get_pro():
    """Return a cached ts.pro_api() instance pointed at the shared endpoint."""
    global _pro
    if _pro is not None:
        return _pro
    import tushare as ts
    token = os.environ.get("TUSHARE_TOKEN", _DEFAULT_TOKEN)
    url = os.environ.get("TUSHARE_HTTP_URL", _DEFAULT_URL)
    pro = ts.pro_api(token)
    # Required for the shared community endpoint; no-op for the official one
    pro._DataApi__http_url = url
    _pro = pro
    return pro


def call_with_retry(api_name: str, retries: int = 3,
                    initial_delay: float = 1.0, **kwargs) -> Any:
    """Call a Tushare endpoint with retry/backoff on transient failures.

    Shared tokens may hit per-minute rate limits; backoff handles that.
    Returns the DataFrame on success, or raises the final exception.
    """
    pro = get_pro()
    fn = getattr(pro, api_name)
    last_err = None
    for attempt in range(retries):
        try:
            return fn(**kwargs)
        except Exception as e:
            last_err = e
            msg = str(e)
            # Permission errors are permanent — don't retry
            if "权限" in msg or "token不对" in msg:
                raise
            # Rate-limit / transient — backoff
            delay = initial_delay * (2 ** attempt)
            logger.debug("tushare %s attempt %d failed: %s (sleep %.1fs)",
                         api_name, attempt + 1, msg[:80], delay)
            time.sleep(delay)
    raise last_err if last_err else RuntimeError(f"{api_name} failed")


def is_permission_error(exc: Exception) -> bool:
    msg = str(exc)
    return "权限" in msg or "没有该接口权限" in msg
