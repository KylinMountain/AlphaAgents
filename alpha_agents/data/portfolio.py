"""Virtual portfolio — pending orders → triggered fills → stop/target close.

Lifecycle:
  pending (挂单) → open (建仓) → stopped/target_hit/expired (平仓)
                 → cancelled (挂单过期/涨走了)

Capital management: 10万 total, 100-share lots, position limits.
T+1 constraint: positions opened today are not checked until tomorrow.
A-share rules: buy in multiples of 100 shares.
"""

import logging
import re
from datetime import datetime

from alpha_agents.data.memory_store import _get_conn, _write_lock, get_theme_by_name

logger = logging.getLogger(__name__)

MAX_HOLDING_DAYS = 5
PENDING_EXPIRE_DAYS = 2  # 挂单有效期（交易日）

# ── Capital Management ──────────────────────────────────────
TOTAL_CAPITAL = 100_000          # 总资金 10万
MAX_POSITION_PCT = 0.15          # 单票初始建仓最多占总资金 15%
MAX_POSITION_WITH_ADD = 0.30     # 补仓后单票最多占总资金 30%
MAX_THEME_PCT = 0.30             # 同主线最多占总资金 30%
LOT_SIZE = 100                   # A股一手 = 100股
ADD_POSITION_DROP_PCT = 5.0      # 持仓跌超5%才考虑补仓

# Sentiment-based total exposure limits
SENTIMENT_EXPOSURE = {
    "extreme_bullish": 0.70,  # 涨跌比 > 5: max 70% invested
    "bullish": 0.60,          # 涨跌比 3-5: max 60%
    "neutral": 0.50,          # 涨跌比 1-3: max 50%
    "bearish": 0.30,          # 涨跌比 0.5-1: max 30%
    "extreme_bearish": 0.15,  # 涨跌比 < 0.5: max 15% (almost all cash)
}


def get_sentiment_exposure_limit() -> float:
    """Get max total exposure based on current market sentiment."""
    try:
        from alpha_agents.tools.market_breadth import get_market_breadth_fn
        import json as _json
        breadth = _json.loads(get_market_breadth_fn())
        ratio = breadth.get("advance_decline_ratio", 1.5)

        if ratio > 5:
            limit = SENTIMENT_EXPOSURE["extreme_bullish"]
            label = f"极度乐观(涨跌比{ratio:.1f})"
        elif ratio > 3:
            limit = SENTIMENT_EXPOSURE["bullish"]
            label = f"乐观(涨跌比{ratio:.1f})"
        elif ratio > 1:
            limit = SENTIMENT_EXPOSURE["neutral"]
            label = f"中性(涨跌比{ratio:.1f})"
        elif ratio > 0.5:
            limit = SENTIMENT_EXPOSURE["bearish"]
            label = f"悲观(涨跌比{ratio:.1f})"
        else:
            limit = SENTIMENT_EXPOSURE["extreme_bearish"]
            label = f"极度悲观(涨跌比{ratio:.1f})"

        max_invest = TOTAL_CAPITAL * limit
        logger.debug("Sentiment exposure: %s → max %.0f元 (%.0f%%)", label, max_invest, limit * 100)
        return max_invest
    except Exception:
        # Default to 50% if can't fetch sentiment
        return TOTAL_CAPITAL * 0.50


def _calc_shares(price: float, max_amount: float) -> int:
    """Calculate how many shares to buy (must be multiple of 100).
    Returns 0 if can't afford even 1 lot."""
    if price <= 0:
        return 0
    max_shares = int(max_amount / price)
    lots = max_shares // LOT_SIZE
    return lots * LOT_SIZE


def get_available_capital() -> float:
    """Get remaining cash = total capital - sum of open position costs."""
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


# ── Pending Orders (挂单) ───────────────────────────────────

def create_pending_order(
    *,
    code: str,
    name: str,
    theme: str,
    order_date: str,
    entry_low: float | None = None,
    entry_high: float | None = None,
    stop_loss: float | None = None,
    target_price: float | None = None,
    source: str = "morning",
    reason: str = "",
) -> int | None:
    """Create a pending order (挂单). Triggered when price enters entry zone.

    Returns order id, or None if duplicate/rejected.
    """
    with _write_lock:
        conn = _get_conn()
        # No duplicate: same stock pending or open
        existing = conn.execute(
            "SELECT id FROM virtual_portfolio WHERE code = ? AND status IN ('pending', 'open')",
            (code,),
        ).fetchone()
        if existing:
            logger.info("Order/position already exists for %s %s, skipping", code, name)
            return None

        cursor = conn.execute(
            "INSERT INTO virtual_portfolio "
            "(code, name, theme, order_date, open_date, open_price, entry_low, entry_high, "
            " stop_loss, target_price, expire_days, status, source, reason) "
            "VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (code, name, theme, order_date, order_date, entry_low, entry_high,
             stop_loss, target_price, PENDING_EXPIRE_DAYS, source, reason),
        )
        conn.commit()

        zone = f"{entry_low:.2f}-{entry_high:.2f}" if entry_low and entry_high else "市价"
        logger.info("Pending order: %s %s 介入区间%s 止损%s (%s)",
                     code, name, zone, stop_loss or "无", source)
        return cursor.lastrowid


def check_pending_orders(
    realtime_prices: dict[str, float],
    today: str,
) -> list[dict]:
    """Check pending orders against realtime prices. Fill if price in entry zone.

    Returns list of fill alerts.
    """
    conn = _get_conn()
    orders = conn.execute(
        "SELECT * FROM virtual_portfolio WHERE status = 'pending'"
    ).fetchall()

    alerts = []
    for order in orders:
        order = dict(order)
        code = order["code"]
        price = realtime_prices.get(code)

        # Check expiry first
        try:
            order_dt = datetime.strptime(order["order_date"], "%Y-%m-%d")
            today_dt = datetime.strptime(today, "%Y-%m-%d")
            days_pending = (today_dt - order_dt).days
        except ValueError:
            days_pending = 0

        expire_days = order.get("expire_days") or PENDING_EXPIRE_DAYS
        if days_pending > expire_days:
            _cancel_order(order["id"], f"挂单过期({days_pending}天未触发)")
            alerts.append({
                "type": "cancelled",
                "code": code,
                "name": order.get("name", ""),
                "reason": f"挂单过期({days_pending}天)",
            })
            continue

        if price is None or price <= 0:
            continue

        entry_low = order.get("entry_low")
        entry_high = order.get("entry_high")

        # Check if price has run away (> 5% above entry_high)
        if entry_high and price > entry_high * 1.05:
            _cancel_order(order["id"], f"价格已涨走({price:.2f}远超介入上限{entry_high:.2f})")
            alerts.append({
                "type": "cancelled",
                "code": code,
                "name": order.get("name", ""),
                "reason": f"价格涨走({price:.2f})",
            })
            continue

        # Check if price is in entry zone
        triggered = False
        if entry_low and entry_high:
            triggered = entry_low <= price <= entry_high
        elif entry_high:
            # Only upper bound: buy at or below
            triggered = price <= entry_high
        elif entry_low:
            # Only lower bound: buy at or above (breakout style)
            triggered = price >= entry_low
        else:
            # No zone specified: trigger immediately (market order)
            triggered = True

        if triggered:
            fill_alert = _fill_order(order, fill_price=price, fill_date=today)
            if fill_alert:
                alerts.append(fill_alert)

    return alerts


def _fill_order(order: dict, fill_price: float, fill_date: str) -> dict | None:
    """Convert a pending order to an open position at fill_price."""
    code = order["code"]
    name = order.get("name", "")
    theme = order.get("theme", "")

    # Capital checks (including sentiment-based total exposure limit)
    available = get_available_capital()
    sentiment_cap = get_sentiment_exposure_limit()
    invested = TOTAL_CAPITAL - available
    sentiment_room = max(0, sentiment_cap - invested)  # How much more we can invest given sentiment

    max_per_stock = TOTAL_CAPITAL * MAX_POSITION_PCT
    max_for_theme = TOTAL_CAPITAL * MAX_THEME_PCT - get_theme_exposure(theme)
    max_amount = min(available, max_per_stock, max(0, max_for_theme), sentiment_room)

    shares = _calc_shares(fill_price, max_amount)
    if shares == 0:
        _cancel_order(order["id"], f"资金不足(需{fill_price * LOT_SIZE:.0f}元/手, 可用{max_amount:.0f}元)")
        return {
            "type": "cancelled",
            "code": code,
            "name": name,
            "reason": f"资金不足",
        }

    cost = shares * fill_price

    with _write_lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE virtual_portfolio SET "
            "status = 'open', open_date = ?, open_price = ?, shares = ? "
            "WHERE id = ?",
            (fill_date, fill_price, shares, order["id"]),
        )
        conn.commit()

    remaining = available - cost
    logger.info("Order filled: %s %s %d股 @ %.2f = %.0f元 (止损%s) | 剩余%.0f",
                 code, name, shares, fill_price, cost,
                 order.get("stop_loss") or "无", remaining)

    return {
        "type": "filled",
        "code": code,
        "name": name,
        "shares": shares,
        "fill_price": fill_price,
        "cost": cost,
        "stop_loss": order.get("stop_loss"),
    }


def _cancel_order(order_id: int, reason: str) -> None:
    """Cancel a pending order."""
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE virtual_portfolio SET status = 'cancelled', close_reason = ? WHERE id = ?",
            (reason, order_id),
        )
        conn.commit()
    logger.info("Cancelled order #%d: %s", order_id, reason)


def get_pending_orders() -> list[dict]:
    """Get all pending orders."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM virtual_portfolio WHERE status = 'pending' ORDER BY order_date"
    ).fetchall()
    return [dict(r) for r in rows]


# ── Open Positions ──────────────────────────────────────────

def get_open_positions() -> list[dict]:
    """Get all currently open (filled) positions."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM virtual_portfolio WHERE status = 'open' ORDER BY open_date"
    ).fetchall()
    return [dict(r) for r in rows]


def close_position(
    position_id: int,
    *,
    close_price: float,
    close_reason: str,
) -> None:
    """Close an open position."""
    with _write_lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT open_price, shares FROM virtual_portfolio WHERE id = ?",
            (position_id,),
        ).fetchone()
        if not row:
            return

        open_price = row["open_price"] or 0
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
        logger.info("Closed #%d: %d股 @ %.2f → %.2f (%+.2f%%, %+.0f元) %s",
                     position_id, shares, open_price, close_price,
                     return_pct, return_amount, close_reason)


def _status_from_reason(reason: str) -> str:
    if "止损" in reason:
        return "stopped"
    if "止盈" in reason:
        return "target_hit"
    return "expired"


def check_positions(
    realtime_prices: dict[str, float],
    today: str,
) -> list[dict]:
    """Check open positions for stop-loss/take-profit/expiry.

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

        open_price = pos["open_price"] or 0
        current_return = round((price - open_price) / open_price * 100, 2) if open_price else 0

        # Update peak and drawdown
        peak = max(pos.get("peak_return_pct", 0) or 0, current_return)
        drawdown = round(peak - current_return, 2) if peak > 0 else 0

        try:
            open_dt = datetime.strptime(pos["open_date"], "%Y-%m-%d")
            today_dt = datetime.strptime(today, "%Y-%m-%d")
            holding_days = max(0, (today_dt - open_dt).days)
        except ValueError:
            holding_days = pos.get("holding_days", 0)

        # ── Trailing stop (移动止损) ──
        # When stock hits new highs, raise stop_loss to protect profits
        stop_loss = pos.get("stop_loss") or 0
        peak_price = open_price * (1 + peak / 100) if open_price else 0

        if peak_price > open_price and stop_loss > 0:
            # Trailing stop = peak price * (1 - trailing_pct)
            # trailing_pct starts at original stop distance, tightens as profit grows
            original_stop_pct = (open_price - stop_loss) / open_price if open_price else 0.05
            trailing_pct = min(original_stop_pct, 0.05)  # Tighten to max 5% from peak

            if current_return >= 5:
                # Once up 5%+, trail at 5% from peak (lock in most of the gain)
                new_stop = round(peak_price * (1 - trailing_pct), 2)
            elif current_return >= 3:
                # Up 3-5%, trail at original stop distance from peak
                new_stop = round(peak_price * (1 - original_stop_pct), 2)
            else:
                new_stop = stop_loss  # Keep original stop

            if new_stop > stop_loss:
                logger.info("Trailing stop: %s %s 止损 %.2f → %.2f (峰值%.2f, 当前%.2f)",
                            code, pos.get("name", ""), stop_loss, new_stop, peak_price, price)
                stop_loss = new_stop

        with _write_lock:
            conn.execute(
                "UPDATE virtual_portfolio SET peak_return_pct = ?, "
                "max_drawdown_pct = ?, holding_days = ?, stop_loss = ? WHERE id = ?",
                (peak, drawdown, holding_days, stop_loss, pos["id"]),
            )
            conn.commit()

        # Check triggers (priority order)
        alert = None
        target_price = pos.get("target_price")

        if stop_loss and price <= stop_loss:
            alert = {"type": "stopped", "reason": f"{'移动' if stop_loss > (pos.get('stop_loss') or 0) else ''}止损触发"}
        elif target_price and price >= target_price:
            alert = {"type": "target_hit", "reason": "止盈触发"}

        # Theme-aware holding + early exit
        if not alert and pos.get("theme"):
            theme = get_theme_by_name(pos["theme"])
            if theme:
                theme_status = theme.get("status", "watching")
                theme_strength = theme.get("strength", 0)

                if theme_status in ("declining", "archived"):
                    # 主线衰退 → 立刻走
                    alert = {"type": "expired", "reason": f"主线衰退({pos['theme']}已{theme_status})"}
                elif theme_status == "peak" and theme_strength >= 8:
                    # 主线 peak + 强度高 → 可以多拿几天，最多 10 天
                    if holding_days >= 10:
                        alert = {"type": "expired", "reason": f"持仓到期({holding_days}天，主线peak允许延长)"}
                elif theme_strength <= 3:
                    # 主线弱（但没到 declining）→ 缩短到 3 天
                    if holding_days >= 3:
                        alert = {"type": "expired", "reason": f"主线走弱(强度{theme_strength})，提前平仓({holding_days}天)"}

        # Default max holding: 5 days
        if not alert and holding_days >= MAX_HOLDING_DAYS:
            alert = {"type": "expired", "reason": f"持仓到期({holding_days}天)"}

        if alert:
            close_position(pos["id"], close_price=price, close_reason=alert["reason"])
            shares = pos.get("shares", 0)
            pnl = round((price - open_price) * shares, 2) if shares else 0
            alert.update({
                "code": code,
                "name": pos.get("name", ""),
                "shares": shares,
                "open_price": open_price,
                "close_price": price,
                "return_pct": current_return,
                "return_amount": pnl,
                "holding_days": holding_days,
            })
            alerts.append(alert)
        else:
            # ── Check for add-position opportunity (补仓) ──
            add_alert = _check_add_position(pos, price, current_return)
            if add_alert:
                alerts.append(add_alert)

    return alerts


def _check_add_position(pos: dict, price: float, current_return: float) -> dict | None:
    """Check if we should add to an existing position (补仓).

    Conditions:
    - Price dropped >= ADD_POSITION_DROP_PCT from entry
    - Theme still healthy (strength >= 4)
    - Current position < MAX_POSITION_WITH_ADD (30%)
    - Have available capital
    """
    if current_return > -ADD_POSITION_DROP_PCT:
        return None  # Not down enough

    code = pos["code"]
    name = pos.get("name", "")
    theme_name = pos.get("theme", "")
    open_price = pos.get("open_price", 0)
    existing_shares = pos.get("shares", 0)
    existing_cost = open_price * existing_shares

    # Check theme health
    if theme_name:
        theme = get_theme_by_name(theme_name)
        if theme and theme.get("strength", 0) < 4:
            return None  # Theme too weak, don't throw good money after bad

    # Check position limit (30% with add)
    max_total_cost = TOTAL_CAPITAL * MAX_POSITION_WITH_ADD
    room = max_total_cost - existing_cost
    if room <= 0:
        return None  # Already at max

    # Check available capital + sentiment limit
    available = get_available_capital()
    try:
        sentiment_cap = get_sentiment_exposure_limit()
        invested = TOTAL_CAPITAL - available
        sentiment_room = max(0, sentiment_cap - invested)
        room = min(room, available, sentiment_room)
    except Exception:
        room = min(room, available)

    add_shares = _calc_shares(price, room)
    if add_shares == 0:
        return None

    add_cost = add_shares * price

    # Execute add: update shares and recalculate avg open_price
    new_total_shares = existing_shares + add_shares
    new_avg_price = round((existing_cost + add_cost) / new_total_shares, 2)

    with _write_lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE virtual_portfolio SET open_price = ?, shares = ? WHERE id = ?",
            (new_avg_price, new_total_shares, pos["id"]),
        )
        conn.commit()

    logger.info("Add position: %s %s +%d股 @ %.2f (均价 %.2f→%.2f, 总%d股, 总成本%.0f元)",
                code, name, add_shares, price,
                open_price, new_avg_price, new_total_shares,
                new_avg_price * new_total_shares)

    return {
        "type": "add_position",
        "code": code,
        "name": name,
        "add_shares": add_shares,
        "add_price": price,
        "new_avg_price": new_avg_price,
        "total_shares": new_total_shares,
        "total_cost": round(new_avg_price * new_total_shares),
    }


# ── Summaries ───────────────────────────────────────────────

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
            lines.append(f"  {r['code']} {r['name']} 介入区间{r.get('entry_low','?')}-{r.get('entry_high','?')} ({r['source']})")
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
                f"  {r['code']} {r['name']} {r.get('shares',0)}股 @ {r['close_price']:.2f} "
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
