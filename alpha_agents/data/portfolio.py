"""Virtual portfolio — pending orders → triggered fills → stop/target close.

Lifecycle:
  pending (挂单) → open (建仓) → stopped/target_hit/expired (平仓)
                 → cancelled (挂单过期/涨走了)

Capital management: 10万 total, 100-share lots, position limits.
T+1 constraint: positions opened today are not checked until tomorrow.
A-share rules: buy in multiples of 100 shares.
"""

import logging
import os
import re
from datetime import datetime

from alpha_agents.data.memory_store import _get_conn, _write_lock, get_theme_by_name

logger = logging.getLogger(__name__)

PENDING_EXPIRE_DAYS = 2  # 挂单有效期（仅用于无主线的挂单，有主线的跟随主线生命周期）

# ── Capital Management ──────────────────────────────────────
# 50万，然后基本让开。
#
# The agent sizes its own positions: how much to open with, when to add,
# how much to take off. Those are half of what separates a trader who
# knows what they are doing from one who does not, and every one of them
# used to be a constant in this file — so they were unlearnable, exactly
# the way selling was before the agent was given that too.
#
# What stays is a ceiling per stock, and it is a backstop rather than a
# control: at 100% one position could take the whole book and every later
# idea would be refused for 资金不足, which is how 上海机电 died with 232元
# left. Sample count is the scarce resource here, and a runaway position
# spends it. 10% is wide enough that a sane decision never touches it.
TOTAL_CAPITAL = int(os.environ.get("TOTAL_CAPITAL", "500000"))

# Default when a pick says nothing about size. Not a cap.
DEFAULT_POSITION_PCT = float(os.environ.get("DEFAULT_POSITION_PCT", "0.03"))

# The backstop. Not a target, and not a budget the agent should aim at.
MAX_POSITION_PCT = float(os.environ.get("MAX_POSITION_PCT", "0.10"))
MAX_POSITION_WITH_ADD = MAX_POSITION_PCT
MAX_THEME_PCT = 0.30             # 单主线上限
LOT_SIZE = 100                   # A股一手 = 100股
ADD_POSITION_DROP_PCT = 5.0      # 持仓跌超5%才考虑补仓

# The one exit the trading agent may not overrule.
#
# Every other trigger here — the trailing stop, the target, a weakening
# theme, the holding cap — is a judgement call, and handing those to the
# agent is the point of letting it trade. A maximum loss from cost basis
# is not a judgement call: it is the line that keeps one bad thesis from
# consuming the account while the agent talks itself into holding.
#
# Measured from open_price, not from stop_loss, because stop_loss
# ratchets upward with the trailing rule and no longer records where the
# position started.
HARD_STOP_PCT = float(os.environ.get("HARD_STOP_PCT", "8.0"))

# Conservative A-share execution assumptions for virtual portfolio P&L.
# Net return includes buy/sell slippage, commissions, transfer fees, and
# sell-side stamp duty so the portfolio is not evaluated on frictionless fills.
COMMISSION_RATE = 0.0003         # 0.03% each side
MIN_COMMISSION = 5.0             # RMB minimum per order
STAMP_DUTY_SELL_RATE = 0.0005    # 0.05% on sell side
TRANSFER_FEE_RATE = 0.00001      # 0.001% each side
SLIPPAGE_RATE = 0.0005           # 5 bps each side


def _estimate_net_close_result(open_price: float, close_price: float, shares: int) -> dict:
    """Estimate net round-trip P&L for an A-share virtual trade."""
    if open_price <= 0 or close_price <= 0 or shares <= 0:
        return {
            "return_pct": 0.0,
            "return_amount": 0.0,
            "gross_amount": 0.0,
            "costs": 0.0,
        }

    buy_price = open_price * (1 + SLIPPAGE_RATE)
    sell_price = close_price * (1 - SLIPPAGE_RATE)
    buy_value = buy_price * shares
    sell_value = sell_price * shares

    buy_commission = max(buy_value * COMMISSION_RATE, MIN_COMMISSION)
    sell_commission = max(sell_value * COMMISSION_RATE, MIN_COMMISSION)
    buy_transfer = buy_value * TRANSFER_FEE_RATE
    sell_transfer = sell_value * TRANSFER_FEE_RATE
    stamp_duty = sell_value * STAMP_DUTY_SELL_RATE

    cost_basis = buy_value + buy_commission + buy_transfer
    net_proceeds = sell_value - sell_commission - sell_transfer - stamp_duty
    net_amount = net_proceeds - cost_basis
    gross_amount = (close_price - open_price) * shares

    return {
        "return_pct": round(net_amount / cost_basis * 100, 2) if cost_basis else 0.0,
        "return_amount": round(net_amount, 2),
        "gross_amount": round(gross_amount, 2),
        "costs": round(gross_amount - net_amount, 2),
    }


def get_sentiment_exposure_limit() -> float:
    """Get max total exposure based on sentiment cycle phase."""
    try:
        from alpha_agents.data.sentiment_cycle import get_sentiment_cycle
        cycle = get_sentiment_cycle()
        phase = cycle.get("phase", "修复")
        pct = cycle["strategy"]["max_exposure_pct"]
        max_invest = TOTAL_CAPITAL * pct / 100
        logger.debug("Sentiment cycle: %s → max %.0f元 (%.0f%%)", phase, max_invest, pct)
        return max_invest
    except Exception as e:
        logger.warning("Sentiment cycle failed, defaulting to 50%%: %s", e)
        return TOTAL_CAPITAL * 0.50


def _calc_shares(price: float, max_amount: float) -> int:
    """Calculate how many shares to buy (must be multiple of 100).
    Returns 0 if can't afford even 1 lot."""
    if price <= 0:
        return 0
    max_shares = int(max_amount / price)
    lots = max_shares // LOT_SIZE
    return lots * LOT_SIZE


def _wanted_pct(code: str) -> float:
    """How much of the book this idea asked for, as a fraction.

    The thesis states it. A pick that says nothing gets
    DEFAULT_POSITION_PCT, which keeps behaviour unchanged for anything
    written before sizing was a decision.
    """
    try:
        from alpha_agents.data.thesis import get_active
        theses = [t for t in get_active(code=code) if t.position_id is None]
        if theses and theses[-1].size_pct:
            return max(0.005, min(1.0, theses[-1].size_pct))
    except Exception as e:
        logger.debug("Size lookup fallback for %s: %s", code, e)
    return DEFAULT_POSITION_PCT


def _cluster_room(theme: str) -> float:
    """Headroom for this theme's correlated cluster, or unlimited on error.

    Falling open rather than closed: a failure in the correlation lookup
    must not silently stop the portfolio from trading. The per-theme and
    per-stock caps still apply underneath.
    """
    try:
        from alpha_agents.data.portfolio_risk import cluster_room
        return cluster_room(theme)
    except Exception as e:
        logger.warning("Cluster check unavailable for %r: %s", theme, e)
        return float("inf")


def _drawdown_blocks_new_risk() -> bool:
    """True only when the account is measurably deep in a drawdown.

    Falls open on any failure: a broken risk lookup must not quietly stop
    the portfolio from trading, which would look exactly like a market
    with no opportunities.
    """
    try:
        from alpha_agents.data.portfolio_risk import current_drawdown
        return bool(current_drawdown().get("blocked"))
    except Exception as e:
        logger.warning("Drawdown check unavailable: %s", e)
        return False


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


def resolve_theme(theme: str) -> str | None:
    """Canonical theme name for an order, or None when there is no thesis.

    ``check_pending_orders`` already refuses to hold a position whose theme
    is not in theme_lines — "no theme, no logical basis". That rule ran a
    cycle too late: an order was created from whatever concept string the
    agent wrote in its 关联概念 column, then cancelled the next cycle with
    "关联主线'磷化工'不存在". A capital slot spent on an idea the system had
    already decided it would not hold.

    Agents also write compound labels ("化肥/磷化工") for a theme tracked
    under one of its parts, so an exact match alone would reject ideas the
    system does hold.
    """
    if not theme:
        return None
    from alpha_agents.data.memory_store import get_active_themes
    try:
        known = [t["name"] for t in get_active_themes()]
    except Exception as e:
        # Without the theme list there is nothing to check against; let the
        # order through rather than dropping ideas on an unrelated failure.
        logger.warning("Theme lookup failed, accepting order theme %r: %s",
                       theme, e)
        return theme

    if theme in known:
        return theme
    parts = [p.strip() for p in re.split(r"[/、,，|]", theme) if p.strip()]
    for part in parts:
        if part in known:
            return part
    for name in known:
        if name and (name in theme or theme in name):
            return name
    return None


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

        resolved = resolve_theme(theme)
        if resolved is None:
            logger.warning(
                "Rejected order %s %s: theme %r is not a tracked theme line. "
                "check_pending_orders would cancel it next cycle anyway.",
                code, name, theme)
            return None
        theme = resolved

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
    # Highest conviction first. Capital is finite and this loop spends it
    # in order, so whatever ran first used to win — by row id, which is
    # arrival order and carries no information. 上海机电 was refused with
    # 232元 left not because it was the weakest idea but because it was
    # inserted last. When the book is full, it should be full of the ideas
    # the agent believed in.
    orders = sorted(orders, key=lambda o: -_order_conviction(o["code"]))

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
            logger.warning("Invalid date in order #%d: order_date=%r — cancelling", order["id"], order.get("order_date"))
            _cancel_order(order["id"], "日期解析失败")
            continue

        # Dynamic expiry based on theme health:
        # - Theme healthy (strength >= 4) → keep the order alive, no expiry
        # - Theme weak (strength < 4) or declining/archived → cancel
        # - No theme → fall back to fixed expiry
        theme_name = order.get("theme", "")
        if theme_name:
            theme = get_theme_by_name(theme_name)
            if theme:
                if theme.get("status") in ("declining", "archived"):
                    _cancel_order(order["id"], f"主线衰退({theme_name}已{theme['status']})")
                    alerts.append({
                        "type": "cancelled",
                        "code": code,
                        "name": order.get("name", ""),
                        "reason": f"主线衰退({theme_name})",
                    })
                    continue
                if theme.get("strength", 0) < 4:
                    _cancel_order(order["id"], f"主线走弱({theme_name}强度{theme['strength']})")
                    alerts.append({
                        "type": "cancelled",
                        "code": code,
                        "name": order.get("name", ""),
                        "reason": f"主线走弱({theme_name})",
                    })
                    continue
                # Theme healthy → keep order alive regardless of days
            else:
                # Theme not found in DB → no logical basis, cancel
                _cancel_order(order["id"], f"关联主线'{theme_name}'不存在")
                alerts.append({"type": "cancelled", "code": code, "name": order.get("name", ""), "reason": f"主线不存在"})
                continue
        else:
            # No theme at all → no logical basis, cancel
            _cancel_order(order["id"], "无关联主线，缺乏持仓逻辑")
            alerts.append({"type": "cancelled", "code": code, "name": order.get("name", ""), "reason": "无关联主线"})
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
            # The thesis has to still be alive at the moment of the fill.
            # An order can sit for days while the reason for it decays, and
            # filling into a dead thesis costs a full round trip — buy and
            # sell commission, stamp duty, slippage, ~0.15% — for a
            # position the monitor closes on its next pass. Seen on the
            # first day of real fills: 东方明珠 filled while 国企改革 was
            # already scoring −1 for the day.
            dead = _thesis_already_broken(code, price, order)
            if dead:
                _cancel_order(order["id"], f"论点在成交前已失效: {dead}")
                alerts.append({"type": "cancelled", "code": code,
                               "name": order.get("name", ""),
                               "reason": f"论点已失效({dead})"})
                continue
            fill_alert = _fill_order(order, fill_price=price, fill_date=today)
            if fill_alert:
                alerts.append(fill_alert)

    return alerts


def _order_conviction(code: str) -> float:
    """The stated conviction behind an order, or a neutral 0.5.

    Neutral rather than zero for an order with no thesis: those predate
    theses entirely, and sorting them to the back would starve them of
    capital for a reason that is about the code's history, not the idea.
    """
    try:
        from alpha_agents.data.thesis import get_active
        theses = get_active(code=code)
        return theses[-1].conviction if theses else 0.5
    except Exception as e:
        logger.debug("Conviction sort fallback for %s: %s", code, e)
        return 0.5


def _thesis_already_broken(code: str, price: float, order: dict) -> str | None:
    """Would this order's thesis have been invalidated the moment it filled?

    Evaluated against the fill price with no position history — there is
    no peak and no holding period yet, so drawdown and time conditions
    cannot fire and are not meant to. What can fire is everything about
    the world: the price level, the theme's strength and today's score,
    its rank and flow. Those are exactly the conditions that decay while
    an order waits.

    Returns the condition's description, or None to fill.
    """
    try:
        from alpha_agents.data import thesis as T
    except Exception as e:
        logger.debug("Thesis pre-check unavailable for %s: %s", code, e)
        return None

    try:
        theses = [t for t in T.get_active(code=code) if t.position_id is None]
        if not theses:
            return None
        th = theses[-1]
        open_price = order.get("entry_high") or order.get("entry_low") or price
        mv = T.MarketView(
            price=price,
            current_return_pct=round((price - open_price) / open_price * 100, 2)
            if open_price else 0.0,
        )
        theme = order.get("theme")
        if theme:
            row = get_theme_by_name(theme)
            if row:
                mv.theme_strength = row.get("strength")
                mv.theme_daily_score = row.get("daily_score")
                mv.theme_status = row.get("status")
        fired = T.evaluate(th.conditions, mv)
        if fired:
            T.close(th.id, T.INVALIDATED, close_kind=fired.kind,
                    close_note=f"成交前失效：{T.describe(fired)}")
            return T.describe(fired)
    except Exception as e:
        logger.warning("Thesis pre-check failed for %s: %s", code, e)
    return None


def _fill_order(order: dict, fill_price: float, fill_date: str) -> dict | None:
    """Convert a pending order to an open position at fill_price."""
    code = order["code"]
    name = order.get("name", "")
    theme = order.get("theme", "")

    with _write_lock:
        # Capital checks (including sentiment-based total exposure limit)
        # Must be inside lock to prevent race conditions with concurrent fills
        available = get_available_capital()
        sentiment_cap = get_sentiment_exposure_limit()
        invested = TOTAL_CAPITAL - available
        sentiment_room = max(0, sentiment_cap - invested)  # How much more we can invest given sentiment

        # Size by conviction rather than filling every position to the cap.
        # A flat 15% everywhere throws away half of what a trader is for:
        # being right more often is worth less than being bigger when
        # right. The thesis carries a conviction the agent stated when it
        # opened the idea; with no thesis this falls back to the cap and
        # behaves exactly as before.
        # What the agent asked for, capped by the backstop. Conviction no
        # longer scales this behind its back — it says the number itself.
        max_per_stock = min(TOTAL_CAPITAL * _wanted_pct(code),
                            TOTAL_CAPITAL * MAX_POSITION_PCT)
        max_for_theme = TOTAL_CAPITAL * MAX_THEME_PCT - get_theme_exposure(theme)
        # Themes that share most of their constituents are one bet. The
        # per-theme cap counted 金属铜 and 小金属概念 as two and would let
        # them take 60% between them while sharing 6 of 10 names.
        cluster_cap = _cluster_room(theme)
        max_amount = min(available, max_per_stock, max(0, max_for_theme),
                         sentiment_room, cluster_cap)

        # Portfolio drawdown gates *new* risk and never forces an exit.
        # Liquidating at a drawdown level sells the bottom, and in a system
        # built to learn from resolved theses it would destroy the samples
        # before they resolve. Positions already open keep their own stops.
        if _drawdown_blocks_new_risk():
            _cancel_order_unlocked(order["id"], "组合回撤触及上限，暂停开新仓")
            return {"type": "cancelled", "code": code, "name": name,
                    "reason": "组合回撤触及上限"}

        shares = _calc_shares(fill_price, max_amount)
        if shares == 0:
            _cancel_order_unlocked(order["id"], f"资金不足(需{fill_price * LOT_SIZE:.0f}元/手, 可用{max_amount:.0f}元)")
            return {
                "type": "cancelled",
                "code": code,
                "name": name,
                "reason": f"资金不足",
            }

        cost = shares * fill_price

        conn = _get_conn()
        conn.execute(
            "UPDATE virtual_portfolio SET "
            "status = 'open', open_date = ?, open_price = ?, shares = ? "
            "WHERE id = ?",
            (fill_date, fill_price, shares, order["id"]),
        )
        conn.commit()

    # Bind the thesis to the position it just became. Until the fill the
    # thesis is an idea; from here the monitor evaluates it against a real
    # cost basis every cycle, and an unbound thesis would be checked
    # against nothing.
    try:
        from alpha_agents.data.thesis import attach_position, get_active
        for th in get_active(code=code):
            if th.position_id is None:
                attach_position(th.id, order["id"])
                break
    except Exception as e:
        logger.warning("Could not bind thesis to position %s: %s", code, e)

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


def _cancel_order_unlocked(order_id: int, reason: str) -> None:
    """Cancel a pending order. Caller must already hold _write_lock."""
    conn = _get_conn()
    conn.execute(
        "UPDATE virtual_portfolio SET status = 'cancelled', close_reason = ? WHERE id = ?",
        (reason, order_id),
    )
    conn.commit()
    logger.info("Cancelled order #%d: %s", order_id, reason)


def _cancel_order(order_id: int, reason: str) -> None:
    """Cancel a pending order (acquires _write_lock)."""
    with _write_lock:
        _cancel_order_unlocked(order_id, reason)


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


def get_closed_positions(limit: int = 50) -> list[dict]:
    """Trades that finished, newest first.

    The dashboard could see what was bought and never what happened to
    it, which is the half that says whether any of the picking works.
    Includes cancelled orders: an order that expired without filling is a
    real outcome, not an absence of one.
    """
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM virtual_portfolio WHERE status NOT IN ('pending', 'open') "
        "ORDER BY COALESCE(close_date, order_date) DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def open_position(
    *,
    code: str,
    name: str,
    theme: str,
    open_date: str,
    open_price: float,
    stop_loss: float | None = None,
    target_price: float | None = None,
    source: str = "manual",
    reason: str = "",
    shares: int | None = None,
) -> int | None:
    """Compatibility helper to insert an already-filled virtual position."""
    if open_price <= 0:
        return None
    if shares is None:
        shares = _calc_shares(open_price, TOTAL_CAPITAL * MAX_POSITION_PCT)
    if shares <= 0:
        return None

    with _write_lock:
        conn = _get_conn()
        existing = conn.execute(
            "SELECT id FROM virtual_portfolio WHERE code = ? AND status IN ('pending', 'open')",
            (code,),
        ).fetchone()
        if existing:
            return None
        cur = conn.execute(
            "INSERT INTO virtual_portfolio "
            "(code, name, theme, order_date, open_date, open_price, shares, "
            " stop_loss, target_price, status, source, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)",
            (code, name, theme, open_date, open_date, open_price, shares,
             stop_loss, target_price, source, reason),
        )
        conn.commit()
        return cur.lastrowid


def _round_lot(shares: int) -> int:
    """Down to a whole 手. A-shares sell in multiples of 100."""
    return (int(shares) // LOT_SIZE) * LOT_SIZE


def _partial_close(conn, position_id: int, open_price: float,
                   close_price: float, sell: int, held: int,
                   booked: float, close_reason: str) -> bool:
    """Book a trim: realise part, keep the rest open at the same cost.

    The realised amount accumulates in return_amount so a position trimmed
    twice and then closed still totals what it actually earned. return_pct
    stays the per-share figure on this sale, which is what the review
    reads when it asks whether the trim was a good call.
    """
    net = _estimate_net_close_result(open_price, close_price, sell)
    remaining = held - sell
    conn.execute(
        "UPDATE virtual_portfolio SET shares = ?, return_amount = ?, "
        "close_reason = ? WHERE id = ?",
        (remaining, round(booked + net["return_amount"], 2),
         close_reason[:200], position_id))
    conn.commit()
    logger.info("Trimmed #%d: 卖出 %d股 @ %.2f (净 %+.2f%%, %+.0f元)，"
                "剩余 %d股 — %s",
                position_id, sell, close_price, net["return_pct"],
                net["return_amount"], remaining, close_reason)
    # No learning feedback here on purpose: the thesis is still live and
    # its outcome is not decided. The full close writes the label.
    return True


def add_to_position(position_id: int, *, price: float, reason: str,
                    size_pct: float | None = None) -> dict | None:
    """Buy the second tranche of a position the agent wants more of.

    How many times to add, and how much each time, is the agent's call —
    the only bound is the per-stock backstop. Returns None when the room
    is gone, which is a legitimate answer and not a failure.

    ``size_pct`` is the share of the book to add; without one it adds the
    default position size again.
    """
    with _write_lock:
        conn = _get_conn()
        pos = conn.execute(
            "SELECT code, name, theme, open_price, shares, reason "
            "FROM virtual_portfolio WHERE id = ? AND status = 'open'",
            (position_id,)).fetchone()
        if not pos or price <= 0:
            return None

        held = pos["shares"] or 0
        cost = (pos["open_price"] or 0) * held
        wanted = TOTAL_CAPITAL * max(0.005, min(1.0, size_pct
                                                or DEFAULT_POSITION_PCT))

        room = min(
            wanted,
            get_available_capital(),
            TOTAL_CAPITAL * MAX_POSITION_WITH_ADD - cost,
            TOTAL_CAPITAL * MAX_THEME_PCT - get_theme_exposure(pos["theme"] or ""),
            max(0.0, get_sentiment_exposure_limit()
                - (TOTAL_CAPITAL - get_available_capital())),
        )
        add_shares = _calc_shares(price, max(0.0, room))
        if add_shares <= 0:
            logger.info("Add refused for %s: no room (%.0f元)", pos["code"], room)
            return None

        total = held + add_shares
        # Weighted average: the position's cost basis is now both buys, and
        # every return the monitor computes has to be against that or the
        # add would flatter the numbers for free.
        avg = round((cost + add_shares * price) / total, 3)
        conn.execute(
            "UPDATE virtual_portfolio SET shares = ?, open_price = ?, "
            "reason = ? WHERE id = ?",
            (total, avg, f"{pos['reason'] or ''} | {reason}"[:300], position_id))
        conn.commit()

    logger.info("Added to #%d %s: +%d股 @ %.2f → %d股 均价%.2f — %s",
                position_id, pos["code"], add_shares, price, total, avg, reason)
    return {"shares": add_shares, "avg_price": avg, "total_shares": total}


def close_position(
    position_id: int,
    *,
    close_price: float,
    close_reason: str,
    shares: int | None = None,
) -> bool:
    """Close a position, or part of one. Returns True on success.

    ``shares`` makes 减仓 a real action. Without it the agent could say
    "trim" and the system recorded a full exit, so every partial-exit
    decision it ever made was executed as something else and graded as
    something else — the one sizing skill it was allowed to express was
    quietly discarded.

    A partial close books the realised P&L on the shares sold and leaves
    the rest open at the same cost basis. Cost basis does not move on a
    sale: the remaining shares were bought at the original price, and
    re-averaging it would flatter the survivors and hide the trim in the
    numbers.
    """
    with _write_lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT open_price, shares, return_amount FROM virtual_portfolio "
            "WHERE id = ?", (position_id,),
        ).fetchone()
        if not row:
            logger.warning("close_position: position #%d not found", position_id)
            return False

        open_price = row["open_price"] or 0
        held = row["shares"] or 0
        sell = held if shares is None else min(max(0, _round_lot(shares)), held)
        if sell <= 0:
            logger.warning("close_position #%d: nothing to sell (asked %s of %d)",
                           position_id, shares, held)
            return False

        if sell < held:
            return _partial_close(conn, position_id, open_price, close_price,
                                  sell, held, row["return_amount"] or 0,
                                  close_reason)

        shares = held
        net_result = _estimate_net_close_result(open_price, close_price, shares)
        return_pct = net_result["return_pct"]
        return_amount = net_result["return_amount"]
        today = datetime.now().strftime("%Y-%m-%d")

        conn.execute(
            "UPDATE virtual_portfolio SET "
            "status = ?, close_date = ?, close_price = ?, return_pct = ?, "
            "return_amount = ?, close_reason = ? WHERE id = ?",
            (_status_from_reason(close_reason), today, close_price, return_pct,
             return_amount, close_reason, position_id),
        )
        conn.commit()
        logger.info("Closed #%d: %d股 @ %.2f → %.2f (net %+.2f%%, %+.0f元, friction %.0f元) %s",
                     position_id, shares, open_price, close_price,
                     return_pct, return_amount, net_result["costs"], close_reason)

    # Outside the write lock — _feed_close_to_learning takes it again.
    _feed_close_to_learning(position_id, return_pct, close_reason)
    return True


def _feed_close_to_learning(position_id: int, return_pct: float,
                            close_reason: str) -> None:
    """Write a realised round trip back into the learning layer.

    Without this the virtual portfolio and the learning system are
    parallel worlds: review.py grades predictions on next-day direction,
    which is not what the portfolio actually earned. A stop-out at -8%
    after a +2% first day counts as a hit under the old scheme.

    The realised net return — costs included, held to the actual exit —
    is the honest label, so it overwrites the prediction's outcome and
    drives the playbook stats. Best-effort: a failure here must never
    prevent a position from closing.
    """
    try:
        conn = _get_conn()
        pos = conn.execute(
            "SELECT code, open_date, theme FROM virtual_portfolio WHERE id = ?",
            (position_id,),
        ).fetchone()
        if not pos:
            return

        # The prediction that originated this order: same stock, on or
        # just before the fill. Orders can sit pending for
        # PENDING_EXPIRE_DAYS, so allow that much slack and take the
        # newest match.
        pred = conn.execute(
            "SELECT id, features_json FROM predictions "
            "WHERE code = ? AND date <= ? AND date >= date(?, ?) "
            "ORDER BY date DESC, id DESC LIMIT 1",
            (pos["code"], pos["open_date"], pos["open_date"],
             f"-{PENDING_EXPIRE_DAYS + 1} days"),
        ).fetchone()
        if not pred:
            return

        hit = 1 if return_pct > 0 else 0
        with _write_lock:
            conn.execute(
                "UPDATE predictions SET hit = ?, week_return = ?, review_note = ? "
                "WHERE id = ?",
                (hit, return_pct, f"实盘平仓 {return_pct:+.2f}% ({close_reason})",
                 pred["id"]),
            )
            conn.commit()

        # Feed the playbook the realised outcome rather than the
        # next-day proxy.
        try:
            import json as _json
            from alpha_agents.data.memory_store import record_playbook_trade
            feats = _json.loads(pred["features_json"] or "{}")
            pb_id = feats.get("playbook_id")
            if pb_id:
                record_playbook_trade(int(pb_id), hit=bool(hit),
                                      return_pct=return_pct)
        except Exception as e:
            logger.debug("Playbook feedback failed for #%d: %s", position_id, e)

        logger.info("Learning feedback: %s prediction #%d ← 实盘 %+.2f%%",
                    pos["code"], pred["id"], return_pct)
    except Exception as e:
        logger.debug("Close-to-learning feedback failed for #%d: %s",
                     position_id, e)


def _status_from_reason(reason: str) -> str:
    if "止损" in reason:
        return "stopped"
    if "止盈" in reason:
        return "target_hit"
    return "expired"


# ── Summaries ───────────────────────────────────────────────


# Re-exported so every existing caller keeps working. The split is about
# file size, not about the API — `from portfolio import get_portfolio_stats`
# reads exactly as it did.
from alpha_agents.data.portfolio_report import (  # noqa: E402
    format_portfolio_stats, get_open_positions_summary, get_portfolio_stats,
    get_today_changes_summary, parse_entry_zone, parse_stop_loss,
)

__all__ = [
    "format_portfolio_stats", "get_open_positions_summary",
    "get_portfolio_stats", "get_today_changes_summary",
    "parse_entry_zone", "parse_stop_loss",
]


# Re-exported so callers keep importing check_positions from portfolio.
# The split is about file size, not about the API.
from alpha_agents.data.position_monitor import (  # noqa: E402
    _check_add_position, _check_bearish_signals, _is_hard_exit,
    _is_phase_bearish, check_positions,
)
