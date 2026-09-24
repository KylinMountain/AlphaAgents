"""The trader's end-of-day review of its own day against the market's.

At the close every number of the session is known, and so is what the
trader did with it: the directions it chose at the open and why, the ones it
saw and passed on, its orders, its fills, what it held. The review sets the
two side by side, board by board:

* 错过 — a board that rose without us: why did the morning not buy it
  (never saw it / saw it and passed / bought too little)?
* 踩坑 — something we bought or held that fell: what did we believe, what
  was wrong?
* 转向 — a direction we were in turned: did we get out, keep holding, add?
* 做对 — what worked, so it is not unlearned.

It is a diagnosis of **today's decisions**, not tomorrow's shortlist. The
2026-01 replays that carried the review's boards forward as candidates
chose worse directions for it (percentile 43.8 → 34.6; the named boards
ran −1.4% over five sessions against +0.2% for the rest): the boards a
review talks about are the day's biggest movers, and buying them the next
morning is chasing. The lessons reach the morning through the handbook and
through yesterday's review, labelled as yesterday's.

The facts and the day's record are assembled by the caller and handed in
as text, so no tool call can run the review past its timeout. The model is
passed in, as everywhere in this layer.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3

logger = logging.getLogger(__name__)

#: Measured 2026-09-24 on the live reasoning model: 180 s timed out on a
#: ~10k-character input for both traders, with an empty message (a timeout's
#: str() is ""), so the log said "failed ()". The close review task allows
#: 1800 s in all.
_TIMEOUT = 420

#: What a board is to the trader today.
KINDS = ("错过", "踩坑", "转向", "做对")

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


_INSTRUCTIONS = """你是这个账户的交易员。现在是收盘后。下面有两份材料：今天全天的盘面数据，和你自己今天的记录
（早盘选了哪些方向、当时的理由、看到但没选的、下了哪些单、成交没有、持仓今天怎么走、今天卖了什么）。都由系统整理。

复盘的对象是**你今天的决策**，不是给明天选票。请像职业短线交易员一样写：
1. market：今天的盘面是什么样的？赚钱效应、情绪（涨停/跌停/炸板率/连板高度）、资金主攻方向，引用数据。
2. themes：哪些主线是真的（资金+涨停+龙头都在），哪些只是一日游？为什么？
3. boards：逐个诊断，每个板块归到一类（kind）：
   - 错过：涨得好、你没参与或参与太少。对照你早盘的记录：是根本没看到（发现环节缺了什么），
     还是看到了没选（当时的理由是什么，事后看错在哪），还是选了但没买到/买少了（价格、仓位）？
   - 踩坑：你买了或持有的、跌了的。你当时相信什么（引用早盘的理由），哪一条被证伪了，早盘能不能看出来？
   - 转向：你在里面的方向今天变了（资金转出、涨停缩、龙头断板）。你撤了、没撤、还是在加仓？该怎么做？
   - 做对：做对的，说清是哪个判断对了，免得下次丢掉。
   每个板块写：
   - driver：驱动——消息催化 / 利好落地（兑现） / 情绪（涨停、连板带动） / 资金（主力持续流入） / 超跌反弹 / 其他，
     可以多选但要分主次。判断消息时看快讯原文和「消息时间线」：今天才出现的是新催化；传了好几天、今天正式发布或兑现的，
     要考虑是不是利好落地（见光死），两者含义相反
   - evidence：支持这个判断的数据（快讯、涨停数、连板高度、主力净额、上涨占比）
   - morning：你早盘对它是怎么想、怎么做的（从你的记录里引用；记录里没有就写「没看到」）
   - verdict：事后看，早盘那个决定对不对，错在哪
   - lesson：一句你下次早盘或持仓时**能执行**的做法。你每天开盘前决定买什么、最多付多少、止损和失效条件，
     按开盘价成交；卖出在开盘和收盘两个时点决定。你看不到盘中分时和封单，写不了「盘中盯着」
别只想着少亏：大盘涨而你不在场，也是亏。如果附了你的仓位与大盘的对比和你的交易守则，参考它们。目标只有一个：盈利。

只输出一个 JSON 对象：
{"market": "...", "themes": "...",
 "boards": [{"name": "...", "kind": "错过", "driver": "...", "evidence": "...",
             "morning": "...", "verdict": "...", "lesson": "..."}]}"""


def board_name(raw: str) -> str:
    """The board's name as the concept table spells it.

    The model echoes the facts line back — "脑机接口 +10.67%", "PCB概念（+1.4%）"
    — and a name with its move attached matches no concept, so a board the
    review named could not join the next morning's shortlist.
    """
    # Only a signed-or-bare number ending in % is a move; "中国AI 50" is a
    # concept's own name and keeps its number.
    move = r"[+\-−]?\d+(?:\.\d+)?\s*%"
    name = re.sub(rf"\s*[（(]\s*{move}\s*[)）]\s*$", "", raw.strip())
    name = re.sub(rf"\s+{move}.*$", "", name)
    return name.strip()


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
    boards = []
    for b in raw.get("boards") or []:
        if isinstance(b, dict) and str(b.get("name") or "").strip():
            row = {k: str(b.get(k) or "").strip()[:300] for k in
                   ("name", "kind", "driver", "evidence", "morning", "verdict",
                    "lesson")}
            row["name"] = board_name(row["name"])
            if row["kind"] not in KINDS:
                row["kind"] = ""
            boards.append(row)
    return {"market": str(raw.get("market") or "").strip()[:800],
            "themes": str(raw.get("themes") or "").strip()[:800],
            "boards": boards[:20]}


async def write(conn: sqlite3.Connection, *, trader_id: str, date: str, facts: str,
                model, record: str = "", context: str = "") -> dict | None:
    """Ask the trader to review its day and store it. Never raises."""
    ensure(conn)
    if model is None or not facts.strip():
        return None
    from agents import Agent

    from alpha_agents.model_factory import run_agent
    message = (f"## 今日盘面（{date}）\n\n{facts}"
               + (f"\n\n## 你今天的记录\n\n{record}" if record else "")
               + (f"\n\n{context}" if context else ""))
    try:
        agent = Agent(name="market_review", instructions=_INSTRUCTIONS,
                      model=model, tools=[])
        result = await run_agent(agent, message, max_turns=2, timeout=_TIMEOUT,
                                  label="market_review")
    except Exception as e:                            # noqa: BLE001
        logger.warning("Market review for %s failed (%s: %s)", trader_id,
                       type(e).__name__, e)
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
    logger.info("Market review [%s] %s: %d boards", trader_id, date,
                len(review["boards"]))
    return review


def _lesson(b: dict) -> str:
    # Reviews written before 2026-09-24 carry why_missed/next_time.
    return b.get("lesson") or b.get("next_time") or ""


def _diagnosis(b: dict) -> str:
    return b.get("verdict") or b.get("why_missed") or ""


def inject(conn: sqlite3.Connection, trader_id: str, *,
           before: str | None = None) -> str:
    """Yesterday's review, labelled as yesterday's diagnosis."""
    ensure(conn)
    sql = ("SELECT date, review_json FROM market_reviews WHERE trader_id = ?"
           + (" AND date < ?" if before else "") + " ORDER BY date DESC LIMIT 1")
    args = (trader_id, before) if before else (trader_id,)
    row = conn.execute(sql, args).fetchone()
    if not row:
        return ""
    day, rj = row[0], json.loads(row[1])
    lines = [f"【昨日复盘（{day} 收盘后你自己写的）】",
             "这是你对昨天自己决策的诊断：哪里错过、哪里踩坑、哪里没跟上转向。"
             "它不是今天的买入名单——昨天涨得最好的板块，今天未必还该买；"
             "今天买什么，看今天的数据。"]
    if rj.get("market"):
        lines.append(f"昨日盘面：{rj['market']}")
    if rj.get("themes"):
        lines.append(f"昨日主线：{rj['themes']}")
    for b in rj.get("boards") or []:
        kind = f"[{b['kind']}] " if b.get("kind") else ""
        lines.append(f"◆ {kind}{b['name']}｜驱动：{b.get('driver', '')}｜"
                     f"诊断：{_diagnosis(b)}｜教训：{_lesson(b)}")
    return "\n".join(lines)


def missed(conn: sqlite3.Connection, trader_id: str, *, up_to: str,
           days: int = 5) -> str:
    """The recent reviews' diagnoses, in the trader's own words.

    Read by the handbook rewrite, so a rule that keeps it out of the market
    is weighed against what being out cost, and one that kept it in a
    turning direction against what that cost.
    """
    ensure(conn)
    rows = conn.execute(
        "SELECT date, review_json FROM market_reviews WHERE trader_id = ? "
        "AND date <= ? ORDER BY date DESC LIMIT ?", (trader_id, up_to, days)
    ).fetchall()
    lines = []
    for day, rj in reversed(rows):
        try:
            boards = json.loads(rj).get("boards") or []
        except json.JSONDecodeError:
            continue
        for b in boards:
            if _diagnosis(b) or _lesson(b):
                kind = f"[{b['kind']}]" if b.get("kind") else ""
                lines.append(f"{day} {kind}{b['name']}（{b.get('driver', '')}）："
                             f"{_diagnosis(b)}｜下次：{_lesson(b)}")
    return "\n".join(lines)
