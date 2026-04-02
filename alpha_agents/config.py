import os
from contextlib import contextmanager
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "alpha_agents" / "data"
PROMPTS_DIR = PROJECT_ROOT / "alpha_agents" / "prompts"

DB_PATH = DATA_DIR / "stocks.db"
WATCHLIST_PATH = CONFIG_DIR / "watchlist.json"

# News monitor settings
MONITOR_INTERVAL_SECONDS = 300
NEWS_FETCH_LIMIT = 50


@contextmanager
def no_proxy():
    """Temporarily disable HTTP proxy for direct access to domestic APIs.

    macOS system proxy is read by urllib3 via urllib.request.getproxies().
    We monkey-patch it to return empty dict, which is more reliable than
    setting NO_PROXY env var.
    """
    import urllib.request
    saved_getproxies = urllib.request.getproxies
    urllib.request.getproxies = lambda: {}

    # Also clear env vars in case anything reads them directly
    saved_env = {}
    proxy_vars = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
    for var in proxy_vars:
        if var in os.environ:
            saved_env[var] = os.environ.pop(var)
    old_no_proxy = os.environ.get("NO_PROXY", "")
    os.environ["NO_PROXY"] = "*"
    try:
        yield
    finally:
        urllib.request.getproxies = saved_getproxies
        os.environ["NO_PROXY"] = old_no_proxy
        for var, val in saved_env.items():
            os.environ[var] = val
