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

# ---------------------------------------------------------------------------
# DeepSeek LLM (backup for VPA analysis — prompt caching saves cost)
# Primary VPA model is AGENT (LongCat). DeepSeek is fallback.
# ---------------------------------------------------------------------------
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

# News monitor settings
MONITOR_INTERVAL_SECONDS = int(os.environ.get("MONITOR_INTERVAL_SECONDS", "300"))
NEWS_FETCH_LIMIT = int(os.environ.get("NEWS_FETCH_LIMIT", "50"))

# ---------------------------------------------------------------------------
# Tradable universe — only recommend stocks from markets the user can actually
# trade. Defaults to main board + ChiNext; override via TRADABLE_PREFIXES env.
#   - 60xxxx: 沪市主板 (600/601/603/605)
#   - 000xxx / 001xxx / 002xxx / 003xxx: 深市主板
#   - 300xxx / 301xxx: 创业板
# Excluded by default:
#   - 688/689xxx: 科创板 (需要 50万+ 开通权限)
#   - 4xxxxx / 8xxxxx / 92xxxx: 北交所
#   - 900xxx / 200xxx / 201xxx: B股
# ---------------------------------------------------------------------------
TRADABLE_PREFIXES = tuple(
    os.environ.get(
        "TRADABLE_PREFIXES",
        "60,000,001,002,003,300,301",
    ).split(",")
)


def is_tradable(code: str) -> bool:
    """True if ``code`` is in a market tier the user can trade.

    Keep cheap — pure string prefix check, no DB lookup. Used at every
    candidate-selection chokepoint (stock_filter / sector_beta / anomaly).
    """
    if not code or not code.strip().isdigit() or len(code.strip()) != 6:
        return False
    return code.startswith(TRADABLE_PREFIXES)


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
