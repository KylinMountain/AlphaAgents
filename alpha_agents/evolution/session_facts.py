"""One session as a trader reads it at the close, computed from daily bars.

The replay had no limit-up pool, no ladder and no sentiment state — 0 of 30
days in both 2026-01 runs — so "was it emotion or money" could not even be
asked. Everything here is derived from ``daily_kline`` instead, which a
replay has for every day and live has once the day's bars land:

* a limit-up is a close at the day's limit price (10% main board, 20% for
  ChiNext/STAR, 30% for Beijing), a failed one (炸板) a high at the limit
  with a close below it, and a streak (连板) consecutive limit-up closes;
* a concept's move is the equal-weight mean of its members' moves;
* its money is the members' main-force net amount (``stock_fund_flow_daily``);
* its news is the number of the session's flashes that name it or its leader.

Each of the day's strongest and weakest concepts is then classified against
what the trader did that day — selected it, saw it and passed, or never had
it in front of it — so "why did we miss it" has a factual first half before
the trader writes the second.

No model is involved; the evidence is the code's (GOLDEN_PRINCIPLES #2).
"""

from __future__ import annotations

import sqlite3
import statistics

TOP = 8
BOTTOM = 5
MIN_MEMBERS = 10

#: What the trader did with a concept that day, in the order that explains
#: a miss: never saw it (discovery), saw it and passed (judgement), took it.
SELECTED, SEEN, UNSEEN = "选中", "看到没选", "没看到"


def _limit_pct(code: str) -> float:
    if code.startswith(("30", "68")):
        return 0.20
    if code.startswith(("8", "4", "92")):
        return 0.30
    return 0.10


def _limit_price(prev: float, code: str) -> float:
    return round(prev * (1 + _limit_pct(code)) + 1e-9, 2)


def _bars(hist: sqlite3.Connection, day: str) -> tuple[str | None, dict]:
    """(previous session, {code: (prev_close, high, close)}) for ``day``."""
    row = hist.execute("SELECT MAX(date) FROM daily_kline WHERE date < ?",
                       (day,)).fetchone()
    prev = row[0] if row else None
    if not prev:
        return None, {}
    rows = hist.execute(
        "SELECT a.code, a.close, b.high, b.close FROM daily_kline a "
        "JOIN daily_kline b ON a.code = b.code "
        "WHERE a.date = ? AND b.date = ? AND a.close > 0 AND b.close > 0",
        (prev, day)).fetchall()
    return prev, {c: (p, h, x) for c, p, h, x in rows}


def _streak(hist: sqlite3.Connection, code: str, day: str) -> int:
    """Consecutive limit-up closes ending on ``day``."""
    rows = hist.execute(
        "SELECT close FROM daily_kline WHERE code = ? AND date <= ? "
        "ORDER BY date DESC LIMIT 12", (code, day)).fetchall()
    closes = [r[0] for r in rows]
    n = 0
    for i in range(len(closes) - 1):
        if closes[i] >= _limit_price(closes[i + 1], code) - 0.005:
            n += 1
        else:
            break
    return n


def _flows(flows: sqlite3.Connection | None, day: str) -> dict[str, float]:
    if flows is None:
        return {}
    try:
        rows = flows.execute(
            "SELECT code, net_amount FROM stock_fund_flow_daily WHERE trade_date = ?",
            (day.replace("-", ""),)).fetchall()
    except sqlite3.Error:
        return {}
    return {c: v for c, v in rows if v is not None}


def compute(hist: sqlite3.Connection, *, day: str,
            members: dict[str, list[str]], names: dict[str, str] | None = None,
            flows: sqlite3.Connection | None = None,
            news_titles: list[str] | None = None,
            ours: dict[str, str] | None = None) -> dict:
    """The session's facts. ``ours`` maps a concept to SELECTED or SEEN;
    every concept not in it is UNSEEN."""
    names = names or {}
    news_titles = news_titles or []
    ours = ours or {}
    prev, bars = _bars(hist, day)
    if not bars:
        return {}
    moves = {c: (x / p - 1) * 100 for c, (p, h, x) in bars.items()}
    limit_up = {c for c, (p, h, x) in bars.items()
                if x >= _limit_price(p, c) - 0.005}
    broken = {c for c, (p, h, x) in bars.items()
              if h >= _limit_price(p, c) - 0.005 and c not in limit_up}
    limit_down = {c for c, (p, h, x) in bars.items()
                  if x <= round(p * (1 - _limit_pct(c)) + 1e-9, 2) + 0.005}
    streaks = {c: _streak(hist, c, day) for c in limit_up}
    net = _flows(flows, day)

    boards = []
    for concept, codes in members.items():
        have = [c for c in codes if c in moves]
        if len(have) < MIN_MEMBERS:
            continue
        leader = max(have, key=lambda c: moves[c])
        lead_name = names.get(leader, leader)
        short = concept.replace("概念", "")
        boards.append({
            "name": concept,
            "pct": round(statistics.mean(moves[c] for c in have), 2),
            "up_ratio": round(sum(moves[c] > 0 for c in have) / len(have) * 100),
            "limit_up": sum(c in limit_up for c in have),
            "max_streak": max([streaks.get(c, 0) for c in have] or [0]),
            "net_yi": round(sum(net.get(c, 0) for c in have) / 1e4, 1),
            "leader": lead_name, "leader_pct": round(moves[leader], 1),
            "news": sum(1 for t in news_titles
                        if (len(short) >= 2 and short in t) or lead_name in t),
            "ours": ours.get(concept, UNSEEN),
        })
    boards.sort(key=lambda b: -b["pct"])
    ladder = sorted(((streaks[c], c) for c in limit_up), reverse=True)[:8]
    return {
        "day": day, "prev": prev,
        "market": {
            "n": len(moves),
            "up": sum(v > 0 for v in moves.values()),
            "down": sum(v < 0 for v in moves.values()),
            "median_pct": round(statistics.median(moves.values()), 2),
            "limit_up": len(limit_up), "limit_down": len(limit_down),
            "broken": len(broken),
            "broken_rate": (round(len(broken) / (len(broken) + len(limit_up)) * 100)
                            if (broken or limit_up) else 0),
            "max_streak": ladder[0][0] if ladder else 0,
            "ladder": [(names.get(c, c), c, s) for s, c in ladder],
        },
        "top": boards[:TOP],
        "bottom": boards[-BOTTOM:][::-1] if len(boards) > TOP else [],
    }


def _board_line(b: dict) -> str:
    return (f"{b['name']} {b['pct']:+.2f}%（上涨占比{b['up_ratio']}%，涨停{b['limit_up']}只，"
            f"最高{b['max_streak']}连板，主力净额{b['net_yi']:+.1f}亿，"
            f"领涨{b['leader']}{b['leader_pct']:+.1f}%，相关快讯{b['news']}条）"
            f"——你今天：{b['ours']}")


def render(f: dict) -> str:
    """The facts as the trader reads them."""
    if not f:
        return ""
    m = f["market"]
    lines = [
        f"全市场 {m['n']} 只：上涨 {m['up']}、下跌 {m['down']}，涨跌幅中位 {m['median_pct']:+.2f}%；"
        f"涨停 {m['limit_up']}、跌停 {m['limit_down']}、炸板 {m['broken']}（炸板率 {m['broken_rate']}%）；"
        f"最高 {m['max_streak']} 连板",
    ]
    if m["ladder"]:
        lines.append("连板梯队：" + "；".join(
            f"{n}({c}) {s}板" for n, c, s in m["ladder"]))
    if f["top"]:
        lines.append("今天涨得最好的板块（等权）：")
        lines += ["  " + _board_line(b) for b in f["top"]]
    if f["bottom"]:
        lines.append("今天跌得最多的板块（等权）：")
        lines += ["  " + _board_line(b) for b in f["bottom"]]
    return "\n".join(lines)


def exposure_line(marks: list[tuple[str, float]], market_pct: float | None,
                  own_pct: float | None) -> str:
    """Opportunity cost in one line: how much it was in, what the market did."""
    if not marks:
        return ""
    avg = statistics.mean(e for _, e in marks)
    empty = sum(1 for _, e in marks if e < 0.5)
    line = (f"最近 {len(marks)} 个交易日你的平均仓位 {avg:.1f}%，空仓 {empty} 天")
    if market_pct is not None:
        line += f"；同期全市场中位累计 {market_pct:+.1f}%"
    if own_pct is not None:
        line += f"，你的账户 {own_pct:+.2f}%"
    return line + "。"


def market_move(hist: sqlite3.Connection, start: str, end: str) -> float | None:
    """Median member-level move from the close before ``start`` to ``end``."""
    row = hist.execute("SELECT MAX(date) FROM daily_kline WHERE date < ?",
                       (start,)).fetchone()
    if not row or not row[0]:
        return None
    rows = hist.execute(
        "SELECT a.close, b.close FROM daily_kline a JOIN daily_kline b "
        "ON a.code = b.code WHERE a.date = ? AND b.date = ? AND a.close > 0",
        (row[0], end)).fetchall()
    moves = [(b / a - 1) * 100 for a, b in rows if b]
    return round(statistics.median(moves), 2) if moves else None
