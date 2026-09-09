"""Reading the book, not changing it.

Split out of portfolio.py when that file crossed the 1200-line lint
ceiling. Everything here formats or parses; nothing opens, closes or
prices a position. The seam is real rather than arithmetic: the lifecycle
half owns the write lock and the money, this half owns the strings the
agents and the dashboard read.

The parsers live here too — ``parse_entry_zone`` and ``parse_stop_loss``
read an agent's prose back into numbers, which is the same
text-and-numbers boundary from the other direction.
"""

import logging
import re

from alpha_agents.data.memory_store import _get_conn
from alpha_agents.data.portfolio import (
    TOTAL_CAPITAL, get_available_capital, get_open_positions,
    get_pending_orders,
)

logger = logging.getLogger(__name__)


def get_open_positions_summary() -> str:
    """Format open positions + pending orders for agent context."""
    positions = get_open_positions()
    pending = get_pending_orders()
    available = get_available_capital()
    invested = TOTAL_CAPITAL - available

    lines = [f"总资金 {TOTAL_CAPITAL:,.0f}元 | 已投 {invested:,.0f}元 | 可用 {available:,.0f}元"]

    if pending:
        lines.append(f"挂单中 {len(pending)} 笔:")
        for p in pending:
            zone = ""
            if p.get("entry_low") and p.get("entry_high"):
                zone = f"{p['entry_low']:.2f}-{p['entry_high']:.2f}"
            elif p.get("entry_high"):
                zone = f"≤{p['entry_high']:.2f}"
            elif p.get("entry_low"):
                zone = f"≥{p['entry_low']:.2f}"
            lines.append(
                f"  {p['code']} {p.get('name','')} | 介入区间{zone} | "
                f"止损{p.get('stop_loss') or '无'} | 挂单日{p['order_date']}"
            )

    if positions:
        lines.append(f"持仓中 {len(positions)} 笔:")
        for p in positions:
            shares = p.get('shares', 0)
            cost = (p['open_price'] or 0) * shares
            lines.append(
                f"  {p['code']} {p.get('name','')} {shares}股 @ {p['open_price']:.2f} "
                f"(市值{cost:,.0f}元) | 止损{p.get('stop_loss') or '无'} | "
                f"持仓{p.get('holding_days', 0)}天"
            )

    if not pending and not positions:
        lines.append("无挂单/持仓")

    return "\n".join(lines)


def get_today_changes_summary(today: str) -> str:
    """Format today's portfolio changes for agent context."""
    conn = _get_conn()
    filled = conn.execute(
        "SELECT code, name, open_price, shares, source "
        "FROM virtual_portfolio WHERE open_date = ? AND status = 'open'",
        (today,),
    ).fetchall()
    closed = conn.execute(
        "SELECT code, name, shares, close_price, return_pct, return_amount, close_reason "
        "FROM virtual_portfolio WHERE close_date = ?",
        (today,),
    ).fetchall()
    new_pending = conn.execute(
        "SELECT code, name, entry_low, entry_high, source "
        "FROM virtual_portfolio WHERE order_date = ? AND status = 'pending'",
        (today,),
    ).fetchall()

    lines = []
    if new_pending:
        lines.append(f"今日新挂单 {len(new_pending)} 笔:")
        for r in new_pending:
            lines.append(f"  {r['code']} {r['name']} 介入区间{r['entry_low'] or '?'}-{r['entry_high'] or '?'} ({r['source']})")
    if filled:
        lines.append(f"今日成交 {len(filled)} 笔:")
        for r in filled:
            cost = (r['open_price'] or 0) * (r['shares'] or 0)
            lines.append(f"  {r['code']} {r['name']} {r['shares']}股 @ {r['open_price']:.2f} = {cost:,.0f}元")
    if closed:
        lines.append(f"今日平仓 {len(closed)} 笔:")
        for r in closed:
            ret = r['return_pct'] or 0
            amt = r['return_amount'] or 0
            lines.append(
                f"  {r['code']} {r['name']} {r['shares'] or 0}股 @ {r['close_price']:.2f} "
                f"({'盈' if ret >= 0 else '亏'}{abs(ret):.1f}%, {amt:+,.0f}元) — {r['close_reason']}"
            )
    return "\n".join(lines) if lines else "今日无持仓变动"


def get_portfolio_stats(days: int = 7) -> dict:
    """Get portfolio performance statistics for recent N days."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM virtual_portfolio WHERE status NOT IN ('open', 'pending', 'cancelled') "
        "ORDER BY close_date DESC LIMIT ?",
        (days * 10,),
    ).fetchall()

    if not rows:
        return {
            "total_closed": 0, "wins": 0, "losses": 0, "win_rate": 0,
            "avg_return": 0, "max_win": 0, "max_loss": 0,
            "total_profit": 0,
            "avg_holding_days": 0, "by_theme": {}, "by_source": {},
        }

    total = len(rows)
    returns = [r["return_pct"] or 0 for r in rows]
    amounts = [r["return_amount"] or 0 for r in rows]
    wins = sum(1 for ret in returns if ret > 0)
    losses = sum(1 for ret in returns if ret <= 0)

    by_theme: dict[str, list[float]] = {}
    for r in rows:
        theme = r["theme"] or "未知"
        by_theme.setdefault(theme, []).append(r["return_pct"] or 0)

    theme_stats = {}
    for theme, rets in by_theme.items():
        theme_stats[theme] = {
            "count": len(rets),
            "win_rate": round(sum(1 for r in rets if r > 0) / len(rets) * 100, 1),
            "avg_return": round(sum(rets) / len(rets), 2),
        }

    by_source: dict[str, list[float]] = {}
    for r in rows:
        src = r["source"] or "未知"
        by_source.setdefault(src, []).append(r["return_pct"] or 0)

    source_stats = {}
    for src, rets in by_source.items():
        source_stats[src] = {
            "count": len(rets),
            "win_rate": round(sum(1 for r in rets if r > 0) / len(rets) * 100, 1),
            "avg_return": round(sum(rets) / len(rets), 2),
        }

    return _add_benchmark({
        "total_closed": total,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / total * 100, 1) if total else 0,
        "avg_return": round(sum(returns) / total, 2) if total else 0,
        "max_win": round(max(returns), 2) if returns else 0,
        "max_loss": round(min(returns), 2) if returns else 0,
        "total_profit": round(sum(amounts), 2),
        "avg_holding_days": round(sum(r["holding_days"] or 0 for r in rows) / total, 1) if total else 0,
        "by_theme": theme_stats,
        "by_source": source_stats,
    }, rows)


def _add_benchmark(stats: dict, rows: list) -> dict:
    """Attach what the market did while these trades were open.

    Win rate and average return answer "did it make money". Neither
    answers "was holding the market better", which is the only question
    that decides whether any of this is worth running.
    """
    try:
        from alpha_agents.data.portfolio_risk import trade_excess
    except Exception as e:
        logger.debug("Benchmark comparison unavailable: %s", e)
        return stats

    excesses = [x for x in (trade_excess(dict(r)) for r in rows) if x is not None]
    if not excesses:
        return stats
    beat = sum(1 for x in excesses if x > 0)
    stats["benchmark"] = {
        "n": len(excesses),
        "avg_excess": round(sum(excesses) / len(excesses), 2),
        "median_excess": round(sorted(excesses)[len(excesses) // 2], 2),
        "beat_rate": round(beat / len(excesses) * 100, 1),
    }
    return stats


def format_portfolio_stats(stats: dict) -> str:
    """Format portfolio stats as text for agent/report context."""
    if stats["total_closed"] == 0:
        return "暂无已平仓记录"

    profit = stats.get('total_profit', 0)
    lines = [
        f"已平仓: {stats['total_closed']}笔 | 胜率: {stats['win_rate']:.1f}% "
        f"({stats['wins']}胜{stats['losses']}负)",
        f"累计盈亏: {profit:+,.0f}元 | 平均收益: {stats['avg_return']:+.2f}%",
        f"最大盈利: {stats['max_win']:+.2f}% | 最大亏损: {stats['max_loss']:+.2f}%",
        f"平均持仓: {stats['avg_holding_days']:.1f}天",
    ]

    if stats["by_theme"]:
        lines.append("按主线归因:")
        for theme, ts in stats["by_theme"].items():
            lines.append(f"  {theme}: {ts['count']}笔, 胜率{ts['win_rate']:.0f}%, 均收{ts['avg_return']:+.2f}%")

    if stats["by_source"]:
        lines.append("按来源归因:")
        for src, ss in stats["by_source"].items():
            lines.append(f"  {src}: {ss['count']}笔, 胜率{ss['win_rate']:.0f}%, 均收{ss['avg_return']:+.2f}%")

    return "\n".join(lines)


# ── Helpers ─────────────────────────────────────────────────

def parse_entry_zone(action_text: str) -> tuple[float | None, float | None]:
    """Parse entry zone from action text.

    Examples:
        "回调至342.0元不破可介入" → (None, 342.0)
        "介入区间329.83-337.00元" → (329.83, 337.0)
        "放量突破497.0元确认后跟进" → (497.0, None)
        "回调至53.5元附近可介入，止损52.0元" → (None, 53.5)
    """
    # Pattern: 介入区间 X-Y
    m = re.search(r"介入区间[：:\s]*(\d+\.?\d*)\s*[-–~]\s*(\d+\.?\d*)", action_text)
    if m:
        return float(m.group(1)), float(m.group(2))

    # Pattern: 回调至X元 → entry_high = X (buy at or below)
    m = re.search(r"回调至[：:\s]*(\d+\.?\d*)\s*元", action_text)
    if m:
        return None, float(m.group(1))

    # Pattern: 突破X元 → entry_low = X (buy at or above)
    m = re.search(r"突破[：:\s]*(\d+\.?\d*)\s*元", action_text)
    if m:
        return float(m.group(1)), None

    return None, None


def parse_stop_loss(action_text: str) -> float | None:
    """Extract stop loss price from action text."""
    m = re.search(r"止损[：:\s]*(\d+\.?\d*)\s*元?", action_text)
    if m:
        return float(m.group(1))
    return None
