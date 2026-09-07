"""Keep the local news store fed from the continuous flash streams.

The flash sources (金十, 新浪7x24, 财联社, 东财7x24 …) are continuous
streams; the analysis tasks are windowed — 晨扫 at 06:30 wants everything
since yesterday's close, 复盘 wants the session. Those two shapes do not
meet if each task calls the APIs itself with ``limit=N``:

  * ``limit=50`` on Jin10 covers roughly the last hour at the observed
    rate (~0.85 items/min), so a 06:30 scan sees about one hour of the
    fifteen since yesterday's close — the overnight policy and offshore
    news it exists to read is simply not in the window.
  * Between two task runs nothing fetches, so anything published in the
    gap is never stored at all.
  * Raise ``limit`` instead and every run re-reads what it already saw.

So ingestion is decoupled from consumption: this task polls every five
minutes and cheaply — no LLM, no analysis, just write-through to
``news_items``,
which already dedups on md5(source|title|published_at). Tasks then read
a *time window* out of the store via ``read_news(since=..., as_of=...)``
and get exactly what arrived, once.

Runs all day, not only on trading days: overnight and weekend news is
what the morning scan is for.
"""

import asyncio
import json
import logging

from alpha_agents.config import NEWS_FETCH_LIMIT
from alpha_agents.data.activity_log import log_activity
from alpha_agents.pipeline.monitor import NEWS_SOURCES

logger = logging.getLogger(__name__)

# Fetch deeper than a single window so a slow cycle or a brief outage does
# not punch a hole in the stream. Overlap is free — the store dedups.
_INGEST_LIMIT = max(NEWS_FETCH_LIMIT, 50)

# Ceiling on any one source. Measured on the box: every source returned
# within 11s except 社交媒体, which took 139s — it walks several
# twitter/google-news handles in sequence and each unreachable one burns
# its own retries. That single source was what kept cancelling the sweep,
# taking the twelve that had already answered down with it.
#
# 40s sits well above the 11s the healthy sources need and well below the
# task's own ceiling.
#
# A slow source is dropped for this cycle, not permanently: the next sweep
# is five minutes away and the store dedups, so the only cost of skipping
# one is a few minutes of latency on that feed.
_PER_SOURCE_TIMEOUT = 40


async def _ingest_one(source_id: str, name: str, fetch_fn_factory) -> tuple[str, int, str]:
    """Fetch one source and let it write through to the store.

    Every ``get_*_fn`` already calls ``save_news`` internally, so this
    only has to trigger the fetch and count what came back.
    """
    try:
        raw = await asyncio.wait_for(
            asyncio.to_thread(fetch_fn_factory), timeout=_PER_SOURCE_TIMEOUT,
        )
        data = json.loads(raw)
        if data.get("error"):
            return name, 0, str(data["error"])[:120]
        return name, len(data.get("news", [])), ""
    except asyncio.TimeoutError:
        # The worker thread keeps running until its blocking call returns;
        # it is a daemon of the default executor and writes through to the
        # store on its own if it ever finishes.
        return name, 0, f"超过 {_PER_SOURCE_TIMEOUT}s"
    except Exception as e:
        return name, 0, f"{type(e).__name__}: {e}"[:120]


async def run_news_ingest() -> str | None:
    """Poll every flash source once and write through to the store.

    Returns a short summary when something was fetched, else None so the
    scheduler does not log an empty cycle.
    """
    tasks = [
        _ingest_one(sid, name, fn)
        for sid, name, fn in NEWS_SOURCES
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    ok, failed, total = [], [], 0
    for r in results:
        if isinstance(r, Exception):
            failed.append(f"?: {r}"[:80])
            continue
        name, count, err = r
        if err:
            failed.append(f"{name}({err[:40]})")
        else:
            total += count
            if count:
                ok.append(f"{name}:{count}")

    if not total and not failed:
        return None

    summary = f"摄取 {total} 条 / {len(ok)} 源"
    if failed:
        summary += f"，{len(failed)} 源异常"
    logger.info("News ingest: %s | ok=%s | failed=%s",
                summary, ",".join(ok), ",".join(failed))

    # Only surface failures in the activity feed — a healthy ingest every
    # few minutes would drown the stream the user actually reads.
    if failed:
        log_activity("task_failed", task="news_ingest", status="failed",
                     message=f"{summary}: {', '.join(failed)}"[:500])
    return None
