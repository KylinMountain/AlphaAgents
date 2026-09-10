"""Concept embedding management for semantic stock search.

Embeddings come from an OpenAI-compatible API (default: SiliconFlow's
free BGE-M3) and are stored locally by data/vector_store.py — a SQLite
table plus a numpy matmul.

That store replaced ChromaDB, which brought 134MB of transitive
dependencies for a corpus of a few thousand vectors. See vector_store.py
for the reasoning.
"""

import logging
import sqlite3
from functools import lru_cache

from openai import OpenAI

from alpha_agents.data.vector_store import VectorStore
from alpha_agents.config import (
    CHROMA_PATH,
    EMBEDDING_API_KEY,
    EMBEDDING_BASE_URL,
    EMBEDDING_MODEL,
)
from alpha_agents.data.token_usage import instrument

logger = logging.getLogger(__name__)

COLLECTION_NAME = "concepts"
BATCH_SIZE = 64


@lru_cache(maxsize=1)
def _get_store() -> VectorStore:
    """The concept vector store. Cached — it holds a SQLite connection."""
    return VectorStore(CHROMA_PATH / f"{COLLECTION_NAME}.db")


def _get_openai_client() -> OpenAI:
    """Create OpenAI-compatible client for embeddings."""
    return instrument(OpenAI(api_key=EMBEDDING_API_KEY,
                             base_url=EMBEDDING_BASE_URL),
                      module="embedding")


def _call_embedding_api(texts: list[str]) -> list[list[float]]:
    """Call OpenAI-compatible embedding API.

    Args:
        texts: List of texts to embed (max BATCH_SIZE per call).

    Returns:
        List of embedding vectors.
    """
    if not EMBEDDING_API_KEY:
        raise RuntimeError(
            "EMBEDDING_API_KEY not set. Set it or SILICONFLOW_API_KEY for free BGE embeddings."
        )

    client = _get_openai_client()
    response = client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=texts,
    )

    # Sort by index to maintain order
    sorted_data = sorted(response.data, key=lambda x: x.index)
    return [item.embedding for item in sorted_data]


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed texts in batches via API.

    Handles batching for large input lists.
    """
    all_embeddings: list[list[float]] = []
    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i : i + BATCH_SIZE]
        embeddings = _call_embedding_api(batch)
        all_embeddings.extend(embeddings)
    return all_embeddings


def build_concept_embeddings(conn: sqlite3.Connection) -> int:
    """Generate and store embeddings for every concept not yet embedded.

    Returns:
        Number of concepts embedded this run.
    """
    rows = conn.execute("SELECT id, name FROM concepts").fetchall()
    if not rows:
        logger.info("No concepts to embed")
        return 0

    ids = [str(row["id"]) for row in rows]
    names = [row["name"] for row in rows]

    store = _get_store()
    existing = store.existing_ids(ids)

    # Pair by position rather than ids.index(i) — that was O(n²) and, on a
    # duplicate name, would have looked up the wrong one.
    pending = [(i, n) for i, n in zip(ids, names) if i not in existing]
    new_ids = [i for i, _ in pending]
    new_names = [n for _, n in pending]

    if not new_ids:
        logger.info("全部 %d 个概念已有向量", len(ids))
        return 0

    logger.info("Embedding %d concepts via %s (%s)...", len(new_ids), EMBEDDING_MODEL, EMBEDDING_BASE_URL)
    embeddings = embed_texts(new_names)

    for i in range(0, len(new_ids), BATCH_SIZE):
        store.upsert(
            ids=new_ids[i : i + BATCH_SIZE],
            embeddings=embeddings[i : i + BATCH_SIZE],
            documents=new_names[i : i + BATCH_SIZE],
        )

    logger.info("已写入 %d 个概念向量", len(new_ids))
    return len(new_ids)


@lru_cache(maxsize=128)
def _get_query_embedding(query: str) -> tuple:
    """Cache query embeddings to avoid repeated API calls."""
    embedding = _call_embedding_api([query])[0]
    return tuple(embedding)  # tuple for hashability


def search_concepts_semantic(
    conn: sqlite3.Connection,
    query: str,
    top_k: int = 10,
) -> list[dict]:
    """Search concepts by cosine similarity over the local vector store.

    Args:
        conn: SQLite connection (used to map concept IDs).
        query: Search query text.
        top_k: Maximum number of results.

    Returns:
        List of dicts with 'id', 'name', 'score'.
    """
    store = _get_store()
    if store.count() == 0:
        return []

    # Embed query via API (cached to avoid repeated calls for same/similar queries)
    query_embedding = list(_get_query_embedding(query))

    # The store returns cosine similarity directly, best first — no
    # distance-to-similarity conversion to get backwards.
    return [
        {
            "id": int(hit["id"]),
            "name": hit["document"],
            "score": round(hit["score"], 4),
        }
        for hit in store.query(query_embedding, top_k=top_k)
    ]


# ── Ad-hoc semantic matching over a small label set ───────────

_BOARD_CACHE: dict[tuple, dict] = {}
_BOARD_CACHE_LIMIT = 4


# Measured on BGE-M3 against the live board list: true pairings scored
# 石油石化→石油加工贸易 0.655, 航运→航运港口 0.816, 半导体设备→半导体
# 0.896; the best false pairings were 白酒→银行 0.560 and 航空→天然气
# 0.514. Short Chinese labels carry a high baseline similarity, so a
# threshold below ~0.6 pairs almost anything.
LABEL_MATCH_THRESHOLD = 0.62


def match_label_semantic(query: str, labels: list[str],
                         threshold: float = LABEL_MATCH_THRESHOLD) -> str | None:
    """Closest label to ``query`` by embedding cosine, or None.

    For pairing vocabularies that describe the same thing in different
    words: the news digest writes 石油石化 and 航运, the exchange's boards
    carry 油气开采及服务 and 航运港口. Character overlap pairs the first
    (石油) and misses the second entirely, and a hand-written synonym
    table would need editing every time a board is renamed.

    Board names change rarely, so their vectors are cached by the exact
    label set — a cycle costs one embedding call for the query, not one
    per board.

    Returns None when nothing clears ``threshold``: a wrong pairing puts a
    theme behind news that has nothing to do with it, which is worse than
    no theme.
    """
    if not query or not labels:
        return None

    key = tuple(sorted(labels))
    cached = _BOARD_CACHE.get(key)
    if cached is None:
        try:
            vectors = embed_texts(list(key))
        except Exception as e:
            logger.warning("Label embedding failed, no semantic match: %s", e)
            return None
        if len(vectors) != len(key):
            logger.warning("Label embedding returned %d vectors for %d labels",
                           len(vectors), len(key))
            return None
        cached = {"labels": list(key), "vectors": vectors}
        if len(_BOARD_CACHE) >= _BOARD_CACHE_LIMIT:
            _BOARD_CACHE.pop(next(iter(_BOARD_CACHE)))
        _BOARD_CACHE[key] = cached

    try:
        q = _call_embedding_api([query])[0]
    except Exception as e:
        logger.warning("Query embedding failed for %r: %s", query, e)
        return None

    import math
    qn = math.sqrt(sum(x * x for x in q)) or 1.0
    best, best_score = None, 0.0
    for label, vec in zip(cached["labels"], cached["vectors"]):
        vn = math.sqrt(sum(x * x for x in vec)) or 1.0
        score = sum(a * b for a, b in zip(q, vec)) / (qn * vn)
        if score > best_score:
            best, best_score = label, score
    if best_score < threshold:
        logger.debug("No board matched %r (best %r at %.2f)",
                     query, best, best_score)
        return None
    return best
