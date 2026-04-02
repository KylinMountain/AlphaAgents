import json
from pathlib import Path

from alpha_agents.config import DB_PATH
from alpha_agents.data.db import get_connection


def search_stocks_fn(keyword: str, db_path: Path = DB_PATH) -> str:
    conn = get_connection(db_path)
    try:
        concepts = conn.execute(
            "SELECT id, name FROM concepts WHERE name LIKE ?",
            (f"%{keyword}%",),
        ).fetchall()

        matches = []
        for concept in concepts:
            stocks = conn.execute(
                """
                SELECT s.code, s.name, s.market_cap, s.industry
                FROM concept_stocks cs
                JOIN stocks s ON s.code = cs.stock_code
                WHERE cs.concept_id = ?
                ORDER BY s.market_cap DESC NULLS LAST
                """,
                (concept["id"],),
            ).fetchall()

            matches.append({
                "concept": concept["name"],
                "stock_count": len(stocks),
                "stocks": [
                    {
                        "code": s["code"],
                        "name": s["name"],
                        "market_cap": s["market_cap"],
                        "industry": s["industry"],
                    }
                    for s in stocks
                ],
            })

        return json.dumps({"keyword": keyword, "matches": matches}, ensure_ascii=False)
    finally:
        conn.close()
