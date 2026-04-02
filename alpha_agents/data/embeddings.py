"""Concept embedding management for semantic stock search.

Uses SiliconFlow's free BGE embedding API + ChromaDB for local vector storage.
"""

import logging
import sqlite3

import chromadb
import httpx

from alpha_agents.config import (
    CHROMA_PATH,
    SILICONFLOW_API_KEY,
    SILICONFLOW_BASE_URL,
    EMBEDDING_MODEL,
)

logger = logging.getLogger(__name__)

COLLECTION_NAME = "concepts"
BATCH_SIZE = 64  # SiliconFlow API batch limit


def _get_chroma_client() -> chromadb.ClientAPI:
    """Get persistent ChromaDB client."""
    CHROMA_PATH.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=str(CHROMA_PATH))


def _get_collection(client: chromadb.ClientAPI) -> chromadb.Collection:
    """Get or create the concepts collection."""
    return client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )


def _call_embedding_api(texts: list[str]) -> list[list[float]]:
    """Call SiliconFlow embedding API.

    Args:
        texts: List of texts to embed (max BATCH_SIZE per call).

    Returns:
        List of embedding vectors.
    """
    if not SILICONFLOW_API_KEY:
        raise RuntimeError(
            "SILICONFLOW_API_KEY not set. Get a free key at https://siliconflow.cn"
        )

    with httpx.Client(timeout=30) as client:
        resp = client.post(
            f"{SILICONFLOW_BASE_URL}/embeddings",
            headers={
                "Authorization": f"Bearer {SILICONFLOW_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": EMBEDDING_MODEL,
                "input": texts,
                "encoding_format": "float",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    # Sort by index to maintain order
    embeddings = sorted(data["data"], key=lambda x: x["index"])
    return [e["embedding"] for e in embeddings]


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed texts in batches via SiliconFlow API.

    Handles batching for large input lists.
    """
    all_embeddings: list[list[float]] = []
    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i : i + BATCH_SIZE]
        embeddings = _call_embedding_api(batch)
        all_embeddings.extend(embeddings)
    return all_embeddings


def build_concept_embeddings(conn: sqlite3.Connection) -> int:
    """Generate and store embeddings for all concepts in ChromaDB.

    Reads concept names from SQLite, embeds them via SiliconFlow,
    and stores in ChromaDB for fast similarity search.

    Returns:
        Number of concepts embedded.
    """
    rows = conn.execute("SELECT id, name FROM concepts").fetchall()
    if not rows:
        logger.info("No concepts to embed")
        return 0

    ids = [str(row["id"]) for row in rows]
    names = [row["name"] for row in rows]

    client = _get_chroma_client()
    collection = _get_collection(client)

    # Check which concepts already exist in ChromaDB
    existing = set()
    try:
        result = collection.get(ids=ids)
        existing = set(result["ids"])
    except Exception:
        pass

    new_ids = [i for i in ids if i not in existing]
    new_names = [names[ids.index(i)] for i in new_ids]

    if not new_ids:
        logger.info("All %d concepts already embedded in ChromaDB", len(ids))
        return 0

    logger.info("Embedding %d new concepts via SiliconFlow %s...", len(new_ids), EMBEDDING_MODEL)
    embeddings = embed_texts(new_names)

    # Upsert into ChromaDB in batches
    for i in range(0, len(new_ids), BATCH_SIZE):
        batch_ids = new_ids[i : i + BATCH_SIZE]
        batch_embeddings = embeddings[i : i + BATCH_SIZE]
        batch_docs = new_names[i : i + BATCH_SIZE]
        collection.upsert(
            ids=batch_ids,
            embeddings=batch_embeddings,
            documents=batch_docs,
        )

    logger.info("Embedded %d concepts into ChromaDB", len(new_ids))
    return len(new_ids)


def search_concepts_semantic(
    conn: sqlite3.Connection,
    query: str,
    top_k: int = 10,
) -> list[dict]:
    """Search concepts by semantic similarity using ChromaDB.

    Args:
        conn: SQLite connection (used to map concept IDs).
        query: Search query text.
        top_k: Maximum number of results.

    Returns:
        List of dicts with 'id', 'name', 'score'.
    """
    client = _get_chroma_client()
    collection = _get_collection(client)

    if collection.count() == 0:
        return []

    # Embed query via API
    query_embedding = _call_embedding_api([query])[0]

    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=min(top_k, collection.count()),
    )

    matches = []
    if results["ids"] and results["ids"][0]:
        for concept_id, doc, distance in zip(
            results["ids"][0],
            results["documents"][0],
            results["distances"][0],
        ):
            # ChromaDB cosine distance = 1 - similarity
            score = 1.0 - distance
            matches.append({
                "id": int(concept_id),
                "name": doc,
                "score": round(score, 4),
            })

    return matches
