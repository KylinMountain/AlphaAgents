"""这一个交易日里，这个交易员已经想过什么。

盘中扫描每五分钟跑一次，同一只票在一个上午会被推上来十几次。
2026-09-10 那天 301511 被定价了 **19 次**，被否了 19 次，理由几乎是
同一句话——它每一次都有完整的决定权，缺的从来不是权限，是记忆。

第一版的修法是设一道墙：否过就 45 分钟内不再问。token 是省下来了，
省掉的恰恰是省 token 的目的——它再也不会发现那只 12.40 嫌贵的票现在
13.60 了。

所以这里存的不是「屏蔽名单」，是**它自己说过的话**：当时什么价、
什么理由、现在什么价。同一条信息，当过滤器用是墙，交回给它是经验。

刻意是进程内的：这是一场盘的工作记忆，不是谁该在几天后拿去推理的
记录。重启之后忘掉上午，和一个人离开盘面再回来是同一件事。
"""

from __future__ import annotations

from datetime import datetime

# How long today's declines stay worth mentioning. Beyond this the
# session has moved on and an old "no" says more about the morning than
# about now.
_DECLINE_TTL_MINUTES = 45

# Today's declines: code -> (price, when, reason). Process-local, because
# it is one session's working memory rather than a record anyone should
# reason about later; a restart forgetting the morning is the same thing
# that happens to a person who steps away from the desk.
_declined: dict[str, tuple[float, datetime, str]] = {}


def note_decline(code: str, price: float, reason: str = "") -> None:
    """Remember that this trader looked at this stock and said no, and why.

    The reason is the whole point. Without it this is a filter; with it,
    it is experience — the difference between a wall and a memory.
    """
    _declined[code] = (price, datetime.now(), reason or "未记录理由")


def recall_decline(code: str, price: float) -> str | None:
    """What this trader said about this stock earlier today, if anything.

    Written for the agent to read, not for code to branch on. 301511 was
    priced nineteen times in one session and declined nineteen times with
    almost the same sentence — not because it lacked the authority to
    change its mind, but because every look was a first encounter. Nobody
    was refusing it the choice; it had no memory to choose against.

    So the fix is not to block the re-ask. It is to hand back what it
    already decided, with the price then and the price now, and let it
    decide whether anything has changed.
    """
    prev = _declined.get(code)
    if not prev:
        return None
    old_price, when, reason = prev
    mins = (datetime.now() - when).total_seconds() / 60
    if mins > _DECLINE_TTL_MINUTES:
        return None
    move = ((price - old_price) / old_price * 100
            if old_price and price else 0.0)
    return (f"{int(mins)} 分钟前你看过这只票并决定不买，当时 {old_price:.2f}，"
            f"理由：{reason[:110]}。现在 {price:.2f}（{move:+.1f}%）。"
            f"变了吗？没变就还是不买，这不算重复劳动。")


