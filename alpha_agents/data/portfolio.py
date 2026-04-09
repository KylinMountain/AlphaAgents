"""Virtual portfolio — track recommendations from open to close.

Simulates holding positions with stop-loss/take-profit monitoring.
T+1 constraint: positions opened today are not checked until tomorrow.
Capital management: 10万 total, 100-share lots, position limits.
"""

import json
import logging
from datetime import datetime

from alpha_agents.data.memory_store import _get_conn, _write_lock, get_theme_by_name

logger = logging.getLogger(__name__)

MAX_HOLDING_DAYS = 5

# ── Capital Management ──────────────────────────────────────
TOTAL_CAPITAL = 100_000          # 总资金 10万
MAX_POSITION_PCT = 0.15          # 单票最多占总资金 15%
MAX_THEME_PCT = 0.30             # 同主线最多占总资金 30%
LOT_SIZE = 100                   # A股一手 = 100股


def _calc_shares(price: float, max_amount: float) -> int:
    """Calculate how many shares to buy (must be multiple of 100).
    Returns 0 if can't afford even 1 lot."""
    if price <= 0:
        return 0
    max_shares = int(max_amount / price)
    lots = max_shares // LOT_SIZE
    return lots * LOT_SIZE


def get_available_capital() -> float:
    """Get remaining cash = total capital - sum of open position market values (at open_price)."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT open_price, shares FROM virtual_portfolio WHERE status = 'open'"
    ).fetchall()
    invested = sum((r["open_price"] or 0) * (r["shares"] or 0) for r in rows)
    return TOTAL_CAPITAL - invested


def get_theme_exposure(theme: str) -> float:
    """Get total market value invested in a given theme."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT open_price, shares FROM virtual_portfolio WHERE status = 'open' AND theme = ?",
        (theme,),
    ).fetchall()
    return sum((r["open_price"] or 0) * (r["shares"] or 0) for r in rows)


def open_position(
    *,
    code: str,
    name: str,
    theme: str,
    open_date: str,
    open_price: float,
    stop_loss: float | None = None,
    target_price: float | None = None,
    source: str = "morning",
    reason: str = "",
) -> int | None:
    """Open a virtual position with capital management.

    Rules:
    - Buy in lots of 100 shares
    - Single position max 15% of total capital
    - Same theme max 30% of total capital
    - Must have enough available cash
    Returns position id, or None if rejected.
    """
    if open_price <= 0:
        logger.info("Skipping %s %s: invalid price %.2f", code, name, open_price)
        return None

    with _write_lock:
        conn = _get_conn()
        # Check for existing open position on same stock
        existing = conn.execute(
            "SELECT id FROM virtual_portfolio WHERE code = ? AND status = 'open'",
            (code,),
        ).fetchone()
        if existing:
            logger.info("Position already open for %s %s, skipping", code, name)
            return None

    # Capital checks (outside write lock to avoid deadlock with get_available_capital)
    available = get_available_capital()
    max_per_stock = TOTAL_CAPITAL * MAX_POSITION_PCT
    max_for_theme = TOTAL_CAPITAL * MAX_THEME_PCT - get_theme_exposure(theme)

    # Take the minimum of all constraints
    max_amount = min(available, max_per_stock, max(0, max_for_theme))
    shares = _calc_shares(open_price, max_amount)

    if shares == 0:
        cost_1lot = open_price * LOT_SIZE
        logger.info("Skipping %s %s: can't afford 1 lot (need %.0f, available %.0f, "
                     "stock_limit %.0f, theme_limit %.0f)",
                     code, name, cost_1lot, available, max_per_stock, max_for_theme)
        return None

    cost = shares * open_price

    with _write_lock:
        conn = _get_conn()
        # Re-check duplicate inside lock
        existing = conn.execute(
            "SELECT id FROM virtual_portfolio WHERE code = ? AND status = 'open'",
            (code,),
        ).fetchone()
        if existing:
            return None

        cursor = conn.execute(
            "INSERT INTO virtual_portfolio "
            "(code, name, theme, open_date, open_price, shares, stop_loss, target_price, "
            " status, source, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)",
            (code, name, theme, open_date, open_price, shares, stop_loss, target_price,
             source, reason),
        )
        conn.commit()
        remaining = available - cost
        logger.info("Opened position: %s %s %d股 @ %.2f = %.0f元 (止损%.2f) | 剩余资金%.0f",
                     code, name, shares, open_price, cost,
                     stop_loss or 0, remaining)
        return cursor.lastrowid


def close_position(
    position_id: int,
    *,
    close_price: float,
    close_reason: str,
) -> None:
    """Close a virtual position."""
    with _write_lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT open_price, shares, open_date, peak_return_pct FROM virtual_portfolio WHERE id = ?",
            (position_id,),
        ).fetchone()
        if not row:
            return

        open_price = row["open_price"]
        shares = row["shares"] or 0
        return_pct = round((close_price - open_price) / open_price * 100, 2) if open_price else 0
        return_amount = round((close_price - open_price) * shares, 2)
        today = datetime.now().strftime("%Y-%m-%d")

        conn.execute(
            "UPDATE virtual_portfolio SET "
            "status = ?, close_date = ?, close_price = ?, return_pct = ?, "
            "return_amount = ?, close_reason = ? WHERE id = ?",
            (_status_from_reason(close_reason), today, close_price, return_pct,
             return_amount, close_reason, position_id),
        )
        conn.commit()
        logger.info("Closed position #%d: %d股 @ %.2f → %.2f (%+.2f%%, %+.0f元) %s",
                     position_id, shares, open_price, close_price,
                     return_pct, return_amount, close_reason)


def _status_from_reason(reason: str) -> str:
    if "止损" in reason:
        return "stopped"
    if "止盈" in reason:
        return "target_hit"
    return "expired"


def get_open_positions() -> list[dict]:
    """Get all currently open positions."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM virtual_portfolio WHERE status = 'open' ORDER BY open_date"
    ).fetchall()
    return [dict(r) for r in rows]


def check_positions(
    realtime_prices: dict[str, float],
    today: str,
) -> list[dict]:
    """Check open positions against realtime prices. Returns list of alerts.

    Respects T+1: skips positions where open_date == today.
    """
    conn = _get_conn()
    positions = conn.execute(
        "SELECT * FROM virtual_portfolio WHERE status = 'open' AND open_date < ?",
        (today,),
    ).fetchall()

    alerts = []
    for pos in positions:
        pos = dict(pos)
        code = pos["code"]
        price = realtime_prices.get(code)
        if price is None or price <= 0:
            continue

        open_price = pos["open_price"]
        current_return = round((price - open_price) / open_price * 100, 2) if open_price else 0

        # Update peak and drawdown
        peak = max(pos.get("peak_return_pct", 0) or 0, current_return)
        drawdown = round(peak - current_return, 2) if peak > 0 else 0

        # Count holding days (simple: business days between open_date and today)
        try:
            open_dt = datetime.strptime(pos["open_date"], "%Y-%m-%d")
            today_dt = datetime.strptime(today, "%Y-%m-%d")
            holding_days = max(0, (today_dt - open_dt).days)
        except ValueError:
            holding_days = pos.get("holding_days", 0)

        # Update tracking fields
        with _write_lock:
            conn.execute(
                "UPDATE virtual_portfolio SET peak_return_pct = ?, "
                "max_drawdown_pct = ?, holding_days = ? WHERE id = ?",
                (peak, drawdown, holding_days, pos["id"]),
            )
            conn.commit()

        # Check triggers (priority order)
        alert = None
        stop_loss = pos.get("stop_loss")
        target_price = pos.get("target_price")

        if stop_loss and price <= stop_loss:
            alert = {"type": "stopped", "reason": "止损触发"}
        elif target_price and price >= target_price:
            alert = {"type": "target_hit", "reason": "止盈触发"}
        elif pos.get("theme"):
            theme = get_theme_by_name(pos["theme"])
            if theme and theme.get("status") in ("declining", "archived"):
                alert = {"type": "expired", "reason": f"主线衰退({pos['theme']}已{theme['status']})"}
        if not alert and holding_days >= MAX_HOLDING_DAYS:
            alert = {"type": "expired", "reason": f"持仓到期({holding_days}天)"}

        if alert:
            close_position(pos["id"], close_price=price, close_reason=alert["reason"])
            alert.update({
                "code": code,
                "name": pos.get("name", ""),
                "open_price": open_price,
                "close_price": price,
                "return_pct": current_return,
                "holding_days": holding_days,
            })
            alerts.append(alert)

    return alerts


def get_open_positions_summary() -> str:
    """Format open positions as text for agent context."""
    positions = get_open_positions()
    available = get_available_capital()
    invested = TOTAL_CAPITAL - available
    if not positions:
        return f"当前无持仓 | 可用资金 {available:,.0f}元 / 总资金 {TOTAL_CAPITAL:,.0f}元"
    lines = [f"总资金 {TOTAL_CAPITAL:,.0f}元 | 已投 {invested:,.0f}元 | 可用 {available:,.0f}元"]
    for p in positions:
        shares = p.get('shares', 0)
        cost = (p['open_price'] or 0) * shares
        lines.append(
            f"  {p['code']} {p.get('name','')} {shares}股 @ {p['open_price']:.2f} "
            f"(持仓{cost:,.0f}元) | 止损{p.get('stop_loss') or '无'} | "
            f"持仓{p.get('holding_days', 0)}天"
        )
    return f"当前持仓 {len(positions)} 笔:\n" + "\n".join(lines)


def get_today_changes_summary(today: str) -> str:
    """Format today's portfolio changes for agent context."""
    conn = _get_conn()
    opened = conn.execute(
        "SELECT code, name, open_price, source FROM virtual_portfolio WHERE open_date = ?",
        (today,),
    ).fetchall()
    closed = conn.execute(
        "SELECT code, name, close_price, return_pct, close_reason "
        "FROM virtual_portfolio WHERE close_date = ?",
        (today,),
    ).fetchall()

    lines = []
    if opened:
        lines.append(f"今日建仓 {len(opened)} 笔:")
        for r in opened:
            lines.append(f"  {r['code']} {r['name']} @ {r['open_price']:.2f} ({r['source']})")
    if closed:
        lines.append(f"今日平仓 {len(closed)} 笔:")
        for r in closed:
            ret = r['return_pct'] or 0
            lines.append(
                f"  {r['code']} {r['name']} @ {r['close_price']:.2f} "
                f"({'盈' if ret >= 0 else '亏'}{abs(ret):.1f}%) — {r['close_reason']}"
            )
    return "\n".join(lines) if lines else "今日无持仓变动"


def get_portfolio_stats(days: int = 7) -> dict:
    """Get portfolio performance statistics for recent N days."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM virtual_portfolio WHERE status != 'open' "
        "ORDER BY close_date DESC LIMIT ?",
        (days * 10,),
    ).fetchall()

    if not rows:
        return {
            "total_closed": 0, "wins": 0, "losses": 0, "win_rate": 0,
            "avg_return": 0, "max_win": 0, "max_loss": 0,
            "avg_holding_days": 0, "by_theme": {}, "by_source": {},
        }

    total = len(rows)
    returns = [r["return_pct"] or 0 for r in rows]
    amounts = [r["return_amount"] or 0 for r in rows]
    wins = sum(1 for ret in returns if ret > 0)
    losses = sum(1 for ret in returns if ret <= 0)

    # By theme
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

    # By source
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

    return {
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
    }


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
