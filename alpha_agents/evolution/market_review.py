"""The trader's end-of-day read of the market, and a watchlist the market grades.

At the close every number of the session is known. The trader reads them —
breadth, where the money went by concept, the limit-up ladder — and writes
what the day was, which lines it believes, and at most :data:`MAX_WATCH`
names it would consider tomorrow, each with why, what would make it buy and
what would make it drop the idea. The next morning it reads that back.

The watchlist is the half of this that can be wrong, so it is graded: for
every name, the next session's and the next three sessions' return against
the market median of the same days, from ``daily_kline``. The grade is the
code's (GOLDEN_PRINCIPLES #2); the trader sees it the next time it writes a
watchlist, so "the kind of name I like at the close" becomes something it
can learn about without waiting for a fill.

The facts are assembled by the caller — live from the day's snapshots — and
handed in as text, so no tool call can run the review past its timeout. The
model is passed in, as everywhere in this layer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import statistics

logger = logging.getLogger(__name__)

MAX_WATCH = 5
_TIMEOUT = 180

_SCHEMA = """
CREATE TABLE IF NOT EXISTS market_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trader_id TEXT NOT NULL,
    date TEXT NOT NULL,
    facts TEXT NOT NULL,
    review_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (trader_id, date)
)"""


def ensure(conn: sqlite3.Connection) -> None:
    conn.execute(_SCHEMA)


_INSTRUCTIONS = f"""你是这个账户的交易员。现在是收盘后，今天全天的数据你都能看到（下面的数据由系统整理）。

请写今天的盘面复盘：
1. market：今天的盘面是什么样的？赚钱效应、情绪、资金主攻方向，两三句话，引用数据。
2. themes：哪些主线是真的（资金+涨停+龙头都在），哪些只是一日游？为什么？
3. watchlist：明天你会考虑的票，最多 {MAX_WATCH} 只（没有就空着，宁缺毋滥）。每只写：
   - why：为什么看好，引用今天的数据
   - buy_if：明天出现什么情况你才会买（具体到价格或盘面条件）
   - drop_if：出现什么情况你就放弃
如果附了你过去观察名单的成绩和你的交易守则，参考它们。目标只有一个：盈利。

只输出一个 JSON 对象：
{{"market": "...", "themes": "...", "watchlist": [{{"code": "600000", "name": "...", "why": "...", "buy_if": "...", "drop_if": "..."}}]}}"""


def _parse(text: str) -> dict | None:
    t = (text or "").strip()
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        raw = json.loads(t[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    watch = []
    for w in raw.get("watchlist") or []:
        if len(watch) == MAX_WATCH:
            break
        if not isinstance(w, dict):
            continue
        code = str(w.get("code") or "").strip()
        if not (len(code) == 6 and code.isdigit()):
            continue
        watch.append({k: str(w.get(k) or "").strip()[:300]
                      for k in ("name", "why", "buy_if", "drop_if")} | {"code": code})
    return {"market": str(raw.get("market") or "").strip()[:800],
            "themes": str(raw.get("themes") or "").strip()[:800],
            "watchlist": watch}


async def write(conn: sqlite3.Connection, *, trader_id: str, date: str, facts: str,
                model, context: str = "") -> dict | None:
    """Ask the trader for today's read and store it. Never raises."""
    ensure(conn)
    if model is None or not facts.strip():
        return None
    from agents import Agent, Runner
    message = f"## 今日数据（{date}）\n\n{facts}" + (f"\n\n{context}" if context else "")
    try:
        agent = Agent(name="market_review", instructions=_INSTRUCTIONS,
                      model=model, tools=[])
        result = await asyncio.wait_for(Runner.run(agent, message, max_turns=2),
                                        timeout=_TIMEOUT)
    except Exception as e:                            # noqa: BLE001
        logger.warning("Market review for %s failed: %s", trader_id, e)
        return None
    review = _parse(result.final_output or "")
    if review is None:
        logger.warning("Market review for %s unreadable", trader_id)
        return None
    conn.execute(
        "INSERT OR REPLACE INTO market_reviews (trader_id, date, facts, review_json) "
        "VALUES (?, ?, ?, ?)",
        (trader_id, date, facts, json.dumps(review, ensure_ascii=False)))
    conn.commit()
    logger.info("Market review [%s] %s: %d watch names", trader_id, date,
                len(review["watchlist"]))
    return review


# ── grading ────────────────────────────────────────────────────

def _sessions_after(hist: sqlite3.Connection, day: str, n: int,
                    up_to: str | None) -> list[str]:
    sql = ("SELECT DISTINCT date FROM daily_kline WHERE date > ?"
           + (" AND date < ?" if up_to else "") + " ORDER BY date LIMIT ?")
    args = (day, up_to, n) if up_to else (day, n)
    return [r[0] for r in hist.execute(sql, args).fetchall()]


def _close(hist, code, day):
    r = hist.execute("SELECT close FROM daily_kline WHERE code=? AND date=?",
                     (code, day)).fetchone()
    return r[0] if r and r[0] else None


def _market_median(hist, d0: str, d1: str) -> float | None:
    rows = hist.execute(
        "SELECT a.close, b.close FROM daily_kline a JOIN daily_kline b "
        "ON a.code = b.code WHERE a.date = ? AND b.date = ? AND a.close > 0",
        (d0, d1)).fetchall()
    moves = [(b / a - 1) * 100 for a, b in rows if b]
    return statistics.median(moves) if moves else None


def grade(conn: sqlite3.Connection, hist: sqlite3.Connection, trader_id: str, *,
          before: str | None = None) -> list[dict]:
    """Every watch name with its 1- and 3-session return and excess.

    Only sessions strictly before ``before`` are used, so a replay morning
    grades yesterday's list on nothing it has not seen. A name whose window
    is not complete yet is left out rather than graded on part of it.
    """
    ensure(conn)
    sql = ("SELECT date, review_json FROM market_reviews WHERE trader_id = ?"
           + (" AND date < ?" if before else "") + " ORDER BY date")
    args = (trader_id, before) if before else (trader_id,)
    out = []
    for day, rj in conn.execute(sql, args).fetchall():
        try:
            watch = json.loads(rj).get("watchlist") or []
        except json.JSONDecodeError:
            continue
        nxt = _sessions_after(hist, day, 3, before)
        for w in watch:
            base = _close(hist, w["code"], day)
            if not base:
                continue
            row = {"date": day, "code": w["code"], "name": w.get("name", "")}
            for k, n in (("d1", 1), ("d3", 3)):
                if len(nxt) < n:
                    continue
                c = _close(hist, w["code"], nxt[n - 1])
                mkt = _market_median(hist, day, nxt[n - 1])
                if c and mkt is not None:
                    row[k] = round((c / base - 1) * 100, 2)
                    row[f"{k}_excess"] = round(row[k] - mkt, 2)
            if "d1" in row:
                out.append(row)
    return out


def grade_line(graded: list[dict]) -> str:
    if not graded:
        return ""
    d1 = [g["d1_excess"] for g in graded]
    line = (f"你过去的观察名单（系统按日线算）：{len(graded)} 只，次日超额中位 "
            f"{statistics.median(d1):+.1f}%，次日跑赢大盘 {sum(x > 0 for x in d1)}/{len(d1)}")
    d3 = [g["d3_excess"] for g in graded if "d3_excess" in g]
    if d3:
        line += (f"；3 日超额中位 {statistics.median(d3):+.1f}%，"
                 f"跑赢 {sum(x > 0 for x in d3)}/{len(d3)}")
    return line + "。"


def inject(conn: sqlite3.Connection, hist: sqlite3.Connection | None,
           trader_id: str, *, before: str | None = None) -> str:
    """Yesterday's read and watchlist, with the grade of every earlier list."""
    ensure(conn)
    sql = ("SELECT date, review_json FROM market_reviews WHERE trader_id = ?"
           + (" AND date < ?" if before else "") + " ORDER BY date DESC LIMIT 1")
    args = (trader_id, before) if before else (trader_id,)
    row = conn.execute(sql, args).fetchone()
    if not row:
        return ""
    day, rj = row[0], json.loads(row[1])
    lines = [f"【你昨天收盘后写的盘面复盘（{day}）】"]
    if rj.get("market"):
        lines.append(f"盘面：{rj['market']}")
    if rj.get("themes"):
        lines.append(f"主线：{rj['themes']}")
    for w in rj.get("watchlist") or []:
        lines.append(f"● 观察 {w['code']} {w.get('name', '')}：{w.get('why', '')}"
                     f"｜买入条件：{w.get('buy_if', '')}｜放弃条件：{w.get('drop_if', '')}")
    if hist is not None:
        g = grade_line(grade(conn, hist, trader_id, before=before))
        if g:
            lines.append(g)
    return "\n".join(lines)
