"""One review per closed trade, written by the trader, read before every decision.

What a trader learns from is the trade itself: why it bought, how far the
position ran, how much of that it handed back, why it sold. Until this module
the only "experience" a replay trader read was one fixed-formula statistic
("T-1 涨幅高于中位数的 6 笔中位收益 −3.54%…"), seen in 81 prompts over 16
sessions while the share of its orders with a T-1 gain above 3.7% went *up*,
from 7/13 to 11/16. There was nothing in that sentence to act on. A live trader
read nothing of its own at all.

The division of labour is the one the review keeps (GOLDEN_PRINCIPLES #2):

* :func:`facts` computes the trade from ``daily_kline``, sealed at the close
  date — peak and the session it came on, the worst point, the give-back, the
  move the day before the order. No model is involved.
* The trader writes the words: what it got right, what it got wrong, what it
  will do next time. It is asked to cite the facts; the summary line the
  prompt shows is computed from the facts only.

A review is the trader's own record — the half of the context that also holds
its book and its calibration curve — not a rule in ``knowledge_in_force``, so
it needs no approval to be read back. Nothing here tells it what to change.

The model is always passed in: a replay passes its journaled model, the live
review task builds one. This module never builds a client of its own.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import statistics

logger = logging.getLogger(__name__)

#: How many recent reviews a decision reads. Enough to see a pattern across
#: trades, few enough that the decision's own facts are not drowned.
RECENT = 5

_TIMEOUT = 120

#: A review that could not be written (no model, a timeout, an unreadable
#: reply) still stores the facts, so the summary line counts every trade.
NO_WORDS = {"verdict": "", "right": "", "wrong": "", "next_time": "",
            "followed": [], "broke": []}

#: The text fields of a review; ``followed`` / ``broke`` are rule-id lists.
_TEXT = ("verdict", "right", "wrong", "next_time")


def _bars(hist: sqlite3.Connection, code: str, start: str, end: str) -> list:
    return hist.execute(
        "SELECT date, open, high, low, close FROM daily_kline "
        "WHERE code = ? AND date >= ? AND date <= ? ORDER BY date",
        (code, start, end)).fetchall()


def _t1_change(hist: sqlite3.Connection, code: str, order_date: str):
    """The last session's move before the order was placed, in percent."""
    rows = hist.execute(
        "SELECT close FROM daily_kline WHERE code = ? AND date < ? "
        "ORDER BY date DESC LIMIT 2", (code, order_date)).fetchall()
    if len(rows) < 2 or not rows[1][0]:
        return None
    return round((rows[0][0] / rows[1][0] - 1) * 100, 2)


def facts(pos: dict, hist: sqlite3.Connection) -> dict | None:
    """The trade in numbers, from bars dated no later than its close.

    ``peak_session`` counts trading sessions from the buy: 0 is the day it
    bought. The peak is the session *high*, which the close-priced exit could
    not necessarily have reached — the give-back is therefore an upper bound,
    and the words say so rather than the number pretending otherwise.
    """
    code, open_date, close_date = pos["code"], pos["open_date"], pos["close_date"]
    entry = pos.get("open_price") or 0
    if not (code and open_date and close_date and entry > 0):
        return None
    bars = _bars(hist, code, open_date, close_date)
    # The close day's own bar must be on disk. Live, the review runs at 15:30
    # and the provider fills the day's K-line at 17:30, so a trade closed
    # today has no last bar yet: skipped, and picked up by the next review
    # rather than scored on a window missing its final session.
    if not bars or bars[-1][0] < close_date:
        return None
    peak_i = max(range(len(bars)), key=lambda i: bars[i][2])
    low = min(b[3] for b in bars)
    peak_pct = round((bars[peak_i][2] / entry - 1) * 100, 2)
    got = pos.get("return_pct")
    got = round(float(got), 2) if got is not None else round(
        ((pos.get("close_price") or entry) / entry - 1) * 100, 2)
    return {
        "position_id": pos["id"], "code": code, "name": pos.get("name") or "",
        "theme": pos.get("theme") or "",
        "order_date": pos.get("order_date") or open_date,
        "open_date": open_date, "close_date": close_date,
        "entry": round(entry, 3),
        "exit": round(pos.get("close_price") or 0, 3),
        "sessions": len(bars),
        "t1_change_pct": _t1_change(hist, code, pos.get("order_date") or open_date),
        "peak_pct": peak_pct,
        "peak_date": bars[peak_i][0],
        "peak_session": peak_i,
        "worst_pct": round((low / entry - 1) * 100, 2),
        "return_pct": got,
        "giveback_pp": round(peak_pct - got, 2),
        "buy_reason": (pos.get("reason") or "")[:600],
        "sell_reason": (pos.get("close_reason") or "")[:600],
    }


def facts_line(f: dict) -> str:
    """The trade on one line, numbers only."""
    t1 = f.get("t1_change_pct")
    when = ("买入当天" if f["peak_session"] == 0
            else f"持有第{f['peak_session'] + 1}天")
    return (f"{f['name'] or f['code']} {f['open_date'][5:]}买→{f['close_date'][5:]}卖"
            f"（{f['sessions']}个交易日）："
            + (f"买前一日涨{t1:+.1f}%，" if t1 is not None else "")
            + f"持有期最高{f['peak_pct']:+.1f}%（{when}），最深{f['worst_pct']:+.1f}%，"
            f"到手{f['return_pct']:+.1f}%，吐回{f['giveback_pp']:.1f}个点")


_INSTRUCTIONS = """你是这笔交易的交易员。它刚刚平仓，现在由你自己复盘。

下面的数字由系统从日线算出，是事实（最高价是盘中价，收盘卖出未必卖得到）。
你当时买入和卖出的理由是你自己写的原话。

请像一个职业交易员一样复盘这一笔：
- 这一笔是做对了、做错了，还是只是运气？
- 做对的地方是什么？做错的地方是什么？要具体到当时的判断，并引用上面的数字。
- 下次遇到类似的情况，你具体会怎么做？写成你自己能照着执行的一句话。

不要写空话（"需要更加谨慎"这种等于没写）。这份复盘会在你之后每一次决策前被你自己读到。

如果下面附了你当时在用的交易守则，请如实标出这笔交易**遵守了**哪几条、**违反了**
哪几条（用编号，如 R2）；和这笔无关的不要列。系统会用这些标记统计每条守则的效果。

只输出一个 JSON 对象：
{"verdict": "对/错/运气", "right": "...", "wrong": "...", "next_time": "...",
 "followed": ["R1"], "broke": ["R3"]}"""


def _parse(text: str) -> dict:
    t = (text or "").strip()
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        return dict(NO_WORDS)
    try:
        raw = json.loads(t[start:end + 1])
    except json.JSONDecodeError:
        return dict(NO_WORDS)
    if not isinstance(raw, dict):
        return dict(NO_WORDS)
    out = {k: str(raw.get(k) or "").strip()[:400] for k in _TEXT}
    for k in ("followed", "broke"):
        ids = raw.get(k) if isinstance(raw.get(k), list) else []
        out[k] = [str(i).strip().upper() for i in ids if str(i).strip()][:10]
    return out


async def write_words(f: dict, *, model, trader=None, rules: str = "") -> dict:
    """Ask the trader for its review of one trade. Never raises.

    ``rules`` is the handbook that was in force while it held the trade, so
    it can say which rules it followed and which it broke.
    """
    if model is None:
        return dict(NO_WORDS)
    from agents import Agent, Runner

    instructions = _INSTRUCTIONS
    if trader is not None and getattr(trader, "extra_prompt", ""):
        instructions += f"\n\n## 你是谁\n\n{trader.extra_prompt.strip()}\n"
    message = (f"事实：{facts_line(f)}\n\n"
               f"你当时的买入理由：{f['buy_reason'] or '未记录'}\n\n"
               f"你当时的卖出理由：{f['sell_reason'] or '未记录'}"
               + (f"\n\n你当时在用的交易守则：\n{rules}" if rules else ""))
    try:
        agent = Agent(name="trade_review", instructions=instructions,
                      model=model, tools=[])
        result = await asyncio.wait_for(Runner.run(agent, message, max_turns=2),
                                        timeout=_TIMEOUT)
    except Exception as e:                            # noqa: BLE001
        logger.warning("Trade review for %s failed (%s) — facts only",
                       f.get("code"), e)
        return dict(NO_WORDS)
    return _parse(result.final_output or "")


def unreviewed(conn: sqlite3.Connection, trader_id: str, as_of: str) -> list[dict]:
    """Positions this trader closed on or before ``as_of`` with no review yet."""
    rows = conn.execute(
        "SELECT p.* FROM virtual_portfolio p "
        "LEFT JOIN trade_reviews r ON r.position_id = p.id "
        "WHERE p.trader_id = ? AND p.close_date IS NOT NULL "
        "AND p.close_date <= ? AND p.open_price > 0 AND r.id IS NULL "
        "ORDER BY p.close_date, p.id", (trader_id, as_of)).fetchall()
    return [dict(r) for r in rows]


def save(conn: sqlite3.Connection, f: dict, words: dict, trader_id: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO trade_reviews "
        "(position_id, trader_id, code, close_date, facts_json, lesson_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (f["position_id"], trader_id, f["code"], f["close_date"],
         json.dumps(f, ensure_ascii=False), json.dumps(words, ensure_ascii=False)))


async def review_closed(conn: sqlite3.Connection, hist: sqlite3.Connection, *,
                        trader_id: str, as_of: str, model, trader=None,
                        handbook_before: str | None = None) -> int:
    """Review every closed, unreviewed position of this trader. Returns count.

    ``hist`` must hold no bar later than ``as_of`` that :func:`facts` would
    read; facts reads only up to each position's own close date, which is on
    or before ``as_of`` by construction.
    """
    from alpha_agents.evolution import handbook
    rules = handbook.load(trader_id, before=handbook_before)
    n = 0
    for pos in unreviewed(conn, trader_id, as_of):
        f = facts(pos, hist)
        if f is None:
            continue
        words = await write_words(f, model=model, trader=trader, rules=rules)
        save(conn, f, words, trader_id)
        conn.commit()
        n += 1
        logger.info("Trade review [%s] %s: %s | 下次: %s", trader_id,
                    f["code"], facts_line(f), words.get("next_time") or "—")
    return n


def reviews_for(conn: sqlite3.Connection, trader_id: str, *,
                up_to: str) -> list[tuple[dict, dict]]:
    """Every review of trades closed on or before ``up_to``, oldest first."""
    rows = _rows(conn, trader_id, None)
    return [r for r in reversed(rows) if r[0]["close_date"] <= up_to]


def _rows(conn: sqlite3.Connection, trader_id: str, before: str | None):
    sql = ("SELECT facts_json, lesson_json FROM trade_reviews WHERE trader_id = ?"
           + (" AND close_date < ?" if before else "")
           + " ORDER BY close_date DESC, id DESC")
    args = (trader_id, before) if before else (trader_id,)
    out = []
    for fj, lj in conn.execute(sql, args).fetchall():
        try:
            out.append((json.loads(fj), json.loads(lj or "{}")))
        except json.JSONDecodeError:
            continue
    return out


def summary_line(facts_list: list[dict]) -> str:
    """Across the trades: how far they ran, what was kept, what was handed back."""
    if not facts_list:
        return ""
    n = len(facts_list)
    med = lambda k: statistics.median(f[k] for f in facts_list)   # noqa: E731
    top_day = sum(1 for f in facts_list if f["peak_session"] == 0)
    lost_after_up = sum(1 for f in facts_list
                        if f["peak_pct"] > 0 and f["return_pct"] < 0)
    return (f"你已平仓 {n} 笔（系统按日线算，中位数）：持有期最高 {med('peak_pct'):+.1f}%，"
            f"到手 {med('return_pct'):+.1f}%，吐回 {med('giveback_pp'):.1f} 个点；"
            f"{top_day}/{n} 笔的最高点就在买入当天；"
            f"{lost_after_up}/{n} 笔曾经浮盈、最后亏着卖出。")


def inject(conn: sqlite3.Connection, trader_id: str, *,
           before: str | None = None, limit: int = RECENT) -> str:
    """The block a decision reads: the summary, then the latest reviews.

    ``before`` excludes reviews of trades closed on or after that date, so a
    replay's morning never reads a review written at a later close.
    """
    from alpha_agents.evolution import handbook
    rules = handbook.load(trader_id, before=before)
    rows = _rows(conn, trader_id, before)
    if not rows and not rules:
        return ""
    lines = []
    if rules:
        lines += [rules, ""]
    if not rows:
        return "\n".join(lines).strip()
    lines += ["【你自己的逐笔复盘】", summary_line([f for f, _ in rows]), ""]
    for f, w in rows[:limit]:
        lines.append("● " + facts_line(f))
        if w.get("verdict") or w.get("wrong") or w.get("next_time"):
            if w.get("verdict"):
                lines.append(f"  你的判断：{w['verdict']}")
            if w.get("right"):
                lines.append(f"  做对：{w['right']}")
            if w.get("wrong"):
                lines.append(f"  做错：{w['wrong']}")
            if w.get("next_time"):
                lines.append(f"  下次：{w['next_time']}")
    return "\n".join(lines)
