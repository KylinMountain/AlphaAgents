"""Shared Tushare Pro client.

The endpoint is the **official** one by default. It used to default to a shared
community proxy (``http://121.40.135.59:8010/``) which, measured 2026-09-17,
answers every call with an empty frame after a five-second stall. Every
Tushare-backed archive therefore wrote nothing, and the failure looked like a
credential problem: the startup log said ``RemoteDisconnected`` on four
endpoints.

The token is read from ``TUSHARE_TOKEN``, ``TUSHARE_API_KEY`` or the
documented shared default, in that order. Two names because both are in use:
``.env`` was written with ``TUSHARE_API_KEY`` while this module read
``TUSHARE_TOKEN``, so the operator's own token was silently ignored and the
call fell back to the shared one — which the official endpoint rejects with
``您的token不对``. Accepting both is the only fix that breaks neither.

Measured against the official endpoint: the operator's token returns data in
0.3s; the shared default is refused. So the fallback is a last resort for a
machine with no token configured, not a substitute for one.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

#: Last-resort token for a machine with nothing configured. Rejected by the
#: official endpoint (measured 2026-09-17: ``您的token不对``), so it only
#: helps against an endpoint that honours it. Kept rather than removed
#: because deleting it would turn a missing token into an import-time crash
#: in five call sites; :func:`token_source` reports which one was used so the
#: degradation is visible instead of silent.
_DEFAULT_TOKEN = "gLtNTVsLQTjuToBauxRcUfHaSIMPGLBFAqDPmsWcNmIzLTczKPgonWkcEhadcQMO"

#: The official endpoint. The community proxy that used to be the default is
#: kept as an opt-in, never as a fallback: it is dead, and a fallback to a dead
#: endpoint is indistinguishable from having no data source at all.
_DEFAULT_URL = "https://api.tushare.pro"

#: Read in order; the first non-empty one wins.
_TOKEN_ENV_VARS = ("TUSHARE_TOKEN", "TUSHARE_API_KEY")

_pro = None


def configured_token() -> tuple[str, str]:
    """``(token, source)`` where source names the variable the token came from.

    Returning the source rather than only the token is what makes the
    degradation visible: a run using the shared default has no operator token
    configured, and that should be a log line, not a mystery.
    """
    for name in _TOKEN_ENV_VARS:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value, name
    return _DEFAULT_TOKEN, "built-in shared default"


def get_pro():
    """Return a cached ``ts.pro_api()`` instance.

    The custom ``__http_url`` is only overridden when ``TUSHARE_HTTP_URL`` is
    set. It used to be set unconditionally to the shared proxy, which meant an
    operator could not reach the official endpoint however they configured
    their token — the endpoint was hard-coded, not defaulted.
    """
    global _pro
    if _pro is not None:
        return _pro
    import tushare as ts
    token, source = configured_token()
    if source == "built-in shared default":
        logger.warning(
            "No TUSHARE_TOKEN or TUSHARE_API_KEY is set; falling back to the "
            "built-in shared token, which the official endpoint rejects. "
            "Tushare-backed archives will write nothing.")
    else:
        logger.debug("Tushare token from %s", source)
    url = (os.environ.get("TUSHARE_HTTP_URL") or "").strip() or _DEFAULT_URL
    pro = ts.pro_api(token)
    # Only pin the URL for a non-official endpoint. Setting it to the official
    # host is harmless but redundant, and skipping it keeps the SDK's own
    # default in play if Tushare ever moves.
    if url.rstrip("/") != _DEFAULT_URL:
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
