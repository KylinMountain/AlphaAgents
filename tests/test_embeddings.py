import json
import sqlite3
from unittest.mock import patch, MagicMock

import pytest

from alpha_agents.data.embeddings import (
    embed_texts,
    build_concept_embeddings,
    search_concepts_semantic,
    _get_store,
    COLLECTION_NAME,
)
from alpha_agents.data.db import init_db, get_connection


@pytest.fixture(autouse=True)
def _isolate_store():
    """_get_store is lru_cached and holds a connection to one file.

    Without clearing it, every test after the first would silently query
    the first test's tmp_path store — passing for the wrong reason.
    """
    _get_store.cache_clear()
    yield
    _get_store.cache_clear()


def _mock_call_embedding_api(texts):
    """Return fake embeddings — 3-dim vectors based on text hash for consistency."""
    import hashlib
    embeddings = []
    for t in texts:
        h = hashlib.md5(t.encode()).hexdigest()
        vec = [int(h[i:i+2], 16) / 255.0 for i in range(0, 6, 2)]
        # Normalize
        norm = sum(x**2 for x in vec) ** 0.5
        vec = [x / norm for x in vec]
        embeddings.append(vec)
    return embeddings


def test_embed_texts_calls_api():
    with patch("alpha_agents.data.embeddings._call_embedding_api", side_effect=_mock_call_embedding_api):
        vecs = embed_texts(["半导体", "芯片"])
        assert len(vecs) == 2
        assert len(vecs[0]) == 3  # our mock returns 3-dim


def test_build_and_search(tmp_path):
    """End-to-end: build embeddings into the local store and search."""
    db_path = tmp_path / "test.db"
    init_db(db_path)
    conn = get_connection(db_path)

    concepts = ["芯片半导体", "集成电路设计", "新能源汽车", "光伏发电", "白酒酿造", "军工装备"]
    for name in concepts:
        conn.execute("INSERT INTO concepts (name, source) VALUES (?, 'test')", (name,))
    conn.commit()

    # Mock the API; point the store at a temp dir.
    chroma_path = tmp_path / "vectors"

    with patch("alpha_agents.data.embeddings._call_embedding_api", side_effect=_mock_call_embedding_api), \
         patch("alpha_agents.data.embeddings.CHROMA_PATH", chroma_path):

        n = build_concept_embeddings(conn)
        assert n == len(concepts)

        # Search — with mock embeddings the semantic quality isn't real,
        # but we verify the pipeline works end-to-end
        results = search_concepts_semantic(conn, "芯片", top_k=3)
        assert len(results) > 0
        assert all("id" in r and "name" in r and "score" in r for r in results)

    conn.close()


def test_build_embeddings_incremental(tmp_path):
    """Second build should not re-embed existing concepts."""
    db_path = tmp_path / "test.db"
    init_db(db_path)
    conn = get_connection(db_path)

    conn.execute("INSERT INTO concepts (name, source) VALUES (?, 'test')", ("测试概念",))
    conn.commit()

    chroma_path = tmp_path / "vectors"
    with patch("alpha_agents.data.embeddings._call_embedding_api", side_effect=_mock_call_embedding_api) as mock_api, \
         patch("alpha_agents.data.embeddings.CHROMA_PATH", chroma_path):

        n1 = build_concept_embeddings(conn)
        assert n1 == 1

        # Second call should skip already-embedded concepts
        n2 = build_concept_embeddings(conn)
        assert n2 == 0

    conn.close()


def test_search_empty_collection(tmp_path):
    """Search with nothing stored returns an empty list."""
    db_path = tmp_path / "test.db"
    init_db(db_path)
    conn = get_connection(db_path)

    chroma_path = tmp_path / "vectors"
    with patch("alpha_agents.data.embeddings._call_embedding_api", side_effect=_mock_call_embedding_api), \
         patch("alpha_agents.data.embeddings.CHROMA_PATH", chroma_path):
        results = search_concepts_semantic(conn, "测试")
        assert results == []

    conn.close()
