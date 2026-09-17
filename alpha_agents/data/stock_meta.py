"""Which concepts a name belongs to, read from the corpus.

Exists because A-share judgement is mostly sector judgement: a name trading
outside its 主线 is a different bet from one leading it, and a panel that
offers twelve bare codes cannot express that. The membership lives in
``stocks.db``, which a replay shares read-only, so this opens through
``corpus_access`` like every other corpus reader.

**The membership is current, not point-in-time.** ``concept_stocks`` has two
columns — ``concept_id`` and ``stock_code`` — and no date, so a name's concept
list is what it is *now* rather than what it was on a replayed session. A
concept assigned after the window therefore appears in it. That is a real
lookahead, and it is weaker than a price leak because a concept label is not
an outcome, but it is not nothing: read anything here as "what this name is
now tagged with", never as "what the market called it then".

The alternative today is not a point-in-time source, it is no sector
information at all, which is worse for the thing this is for. Callers are
expected to say which they are doing — ``walk_forward`` labels the panel
column 概念（当前成分）.
"""

from __future__ import annotations

import logging
import sqlite3

from alpha_agents.config import DATA_DIR
from alpha_agents.data import corpus_access

logger = logging.getLogger(__name__)

DB_PATH = DATA_DIR / "stocks.db"

#: A name in this many concepts is not being described, it is being listed.
#: The corpus has names tagged with up to 28, and a 28-item list in a prompt
#: is noise that crowds out the ones that matter.
MAX_CONCEPTS = 4

_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    """One connection per process, read-only when the corpus is shared.

    Not cached across a rebuild: ``walk_bootstrap`` replaces the file with a
    symlink, and a caller that swaps the corpus mid-process must not keep
    reading the old inode. ``connect`` is cheap and the maps below are the
    cache that matters.
    """
    global _conn
    if _conn is None:
        _conn = corpus_access.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
    return _conn


def reset_connection() -> None:
    """Drop the cached connection. For tests that rebuild the corpus."""
    global _conn
    if _conn is not None:
        try:
            _conn.close()
        except sqlite3.Error as exc:
            logger.debug("Closing the concept connection failed: %s", exc)
    _conn = None


def concepts_by_code() -> dict[str, list[str]]:
    """``code → [concept names]`` for the whole corpus, in one read.

    One query rather than one per name: the panel shows a few dozen codes and
    a per-code lookup is a query per row for a table that fits in memory
    (375 concepts, ~3.7k memberships).

    Capped at :data:`MAX_CONCEPTS` per name, longest-first is *not* the rule —
    the order is the corpus's own, so it is stable across runs and a diff
    between two windows means something.
    """
    try:
        rows = _get_conn().execute(
            "SELECT cs.stock_code AS code, c.name AS name "
            "  FROM concept_stocks cs "
            "  JOIN concepts c ON c.id = cs.concept_id "
            " ORDER BY cs.stock_code, c.name").fetchall()
    except sqlite3.DatabaseError as exc:
        # A missing or unreadable corpus is not a reason to refuse a run: the
        # panel simply carries no concepts and the report says so.
        logger.warning("Concept membership unavailable: %s", exc)
        return {}

    out: dict[str, list[str]] = {}
    for row in rows:
        name = (row["name"] or "").strip()
        if not name:
            continue
        bucket = out.setdefault(row["code"], [])
        if len(bucket) < MAX_CONCEPTS:
            bucket.append(name)
    return out


def concepts_for(code: str) -> list[str]:
    """One name's concepts. Convenience over :func:`concepts_by_code`."""
    return concepts_by_code().get(code, [])
