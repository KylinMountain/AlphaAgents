"""Watchlist — stored in SQLite, managed via Web API."""

import json
import logging

from alpha_agents.config import DB_PATH
from alpha_agents.data.db import get_connection

logger = logging.getLogger(__name__)


def get_watchlist_fn() -> str:
    """Get watchlist stocks from database."""
    try:
        conn = get_connection(DB_PATH)
        rows = conn.execute(
            "SELECT code, name, concepts, added_at FROM watchlist ORDER BY added_at"
        ).fetchall()
        conn.close()
        stocks = []
        for r in rows:
            stocks.append({
                "code": r["code"],
                "name": r["name"],
                "concepts": json.loads(r["concepts"]),
                "added_at": r["added_at"],
            })
        return json.dumps({"stocks": stocks}, ensure_ascii=False)
    except Exception as e:
        logger.error("Failed to read watchlist: %s", e)
        return json.dumps({"stocks": [], "error": str(e)}, ensure_ascii=False)


def add_to_watchlist(code: str, name: str, concepts: list[str] | None = None) -> dict:
    """Add a stock to the watchlist."""
    conn = get_connection(DB_PATH)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO watchlist (code, name, concepts) VALUES (?, ?, ?)",
            (code, name, json.dumps(concepts or [], ensure_ascii=False)),
        )
        conn.commit()
        return {"ok": True, "code": code, "name": name}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        conn.close()


def remove_from_watchlist(code: str) -> dict:
    """Remove a stock from the watchlist."""
    conn = get_connection(DB_PATH)
    try:
        cur = conn.execute("DELETE FROM watchlist WHERE code = ?", (code,))
        conn.commit()
        return {"ok": True, "deleted": cur.rowcount}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        conn.close()


def list_watchlist() -> list[dict]:
    """Return watchlist as a list of dicts (for API responses)."""
    conn = get_connection(DB_PATH)
    rows = conn.execute(
        "SELECT code, name, concepts, added_at FROM watchlist ORDER BY added_at"
    ).fetchall()
    conn.close()
    return [
        {"code": r["code"], "name": r["name"],
         "concepts": json.loads(r["concepts"]), "added_at": r["added_at"]}
        for r in rows
    ]
