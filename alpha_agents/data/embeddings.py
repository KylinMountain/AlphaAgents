"""Concept embedding management for semantic stock search.

Uses a lightweight Chinese sentence-transformer model to encode concept names
into dense vectors, stored in SQLite for fast similarity search.
"""

import logging
import struct
import sqlite3
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

# Lightweight Chinese text embedding model (~100MB)
MODEL_NAME = "shibing624/text2vec-base-chinese"

_model = None


def _get_model():
    """Lazy-load the embedding model."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        logger.info("Loading embedding model: %s", MODEL_NAME)
        _model = SentenceTransformer(MODEL_NAME)
    return _model


def encode_texts(texts: list[str]) -> np.ndarray:
    """Encode a list of texts into embedding vectors.

    Returns:
        numpy array of shape (len(texts), dim)
    """
    model = _get_model()
    return model.encode(texts, normalize_embeddings=True, show_progress_bar=False)


def encode_single(text: str) -> np.ndarray:
    """Encode a single text into an embedding vector."""
    return encode_texts([text])[0]


def embedding_to_bytes(vec: np.ndarray) -> bytes:
    """Serialize a numpy vector to bytes for SQLite storage."""
    return vec.astype(np.float32).tobytes()


def bytes_to_embedding(data: bytes) -> np.ndarray:
    """Deserialize bytes back to numpy vector."""
    return np.frombuffer(data, dtype=np.float32)


def cosine_similarity(query_vec: np.ndarray, candidate_vecs: np.ndarray) -> np.ndarray:
    """Compute cosine similarity between query and candidates.

    Assumes vectors are already L2-normalized (which encode_texts does).
    """
    return candidate_vecs @ query_vec


def ensure_embedding_column(conn: sqlite3.Connection) -> None:
    """Add embedding column to concepts table if not exists."""
    cursor = conn.execute("PRAGMA table_info(concepts)")
    columns = {row[1] for row in cursor.fetchall()}
    if "embedding" not in columns:
        conn.execute("ALTER TABLE concepts ADD COLUMN embedding BLOB")
        conn.commit()


def build_concept_embeddings(conn: sqlite3.Connection) -> int:
    """Generate and store embeddings for all concepts without one.

    Returns:
        Number of concepts embedded.
    """
    ensure_embedding_column(conn)

    rows = conn.execute(
        "SELECT id, name FROM concepts WHERE embedding IS NULL"
    ).fetchall()

    if not rows:
        logger.info("All concepts already have embeddings")
        return 0

    names = [row["name"] for row in rows]
    ids = [row["id"] for row in rows]

    logger.info("Generating embeddings for %d concepts...", len(names))
    vectors = encode_texts(names)

    for concept_id, vec in zip(ids, vectors):
        conn.execute(
            "UPDATE concepts SET embedding = ? WHERE id = ?",
            (embedding_to_bytes(vec), concept_id),
        )

    conn.commit()
    logger.info("Embedded %d concepts", len(names))
    return len(names)


def search_concepts_semantic(
    conn: sqlite3.Connection,
    query: str,
    top_k: int = 10,
    threshold: float = 0.35,
) -> list[dict]:
    """Search concepts by semantic similarity.

    Args:
        conn: SQLite connection.
        query: Search query text.
        top_k: Maximum number of results.
        threshold: Minimum similarity score (0-1).

    Returns:
        List of dicts with 'id', 'name', 'score'.
    """
    rows = conn.execute(
        "SELECT id, name, embedding FROM concepts WHERE embedding IS NOT NULL"
    ).fetchall()

    if not rows:
        return []

    query_vec = encode_single(query)

    results = []
    for row in rows:
        candidate_vec = bytes_to_embedding(row["embedding"])
        score = float(query_vec @ candidate_vec)
        if score >= threshold:
            results.append({
                "id": row["id"],
                "name": row["name"],
                "score": round(score, 4),
            })

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_k]
