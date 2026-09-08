"""A vector store for the corpora this project actually has.

This replaced ChromaDB. Chroma is a fine database and the wrong tool at
this size: it pulled 134MB of transitive dependencies into the runtime
image — onnxruntime, tokenizers, grpcio, kubernetes — of which
onnxruntime and tokenizers exist to serve Chroma's *local* embedding
models. This project embeds through a remote API, so that code never ran.

Two corpora use it:

- concepts: the A-share concept list, a few thousand vectors, static.
- news: a rolling window of flashes, ~2500/day. Thirty days is 75k
  vectors at 1024 dims — 300MB of float32 and roughly 40ms per brute
  force scan, which is why the window matters more than the index. An
  ANN structure starts paying off a couple of orders of magnitude above
  that; at 75k it would cost memory and recall for nothing.

Rows carry an optional ``stamp`` (the document's own time, not the write
time) and the query filters on it in SQL *before* the matmul. Scoring
75k vectors to then discard everything older than six hours would spend
the whole budget on rows the caller already knows it does not want.

Storage is float32 blobs in SQLite: one fewer store to back up, and the
search is exact rather than approximate.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_TABLE = "concept_vectors"

# Table names are interpolated into SQL — they come from code, never from
# input, and this keeps it that way.
_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _schema(table: str) -> str:
    """Table only. The index on ``stamp`` is created after _migrate, since
    on a store that predates that column the index would be built against
    a column that does not exist yet."""
    return f"""
CREATE TABLE IF NOT EXISTS {table} (
    id TEXT PRIMARY KEY,
    document TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vector BLOB NOT NULL,
    stamp TEXT,
    meta TEXT,
    updated_at TEXT DEFAULT (datetime('now','localtime'))
);
"""


class VectorStore:
    """Exact cosine search over a persistent set of vectors."""

    def __init__(self, path: Path | str, table: str = DEFAULT_TABLE):
        if not _TABLE_RE.match(table):
            raise ValueError(f"表名不合法: {table!r}")
        self.path = Path(path)
        self.table = table
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(str(self.path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.executescript(_schema(self.table))
            self._migrate(conn)
            conn.commit()
            self._conn = conn
        return self._conn

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Add columns a database created before them.

        CREATE TABLE IF NOT EXISTS is a no-op on a table that exists, so a
        new column in the schema never reaches a store already on disk —
        the reads then fail on a column the code believes is there.
        """
        have = {r["name"] for r in conn.execute(
            f"PRAGMA table_info({self.table})")}
        for column in ("stamp", "meta"):
            if column not in have:
                conn.execute(f"ALTER TABLE {self.table} ADD COLUMN {column} TEXT")
                logger.info("%s: added %s column", self.table, column)
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{self.table}_stamp "
            f"ON {self.table}(stamp)")

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ── writes ───────────────────────────────────────────────────────

    def upsert(self, ids: list[str], embeddings: list[list[float]],
               documents: list[str], stamps: list[str] | None = None,
               metas: list[dict] | None = None) -> int:
        """Insert or replace vectors. Returns the number written.

        Vectors are L2-normalised on the way in, which turns the cosine
        similarity at query time into a plain dot product.

        ``stamps`` is the document's own time (a news item's publication
        time, say), used by the query's window filter. ``metas`` rides
        along as JSON for whatever the caller needs back.
        """
        if not ids:
            return 0
        if not (len(ids) == len(embeddings) == len(documents)):
            raise ValueError(
                f"长度不一致: ids={len(ids)} embeddings={len(embeddings)} "
                f"documents={len(documents)}"
            )
        if stamps is not None and len(stamps) != len(ids):
            raise ValueError(f"stamps 长度 {len(stamps)} != ids {len(ids)}")
        if metas is not None and len(metas) != len(ids):
            raise ValueError(f"metas 长度 {len(metas)} != ids {len(ids)}")

        rows = []
        for i, (ident, vec, doc) in enumerate(zip(ids, embeddings, documents)):
            arr = np.asarray(vec, dtype=np.float32)
            norm = float(np.linalg.norm(arr))
            if norm > 0:
                arr = arr / norm
            rows.append((
                str(ident), doc, int(arr.shape[0]), arr.tobytes(),
                stamps[i] if stamps else None,
                json.dumps(metas[i], ensure_ascii=False) if metas else None,
            ))

        conn = self._connection()
        conn.executemany(
            f"INSERT INTO {self.table} (id, document, dim, vector, stamp, meta) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET document=excluded.document, "
            "dim=excluded.dim, vector=excluded.vector, stamp=excluded.stamp, "
            "meta=excluded.meta, updated_at=datetime('now','localtime')",
            rows,
        )
        conn.commit()
        return len(rows)

    def prune_before(self, stamp: str) -> int:
        """Drop rows older than ``stamp``. Returns how many went.

        The window is what keeps a brute-force scan viable: without it a
        year of flashes is 900k vectors and 3.6GB, and every attribution
        pays for news nobody will ever ask about.
        """
        conn = self._connection()
        cur = conn.execute(
            f"DELETE FROM {self.table} WHERE stamp IS NOT NULL AND stamp < ?",
            (stamp,),
        )
        conn.commit()
        if cur.rowcount:
            logger.info("%s: pruned %d rows older than %s",
                        self.table, cur.rowcount, stamp)
        return cur.rowcount

    # ── reads ────────────────────────────────────────────────────────

    def count(self) -> int:
        return self._connection().execute(
            f"SELECT COUNT(*) FROM {self.table}"
        ).fetchone()[0]

    def existing_ids(self, ids: list[str]) -> set[str]:
        """Which of these ids are already stored.

        Chunked because SQLite caps the number of bound parameters, and
        the caller passes a whole corpus.
        """
        if not ids:
            return set()
        conn = self._connection()
        found: set[str] = set()
        for i in range(0, len(ids), 500):
            chunk = [str(x) for x in ids[i:i + 500]]
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT id FROM {self.table} WHERE id IN ({placeholders})",
                chunk,
            ).fetchall()
            found.update(r["id"] for r in rows)
        return found

    def query(self, embedding: list[float], top_k: int = 10,
              since: str | None = None, until: str | None = None) -> list[dict]:
        """Nearest vectors by cosine similarity, best first.

        ``since``/``until`` filter on ``stamp`` in SQL before any scoring:
        an attribution asks about the last few hours, and scoring a month
        of flashes to then throw them away is the whole cost of the query.

        Returns dicts of {id, document, score, stamp, meta}.
        """
        if top_k <= 0:
            return []

        sql = [f"SELECT id, document, dim, vector, stamp, meta FROM {self.table}"]
        params: list = []
        clauses = []
        if since:
            clauses.append("stamp >= ?")
            params.append(since)
        if until:
            clauses.append("stamp <= ?")
            params.append(until)
        if clauses:
            sql.append("WHERE " + " AND ".join(clauses))

        rows = self._connection().execute(" ".join(sql), params).fetchall()
        if not rows:
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

        out = []
        for i in top:
            row = usable[i]
            meta = None
            if row["meta"]:
                try:
                    meta = json.loads(row["meta"])
                except json.JSONDecodeError:
                    logger.debug("%s: row %s has unparseable meta",
                                 self.table, row["id"])
            out.append({
                "id": row["id"],
                "document": row["document"],
                "score": float(scores[i]),
                "stamp": row["stamp"],
                "meta": meta,
            })
        return out
