import json
from pathlib import Path

from alpha_agents.config import DB_PATH
from alpha_agents.data.db import get_connection

DEFAULT_MIN_MARKET_CAP = 1_000_000_000  # 10亿


def filter_stocks_fn(
    stock_codes: list[str],
    db_path: Path = DB_PATH,
    min_market_cap: float = DEFAULT_MIN_MARKET_CAP,
) -> str:
    conn = get_connection(db_path)
    try:
        placeholders = ",".join("?" for _ in stock_codes)
        rows = conn.execute(
            f"SELECT code, name, market_cap, industry, is_st, is_suspended "
            f"FROM stocks WHERE code IN ({placeholders})",
            stock_codes,
        ).fetchall()

        kept = []
        removed = []
        for r in rows:
            reasons = []
            if r["is_st"]:
                reasons.append("ST")
            if r["is_suspended"]:
                reasons.append("停牌")
            if r["market_cap"] is not None and r["market_cap"] < min_market_cap:
                reasons.append(f"市值不足{min_market_cap/1e8:.0f}亿")

            stock = {"code": r["code"], "name": r["name"], "market_cap": r["market_cap"], "industry": r["industry"]}
            if reasons:
                removed.append({**stock, "reasons": reasons})
            else:
                kept.append(stock)

        return json.dumps({"stocks": kept, "removed": removed}, ensure_ascii=False)
    finally:
        conn.close()
