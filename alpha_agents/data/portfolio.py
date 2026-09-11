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

from alpha_agents.data import (
    attribution, order_state, reservations, settlement, trade_ledger,
)
from alpha_agents.data.memory_store import _get_conn, _write_lock, get_theme_by_name
from alpha_agents.data.trader import DEFAULT_TRADER
# The exit slice lives in portfolio_exit: the friction model, the append-only
# exit legs, and the close path that books them. Re-exported here because entry
# sizing needs LOT_SIZE and because callers have always reached for
# close_position through this module.
from alpha_agents.data.portfolio_exit import (
    LOT_SIZE,
    SLIPPAGE_RATE,
    _blended_return_pct,
    _estimate_net_close_result,
    _status_from_reason,
    close_position,
)
# Sizing lives in portfolio_sizing: how big a position may be is a
# different question from whether it may exist, and splitting them is
# what kept this file under the ceiling when S5 added the intent
# wrappers. Imported at module scope rather than re-exported at the
# bottom because the implementations above call into them directly.
from alpha_agents.data.portfolio_sizing import (
    _calc_shares,
    _trader_pct,
    _wanted_pct,
    get_sentiment_exposure_limit,
)

logger = logging.getLogger(__name__)

PENDING_EXPIRE_DAYS = 2  # 挂单有效期（仅用于无主线的挂单，有主线的跟随主线生命周期）

# ── Capital Management ──────────────────────────────────────
# 100万，然后基本让开。
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
TOTAL_CAPITAL = int(os.environ.get("TOTAL_CAPITAL", "1000000"))

# Default when a pick says nothing about size. Not a cap.
DEFAULT_POSITION_PCT = float(os.environ.get("DEFAULT_POSITION_PCT", "0.03"))

# The backstop. Not a target, and not a budget the agent should aim at.
MAX_POSITION_PCT = float(os.environ.get("MAX_POSITION_PCT", "0.10"))
MAX_POSITION_WITH_ADD = MAX_POSITION_PCT
MAX_THEME_PCT = 0.30             # 单主线上限
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

# The strength a theme needs before a new order may be written against it.
#
# Read at creation *and* on every pending check, from this one constant so
# the two cannot drift apart. They used to be enforced only on the check,
# one cycle late: of 97 cancelled orders, ~80 died as 主线走弱 on themes
# that were already at strength 0-2 when the order was written. The system
# was creating orders it had already decided it would not hold, then
# cancelling them, then counting those cancellations as evidence about
# entry prices.
MIN_THEME_STRENGTH = 4


def _cluster_room(theme: str, trader_id: str = DEFAULT_TRADER) -> float:
    """Headroom for this theme's correlated cluster, or unlimited on error.

    Falling open rather than closed: a failure in the correlation lookup
    must not silently stop the portfolio from trading. The per-theme and
    per-stock caps still apply underneath.
    """
    try:
        from alpha_agents.data.portfolio_risk import cluster_room
        return cluster_room(theme, trader_id)
    except Exception as e:
        logger.warning("Cluster check unavailable for %r: %s", theme, e)
        return float("inf")


def _drawdown_blocks_new_risk(trader_id: str = DEFAULT_TRADER) -> bool:
    """True only when this trader is measurably deep in a drawdown.

    Falls open on any failure: a broken risk lookup must not quietly stop
    the portfolio from trading, which would look exactly like a market
    with no opportunities.
    """
    try:
        from alpha_agents.data.portfolio_risk import current_drawdown
        return bool(current_drawdown(trader_id).get("blocked"))
    except Exception as e:
        logger.warning("Drawdown check unavailable: %s", e)
        return False


def trader_capital(trader_id: str = DEFAULT_TRADER) -> float:
    """How much this trader was given. Its own pot, not a share of one.

    Two strategies drawing from a single pot would measure ordering
    rather than skill: whichever filled first would starve the other and
    the comparison the traders exist for would be meaningless.
    """
    if trader_id == DEFAULT_TRADER:
        return float(TOTAL_CAPITAL)
    from alpha_agents.data.trader import get_trader
    return float(get_trader(trader_id).capital)


def account_capital() -> float:
    """Every trader's capital added up — the account, not a book.

    A legacy trader counts only what it still has invested. Its remaining
    cash will never be deployed — it is winding down and buys nothing — so
    including the full pot would show an account with hundreds of
    thousands "available" that no trader is allowed to spend.

    Only the dashboard wants this. No trading decision may use it: a
    trader that sized against the account total would be spending the
    others' money.
    """
    from alpha_agents.data.trader import DEFAULT_TRADER as _D, load_traders
    traders = load_traders()
    if len(traders) == 1 and traders[0].id == _D:
        return float(TOTAL_CAPITAL)
    total = 0.0
    for t in traders:
        if t.legacy:
            total += get_invested_capital(t.id)
        else:
            total += t.capital
    return float(total)


def get_available_capital(trader_id: str = DEFAULT_TRADER) -> float:
    """Remaining cash for one trader, after every commitment has spoken.

    The pot, plus what this trader has actually realised on closed
    positions, minus what is still tied up in its open book *and* minus
    what its pending orders have reserved. The old ``capital −
    positions`` reading left realised P&L out entirely, so a trader
    that had made money could not spend it and one that had lost money
    could still spend money it no longer had. Adding reservations to
    the subtraction closes the second half of the same gap: two
    pending orders could each respect the pre-reservation number, fill,
    and the pot would be the thing that was lying.

    The reservation subtraction is the backstop on the held row plus
    the un-absorbed part of any consumed row. Released rows contribute
    nothing — the cash is back.

    Cash-side T+1 is subtracted here too: sales settled today don't
    become spendable until exit_date + 1. ``unreleased_pending_total``
    is the cash the trader has earned but the broker is still holding.

    Legacy aggregate results are included as compatibility estimates,
    never reconstructed fills. This is not yet a fee-at-fill cash ledger.
    """
    return (trader_capital(trader_id)
            + trade_ledger.realized_total(_get_conn(), trader_id)
            - get_invested_capital(trader_id)
            - reservations.unconsumed_total(_get_conn(), trader_id)
            - settlement.unreleased_pending_total(_get_conn(), trader_id))


def get_total_capital(trader_id: str = DEFAULT_TRADER) -> float:
    """What the trader owns, whether or not it can deploy it yet.

    ``capital + realized`` — the mandate plus everything won or lost.
    The decomposition against ``get_available_capital`` is::

        total = available + invested + reservations + pending

    i.e. the only things separating "owns" from "can spend" are the open
    book (cash converted to shares at cost), the cash earmarked against
    pending orders, and the sale proceeds still in transit under the
    cash-side T+1 rule. Nothing is double-counted: a sale moves cash
    from ``available`` to ``pending`` and total does not move at all —
    which is the point, the trader has not gained anything from the sale
    that it did not already have.

    Reports that say "what do I actually have" read this. Trading
    decisions must read ``get_available_capital`` instead: sizing
    against ``total`` would spend money the broker is still holding.
    """
    return (trader_capital(trader_id)
            + trade_ledger.realized_total(_get_conn(), trader_id))


def get_invested_capital(trader_id: str = DEFAULT_TRADER) -> float:
    """Open cost exposure, independent of realized gains or losses."""
    row = _get_conn().execute(
        "SELECT COALESCE(SUM(open_price * shares), 0) FROM virtual_portfolio "
        "WHERE status='open' AND trader_id=?", (trader_id,),
    ).fetchone()
    return float(row[0])


def get_theme_exposure(theme: str, trader_id: str = DEFAULT_TRADER) -> float:
    """Market value one trader has in a theme."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT open_price, shares FROM virtual_portfolio "
        "WHERE status = 'open' AND theme = ? AND trader_id = ?",
        (theme, trader_id),
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


def _valid_prediction(conn, prediction_id: int | None, code: str, trader_id: str) -> bool:
    if prediction_id is None:
        return True
    if type(prediction_id) is not int or prediction_id <= 0:
        return False
    return conn.execute(
        "SELECT 1 FROM predictions WHERE id=? AND code=? AND trader_id=?",
        (prediction_id, code, trader_id),
    ).fetchone() is not None


def _create_pending_order_impl(
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
    trader_id: str = DEFAULT_TRADER,
    prediction_id: int | None = None,
    thesis_id: int | None = None,
) -> int | None:
    """Create a pending order (挂单). Triggered when price enters entry zone.

    ``prediction_id`` names the call this order came from. It is optional
    because a manual order has no forecast behind it, but when the caller
    knows the prediction it must pass it: the closing path no longer guesses
    the link from stock and date, so an order created without one closes
    with no learning feedback rather than with a plausible but wrong one.

    ``thesis_id`` names the idea the order serves. Both links are stored on
    the order, so the chain ``thesis → order → exit`` is a set of columns
    rather than a search, and both are checked for ownership before the row
    exists — a mismatched link is refused here, not discovered later.

    The decision's information boundary is frozen at the same moment: the
    order is the decision, and a boundary recorded afterwards is a
    reconstruction, not a record.

    Returns order id, or None if duplicate/rejected.
    """
    with _write_lock:
        conn = _get_conn()
        if not _valid_prediction(conn, prediction_id, code, trader_id):
            logger.warning("Rejected prediction link %s for %s/%s", prediction_id, trader_id, code)
            return None
        if not attribution.valid_thesis(conn, thesis_id, code, trader_id):
            logger.warning("Rejected thesis link %s for %s/%s — not this "
                           "book's idea about this stock",
                           thesis_id, trader_id, code)
            return None
        # No duplicate: same stock pending or open
        # Scoped to the trader: two traders holding the same stock is the
        # comparison working, not a duplicate. Only one book may hold it
        # twice.
        existing = conn.execute(
            "SELECT id FROM virtual_portfolio WHERE code = ? AND trader_id = ? "
            "AND status IN ('pending', 'open')",
            (code, trader_id),
        ).fetchone()
        if existing:
            logger.info("Order/position already exists for %s %s (%s), skipping",
                        code, name, trader_id)
            return None

        resolved = resolve_theme(theme)
        if resolved is None:
            logger.warning(
                "Rejected order %s %s: theme %r is not a tracked theme line. "
                "check_pending_orders would cancel it next cycle anyway.",
                code, name, theme)
            return None
        theme = resolved

        # The same strength bar check_pending_orders applies, applied at
        # creation instead of one cycle later. Of 97 cancelled orders, ~80
        # died as 主线走弱 — and the theme was already at strength 0-2 when
        # the order was written. The system was creating orders it had
        # already decided it would not hold, then cancelling them, then
        # counting the cancellations as evidence about entry prices.
        weak = _theme_too_weak(theme)
        if weak:
            logger.info("Rejected order %s %s: %s — 下一轮也会被撤，不如不建",
                        code, name, weak)
            return None

        cursor = conn.execute(
            "INSERT INTO virtual_portfolio "
            "(code, name, theme, order_date, open_date, open_price, entry_low, entry_high, "
            " stop_loss, target_price, expire_days, status, source, reason, trader_id, "
            " prediction_id, thesis_id) "
            "VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)",
            (code, name, theme, order_date, order_date, entry_low, entry_high,
             stop_loss, target_price, PENDING_EXPIRE_DAYS, source, reason,
             trader_id, prediction_id, thesis_id),
        )
        order_id = cursor.lastrowid
        # Freeze what this decision was allowed to know, while it is still
        # the decision rather than a memory of it. The declared inputs are
        # the order's own terms, so a later edit to the thesis cannot
        # retcon the basis on which it was placed.
        try:
            attribution.freeze(
                conn, trader_id=trader_id, code=code,
                information_cutoff=order_date, decided_at=order_date,
                payload={
                    "action": "open",
                    "entry_low": entry_low, "entry_high": entry_high,
                    "stop_loss": stop_loss, "target_price": target_price,
                    "expire_days": PENDING_EXPIRE_DAYS,
                    "source": source, "reason": reason,
                },
                thesis_id=thesis_id, order_id=order_id,
                prediction_id=prediction_id,
                sources=[source] if source else [],
            )
        except Exception as e:
            # The order is the trade; the boundary is the audit. Losing the
            # audit must not stop the trade — but it must be visible.
            logger.warning("Could not freeze decision boundary for order "
                           "#%s: %s", order_id, e)
        # Reserve the worst-case fill cost against the order. The held row
        # is what keeps a second pending order from spending the same
        # cash; computing it here, before commit, means the order and
        # the reservation are written together or not at all.
        reservation_amount = (trader_capital(trader_id) * MAX_POSITION_PCT
                              * (1 + SLIPPAGE_RATE))
        reservations.reserve_for_order(
            conn, order_id=order_id, trader_id=trader_id,
            code=code, amount=reservation_amount,
            reason="pending-order backstop")
        conn.commit()

        zone = f"{entry_low:.2f}-{entry_high:.2f}" if entry_low and entry_high else "市价"
        logger.info("Pending order: %s %s 介入区间%s 止损%s (%s)",
                     code, name, zone, stop_loss or "无", source)
        return cursor.lastrowid


def _theme_too_weak(theme: str) -> str | None:
    """Why this theme cannot carry a new order, or None if it can.

    Reads the same thresholds check_pending_orders enforces, so the two
    cannot disagree — an order accepted here and cancelled there is a
    wasted capital slot and a fake data point about entry pricing.

    Falls open on a lookup failure: a broken theme read must not stop the
    system from trading, and the cancel path still runs a cycle later.
    """
    if not theme:
        return None
    try:
        row = get_theme_by_name(theme)
    except Exception as e:
        logger.debug("Theme strength check unavailable for %r: %s", theme, e)
        return None
    if not row:
        return None
    status = row.get("status")
    if status in ("declining", "archived"):
        return f"主线已{status}({theme})"
    strength = row.get("strength") or 0
    if strength < MIN_THEME_STRENGTH:
        return f"主线太弱({theme}强度{strength}<{MIN_THEME_STRENGTH})"
    return None


def check_pending_orders(
    realtime_prices: dict[str, float],
    today: str,
    trader_id: str | None = None,
) -> list[dict]:
    """Check pending orders against realtime prices. Fill if price in entry zone.

    One trader at a time. The conviction sort below spends finite capital
    in order, so running two books through one pass would let whichever
    sorted first spend the other's money.

    Returns list of fill alerts.
    """
    conn = _get_conn()
    orders = conn.execute(
        "SELECT * FROM virtual_portfolio WHERE status = 'pending'"
        + (" AND trader_id = ?" if trader_id else ""),
        [trader_id] if trader_id else []
    ).fetchall()
    # Highest conviction first. Capital is finite and this loop spends it
    # in order, so whatever ran first used to win — by row id, which is
    # arrival order and carries no information. 上海机电 was refused with
    # 232元 left not because it was the weakest idea but because it was
    # inserted last. When the book is full, it should be full of the ideas
    # the agent believed in.
    orders = sorted(orders, key=lambda o: -_order_conviction(
        o["code"], o["trader_id"] or DEFAULT_TRADER))

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
        # - Theme weak (strength < MIN_THEME_STRENGTH) or declining/archived → cancel
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
                if (theme.get("strength") or 0) < MIN_THEME_STRENGTH:
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


def _order_conviction(code: str, trader_id: str = DEFAULT_TRADER) -> float:
    """The stated conviction behind an order, or a neutral 0.5.

    Neutral rather than zero for an order with no thesis: those predate
    theses entirely, and sorting them to the back would starve them of
    capital for a reason that is about the code's history, not the idea.
    """
    try:
        from alpha_agents.data.thesis import get_active
        theses = get_active(code=code, trader_id=trader_id)
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
        theses = [t for t in T.get_active(
            code=code, trader_id=order.get("trader_id") or DEFAULT_TRADER)
            if t.position_id is None]
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
    """Convert a pending order to an open position at fill_price.

    Every limit below is measured against the order's own trader: its
    capital, its exposure, its drawdown. A limit read off the pooled book
    would let one strategy's positions decide whether another gets to
    open one.
    """
    code = order["code"]
    name = order.get("name", "")
    theme = order.get("theme", "")
    trader_id = order.get("trader_id") or DEFAULT_TRADER
    capital = trader_capital(trader_id)

    with _write_lock:
        # Capital checks (including sentiment-based total exposure limit)
        # Must be inside lock to prevent race conditions with concurrent fills
        available = get_available_capital(trader_id)
        sentiment_cap = get_sentiment_exposure_limit(trader_id)
        invested = get_invested_capital(trader_id)
        sentiment_room = max(0, sentiment_cap - invested)  # How much more we can invest given sentiment

        # Size by conviction rather than filling every position to the cap.
        # A flat 15% everywhere throws away half of what a trader is for:
        # being right more often is worth less than being bigger when
        # right. The thesis carries a conviction the agent stated when it
        # opened the idea; with no thesis this falls back to the cap and
        # behaves exactly as before.
        # What the agent asked for, capped by the backstop. Conviction no
        # longer scales this behind its back — it says the number itself.
        max_pos_pct = _trader_pct(trader_id, "max_position_pct",
                                  MAX_POSITION_PCT)
        max_per_stock = min(capital * _wanted_pct(code, trader_id),
                            capital * max_pos_pct)
        max_for_theme = (capital * MAX_THEME_PCT
                         - get_theme_exposure(theme, trader_id))
        # Themes that share most of their constituents are one bet. The
        # per-theme cap counted 金属铜 and 小金属概念 as two and would let
        # them take 60% between them while sharing 6 of 10 names.
        cluster_cap = _cluster_room(theme, trader_id)
        max_amount = min(available, max_per_stock, max(0, max_for_theme),
                         sentiment_room, cluster_cap)

        # Portfolio drawdown gates *new* risk and never forces an exit.
        # Liquidating at a drawdown level sells the bottom, and in a system
        # built to learn from resolved theses it would destroy the samples
        # before they resolve. Positions already open keep their own stops.
        if _drawdown_blocks_new_risk(trader_id):
            _cancel_order_unlocked(order["id"], "组合回撤触及上限，暂停开新仓")
            return {"type": "cancelled", "code": code, "name": name,
                    "reason": "组合回撤触及上限"}

        shares = _calc_shares(fill_price, max_amount)
        if shares == 0:
            # The size the agent asked for is a target, not a ceiling —
            # the ceiling is MAX_POSITION_PCT. At the default 3% of 50万,
            # one lot of anything above ~150元 costs more than the target
            # and the order was cancelled for 资金不足: 太辰光 needed
            # 20278元 against a 15000元 target while the backstop sat at
            # 50000元. That silently excluded every expensive stock from
            # the book, which is a selection bias in the learning data
            # with nothing to do with the agent's judgement.
            one_lot = fill_price * LOT_SIZE
            ceiling = min(available, capital * max_pos_pct,
                          max(0, max_for_theme), sentiment_room, cluster_cap)
            if one_lot <= ceiling:
                shares = LOT_SIZE
                logger.info("%s: 一手 %.0f元 超过目标 %.0f元，但在上限 %.0f元"
                            "之内 — 按一手成交", code, one_lot, max_amount,
                            ceiling)

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
        cur = conn.execute(
            "SELECT status FROM virtual_portfolio WHERE id = ?",
            (order["id"],),
        ).fetchone()
        if not cur:
            logger.info("Order #%s disappeared before fill; no-op",
                        order["id"])
            return None
        # _fill_order is the pending-order → position path. The state
        # machine permits open → open (a partial exit through
        # close_position), but that is a different operation with a
        # different UPDATE; running it here would silently overwrite an
        # open position's open_date, open_price and shares. Domain guard
        # first, state machine as the backstop.
        if cur["status"] != order_state.PENDING:
            logger.info("Order #%s no longer pending (%s), fill rejected",
                        order["id"], cur["status"])
            return {
                "type": "cancelled", "code": code, "name": name,
                "reason": f"fill rejected: row is {cur['status']}, not pending",
            }
        try:
            target = order_state.assert_transition(cur["status"],
                                                  order_state.OPEN)
        except order_state.IllegalTransition as e:
            # Defensive backstop: the domain guard above already passed,
            # so this would only trigger if a new state were added that
            # is not PENDING but still allows → OPEN. Honour the refusal
            # rather than write on top of whoever finished the row first.
            logger.info("Order #%s no longer fillable (%s): %s",
                        order["id"], cur["status"], e)
            return {
                "type": "cancelled", "code": code, "name": name,
                "reason": f"fill rejected: row already {cur['status']}",
            }
        conn.execute(
            "UPDATE virtual_portfolio SET "
            "status = ?, open_date = ?, open_price = ?, shares = ? "
            "WHERE id = ?",
            (target, fill_date, fill_price, shares, order["id"]),
        )
        # The held reservation is now the actual cost. Entry_high
        # over-estimated; the over-reserve is returned to available.
        actual_cost = shares * fill_price * (1 + SLIPPAGE_RATE)
        reservations.consume_reservation(
            conn, order_id=order["id"], actual_cost=actual_cost)
        # T+1 share-side: each fill is its own settlement lot. settle_date
        # is fill_date + 1 calendar day, so the position cannot be sold
        # back the same day the order fills. The legacy open_date check
        # keeps working for rows that pre-date the S4 cutover (no lot,
        # fall back to open_date < today).
        settlement.create_lot(
            conn, position_id=order["id"], trader_id=trader_id,
            code=code, shares=shares, open_date=fill_date,
            open_price=fill_price, source="initial")
        conn.commit()

    # Bind the thesis to the position it just became. Until the fill the
    # thesis is an idea; from here the monitor evaluates it against a real
    # cost basis every cycle, and an unbound thesis would be checked
    # against nothing.
    #
    # Scoped by trader, and the order's own thesis wins when it named one.
    # This used to scan every live thesis on the code and take the first
    # unfilled one: with two traders holding the same stock, the second
    # fill could bind the first trader's idea, after which the monitor
    # evaluated one book's thesis against the other book's cost basis and
    # the review attributed the result to an idea that never traded.
    try:
        from alpha_agents.data.thesis import attach_position, get_active
        candidates = [t for t in get_active(code=code, trader_id=trader_id)
                      if t.position_id is None]
        wanted = order.get("thesis_id")
        if wanted is not None:
            bound = [t for t in candidates if t.id == wanted]
            if not bound:
                logger.warning(
                    "Order #%s names thesis %s, which is not a live unfilled "
                    "idea of %s on %s — no binding", order["id"], wanted,
                    trader_id, code)
            candidates = bound
        if candidates:
            attach_position(candidates[-1].id, order["id"])
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
    """Cancel a pending order. Caller must already hold _write_lock.

    A cancel request is meaningful only while the row is still pending (or
    a cancellation is in flight). Once the row has been filled or already
    finished some other way, the first finisher owns its history and a
    later cancel is a logged no-op — overwriting a closed row's record
    would be the silent history edit the state machine exists to stop.
    """
    conn = _get_conn()
    current = conn.execute(
        "SELECT status FROM virtual_portfolio WHERE id = ?", (order_id,),
    ).fetchone()
    if not current:
        logger.warning("Cancel requested for unknown order #%d", order_id)
        return
    current_status = current["status"]
    if current_status not in (order_state.PENDING, order_state.CANCEL_PENDING):
        logger.info("Order #%d no longer pending (%s), cancel ignored",
                    order_id, current_status)
        return
    target = order_state.assert_transition(current_status,
                                          order_state.CANCELLED)
    conn.execute(
        "UPDATE virtual_portfolio SET status = ?, close_reason = ? WHERE id = ?",
        (target, reason, order_id),
    )
    # Release the held cash back to available. Only pending /
    # cancel-pending rows reach this point (the earlier guard
    # short-circuits everything else), so the reservation is held and
    # this is a release, not a refund of an already-consumed one.
    reservations.release_reservation(conn, order_id=order_id, reason=reason)
    conn.commit()
    logger.info("Cancelled order #%d: %s", order_id, reason)


def _cancel_order(order_id: int, reason: str) -> None:
    """Cancel a pending order through the intent path (compat alias).

    Internal callers — the weak-theme, expiry and run-away-price cancels
    in ``check_pending_orders`` — reach the same audit trail a
    business-initiated cancel does.
    """
    from alpha_agents.data.portfolio_intent import cancel_order
    cancel_order(order_id, reason)


def _cancel_order_impl(order_id: int, reason: str) -> None:
    """Cancel a pending order (acquires _write_lock)."""
    with _write_lock:
        _cancel_order_unlocked(order_id, reason)


def _trader_filter(trader_id: str | None) -> str:
    """SQL fragment scoping a query to one trader, or to all of them.

    None means every trader on purpose: the dashboard, the drawdown gate
    and the correlation check all reason about total exposure, and
    scoping those to one book would understate the risk actually taken.
    """
    return " AND trader_id = ?" if trader_id else ""


def _trader_args(trader_id: str | None) -> tuple:
    return (trader_id,) if trader_id else ()


def get_pending_orders(trader_id: str | None = None) -> list[dict]:
    """Get all pending orders."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM virtual_portfolio WHERE status = 'pending'"
        + _trader_filter(trader_id) + " ORDER BY order_date",
        _trader_args(trader_id)
    ).fetchall()
    return [dict(r) for r in rows]


# ── Open Positions ──────────────────────────────────────────

def get_open_positions(trader_id: str | None = None) -> list[dict]:
    """Get all currently open (filled) positions."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM virtual_portfolio WHERE status = 'open'"
        + _trader_filter(trader_id) + " ORDER BY open_date",
        _trader_args(trader_id)
    ).fetchall()
    return [dict(r) for r in rows]


def get_closed_positions(limit: int = 50,
                         trader_id: str | None = None) -> list[dict]:
    """Trades that finished, newest first.

    The dashboard could see what was bought and never what happened to
    it, which is the half that says whether any of the picking works.
    Includes cancelled orders: an order that expired without filling is a
    real outcome, not an absence of one.
    """
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM virtual_portfolio WHERE status NOT IN ('pending', 'open')"
        + _trader_filter(trader_id) +
        " ORDER BY COALESCE(close_date, order_date) DESC, id DESC LIMIT ?",
        _trader_args(trader_id) + (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def _open_position_impl(
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
    trader_id: str = DEFAULT_TRADER,
    prediction_id: int | None = None,
    thesis_id: int | None = None,
) -> int | None:
    """Compatibility helper to insert an already-filled virtual position.

    Takes the same attribution links as ``create_pending_order`` and checks
    them the same way: a position that cannot name the idea behind it is a
    position whose result nobody can learn from.
    """
    if not trade_ledger.positive_price(open_price):
        return None
    if shares is None:
        shares = _calc_shares(open_price, trader_capital(trader_id) * MAX_POSITION_PCT)
    if type(shares) is not int or shares <= 0:
        return None

    with _write_lock:
        conn = _get_conn()
        if not _valid_prediction(conn, prediction_id, code, trader_id):
            logger.warning("Rejected prediction link %s for %s/%s", prediction_id, trader_id, code)
            return None
        if not attribution.valid_thesis(conn, thesis_id, code, trader_id):
            logger.warning("Rejected thesis link %s for %s/%s — not this "
                           "book's idea about this stock",
                           thesis_id, trader_id, code)
            return None
        existing = conn.execute(
            "SELECT id FROM virtual_portfolio WHERE code = ? AND trader_id=? "
            "AND status IN ('pending', 'open')",
            (code, trader_id),
        ).fetchone()
        if existing:
            return None
        cur = conn.execute(
            "INSERT INTO virtual_portfolio "
            "(code, name, theme, order_date, open_date, open_price, shares, "
            " stop_loss, target_price, status, source, reason, trader_id, "
            " prediction_id, thesis_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)",
            (code, name, theme, open_date, open_date, open_price, shares,
             stop_loss, target_price, source, reason, trader_id,
             prediction_id, thesis_id),
        )
        position_id = cur.lastrowid
        try:
            attribution.freeze(
                conn, trader_id=trader_id, code=code,
                information_cutoff=open_date, decided_at=open_date,
                payload={"action": "open", "open_price": open_price,
                         "shares": shares, "stop_loss": stop_loss,
                         "target_price": target_price, "source": source,
                         "reason": reason},
                thesis_id=thesis_id, order_id=position_id,
                prediction_id=prediction_id,
                sources=[source] if source else [],
            )
        except Exception as e:
            logger.warning("Could not freeze decision boundary for position "
                           "#%s: %s", position_id, e)
        # T+1 share-side: the compatibility helper inserts an
        # already-filled row, so it must also create the matching lot
        # (legacy open_position callers expected open_date < today to
        # gate T+1 — now the lot does the same job mechanically).
        settlement.create_lot(
            conn, position_id=position_id, trader_id=trader_id,
            code=code, shares=shares, open_date=open_date,
            open_price=open_price, source="initial")
        conn.commit()
        return position_id


def _round_lot(shares: int) -> int:
    """Down to a whole 手. A-shares sell in multiples of 100."""
    return (int(shares) // LOT_SIZE) * LOT_SIZE


def _add_to_position_impl(position_id: int, *, price: float, reason: str,
                          size_pct: float | None = None,
                          recalc_stop: bool = False) -> dict | None:
    """Buy the second tranche of a position the agent wants more of.

    How many times to add, and how much each time, is the agent's call —
    the only bound is the per-stock backstop. Returns None when the room
    is gone, which is a legitimate answer and not a failure.

    ``size_pct`` is the share of the book to add; without one it adds the
    default position size again.

    ``recalc_stop`` keeps the stop the same percentage below the new
    (lower) average. It is on for the automated pullback top-up, off for
    the agent's own adds — see the wrapper.
    """
    with _write_lock:
        conn = _get_conn()
        pos = conn.execute(
            "SELECT code, name, theme, open_price, shares, reason, trader_id, "
            "stop_loss FROM virtual_portfolio WHERE id = ? AND status = 'open'",
            (position_id,)).fetchone()
        if not pos or price <= 0:
            return None

        trader_id = pos["trader_id"] or DEFAULT_TRADER
        capital = trader_capital(trader_id)
        held = pos["shares"] or 0
        cost = (pos["open_price"] or 0) * held
        default_pct = _trader_pct(trader_id, "default_size_pct",
                                  DEFAULT_POSITION_PCT)
        wanted = capital * max(0.005, min(1.0, size_pct or default_pct))
        available = get_available_capital(trader_id)

        room = min(
            wanted,
            available,
            capital * MAX_POSITION_WITH_ADD - cost,
            capital * MAX_THEME_PCT
            - get_theme_exposure(pos["theme"] or "", trader_id),
            max(0.0, get_sentiment_exposure_limit(trader_id)
                - get_invested_capital(trader_id)),
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
        # ``recalc_stop`` keeps the stop the same fraction below the new
        # average: averaging down must not widen the risk per share. Only
        # the automated top-up sets it; the agent's own adds leave the
        # stop where the agent put it.
        new_stop = None
        if recalc_stop:
            old_stop = pos["stop_loss"] or 0
            old_open = pos["open_price"] or 0
            if old_open > 0 and old_stop > 0:
                new_stop = round(
                    avg * (1 - (old_open - old_stop) / old_open), 2)
        if new_stop is not None:
            conn.execute(
                "UPDATE virtual_portfolio SET shares = ?, open_price = ?, "
                "stop_loss = ?, reason = ? WHERE id = ?",
                (total, avg, new_stop,
                 f"{pos['reason'] or ''} | {reason}"[:300], position_id))
        else:
            conn.execute(
                "UPDATE virtual_portfolio SET shares = ?, open_price = ?, "
                "reason = ? WHERE id = ?",
                (total, avg, f"{pos['reason'] or ''} | {reason}"[:300],
                 position_id))
        # T+1 share-side: the add gets its own lot so its shares are
        # sellable only from add_date + 1 onward. Without this, the
        # entire position looked like one old batch on the monitor's
        # T+1 check, and the agent could sell today's shares today.
        add_date = datetime.now().strftime("%Y-%m-%d")
        settlement.create_lot(
            conn, position_id=position_id, trader_id=trader_id,
            code=pos["code"], shares=add_shares, open_date=add_date,
            open_price=price, source="add")
        conn.commit()

    logger.info("Added to #%d %s: +%d股 @ %.2f → %d股 均价%.2f — %s",
                position_id, pos["code"], add_shares, price, total, avg, reason)
    return {"shares": add_shares, "avg_price": avg, "total_shares": total,
            "stop_loss": new_stop}


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


# The public write API, split out when the S5 intent wrappers pushed this
# file past the 1200-line ceiling. Re-exported so `from portfolio import
# create_pending_order` keeps working.
#
# Imported *before* position_monitor, which imports it: the bottom of this
# file is a dependency chain, not a list, and one of the modules below
# reads `add_to_position` back out of this namespace. Ordering it the
# other way round only worked while position_monitor happened to be the
# only consumer.
from alpha_agents.data.portfolio_intent import (  # noqa: E402
    add_to_position, cancel_order, create_pending_order, open_position,
)


# Re-exported so callers keep importing check_positions from portfolio.
# The split is about file size, not about the API.
from alpha_agents.data.position_monitor import (  # noqa: E402
    _check_add_position, _check_bearish_signals, _is_hard_exit,
    _is_phase_bearish, check_positions,
)
