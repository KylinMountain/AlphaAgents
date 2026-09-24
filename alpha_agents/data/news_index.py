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


class NewsSearchUnavailable(RuntimeError):
    """The index could not be searched at all.

    Distinct from an empty result on purpose. "This theme has no news" is a
    statement about the market; "the embedding provider returned 402" is a
    statement about us, and a caller that receives the same empty list for
    both will report the first when the second is true.
    """

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
    # A stamp in the future is a mis-parsed date, not news from the future —
    # but indexed, it would answer every later window it falls in. The live
    # index held one stamped 2026-12-16 on 2026-09-24.
    horizon = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    fresh = [r for r in rows if cutoff <= (r.get("time") or "") <= horizon]

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


def _embed_and_store(store: VectorStore, rows: list[dict]) -> int:
    from alpha_agents.data.embeddings import embed_texts
    candidates = [(_row_id(r), _document(r), r) for r in rows
                  if len(_document(r)) >= MIN_TEXT_LENGTH]
    known = store.existing_ids([c[0] for c in candidates]) if candidates else set()
    todo = [c for c in candidates if c[0] not in known]
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
            logger.error("Embedding returned %d vectors for %d items — skipping",
                         len(vectors), len(batch))
            continue
        indexed += store.upsert(
            ids=[b[0] for b in batch], embeddings=vectors,
            documents=[b[1] for b in batch],
            stamps=[b[2].get("time") or "" for b in batch],
            metas=[{"source": b[2].get("source", ""),
                    "title": b[2].get("title", ""),
                    "url": b[2].get("url", "")} for b in batch])
    return indexed


def index_window(since: str, until: str, limit: int = 100000) -> int:
    """Embed every flash published in [since, until] that is not indexed yet.

    For a replay: the live index keeps 30 days, so a 2026-01 window has no
    vectors at all. A replay runs with its own ``DATA_DIR``, so this writes to
    its own index. Nothing is pruned here — pruning is by wall clock and would
    delete the whole historical window.
    """
    from alpha_agents.data.snapshot_store import read_news
    rows = read_news(None, as_of=until, since=since, limit=limit)
    rows = [r for r in rows if since <= (r.get("time") or "") <= until]
    n = _embed_and_store(get_store(), rows)
    if n:
        logger.info("News index window %s → %s: %d newly embedded", since, until, n)
    return n


def search_window(query: str, since: str, until: str, top_k: int = 5,
                  min_score: float = 0.45) -> list[dict]:
    """Flashes semantically closest to ``query`` published in [since, until].

    The window is the whole as-of statement, and it is checked twice: the
    store filters on it before scoring, and every hit's stamp is compared
    again here, so no flash stamped after ``until`` can reach a caller.
    """
    if not query:
        return []
    from alpha_agents.data.embeddings import embed_texts
    try:
        vector = embed_texts([query])[0]
    except Exception as e:
        logger.warning("News search unavailable (embedding failed): %s", e)
        raise NewsSearchUnavailable(str(e)) from e
    hits = get_store().query(vector, top_k=top_k * 2, since=since, until=until)
    out = []
    for hit in hits:
        stamp = str(hit.get("stamp") or "")
        if hit["score"] < min_score or not stamp or not (since <= stamp <= until):
            continue
        meta = hit.get("meta") or {}
        out.append({"title": meta.get("title") or hit["document"][:60],
                    "time": stamp, "score": round(hit["score"], 3)})
        if len(out) >= top_k:
            break
    return out


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
        # Raised, not swallowed into an empty list. A caller that cannot
        # tell "searched and found nothing" from "could not search" ends up
        # telling an agent that a theme has no news when the embedding
        # provider is down — a market fact reported from an outage. Seen
        # live 2026-09-22: 343 of these in one session, every one a 402
        # "account balance is insufficient", and every agent reading news
        # was told 无新消息 for three and a half hours.
        logger.warning("News search unavailable (embedding failed): %s", e)
        raise NewsSearchUnavailable(str(e)) from e

    now = datetime.now()
    since = (now - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    # Bounded above as well: a flash stamped in the future (a mis-parsed date;
    # the index held two on 2026-09-24, one dated 2026-12-16) would otherwise
    # answer every live search until that date came.
    until = now.strftime("%Y-%m-%d %H:%M:%S")
    hits = get_store().query(vector, top_k=top_k * 2, since=since, until=until)

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
