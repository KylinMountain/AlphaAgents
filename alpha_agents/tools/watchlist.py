import json
from pathlib import Path

from alpha_agents.config import WATCHLIST_PATH


def get_watchlist_fn(watchlist_path: Path = WATCHLIST_PATH) -> str:
    try:
        data = json.loads(watchlist_path.read_text(encoding="utf-8"))
        return json.dumps(data, ensure_ascii=False)
    except FileNotFoundError:
        return json.dumps({"stocks": [], "error": "自选股文件不存在"}, ensure_ascii=False)
