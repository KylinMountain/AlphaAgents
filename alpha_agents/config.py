import os
from contextlib import contextmanager
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
PROMPTS_DIR = PROJECT_ROOT / "alpha_agents" / "prompts"

DB_PATH = DATA_DIR / "stocks.db"
CHROMA_PATH = DATA_DIR / "chroma"
MEMORY_DB_PATH = DATA_DIR / "memory.db"

# ---------------------------------------------------------------------------
# Embedding model (OpenAI-compatible, for concept vector search)
# Default: SiliconFlow free BGE-M3
# ---------------------------------------------------------------------------
EMBEDDING_API_KEY = os.environ.get("EMBEDDING_API_KEY", os.environ.get("SILICONFLOW_API_KEY", ""))
EMBEDDING_BASE_URL = os.environ.get("EMBEDDING_BASE_URL", "https://api.siliconflow.cn/v1")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-m3")

# ---------------------------------------------------------------------------
# Digest LLM (OpenAI-compatible, cheap model for news filtering)
# Default: SiliconFlow free Qwen
# ---------------------------------------------------------------------------
DIGEST_API_KEY = os.environ.get("DIGEST_API_KEY", os.environ.get("SILICONFLOW_API_KEY", ""))
DIGEST_BASE_URL = os.environ.get("DIGEST_BASE_URL", "https://api.siliconflow.cn/v1")
DIGEST_MODEL = os.environ.get("DIGEST_MODEL", "Qwen/Qwen2.5-7B-Instruct")

# ---------------------------------------------------------------------------
# Agent LLM (OpenAI-compatible, for strategist & geopolitical agents)
# Works with any OpenAI-compatible provider: DashScope, DeepSeek, SiliconFlow, etc.
# ---------------------------------------------------------------------------
AGENT_API_KEY = os.environ.get("AGENT_API_KEY", os.environ.get("SILICONFLOW_API_KEY", ""))
AGENT_BASE_URL = os.environ.get("AGENT_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
AGENT_MODEL = os.environ.get("AGENT_MODEL", "qwen-plus")

# News monitor settings
MONITOR_INTERVAL_SECONDS = int(os.environ.get("MONITOR_INTERVAL_SECONDS", "300"))
NEWS_FETCH_LIMIT = int(os.environ.get("NEWS_FETCH_LIMIT", "50"))

# ---------------------------------------------------------------------------
# Intraday anomaly detection thresholds
# ---------------------------------------------------------------------------
ANOMALY_SECTOR_CHANGE_PCT = float(os.environ.get("ANOMALY_SECTOR_CHANGE_PCT", "2.0"))
ANOMALY_LIMIT_UP_COUNT = int(os.environ.get("ANOMALY_LIMIT_UP_COUNT", "30"))
ANOMALY_BREADTH_EXTREME_HIGH = float(os.environ.get("ANOMALY_BREADTH_EXTREME_HIGH", "5.0"))
ANOMALY_BREADTH_EXTREME_LOW = float(os.environ.get("ANOMALY_BREADTH_EXTREME_LOW", "0.3"))

# ---------------------------------------------------------------------------
# Agent retry settings
# ---------------------------------------------------------------------------
AGENT_MAX_RETRIES = int(os.environ.get("AGENT_MAX_RETRIES", "2"))
AGENT_RETRY_BASE_DELAY = float(os.environ.get("AGENT_RETRY_BASE_DELAY", "3.0"))


@contextmanager
def no_proxy():
    """Temporarily disable HTTP proxy for direct access to domestic APIs.

    Patches both urllib and requests to bypass macOS system proxy.
    """
    import urllib.request
    import requests

    # Patch urllib
    saved_getproxies = urllib.request.getproxies
    urllib.request.getproxies = lambda: {}

    # Patch requests — trust_env=False prevents reading macOS system proxy
    saved_trust_env_init = requests.Session.__init__
    _original_init = saved_trust_env_init

    def _patched_init(self, *args, **kwargs):
        _original_init(self, *args, **kwargs)
        self.trust_env = False

    requests.Session.__init__ = _patched_init

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
        requests.Session.__init__ = saved_trust_env_init
        os.environ["NO_PROXY"] = old_no_proxy
        for var, val in saved_env.items():
            os.environ[var] = val
