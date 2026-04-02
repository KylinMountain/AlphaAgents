import json
import logging
from pathlib import Path

from alpha_agents.config import DB_PATH
from alpha_agents.data.db import get_connection

logger = logging.getLogger(__name__)


def _fetch_stocks_for_concept(conn, concept_id: int) -> list[dict]:
    """Fetch stocks linked to a concept, ordered by market cap."""
    stocks = conn.execute(
        """
        SELECT s.code, s.name, s.market_cap, s.industry
        FROM concept_stocks cs
        JOIN stocks s ON s.code = cs.stock_code
        WHERE cs.concept_id = ?
        ORDER BY s.market_cap DESC NULLS LAST
        """,
        (concept_id,),
    ).fetchall()
    return [
        {
            "code": s["code"],
            "name": s["name"],
            "market_cap": s["market_cap"],
            "industry": s["industry"],
        }
        for s in stocks
    ]


def _search_like(conn, keyword: str) -> list[dict]:
    """Fuzzy text search using SQL LIKE."""
    concepts = conn.execute(
        "SELECT id, name FROM concepts WHERE name LIKE ?",
        (f"%{keyword}%",),
    ).fetchall()

    matches = []
    for concept in concepts:
        stocks = _fetch_stocks_for_concept(conn, concept["id"])
        matches.append({
            "concept": concept["name"],
            "score": 1.0,
            "stock_count": len(stocks),
            "stocks": stocks,
        })
    return matches


def _search_semantic(conn, keyword: str, top_k: int = 10) -> list[dict]:
    """Semantic search using ChromaDB embeddings (if available)."""
    try:
        from alpha_agents.data.embeddings import search_concepts_semantic
        concept_matches = search_concepts_semantic(conn, keyword, top_k=top_k)
    except Exception:
        logger.debug("Semantic search unavailable, skipping", exc_info=True)
        return []

    matches = []
    for cm in concept_matches:
        stocks = _fetch_stocks_for_concept(conn, cm["id"])
        matches.append({
            "concept": cm["name"],
            "score": cm["score"],
            "stock_count": len(stocks),
            "stocks": stocks,
        })
    return matches


def search_stocks_fn(keyword: str, db_path: Path = DB_PATH) -> str:
    """Search stocks by concept keyword using both semantic and text matching.

    Tries semantic search first (if embeddings are built), then falls back
    to LIKE matching. Deduplicates results.
    """
    conn = get_connection(db_path)
    try:
        # Try semantic search first
        matches = _search_semantic(conn, keyword)

        # Always also do text search to catch exact matches
        like_matches = _search_like(conn, keyword)

        # Merge: add LIKE results that aren't already in semantic results
        seen_concepts = {m["concept"] for m in matches}
        for m in like_matches:
            if m["concept"] not in seen_concepts:
                matches.append(m)
                seen_concepts.add(m["concept"])

        return json.dumps(
            {"keyword": keyword, "matches": matches},
            ensure_ascii=False,
        )
    finally:
        conn.close()
