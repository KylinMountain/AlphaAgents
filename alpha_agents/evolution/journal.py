"""The trader's own dated notes about its own closed trades.

Distinct from two neighbours it is easy to confuse with:

* **Approved knowledge** (``feedback.inject_principles``) is a *rule*, and a
  rule reaches a decision only through an approved snapshot. That gate is
  right and is untouched here.
* **Raw lessons** (``feedback.inject_recent_lessons``) are the model's own
  suggestions about what to do. They are research material and the decision
  path deliberately cannot see them.

This is neither. It is what happened to this trader's own positions, dated,
with the episodes cited. Remembering is not deciding, and the argument is
the one ``walk_forward._knowledge_block`` already made for the replay:

    the gate belongs on *promotion* — on a note becoming a rule — not on a
    trader remembering what happened to it. A loop with no feedback at all
    is not a conservative loop; it is not a loop.

**Why this module exists.** That argument was implemented for the replay and
never for production. Measured 2026-09-22: ``build_review_context`` reads the
trader's lessons, ``build_morning_context`` — the context that actually picks
the stocks — read only the approved snapshot, which names nothing. So the
production trader chose what to buy with no recollection of its own trades,
while the post-mortem that could not act on it had the record. The decision
path was the one without the memory.

``as_of`` is used, not decorative: notes are filtered to ``source_date <
as_of``. The learning step runs at the close and this block is read at the
next open, so in a single pass they never overlap anyway — the filter is
what makes that a property of the code rather than of the loop's shape.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

#: How many notes reach the prompt. A journal is read, not searched: past a
#: dozen entries the agent is skimming and the oldest crowd out the newest.
LIMIT = 12

_HEADER = ("【你自己的交易记录（你写下的观察，样本很小——"
           "是观察不是结论，不要当规则执行）】")


def own_trade_notes(as_of: str | None = None, *, limit: int = LIMIT) -> str:
    """Dated observations this trader wrote about its own closed trades.

    Empty string when there are none, which is honest: a trader with no
    closed trades has nothing to remember. A failure is also empty and is
    logged — a broken read must not stop a decision, and it must not look
    like "nothing happened yet" in the log either.
    """
    try:
        from alpha_agents.data import learning_candidates as LC
        rows = LC.candidates_by_status(LC.OBSERVATION, limit=200)
    except Exception as exc:                          # noqa: BLE001
        logger.warning("Own-trade notes unavailable: %s", exc)
        return ""
    if as_of:
        rows = [row for row in rows if (row["source_date"] or "") < as_of]
    if not rows:
        return ""

    lines = [_HEADER]
    for row in rows[-limit:]:
        lines.append(f"· {row['source_date']}｜{row['claim']}")
        try:
            cited = json.loads(row["evidence_episode_ids"] or "{}")
        except (TypeError, ValueError):
            continue
        ids = sorted(set(cited.get("supporting") or [])
                     | set(cited.get("opposing") or []))
        if ids:
            lines.append("  证据 episode：" + "、".join(f"#{i}" for i in ids))
    return "\n".join(lines)
