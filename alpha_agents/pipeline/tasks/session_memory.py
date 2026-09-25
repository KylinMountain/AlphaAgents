"""Persistent, trader-local prior views; never a filter or an invented refusal."""
from __future__ import annotations

from datetime import datetime

from alpha_agents.data import trader_session

_DECLINE_TTL_MINUTES = 45


def note_decline(code: str, price: float, reason: str, *, trader_id: str,
                 run_id: str | None = None) -> None:
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("An explicit decline requires a reason")
    trader_session.append(trader_id=trader_id, run_id=run_id, code=code,
                          kind="entry_observation", payload={"action": "decline",
                          "price": price, "reason": reason})


def note_undecided(code: str, price: float, *, trader_id: str,
                   run_id: str | None = None) -> None:
    trader_session.append(trader_id=trader_id, run_id=run_id, code=code,
                          kind="entry_observation", payload={"action": "undecided", "price": price})


def recall_decline(code: str, price: float, *, trader_id: str,
                   run_id: str | None = None) -> str | None:
    rows = trader_session.read(trader_id=trader_id, run_id=run_id,
                               kind="entry_observation", code=code)
    if not rows or rows[-1]["payload"].get("action") != "decline":
        return None
    row = rows[-1]
    minutes = (datetime.fromisoformat(trader_session.instant()) -
               datetime.fromisoformat(row["observed_at"])).total_seconds() / 60
    if not 0 <= minutes <= _DECLINE_TTL_MINUTES:
        return None
    old, reason = row["payload"]["price"], row["payload"]["reason"]
    change = (price / old - 1) * 100 if old and price else 0
    return (f"{int(minutes)} \u5206\u949f\u524d\u4f60\u770b\u8fc7\u8fd9\u53ea\u7968\u5e76\u51b3\u5b9a\u4e0d\u4e70\uff0c\u5f53\u65f6 {old:.2f}\uff0c"
            f"\u7406\u7531\uff1a{reason[:110]}\u3002\u73b0\u5728 {price:.2f}\uff08{change:+.1f}%\uff09\u3002"
            "\u8bf7\u6839\u636e\u65b0\u4ef7\u683c\u548c\u65b0\u8bc1\u636e\u91cd\u65b0\u5224\u65ad\uff0c\u539f\u51b3\u5b9a\u4e0d\u662f\u7981\u4ee4\u3002")
