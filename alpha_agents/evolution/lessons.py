"""L2 Lessons — extract structured lessons from review report, consolidate into principles."""

from __future__ import annotations

import json
import logging
import re

from alpha_agents.data.memory_store import insert_daily_lesson

logger = logging.getLogger(__name__)

# Parallel to vpa.py's <!-- VERDICT: {...} --> pattern.
_LESSONS_TAG_RE = re.compile(r"<!--\s*LESSONS:\s*(\[.*?\])\s*-->", re.DOTALL)


def extract_daily_lessons(report: str, today: str) -> int:
    """Parse the <!-- LESSONS: [...] --> tag from a review report and persist rows.

    Returns the count of lessons successfully inserted. Unparseable/missing
    tag returns 0 (warn-logs only — don't raise, the review itself succeeded).
    """
    m = _LESSONS_TAG_RE.search(report)
    if not m:
        logger.info("No LESSONS tag in review report; skipping extraction")
        return 0

    raw = m.group(1).strip()
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("LESSONS JSON malformed: %s", e)
        return 0

    if not isinstance(items, list):
        logger.warning("LESSONS payload is not a list")
        return 0

    count = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        content = (item.get("content") or "").strip()
        lesson_type = (item.get("type") or "").strip()
        if not content or lesson_type not in ("success", "failure", "insight"):
            continue
        try:
            insert_daily_lesson(
                date=today,
                lesson_type=lesson_type,
                theme=(item.get("theme") or None) or None,
                content=content,
                tags=(item.get("tags") or ""),
            )
            count += 1
        except Exception as e:
            logger.warning("Failed to insert lesson %s: %s", content[:40], e)
    return count
