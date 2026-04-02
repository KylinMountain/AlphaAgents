import sqlite3
import numpy as np

from alpha_agents.data.embeddings import (
    encode_single,
    encode_texts,
    embedding_to_bytes,
    bytes_to_embedding,
    cosine_similarity,
    ensure_embedding_column,
    build_concept_embeddings,
    search_concepts_semantic,
)
from alpha_agents.data.db import init_db, get_connection


def test_encode_single_returns_vector():
    vec = encode_single("半导体")
    assert isinstance(vec, np.ndarray)
    assert vec.ndim == 1
    assert vec.shape[0] > 0


def test_encode_texts_batch():
    vecs = encode_texts(["半导体", "芯片", "新能源"])
    assert vecs.shape[0] == 3
    assert vecs.ndim == 2


def test_serialization_roundtrip():
    vec = encode_single("测试文本")
    data = embedding_to_bytes(vec)
    restored = bytes_to_embedding(data)
    np.testing.assert_array_almost_equal(vec, restored)


def test_semantic_similarity():
    """Similar concepts should have higher similarity than unrelated ones."""
    vecs = encode_texts(["芯片半导体", "集成电路", "白酒酿造"])
    # 芯片半导体 vs 集成电路 should be more similar than vs 白酒
    sim_related = float(vecs[0] @ vecs[1])
    sim_unrelated = float(vecs[0] @ vecs[2])
    assert sim_related > sim_unrelated


def test_build_and_search(tmp_path):
    db_path = tmp_path / "test.db"
    init_db(db_path)
    conn = get_connection(db_path)

    # Insert test concepts
    concepts = ["芯片半导体", "集成电路设计", "新能源汽车", "光伏发电", "白酒酿造", "军工装备"]
    for name in concepts:
        conn.execute("INSERT INTO concepts (name, source) VALUES (?, 'test')", (name,))
    conn.commit()

    # Build embeddings
    n = build_concept_embeddings(conn)
    assert n == len(concepts)

    # Search for semiconductor-related concepts
    results = search_concepts_semantic(conn, "国产替代芯片", top_k=3)
    assert len(results) > 0
    # Top results should be semiconductor-related
    top_names = [r["name"] for r in results]
    assert any("芯片" in n or "集成电路" in n for n in top_names)

    # Search for energy-related
    results = search_concepts_semantic(conn, "清洁能源", top_k=3)
    top_names = [r["name"] for r in results]
    assert any("新能源" in n or "光伏" in n for n in top_names)

    conn.close()


def test_search_empty_db(tmp_path):
    db_path = tmp_path / "empty.db"
    init_db(db_path)
    conn = get_connection(db_path)
    ensure_embedding_column(conn)

    results = search_concepts_semantic(conn, "测试")
    assert results == []
    conn.close()
