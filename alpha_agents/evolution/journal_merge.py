"""A replay's notes are the trader's notes. Keep them.

The isolation a replay runs under protects the **book**: a simulated fill
must never touch a real position, and ``walk_forward._refuse_production``
enforces that. It was applied to everything the replay wrote, including the
notes it made about its own closed trades, and those notes live in a
scratch directory that is deleted when the run ends.

Measured 2026-09-22: a 20-day replay wrote five observations, each with its
evidence episodes, and all five died with the temp directory. There is no
transfer path anywhere in the repository; ``walk_forward`` states the
position outright — "historical replay screens candidates and never promotes
them".

That is the wrong line in the wrong place. Screening a *rule* is what the
promotion gate is for, and that gate is untouched here. What died was not a
rule; it was the trader's recollection of what happened to it, and a trader
that forgets every run cannot get better at anything. The whole reason to
run a replay is to let it accumulate experience.

**What this does not relax.** The book stays isolated — nothing here reads
or writes a position. Nothing becomes a rule: merged rows land as
``observation`` and only the promotion path can move them. Fingerprints are
unique with ``ON CONFLICT DO NOTHING``, so re-running the same window is a
no-op rather than a second copy of the same memory.

``source`` is preserved as bookkeeping and the agent never sees it:
``journal.own_trade_notes`` renders the date and the claim. To the trader
this is simply what it remembers.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

_COLUMNS = ("fingerprint", "entity_type", "operation", "target_id", "source",
            "source_date", "payload_json", "claim", "applicable_context",
            "proposed_behavior_delta", "evidence_episode_ids", "status",
            "created_at")


def merge_from(replay_dir: str | Path) -> dict:
    """Copy a finished replay's observations into the durable journal.

    Returns what moved. Raises nothing for an absent or empty replay: a run
    that learned nothing is a fact about the run, not an error here.
    """
    from alpha_agents.data import learning_candidates as LC
    from alpha_agents.data import memory_store

    path = Path(replay_dir) / "memory.db"
    if not path.exists():
        logger.warning("Journal merge: %s has no memory.db", replay_dir)
        return {"merged": 0, "seen": 0, "why": "no replay database"}

    try:
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        src.row_factory = sqlite3.Row
        rows = src.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM learning_candidates "
            "WHERE status = 'observation' ORDER BY id").fetchall()
        src.close()
    except sqlite3.Error as exc:
        logger.warning("Journal merge: cannot read %s: %s", path, exc)
        return {"merged": 0, "seen": 0, "why": str(exc)}

    if not rows:
        logger.info("Journal merge: %s has no observations", replay_dir)
        return {"merged": 0, "seen": 0}

    merged = 0
    with memory_store._write_lock:
        conn = memory_store._get_conn()
        with conn:
            LC.init_schema(conn)
            for row in rows:
                cursor = conn.execute(
                    f"INSERT INTO learning_candidates ({', '.join(_COLUMNS)}) "
                    f"VALUES ({', '.join('?' * len(_COLUMNS))}) "
                    "ON CONFLICT(fingerprint) DO NOTHING",
                    tuple(row[name] for name in _COLUMNS))
                merged += int(cursor.rowcount or 0)

    logger.info("Journal merge: %d of %d observation(s) from %s are now the "
                "trader's own; %d were already remembered",
                merged, len(rows), replay_dir, len(rows) - merged)
    return {"merged": merged, "seen": len(rows)}
