"""Backfill historical news from Eastmoney 7x24 archive.

Eastmoney's newsapi paginates backwards in time:
  https://newsapi.eastmoney.com/kuaixun/v1/getlist_102_ajaxResult_50_{page}_.html

  page=1 → latest news
  page=~8 per day worth of items
  Archive reaches ~2025-12-27 (cap).

Each page returns 50 items wrapped in JSONP:
  var ajaxResult={"rc":1,"LivesList":[...]};

We walk pages until we hit the target start date (or the archive cap), then
group items by date and write one JSON blob per date into daily_snapshots
(data_type='news_em'). Existing dates are OVERWRITTEN.

Usage:
  uv run python scripts/backfill_news.py --start 2026-01-01
  uv run python scripts/backfill_news.py --start 2026-01-01 --resume  # skip dates already present
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections import defaultdict
from datetime import datetime

import requests

from alpha_agents.config import no_proxy
from alpha_agents.data.memory_store import _get_conn

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("backfill_news")

URL_TMPL = ("https://newsapi.eastmoney.com/kuaixun/v1/"
            "getlist_102_ajaxResult_50_{page}_.html")
HEADERS = {"User-Agent": "Mozilla/5.0"}


def _parse_jsonp(text: str) -> dict:
    if text.startswith("var "):
        eq = text.find("=")
        text = text[eq + 1:].rstrip().rstrip(";")
    return json.loads(text)


def fetch_page(page: int, retries: int = 3) -> list[dict]:
    url = URL_TMPL.format(page=page)
    for attempt in range(retries):
        try:
            with no_proxy():
                r = requests.get(url, headers=HEADERS, timeout=15)
            r.raise_for_status()
            data = _parse_jsonp(r.text)
            return data.get("LivesList", []) or []
        except Exception as e:
            if attempt == retries - 1:
                logger.warning("page %d failed after %d tries: %s", page, retries, e)
                return []
            time.sleep(1 + attempt)
    return []


def _item_to_record(item: dict) -> dict | None:
    show = item.get("showtime", "")
    if not show or len(show) < 10:
        return None
    return {
        "time": show,
        "title": (item.get("title") or "").strip(),
        "summary": (item.get("digest") or "").strip()[:500],
        "url": item.get("url_w") or item.get("url_m") or "",
        "source": "东方财富7x24",
    }


def _existing_dates() -> set[str]:
    rows = _get_conn().execute(
        "SELECT date FROM daily_snapshots WHERE data_type='news_em'"
    ).fetchall()
    return {r["date"] for r in rows}


def _save_date(date: str, items: list[dict]) -> None:
    # Sort ascending by time so consumers get chronological order.
    items.sort(key=lambda x: x["time"])
    payload = json.dumps(
        {"date": date, "count": len(items), "news": items},
        ensure_ascii=False,
    )
    conn = _get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO daily_snapshots (date, data_type, data, created_at) "
        "VALUES (?, 'news_em', ?, datetime('now'))",
        (date, payload),
    )
    conn.commit()


def backfill(start_date: str, resume: bool, throttle: float, max_pages: int) -> None:
    start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
    skip = _existing_dates() if resume else set()
    if skip:
        logger.info("Resume mode: %d dates already present, will skip", len(skip))

    by_date: dict[str, list[dict]] = defaultdict(list)
    seen_ids: set[str] = set()
    prev_last_time: str | None = None
    stall_count = 0

    page = 1
    min_date_seen: str | None = None

    while page <= max_pages:
        items = fetch_page(page)
        if not items:
            logger.info("page %d empty, stopping", page)
            break

        # Detect stall (cap reached): same last timestamp as previous page means
        # the archive is exhausted and server is echoing the same window.
        last_time = items[-1].get("showtime", "")
        if last_time == prev_last_time:
            stall_count += 1
            if stall_count >= 2:
                logger.info("page %d: archive cap reached (%s), stopping",
                            page, last_time)
                break
        else:
            stall_count = 0
            prev_last_time = last_time

        new_items = 0
        for item in items:
            nid = item.get("newsid") or item.get("id")
            if nid in seen_ids:
                continue
            seen_ids.add(nid)
            rec = _item_to_record(item)
            if not rec:
                continue
            date = rec["time"][:10]
            by_date[date].append(rec)
            new_items += 1
            if min_date_seen is None or date < min_date_seen:
                min_date_seen = date

        first_time = items[0].get("showtime", "")
        logger.info("page %4d: +%2d new, range %s → %s (min_seen=%s, dates=%d)",
                    page, new_items, first_time[:19], last_time[:19],
                    min_date_seen, len(by_date))

        # Stop when we've pulled at least one page where everything is < start
        if min_date_seen and min_date_seen < start_date:
            # Check if page is fully below start_date
            all_below = all(
                (it.get("showtime", "")[:10] < start_date) for it in items
            )
            if all_below:
                logger.info("page %d fully before %s, stopping", page, start_date)
                break

        page += 1
        if throttle > 0:
            time.sleep(throttle)

    # Write out: only dates >= start_date, skipping resume-existing.
    logger.info("Fetched %d unique items across %d dates (min=%s)",
                len(seen_ids), len(by_date), min_date_seen)

    written = 0
    skipped = 0
    for date in sorted(by_date.keys()):
        if date < start_date:
            continue
        if date in skip:
            skipped += 1
            continue
        _save_date(date, by_date[date])
        written += 1
        logger.info("  saved %s: %d items", date, len(by_date[date]))

    logger.info("Done: %d dates written, %d skipped (resume)",
                written, skipped)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True, help="earliest date, YYYY-MM-DD")
    parser.add_argument("--resume", action="store_true",
                        help="skip dates already present in daily_snapshots")
    parser.add_argument("--throttle", type=float, default=0.3,
                        help="seconds between pages (default 0.3)")
    parser.add_argument("--max-pages", type=int, default=1500,
                        help="safety cap on page count (default 1500)")
    args = parser.parse_args()

    try:
        datetime.strptime(args.start, "%Y-%m-%d")
    except ValueError:
        print("--start must be YYYY-MM-DD", file=sys.stderr)
        sys.exit(1)

    backfill(args.start, args.resume, args.throttle, args.max_pages)


if __name__ == "__main__":
    main()
