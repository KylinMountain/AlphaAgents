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
