import os
from contextlib import contextmanager
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "alpha_agents" / "data"
PROMPTS_DIR = PROJECT_ROOT / "alpha_agents" / "prompts"

DB_PATH = DATA_DIR / "stocks.db"
CHROMA_PATH = DATA_DIR / "chroma"

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
