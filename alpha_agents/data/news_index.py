"""Semantic index over the flashes this system already collects.

Attribution asks "why did this sector move", and the answer is usually in
the 2500 flashes a day already sitting in news_items — 财联社, 金十,
新浪7x24, all directly reachable. The agents instead reached for
DuckDuckGo, which on a mainland host returns SEO listicles when it works
at all, and their attribution was left to a search engine's idea of
relevance rather than the corpus this project maintains.

Window, not archive. Thirty days is ~75k vectors at 1024 dims: 300MB and
a ~40ms brute-force scan. A year would be 900k and 3.6GB, spent on news
no attribution will ever ask about — the question is always "the last few
hours", occasionally "the last few days".
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

from alpha_agents.config import CHROMA_PATH
from alpha_agents.data.vector_store import VectorStore

logger = logging.getLogger(__name__)

COLLECTION = "news_vectors"
RETENTION_DAYS = int(os.environ.get("NEWS_INDEX_RETENTION_DAYS", "30"))

# One embedding call per batch; the API caps how much it will take at once.
BATCH_SIZE = 64

# Indexing every flash would spend the budget on "某某公司发布公告" filler.
# A flash short enough to be a headline with no body carries no
# attribution value.
MIN_TEXT_LENGTH = 12

_store: VectorStore | None = None


def get_store() -> VectorStore:
    global _store
    if _store is None:
        _store = VectorStore(CHROMA_PATH / f"{COLLECTION}.db", table=COLLECTION)
    return _store


def _document(row: dict) -> str:
    """What gets embedded: headline plus the part of the body that adds to it.

    Sources repeat the headline verbatim in the summary, so concatenating
    both would weight the headline twice against every other flash.
    """
    title = (row.get("title") or "").strip()
    summary = (row.get("summary") or "").strip()
    if summary.startswith(title):
        summary = summary[len(title):].strip()
    if not summary or summary == title:
        return title
    return f"{title}。{summary}"[:600]


def _row_id(row: dict) -> str:
    return f"{row.get('source', '')}|{row.get('time', '')}|{(row.get('title') or '')[:60]}"


def index_recent_news(hours: int = 24, limit: int = 2000) -> dict:
    """Embed flashes from the last ``hours`` that are not indexed yet.

    Returns {"scanned", "indexed", "skipped", "pruned"}.

    Incremental by construction: ids are derived from source + time +
    headline, so a re-run over an overlapping window costs one existence
    check and no embedding calls.
    """
    from alpha_agents.data.embeddings import embed_texts
    from alpha_agents.data.snapshot_store import read_latest_news

    store = get_store()
    rows = read_latest_news(limit=limit)
    if not rows:
        return {"scanned": 0, "indexed": 0, "skipped": 0, "pruned": 0}

    cutoff = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    fresh = [r for r in rows if (r.get("time") or "") >= cutoff]

    candidates = []
    for row in fresh:
        doc = _document(row)
        if len(doc) < MIN_TEXT_LENGTH:
            continue
        candidates.append((_row_id(row), doc, row))

    if not candidates:
        return {"scanned": len(fresh), "indexed": 0, "skipped": 0, "pruned": 0}

    known = store.existing_ids([c[0] for c in candidates])
    todo = [c for c in candidates if c[0] not in known]
    if not todo:
        return {"scanned": len(fresh), "indexed": 0,
                "skipped": len(candidates), "pruned": 0}

    indexed = 0
    for i in range(0, len(todo), BATCH_SIZE):
        batch = todo[i:i + BATCH_SIZE]
        try:
            vectors = embed_texts([b[1] for b in batch])
        except Exception as e:
            logger.error("News embedding failed for batch %d (%d items): %s",
                         i // BATCH_SIZE + 1, len(batch), e)
            continue
        if len(vectors) != len(batch):
            logger.error("Embedding returned %d vectors for %d items — skipping "
                         "batch rather than pairing them wrongly",
                         len(vectors), len(batch))
            continue
        indexed += store.upsert(
            ids=[b[0] for b in batch],
            embeddings=vectors,
            documents=[b[1] for b in batch],
            stamps=[b[2].get("time") or "" for b in batch],
            metas=[{"source": b[2].get("source", ""),
                    "title": b[2].get("title", ""),
                    "url": b[2].get("url", "")} for b in batch],
        )

    pruned = prune_old()
    logger.info("News index: scanned %d, indexed %d, already had %d, pruned %d "
                "(total %d)", len(fresh), indexed, len(candidates) - len(todo),
                pruned, store.count())
    return {"scanned": len(fresh), "indexed": indexed,
            "skipped": len(candidates) - len(todo), "pruned": pruned}


def prune_old(days: int | None = None) -> int:
    """Drop vectors older than the retention window."""
    days = days if days is not None else RETENTION_DAYS
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    return get_store().prune_before(cutoff)


def search_news(query: str, hours: int = 24, top_k: int = 8,
                min_score: float = 0.45) -> list[dict]:
    """Flashes semantically closest to ``query`` within the time window.

    ``min_score`` exists because cosine always returns *something*: over a
    corpus of financial flashes the nearest neighbour to "光刻机" is never
    far away in the geometry even when nothing that day mentioned it, and
    an attribution built on the 12th-closest flash is worse than one that
    admits it found nothing.
    """
    if not query:
        return []

    from alpha_agents.data.embeddings import embed_texts

    try:
        vector = embed_texts([query])[0]
    except Exception as e:
        logger.warning("News search unavailable (embedding failed): %s", e)
        return []

    since = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    hits = get_store().query(vector, top_k=top_k * 2, since=since)

    out = []
    for hit in hits:
        if hit["score"] < min_score:
            continue
        meta = hit.get("meta") or {}
        out.append({
            "title": meta.get("title") or hit["document"][:60],
            "text": hit["document"],
            "source": meta.get("source", ""),
            "url": meta.get("url", ""),
            "time": hit.get("stamp") or "",
            "score": round(hit["score"], 3),
        })
        if len(out) >= top_k:
            break
    return out
