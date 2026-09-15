"""Walk 财联社电报's archive backwards into the snapshot store.

Why this exists
---------------
The live pipeline only ever reads the newest page of CLS's roll endpoint, so
``news_items`` holds news from the day the pipeline first ran (2026-09-07).
That makes the news side of any historical reconstruction impossible, while the
price side goes back to 2020 — an asymmetry that blocks evaluating the entry
path on history at all.

CLS's ``get_roll_list`` takes ``last_time``, and the server does not check that
it equals "now": passing a past timestamp returns the items just before it. So
the archive can be walked backwards. Probed 2026-09-15, every date tested down
to **2020-01-01** returned real items dated the day before, which is exactly
where ``daily_kline`` starts — so the news side can be rebuilt for the same
window the prices cover.

How it is kept safe
-------------------
Backfilled rows are written under a **distinct source label**
(``财联社电报(历史回填)``), not the live ``财联社电报`` label. ``read_news``
filters on ``published_at`` only and does not return ``captured_at``, so a
consumer cannot tell reconstructed from originally-captured news; the source
label is what keeps the two evidence classes apart, which the design requires
(§2 invariant 8: simulation and historical replay stay distinguishable). It
also means no source-filtered reader — live or replay — can pick these up by
accident. The one unfiltered reader, ``morning_scan``, reads a ``since`` window
of the last few hours, so rows published years ago cannot enter it.

The overlap with live capture (2026-09-07 onward) is deliberately left
overlapping rather than merged: comparing the two is a free fidelity check on
the reconstruction.

Pacing
------
The shared client already rotates User-Agents, jitters per-domain waits and
retries with backoff. This adds its own longer jitter and a periodic pause, so
the walk does not look like a metronome. ``www.cls.cn`` is on the client's
domestic list and therefore goes direct; ``--via-worker`` forces it through the
Cloudflare Worker instead, which is worth having if the home IP is ever
throttled.

Resumable
---------
The cursor defaults to the oldest item already stored under the backfill label,
so a restart continues where the last run stopped. ``save_news`` dedups on
``(source, title, published_at)``, so re-fetching a boundary page is harmless.

Usage
-----
    .venv/bin/python scripts/backfill_cls_history.py --dry-run --max-requests 3
    .venv/bin/python scripts/backfill_cls_history.py --floor 2020-01-01
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alpha_agents.data import snapshot_store  # noqa: E402
from alpha_agents.http_client import fetch  # noqa: E402
from alpha_agents.sources import cls_telegraph as cls  # noqa: E402

logger = logging.getLogger("backfill_cls")

# Distinct from the live label on purpose — see the module docstring.
BACKFILL_SOURCE = "财联社电报(历史回填)"
LIVE_SOURCE = "财联社电报"

MAX_PAGE = 50

#: CLS refuses to serve the very newest items to a walk that asks for a past
#: cursor twice in a row, so the cursor is nudged one second back each page.
_CURSOR_NUDGE = 1


def _epoch(day: str) -> int:
    return int(time.mktime(time.strptime(day, "%Y-%m-%d")))


def _epoch_of(stamp: str) -> int:
    """Epoch seconds for a stored ``published_at``, date-only or full.

    Truncating a stored cursor to its date would make every restart re-walk
    that whole day (hundreds of items, tens of pages) before reaching new
    ground. The stored value is a full timestamp, so use all of it.
    """
    stamp = stamp[:19]
    fmt = "%Y-%m-%d %H:%M:%S" if len(stamp) > 10 else "%Y-%m-%d"
    return int(time.mktime(time.strptime(stamp, fmt)))


def _stamp(ts: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _page_url(last_time: int, page_size: int) -> str:
    """One roll request with ``last_time`` as the walking cursor.

    Mirrors ``cls_telegraph._build_url`` except that the cursor is a parameter
    rather than ``now``. Signing is delegated to the source module so the
    request shape has one definition.
    """
    params = {
        "app": "CailianpressWeb",
        "os": "web",
        "sv": cls._CLIENT_VERSION,
        "last_time": last_time,
        "refresh_type": 1,
        "rn": max(1, min(page_size, MAX_PAGE)),
        "category": "",
    }
    params["sign"] = cls._sign(params)
    return f"{cls.ROLL_URL}?{urlencode(sorted(params.items()))}"


def _fetch_page(last_time: int, page_size: int, *, via_worker: bool):
    """Return (parsed_items, oldest_epoch) for one page, or ([], 0)."""
    url = _page_url(last_time, page_size)
    resp = fetch(url, headers=cls._EXTRA_HEADERS, timeout=cls.FETCH_TIMEOUT)
    payload = json.loads(resp.text)
    if payload.get("errno"):
        raise RuntimeError(
            f"CLS errno={payload.get('errno')} msg={payload.get('msg', '')} "
            f"(via_worker={via_worker})"
        )
    rows = (payload.get("data") or {}).get("roll_data") or []
    items, stamps = [], []
    for row in rows:
        if not isinstance(row, dict):
            continue
        parsed = cls._parse_item(row)
        if parsed:
            items.append(parsed)
        try:
            stamps.append(int(row.get("ctime")))
        except (TypeError, ValueError):
            continue
    return items, (min(stamps) if stamps else 0)


def _to_store_rows(items: list[dict]) -> list[dict]:
    """Project the source module's dicts onto ``save_news``'s input shape.

    ``_parse_item`` names the share link ``link``; ``save_news`` reads ``url``.
    Mapping here keeps both definitions where they already live instead of
    teaching ``save_news`` a second vocabulary.
    """
    return [{
        "title": i.get("title") or "",
        "summary": i.get("summary") or "",
        "time": i.get("time") or "",
        "url": i.get("link") or "",
    } for i in items]


def _resume_cursor(args) -> tuple[int, int]:
    """Where to start walking, and how many rows are already stored.

    Resuming from the **oldest** stored row, not the newest, is what makes an
    interrupted multi-hour walk restartable: the walk moves backwards through
    time, so the oldest row already held *is* the frontier. With nothing
    stored, the frontier is now.
    """
    floor_ts = _epoch(args.floor)
    stored = snapshot_store.news_count(args.source)
    oldest = snapshot_store.oldest_news_published_at(args.source)
    if oldest:
        try:
            return max(_epoch_of(oldest), floor_ts), stored
        except ValueError:
            logger.warning("Stored cursor %r is unparseable; using the floor",
                           oldest)
    return int(time.time()), stored


def _pace(args) -> None:
    time.sleep(random.uniform(args.pace_min, args.pace_max))


def run(args) -> int:
    floor_ts = _epoch(args.floor)
    cursor, stored = _resume_cursor(args)
    logger.info("resuming from %s (floor %s); %d row(s) already stored",
                _stamp(cursor), args.floor, stored)

    pages = requests_made = written = 0
    started = time.monotonic()
    stalled = 0

    while cursor > floor_ts:
        if args.max_requests and requests_made >= args.max_requests:
            logger.info("stopping: reached --max-requests %d", args.max_requests)
            break
        try:
            items, oldest = _fetch_page(cursor, args.page_size,
                                        via_worker=args.via_worker)
        except Exception as exc:  # noqa: BLE001 — reported, then backing off
            stalled += 1
            backoff = min(60.0, 2.0 ** stalled) * random.uniform(0.8, 1.4)
            logger.warning("page failed (%d in a row): %s — sleeping %.0fs",
                           stalled, exc, backoff)
            if stalled >= args.max_failures:
                logger.error("giving up after %d consecutive failures", stalled)
                break
            time.sleep(backoff)
            continue

        stalled = 0
        requests_made += 1
        pages += 1

        if not items or oldest <= 0:
            logger.info("no items at %s — treating as the archive floor",
                        _stamp(cursor))
            break
        if oldest >= cursor:
            logger.warning("cursor did not advance at %s (oldest %s); stopping",
                           _stamp(cursor), _stamp(oldest))
            break

        rows = _to_store_rows(items)
        if args.dry_run:
            logger.info("dry-run: %s would write %d item(s), oldest %s",
                        _stamp(cursor), len(rows), rows[-1]["time"])
        else:
            written += snapshot_store.save_news(args.source, rows)

        if pages % args.progress_every == 0:
            rate = (cursor - floor_ts) / max(1.0, time.monotonic() - started)
            logger.info("page %d: cursor %s | written %d | %.0f epoch-s/s | "
                        "%.1f min elapsed", pages, _stamp(cursor), written,
                        rate, (time.monotonic() - started) / 60.0)

        cursor = oldest - _CURSOR_NUDGE

        if args.long_pause_every and pages % args.long_pause_every == 0:
            pause = random.uniform(args.long_pause_min, args.long_pause_max)
            logger.info("long pause %.0fs after %d pages", pause, pages)
            time.sleep(pause)
        else:
            _pace(args)

    elapsed = time.monotonic() - started
    logger.info("done: %d page(s), %d new row(s), cursor %s, %.1f min",
                pages, written, _stamp(max(cursor, floor_ts)), elapsed / 60.0)
    print(f"pages={pages} written={written} cursor={_stamp(max(cursor, floor_ts))} "
          f"minutes={elapsed / 60.0:.1f} source={args.source!r}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--floor", default="2020-01-01",
                        help="stop when the cursor reaches this date.")
    parser.add_argument("--source", default=BACKFILL_SOURCE,
                        help="source label to store under (kept distinct from "
                             "the live label on purpose).")
    parser.add_argument("--page-size", type=int, default=MAX_PAGE)
    parser.add_argument("--max-requests", type=int, default=0,
                        help="stop after N requests (0 = no limit).")
    parser.add_argument("--max-failures", type=int, default=8,
                        help="give up after N consecutive failures.")
    parser.add_argument("--pace-min", type=float, default=0.9)
    parser.add_argument("--pace-max", type=float, default=2.6)
    parser.add_argument("--long-pause-every", type=int, default=120,
                        help="every N pages take a longer break (0 = never).")
    parser.add_argument("--long-pause-min", type=float, default=6.0)
    parser.add_argument("--long-pause-max", type=float, default=18.0)
    parser.add_argument("--progress-every", type=int, default=20)
    parser.add_argument("--via-worker", action="store_true",
                        help="force the Cloudflare Worker even though cls.cn is "
                             "on the client's domestic list.")
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch and log, write nothing.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.via_worker:
        from alpha_agents import http_client
        http_client._is_domestic = lambda domain: False  # noqa: SLF001
        logger.warning("routing cls.cn through the Cloudflare Worker")

    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
