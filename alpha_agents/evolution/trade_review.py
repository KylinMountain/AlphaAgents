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

import json
import logging
import math
import sqlite3
import statistics
from datetime import date, timedelta

from alpha_agents.data import trade_review_store as store

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
    """Use actual fills and interior sessions when boundary-day phases are unknown.

    Whole-span intraday potential is diagnostic, never an achievable exit.
    """
    code, open_date, close_date = pos["code"], pos["open_date"], pos["close_date"]
    entry = pos.get("open_price") or 0
    exit_price = pos.get("close_price") or 0
    if not (code and open_date and close_date and entry > 0 and exit_price > 0
            and math.isfinite(entry) and math.isfinite(exit_price)):
        return None
    bars = _bars(hist, code, open_date, close_date)
    # The close day's own bar must be on disk. Live, the review runs at 15:30
    # and the provider fills the day's K-line at 17:30, so a trade closed
    # today has no last bar yet: skipped, and picked up by the next review
    # rather than scored on a window missing its final session.
    if not bars or bars[-1][0] < close_date:
        return None
    if bars[0][0] != open_date:
        return None
    observed = [(open_date, entry, entry, "entry_fill"),
                *[(b[0], b[2], b[3], "interior_daily_bar") for b in bars
                  if open_date < b[0] < close_date],
                (close_date, exit_price, exit_price, "exit_fill")]
    peak = max(observed, key=lambda b: b[1])
    peak_i = next(i for i, b in enumerate(bars) if b[0] == peak[0])
    low = min(b[2] for b in observed)
    peak_pct = round((peak[1] / entry - 1) * 100, 2)
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
        "peak_date": peak[0], "peak_source": peak[3],
        "extrema_scope": "interior_sessions_and_fills",
        "boundary_extrema_excluded": True,
        "potential_intraday_high_pct": round((max(b[2] for b in bars) / entry - 1) * 100, 2),
        "achievable_exit": None,
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
            + f"观察峰值{f['peak_pct']:+.1f}%（{when}），观察低点{f['worst_pct']:+.1f}%，"
            f"到手{f['return_pct']:+.1f}%，峰值差{f['giveback_pp']:.1f}个点（非可达利润）"
            + ("；已排除买卖当日未知时序高低点，可执行退出未知"
               if f.get("boundary_extrema_excluded") else
               "；旧数据为整日范围上界，不能认定为真实持有期极值"))


_INSTRUCTIONS = """你是这笔交易的交易员。它刚刚平仓，现在由你自己复盘。

观察峰值不等于可成交退出价，峰值差不是本应赚到的利润。
将决策质量与结果质量分开：先根据当时记录判断，再评论盈亏。
未保存的当时信息就写信息不足，不要补写动机或假定可卖在高点。
T+1、停牌、涨跌停及成交时点限制下不可执行的动作，不能被写成改进经验。
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


async def write_words(f: dict, *, model, trader=None, rules: str = "",
                      timeout: float | None = None) -> dict:
    """Ask for an interpretation; only temporal-integrity faults propagate.

    ``rules`` is the handbook that was in force while it held the trade, so
    it can say which rules it followed and which it broke.
    """
    if model is None:
        return dict(NO_WORDS)
    from agents import Agent

    from alpha_agents.model_factory import run_agent

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
        result = await run_agent(agent, message, max_turns=2,
                                 timeout=timeout if timeout is not None else _TIMEOUT,
                                  label="trade_review")
    except Exception as e:                            # noqa: BLE001
        logger.warning("Trade review for %s failed (%s) — facts only",
                       f.get("code"), e)
        from alpha_agents.data.clock import LookAheadError
        if isinstance(e, LookAheadError):
            raise
        return {**NO_WORDS, "_error": f"{type(e).__name__}: {e}"}
    return _parse(result.final_output or "")


def unreviewed(conn: sqlite3.Connection, trader_id: str, as_of: str) -> list[dict]:
    """Missing explanations, not just missing rows, remain pending work."""
    rows = conn.execute(
        "SELECT p.* FROM virtual_portfolio p LEFT JOIN trade_reviews r ON r.position_id=p.id "
        "WHERE p.trader_id=? AND p.close_date IS NOT NULL AND p.close_date<=? "
        "AND p.open_price>0 AND (r.id IS NULL OR r.review_status<>'complete') "
        "ORDER BY p.close_date,p.id", (trader_id, as_of)).fetchall()
    return [dict(row) for row in rows]


def save(conn: sqlite3.Connection, f: dict, words: dict, trader_id: str, *,
         as_of: str | None = None) -> None:
    """Import a dated record; default visibility is now, not the old close date."""
    from alpha_agents.data import clock
    available = as_of or clock.today()
    if store.save_facts(conn, f, trader_id, available) and store.complete(words):
        conn.execute("UPDATE trade_reviews SET lesson_json=?,review_status='complete',"
                     "review_available_on=? WHERE position_id=?", (
                         json.dumps(words, ensure_ascii=False), available, f["position_id"]))


async def review_closed(conn: sqlite3.Connection, hist: sqlite3.Connection, *,
                        trader_id: str, as_of: str, model, trader=None,
                        handbook_before: str | None = None,
                        stats: dict | None = None,
                        timeout: float | None = None) -> int:
    """Persist facts first. Return COMPLETED interpretations, not processed rows.

    Availability is conservative at day granularity. The claim is committed
    before asking; after a crash it can be retried on a later processing day.
    """
    from alpha_agents.evolution import handbook
    from alpha_agents.data import trader_session
    processing_day = max(as_of, trader_session.instant()[:10])
    counts = {"trade_review_facts": 0, "trade_review_attempted": 0,
              "trade_review_failed": 0, "trade_review_waiting_data": 0}
    n = 0
    try:
        for pos in unreviewed(conn, trader_id, as_of):
            row = conn.execute("SELECT facts_json FROM trade_reviews WHERE position_id=?",
                               (pos["id"],)).fetchone()
            f = json.loads(row[0]) if row else facts(pos, hist)
            if f is None:
                counts["trade_review_waiting_data"] += 1
                continue
            counts["trade_review_facts"] += int(store.save_facts(conn, f, trader_id, processing_day))
            conn.commit()
            if model is None:
                continue
            attempt = store.claim(conn, pos["id"], processing_day)
            conn.commit()
            if attempt is None:
                continue
            counts["trade_review_attempted"] += 1
            before = f.get("order_date") or f["open_date"]
            if handbook_before and handbook_before < before:
                before = handbook_before
            rules = handbook.load(trader_id, before=before)
            versions = handbook.bindings(trader_id, before=before)
            word_options = {"model": model, "trader": trader, "rules": rules}
            if timeout is not None:
                word_options["timeout"] = timeout
            words = dict(await write_words(f, **word_options))
            words["rule_versions"] = versions
            for field in ("followed", "broke"):
                words[field] = [rid for rid in words.get(field) or [] if rid in versions]
            done = store.finish(conn, pos["id"], attempt, words, processing_day,
                                words.get("_error") or ("" if store.complete(words) else "incomplete_reply"),
                                completed_on=trader_session.instant()[:10])
            conn.commit()
            n += int(done)
            counts["trade_review_failed"] += int(not done)
            logger.info("Trade review [%s] %s attempt=%d complete=%s", trader_id,
                        f["code"], attempt, done)
    finally:
        counts.update(store.pending_counts(conn, trader_id, as_of))
        counts["trade_review_pending"] += counts["trade_review_waiting_data"]
        if stats is not None:
            stats.update(counts)
    return n


def reviews_for(conn: sqlite3.Connection, trader_id: str, *,
                up_to: str) -> list[tuple[dict, dict]]:
    """Only facts and explanations available by this processing day."""
    before = (date.fromisoformat(up_to) + timedelta(days=1)).isoformat()
    return list(reversed(_rows(conn, trader_id, before)))


def _rows(conn: sqlite3.Connection, trader_id: str, before: str | None):
    before = before[:10] if before else None
    sql = ("SELECT facts_json,lesson_json,review_status,review_available_on "
           "FROM trade_reviews WHERE trader_id=?"
           + (" AND close_date<? AND facts_available_on<?" if before else "")
           + " ORDER BY close_date DESC,id DESC")
    args = (trader_id, before, before) if before else (trader_id,)
    out = []
    for fj, lj, status, available in conn.execute(sql, args):
        try:
            visible = status == "complete" and available and (before is None or available < before)
            out.append((json.loads(fj), json.loads(lj or "{}") if visible else dict(NO_WORDS)))
        except json.JSONDecodeError as exc:
            logger.warning("Stored review unreadable for %s: %s", trader_id, exc)
    return out


def summary_line(facts_list: list[dict]) -> str:
    """Do not mix legacy whole-day upper bounds with observed held-period extrema."""
    if not facts_list:
        return ""
    n = len(facts_list)
    realized = statistics.median(f["return_pct"] for f in facts_list)
    observed = [f for f in facts_list if f.get("boundary_extrema_excluded")]
    text = f"你已平仓 {n} 笔（系统计算）：到手中位 {realized:+.1f}%。"
    if observed:
        peak = statistics.median(f["peak_pct"] for f in observed)
        gap = statistics.median(f["giveback_pp"] for f in observed)
        text += (f"其中 {len(observed)} 笔已排除买卖日未知时序极值："
                 f"观察峰值中位 {peak:+.1f}%，峰值差中位 {gap:.1f} 个点（非可达利润）。")
    if len(observed) != n:
        text += (f"{n-len(observed)} 笔旧记录仅有整日上界，"
                 "不纳入持有期极值统计，不据此判断错过利润。")
    return text


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
        if not any(w.get(k) for k in _TEXT):
            lines.append("  仅事实：复盘解释尚未完成或在该时点不可见。")
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
