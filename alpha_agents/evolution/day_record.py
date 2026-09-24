"""What the trader did on one day, from its own records.

The close review diagnoses the day's decisions, so it needs them in front of
it: which directions the open chose and why, which it saw and passed on, the
orders and whether they filled, what it held and how that moved, what it
sold. Everything here is read from the trader's own tables and from
``daily_kline`` on or before the day — a replay's market history holds the
future.
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)

_CLIP = 160


def _clip(text: str | None, n: int = _CLIP) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[:n] + "…"


def _bar(hist: sqlite3.Connection, code: str, day: str) -> tuple[float, float] | None:
    """(close on day, change vs the previous close) — nothing after ``day``."""
    rows = hist.execute(
        "SELECT close FROM daily_kline WHERE code = ? AND date <= ? "
        "ORDER BY date DESC LIMIT 2", (code, day)).fetchall()
    if len(rows) < 2 or not rows[1][0]:
        return None
    return rows[0][0], (rows[0][0] / rows[1][0] - 1) * 100


def directions_text(selected: list[tuple[str, str]], passed: list[str]) -> str:
    """The open's direction stage: chosen with the thesis, seen and passed."""
    lines = []
    if selected:
        lines.append("早盘选中的方向：")
        lines += [f"- {name}：{_clip(thesis) or '（没写理由）'}"
                  for name, thesis in selected]
    else:
        lines.append("早盘没有选中任何方向。")
    if passed:
        lines.append("早盘看到但没选：" + "、".join(passed[:20]))
    return "\n".join(lines)


def render(conn: sqlite3.Connection, hist: sqlite3.Connection, *,
           trader_id: str, day: str, directions: str = "") -> str:
    """The day's record as the close review reads it."""
    lines = [directions] if directions else []
    try:
        orders = conn.execute(
            "SELECT code, name, theme, open_date, open_price, status, reason "
            "FROM virtual_portfolio WHERE trader_id = ? AND order_date = ? "
            "ORDER BY id", (trader_id, day)).fetchall()
        held = conn.execute(
            "SELECT code, name, theme, open_date, open_price, shares "
            "FROM virtual_portfolio WHERE trader_id = ? AND status = 'open' "
            "AND open_date IS NOT NULL AND open_date <= ? ORDER BY open_date",
            (trader_id, day)).fetchall()
        closed = conn.execute(
            "SELECT code, name, theme, return_pct, close_reason "
            "FROM virtual_portfolio WHERE trader_id = ? AND close_date = ? "
            "AND open_date IS NOT NULL ORDER BY id", (trader_id, day)).fetchall()
    except sqlite3.Error as e:
        logger.warning("Day record for %s %s unavailable: %s", trader_id, day, e)
        return "\n".join(lines)

    if orders:
        lines.append("今天下的单：")
        for code, name, theme, open_date, price, status, reason in orders:
            state = (f"已成交 {price}" if open_date == day and price
                     else "撤单" if status == "cancelled" else "未成交")
            lines.append(f"- {code} {name or ''}（{theme or '无主线'}）{state}；"
                         f"理由：{_clip(reason)}")
    else:
        lines.append("今天没有下单。")

    if held:
        before = {r[0] for r in held if r[3] < day}
        lines.append("收盘时的持仓：")
        for code, name, theme, open_date, price, _shares in held:
            bar = _bar(hist, code, day)
            move = (f"今日 {bar[1]:+.2f}%，持仓 {(bar[0] / price - 1) * 100:+.2f}%"
                    if bar and price else "今日行情缺失")
            add = ("" if open_date != day
                   else "（今天加仓）" if code in before else "（今天新买）")
            lines.append(f"- {code} {name or ''}（{theme or '无主线'}）"
                         f"{open_date} 买入 {price}，{move}{add}")
    else:
        lines.append("收盘时空仓。")

    if closed:
        lines.append("今天卖出：")
        for code, name, theme, ret, why in closed:
            r = f"{ret:+.2f}%" if ret is not None else "—"
            lines.append(f"- {code} {name or ''}（{theme or '无主线'}）到手 {r}；"
                         f"理由：{_clip(why)}")
    return "\n".join(lines)
