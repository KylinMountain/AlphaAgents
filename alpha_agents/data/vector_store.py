"""A vector store for a few thousand concept embeddings.

This replaced ChromaDB. Chroma is a fine database and the wrong tool at
this size: it pulled 134MB of transitive dependencies into the runtime
image — onnxruntime, tokenizers, grpcio, kubernetes — of which
onnxruntime and tokenizers exist to serve Chroma's *local* embedding
models. This project embeds through a remote API, so that code never ran.

The corpus is the A-share concept list: a few thousand vectors of ~1024
dimensions. Brute-force cosine over that is a single numpy matmul,
sub-millisecond, and needs no index, no server, and no shard management.
ANN structures start paying off several orders of magnitude further up.

Vectors are stored as float32 blobs in SQLite next to everything else, so
there is one fewer store to back up, and the query is exact rather than
approximate.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS concept_vectors (
    id TEXT PRIMARY KEY,
    document TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vector BLOB NOT NULL,
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
"""


class VectorStore:
    """Exact cosine search over a small, persistent set of vectors."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(str(self.path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.executescript(_SCHEMA)
            conn.commit()
            self._conn = conn
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ── writes ───────────────────────────────────────────────────────

    def upsert(self, ids: list[str], embeddings: list[list[float]],
               documents: list[str]) -> int:
        """Insert or replace vectors. Returns the number written.

        Vectors are L2-normalised on the way in, which turns the cosine
        similarity at query time into a plain dot product.
        """
        if not ids:
            return 0
        if not (len(ids) == len(embeddings) == len(documents)):
            raise ValueError(
                f"长度不一致: ids={len(ids)} embeddings={len(embeddings)} "
                f"documents={len(documents)}"
            )

        rows = []
        for i, vec, doc in zip(ids, embeddings, documents):
            arr = np.asarray(vec, dtype=np.float32)
            norm = float(np.linalg.norm(arr))
            if norm > 0:
                arr = arr / norm
            rows.append((str(i), doc, int(arr.shape[0]), arr.tobytes()))

        conn = self._connection()
        conn.executemany(
            "INSERT INTO concept_vectors (id, document, dim, vector) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET document=excluded.document, "
            "dim=excluded.dim, vector=excluded.vector, "
            "updated_at=datetime('now','localtime')",
            rows,
        )
        conn.commit()
        return len(rows)

    # ── reads ────────────────────────────────────────────────────────

    def count(self) -> int:
        return self._connection().execute(
            "SELECT COUNT(*) FROM concept_vectors"
        ).fetchone()[0]

    def existing_ids(self, ids: list[str]) -> set[str]:
        """Which of these ids are already stored.

        Chunked because SQLite caps the number of bound parameters, and
        the caller passes the whole concept list.
        """
        if not ids:
            return set()
        conn = self._connection()
        found: set[str] = set()
        for i in range(0, len(ids), 500):
            chunk = [str(x) for x in ids[i:i + 500]]
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT id FROM concept_vectors WHERE id IN ({placeholders})",
                chunk,
            ).fetchall()
            found.update(r["id"] for r in rows)
        return found

    def query(self, embedding: list[float], top_k: int = 10) -> list[dict]:
        """Nearest vectors by cosine similarity, best first.

        Returns dicts of {id, document, score} where score is in [-1, 1].
        """
        conn = self._connection()
        rows = conn.execute(
            "SELECT id, document, dim, vector FROM concept_vectors"
        ).fetchall()
        if not rows:
            return []

        if top_k <= 0:
            return []

        q = np.asarray(embedding, dtype=np.float32)
        norm = float(np.linalg.norm(q))
        if norm == 0:
            return []
        q = q / norm
        dim = q.shape[0]
        expected_bytes = dim * 4          # float32

        # Two ways a row can be unusable, and neither should take the
        # search down with it:
        #   - a different dim, i.e. written by an older embedding model;
        #   - a blob whose length disagrees with its own dim column, i.e.
        #     a truncated or corrupted write. Left in, that one turns a
        #     single bad row into a ValueError from reshape and kills
        #     every search until someone finds it by hand.
        usable = [
            r for r in rows
            if r["dim"] == dim and len(r["vector"]) == expected_bytes
        ]
        skipped = len(rows) - len(usable)
        if skipped:
            logger.warning(
                "跳过 %d 条不可用向量 (期望 dim=%d/%d字节) — 换过 embedding "
                "模型或写入损坏；重建: python main.py build-embeddings",
                skipped, dim, expected_bytes,
            )
        if not usable:
            return []

        matrix = np.frombuffer(
            b"".join(r["vector"] for r in usable), dtype=np.float32,
        ).reshape(len(usable), dim)

        scores = matrix @ q          # both sides are unit vectors
        k = min(top_k, len(usable))
        top = np.argpartition(-scores, k - 1)[:k]
        top = top[np.argsort(-scores[top])]

        return [
            {
                "id": usable[i]["id"],
                "document": usable[i]["document"],
                "score": float(scores[i]),
            }
            for i in top
        ]
