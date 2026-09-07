"""Local vector store — exact cosine over a few thousand concepts."""

import math

import numpy as np
import pytest

from alpha_agents.data.vector_store import VectorStore


@pytest.fixture
def store(tmp_path):
    s = VectorStore(tmp_path / "vec.db")
    yield s
    s.close()


def _unit(*components) -> list[float]:
    v = np.asarray(components, dtype=np.float32)
    return (v / np.linalg.norm(v)).tolist()


class TestUpsert:
    def test_writes_and_counts(self, store):
        assert store.count() == 0
        n = store.upsert(["1", "2"], [_unit(1, 0, 0), _unit(0, 1, 0)],
                         ["AI算力", "军工"])
        assert n == 2 and store.count() == 2

    def test_same_id_replaces_rather_than_duplicates(self, store):
        store.upsert(["1"], [_unit(1, 0, 0)], ["旧名"])
        store.upsert(["1"], [_unit(0, 1, 0)], ["新名"])
        assert store.count() == 1
        assert store.query(_unit(0, 1, 0))[0]["document"] == "新名"

    def test_empty_input_is_a_no_op(self, store):
        assert store.upsert([], [], []) == 0

    def test_mismatched_lengths_raise(self, store):
        with pytest.raises(ValueError, match="长度不一致"):
            store.upsert(["1", "2"], [_unit(1, 0)], ["只有一个"])

    def test_survives_reopening(self, tmp_path):
        path = tmp_path / "vec.db"
        s1 = VectorStore(path)
        s1.upsert(["1"], [_unit(1, 0, 0)], ["AI算力"])
        s1.close()

        s2 = VectorStore(path)
        assert s2.count() == 1
        assert s2.query(_unit(1, 0, 0))[0]["document"] == "AI算力"
        s2.close()

    def test_zero_vector_does_not_divide_by_zero(self, store):
        assert store.upsert(["1"], [[0.0, 0.0, 0.0]], ["空"]) == 1


class TestExistingIds:
    def test_reports_only_what_is_stored(self, store):
        store.upsert(["1", "2"], [_unit(1, 0), _unit(0, 1)], ["a", "b"])
        assert store.existing_ids(["1", "2", "3"]) == {"1", "2"}

    def test_empty_query(self, store):
        assert store.existing_ids([]) == set()

    def test_chunks_past_the_sqlite_parameter_cap(self, store):
        """The caller passes the whole concept list; SQLite caps bound params."""
        ids = [str(i) for i in range(1200)]
        store.upsert(ids, [_unit(1, 0)] * 1200, ["x"] * 1200)
        assert store.existing_ids(ids) == set(ids)


class TestQuery:
    def test_returns_the_nearest_first(self, store):
        store.upsert(
            ["1", "2", "3"],
            [_unit(1, 0, 0), _unit(0, 1, 0), _unit(0.9, 0.1, 0)],
            ["正东", "正北", "偏东"],
        )
        hits = store.query(_unit(1, 0, 0), top_k=3)
        assert [h["document"] for h in hits] == ["正东", "偏东", "正北"]

    def test_score_is_cosine_similarity(self, store):
        store.upsert(["1"], [_unit(1, 0)], ["同向"])
        assert store.query(_unit(1, 0))[0]["score"] == pytest.approx(1.0, abs=1e-5)

        store.upsert(["2"], [_unit(-1, 0)], ["反向"])
        hits = {h["document"]: h["score"] for h in store.query(_unit(1, 0), top_k=2)}
        assert hits["反向"] == pytest.approx(-1.0, abs=1e-5)

    def test_orthogonal_scores_zero(self, store):
        store.upsert(["1"], [_unit(0, 1)], ["正交"])
        assert store.query(_unit(1, 0))[0]["score"] == pytest.approx(0.0, abs=1e-5)

    def test_input_need_not_be_normalised(self, store):
        store.upsert(["1"], [[3.0, 4.0]], ["缩放"])   # |v| = 5
        assert store.query([30.0, 40.0])[0]["score"] == pytest.approx(1.0, abs=1e-5)

    def test_top_k_limits_results(self, store):
        store.upsert([str(i) for i in range(10)],
                     [_unit(1, i * 0.1) for i in range(10)],
                     [f"c{i}" for i in range(10)])
        assert len(store.query(_unit(1, 0), top_k=3)) == 3

    def test_top_k_larger_than_corpus_is_safe(self, store):
        store.upsert(["1"], [_unit(1, 0)], ["唯一"])
        assert len(store.query(_unit(1, 0), top_k=99)) == 1

    def test_empty_store_returns_nothing(self, store):
        assert store.query(_unit(1, 0)) == []

    def test_zero_query_vector_returns_nothing(self, store):
        store.upsert(["1"], [_unit(1, 0)], ["x"])
        assert store.query([0.0, 0.0]) == []

    @pytest.mark.parametrize("k", [0, -1, -5])
    def test_non_positive_top_k_returns_nothing(self, store, k):
        """min(k, n) with a negative k fed argpartition a negative kth,
        which numpy reads as an index from the end — so top_k=-1 used to
        return a result instead of none."""
        store.upsert(["1", "2"], [_unit(1, 0), _unit(0, 1)], ["a", "b"])
        assert store.query(_unit(1, 0), top_k=k) == []

    def test_corrupted_blob_is_skipped_not_fatal(self, store):
        """A blob whose length disagrees with its dim column used to make
        reshape throw, killing every search until someone found the row."""
        store.upsert(["good"], [_unit(1, 0)], ["正常"])
        conn = store._connection()
        conn.execute(
            "INSERT INTO concept_vectors (id, document, dim, vector) "
            "VALUES ('bad', '损坏', 2, ?)",
            (np.array([1, 2, 3], dtype=np.float32).tobytes(),),
        )
        conn.commit()

        hits = store.query(_unit(1, 0), top_k=5)
        assert [h["document"] for h in hits] == ["正常"]

    def test_dimension_mismatch_is_skipped_not_scored(self, store):
        """A model change leaves old vectors behind; scoring them would be
        meaningless rather than merely wrong."""
        store.upsert(["old"], [_unit(1, 0)], ["旧模型"])
        store.upsert(["new"], [_unit(1, 0, 0)], ["新模型"])
        hits = store.query(_unit(1, 0, 0), top_k=5)
        assert [h["document"] for h in hits] == ["新模型"]

    def test_all_dimensions_mismatched_returns_nothing(self, store):
        store.upsert(["1"], [_unit(1, 0)], ["旧"])
        assert store.query(_unit(1, 0, 0)) == []


class TestScale:
    def test_a_realistic_corpus_searches_correctly(self, store):
        """The real corpus is a few thousand concepts — brute force is fine."""
        rng = np.random.default_rng(7)
        n, dim = 3000, 128
        vecs = rng.normal(size=(n, dim)).astype(np.float32)
        vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)

        store.upsert([str(i) for i in range(n)], vecs.tolist(),
                     [f"概念{i}" for i in range(n)])
        assert store.count() == n

        target = vecs[1234]
        hits = store.query(target.tolist(), top_k=5)
        assert hits[0]["document"] == "概念1234"
        assert hits[0]["score"] == pytest.approx(1.0, abs=1e-4)
        # Scores come back in descending order.
        scores = [h["score"] for h in hits]
        assert scores == sorted(scores, reverse=True)

    def test_matches_a_naive_numpy_baseline(self, store):
        """Guard against an indexing bug in the matmul path."""
        rng = np.random.default_rng(11)
        vecs = rng.normal(size=(200, 32)).astype(np.float32)
        vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
        store.upsert([str(i) for i in range(200)], vecs.tolist(),
                     [str(i) for i in range(200)])

        q = vecs[42]
        expected = int(np.argmax(vecs @ q))
        assert store.query(q.tolist(), top_k=1)[0]["id"] == str(expected)
