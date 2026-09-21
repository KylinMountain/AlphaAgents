"""THS concept creation dates, for the replay's look-ahead gate.

Why this exists. ``concept_stocks`` is a current snapshot: every concept in
it carries today's members, and ``ths_local`` says so explicitly. That fixes
how *deep* the pool is, never *when* a concept came into being. Aggregating
member fund flow into a concept-level series therefore invents a series for
concepts that did not exist yet — and the invention is not neutral, because
THS creates a concept *after* its stocks start moving together. ``MLCC概念``
was created 2026-07-31; a "MLCC concept fund flow" for January is a grouping
chosen with knowledge of July.

The date lives on the THS 概念解析 page, sorted by addtime descending. Two
things about that page, both measured 2026-09-21:

- The ``/ajax/1/`` variant answers 401 without a ``hexin-v`` signature. The
  plain ``/page/N/`` variant answers 200 with the same table.
- After roughly five pages it answers 302 with an empty body — rate limiting
  by source IP, not a signature problem, so backing off is the only remedy.

Because the sort is addtime-descending, a partial fetch is still a *complete*
answer for any window that starts after the oldest date fetched: every
concept not listed was created earlier than that. :func:`earliest_known`
reports the bound so a caller can say which windows its data covers, and
``created_date IS NULL`` means "older than the bound", which
:func:`concepts_as_of` admits rather than drops.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")

#: The listing, newest first. ``page/1/`` and the bare path are the same page.
_PAGE_URL = "http://q.10jqka.com.cn/gn/index/field/addtime/order/desc/page/%d/"

#: One row: date, concept code, concept name, member count.
_ROW_RE = re.compile(
    r'<tr>\s*<td>(\d{4}-\d{2}-\d{2})</td>\s*'
    r'<td><a href="[^"]*?/code/(\d+)/?"[^>]*>([^<]+)</a></td>.*?'
    r'<td>(\d+)</td>\s*</tr>', re.S)


def _fetch_page(page: int, timeout: int = 30) -> str:
    req = urllib.request.Request(_PAGE_URL % page, headers={
        "User-Agent": _UA, "Referer": "http://q.10jqka.com.cn/gn/"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("gbk", errors="replace")


def parse_page(html: str) -> list[tuple[str, str, str, int]]:
    """``(created, ths_code, name, member_count)`` for one listing page."""
    return [(d, code, name.strip(), int(cnt))
            for d, code, name, cnt in _ROW_RE.findall(html)]


def fetch_creation_dates(max_pages: int = 39, throttle: float = 8.0,
                         retries: int = 3) -> dict[str, str]:
    """``{concept name: YYYY-MM-DD}``, newest first, until the page stops.

    Stops at the first page that yields nothing after ``retries`` backed-off
    attempts. That is a partial answer by design — see the module docstring
    for why a prefix of an addtime-descending listing is still sound.
    """
    found: dict[str, str] = {}
    for page in range(1, max_pages + 1):
        rows: list[tuple[str, str, str, int]] = []
        for attempt in range(retries):
            try:
                rows = parse_page(_fetch_page(page))
            except OSError as e:
                logger.warning("concept dates page %d: %s", page, e)
                rows = []
            if rows:
                break
            wait = throttle * (attempt + 1)
            logger.info("concept dates page %d empty (rate limit), "
                        "waiting %.0fs", page, wait)
            time.sleep(wait)
        if not rows:
            logger.info("concept dates: stopped at page %d, %d concepts known",
                        page, len(found))
            break
        for created, _code, name, _cnt in rows:
            found.setdefault(name, created)
        time.sleep(throttle)
    return found


def store_creation_dates(db_path: Path, dates: dict[str, str]) -> int:
    """Write ``created_date`` for the concepts we have a date for."""
    conn = sqlite3.connect(db_path)
    try:
        updated = 0
        for name, created in dates.items():
            cur = conn.execute(
                "UPDATE concepts SET created_date = ? WHERE name = ?",
                (created, name))
            updated += cur.rowcount
        conn.commit()
    finally:
        conn.close()
    logger.info("concept dates: wrote %d of %d", updated, len(dates))
    return updated


def earliest_known(conn: sqlite3.Connection) -> str | None:
    """Oldest stored ``created_date``: the bound below which NULL is safe."""
    row = conn.execute(
        "SELECT MIN(created_date) FROM concepts "
        "WHERE created_date IS NOT NULL").fetchone()
    return row[0] if row else None


def concepts_as_of(conn: sqlite3.Connection, trade_date: str) -> set[str]:
    """Concept names that existed on ``trade_date`` (YYYY-MM-DD).

    A NULL ``created_date`` is admitted: the listing is newest-first, so an
    unfetched concept is older than everything fetched.
    """
    return {name for (name,) in conn.execute(
        "SELECT name FROM concepts "
        "WHERE created_date IS NULL OR created_date <= ?", (trade_date,))}
