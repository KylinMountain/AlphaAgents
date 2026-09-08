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
TOTAL_CAPITAL = 100_000          # 总资金 10万
MAX_POSITION_PCT = 0.15          # 单票初始建仓最多占总资金 15%
MAX_POSITION_WITH_ADD = 0.30     # 补仓后单票最多占总资金 30%
MAX_THEME_PCT = 0.30             # 同主线最多占总资金 30%
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


# Conviction maps onto [floor, 1.0] of the per-stock cap. The floor is not
# zero: an idea the agent thought worth opening at all still deserves
# enough size to produce a readable outcome, and a 2% position teaches the
# learning layer nothing when it works.
MIN_CONVICTION_FACTOR = 0.45


def _conviction_factor(code: str) -> float:
    """How much of the per-stock cap this idea has earned, from its thesis.

    Returns 1.0 when there is no thesis — every caller predates them, and
    a missing thesis must not silently shrink a position.
    """
    try:
        from alpha_agents.data.thesis import get_active
        theses = get_active(code=code)
    except Exception as e:
        logger.debug("Conviction lookup failed for %s: %s", code, e)
        return 1.0
    if not theses:
        return 1.0
    conviction = max(0.0, min(1.0, theses[-1].conviction or 0.0))
    return MIN_CONVICTION_FACTOR + (1.0 - MIN_CONVICTION_FACTOR) * conviction


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
            fill_alert = _fill_order(order, fill_price=price, fill_date=today)
            if fill_alert:
                alerts.append(fill_alert)

    return alerts


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
        max_per_stock = TOTAL_CAPITAL * MAX_POSITION_PCT * _conviction_factor(code)
        max_for_theme = TOTAL_CAPITAL * MAX_THEME_PCT - get_theme_exposure(theme)
        max_amount = min(available, max_per_stock, max(0, max_for_theme), sentiment_room)

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


def close_position(
    position_id: int,
    *,
    close_price: float,
    close_reason: str,
) -> bool:
    """Close an open position. Returns True on success, False if not found."""
    with _write_lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT open_price, shares FROM virtual_portfolio WHERE id = ?",
            (position_id,),
        ).fetchone()
        if not row:
            logger.warning("close_position: position #%d not found", position_id)
            return False

        open_price = row["open_price"] or 0
        shares = row["shares"] or 0
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


def _is_phase_bearish() -> tuple[bool, str]:
    """Check if current sentiment phase demands aggressive stop-tightening.

    V2 design: 分歧/退潮 phases have tighter trailing_stop_pct and higher
    theme_exit_threshold. In these phases, we also tighten stops on all
    positions — even unprofitable ones — to cut losses faster.

    Returns (is_bearish, phase_name).
    """
    try:
        from alpha_agents.data.sentiment_cycle import get_sentiment_cycle
        phase = get_sentiment_cycle().get("phase", "")
        return phase in ("分歧", "退潮"), phase
    except Exception:
        return False, ""



def _check_bearish_signals(
    pos: dict, price: float, phase_bearish: bool, phase_name: str
) -> tuple[float, list[str]]:
    """Detect bearish signals on a position and compute a tightened stop.

    Implements V2 principle #2 (资金行为优先) on the sell side. Four signals,
    in priority order:
      1. **量价派发** (alpha_agents.tools.exit_signals): 顶背离 95%/97%,
         放量滞涨 96%, 放量下跌 97%, 高位缩量 98%. Deterministic, computed
         from the local K-line — no LLM, so it is backtestable.
      2. P0 资金流：主力连续净流出 ≥3天 (3-day: 97%, 5+ day: 98%)
      3. P1 情绪周期：分歧/退潮 phase → 97% of current price
      4. P2 主线速率：2日内强度下降≥3 → 98% of current price

    Returns (tightest_stop, reasons). tightest_stop is 0 if no signal.
    The caller integrates this with trailing stop logic (take the max).

    "Tighten stop" beats "partial close" for MVP: less new infrastructure,
    same effective outcome — if price keeps falling, existing stop-loss
    machinery triggers the close; if price recovers, we never forced a sale.
    """
    import json as _json
    code = pos.get("code", "")
    name = pos.get("name", "")
    reasons: list[str] = []
    tightest_stop = 0.0

    def _apply(factor: float, label: str) -> None:
        """Raise tightest_stop to price*factor if it beats prior candidates."""
        nonlocal tightest_stop
        candidate = round(price * factor, 2)
        if candidate > tightest_stop:
            tightest_stop = candidate
        reasons.append(label)

    # ── Signal 1 (highest priority): 大盘弱势 ──
    # Forward-testing showed the exit question is answered by market
    # regime, not by per-stock distribution patterns: for positions
    # already up ≥15%, a weak market costs -1.73% of median excess return
    # by day 3 and -6.48% by day 20. Tighten hard when the market turns.
    # (See alpha_agents/tools/exit_signals for the numbers.)
    try:
        from alpha_agents.tools.exit_signals import get_market_regime
        regime, regime_pct = get_market_regime()
        if regime == "weak":
            _apply(0.96, f"大盘弱势({regime_pct:+.1f}%)")
    except Exception as e:
        logger.debug("Regime check failed for %s: %s", code, e)

    # ── Signal 2: 资金流 ──
    try:
        from alpha_agents.tools.fund_flow import get_stock_fund_flow_fn
        ff = _json.loads(get_stock_fund_flow_fn(code))
        out_days = ff.get("consecutive_outflow_days", 0) or 0
        if out_days >= 5:
            _apply(0.98, f"主力{out_days}日净流出")
        elif out_days >= 3:
            _apply(0.97, f"主力{out_days}日净流出")
    except Exception as e:
        logger.debug("Fund flow check failed for %s: %s", code, e)

    # ── Signal 3: 情绪周期 分歧/退潮 ──
    if phase_bearish:
        _apply(0.97, f"情绪{phase_name}")

    # ── Signal 4: 主线强度速率 ──
    try:
        from alpha_agents.data.memory_store import get_theme_strength_history
        theme = pos.get("theme", "")
        if theme:
            history = get_theme_strength_history(theme, days=3)
            if len(history) >= 2:
                newest = history[0]["strength"]
                oldest = history[-1]["strength"]
                drop = oldest - newest
                if drop >= 3:
                    _apply(0.98, f"主线{drop}日跌{drop}点")
    except Exception as e:
        logger.debug("Theme velocity check failed for %s: %s", code, e)

    return tightest_stop, reasons


def _is_hard_exit(pos: dict, current_return: float) -> bool:
    """Would this position close even if the trading agent said hold?

    Two lines only: the maximum loss from cost basis, and a theme that has
    been archived — at which point the reason the position was opened no
    longer exists in the system at all.
    """
    if current_return <= -HARD_STOP_PCT:
        return True
    if pos.get("theme"):
        theme = get_theme_by_name(pos["theme"])
        if theme and theme.get("status") == "archived":
            return True
    return False


def check_positions(
    realtime_prices: dict[str, float],
    today: str,
    hard_only: bool = False,
) -> list[dict]:
    """Check open positions for stop-loss/take-profit/expiry.

    ``hard_only`` is what makes room for a trading agent. Left False, every
    trigger below closes the position, which is the behaviour that leaves
    the agent nothing to learn about selling — it picks the stock and the
    rules dispose of it. Set True, only ``_is_hard_exit`` closes anything;
    every other trigger comes back as ``type="signal"`` for the agent to
    weigh, alongside the news and the theme state it already sees.

    Respects T+1: skips positions where open_date == today.

    Sell triggers (priority order):
      1. Hard stop / trailing stop triggered → close
      2. Target price hit → close
      3. Theme declining/weakening → close
      4. Bearish signals (量价派发 / fund flow / sentiment / theme) → tighten
         stop (may trigger #1 on next tick)

    The bearish-signal mechanism implements V2 principle #2 (资金行为优先) on
    the sell side: we no longer wait for the price to touch the original
    stop — we proactively raise it whenever the 量价 / fund-flow / sentiment
    / theme signals say the position is at risk.
    """
    conn = _get_conn()
    positions = conn.execute(
        "SELECT * FROM virtual_portfolio WHERE status = 'open' AND open_date < ?",
        (today,),
    ).fetchall()

    # ── Compute shared sentiment context (once per cycle, not per-position) ──
    phase_bearish, phase_name = _is_phase_bearish()

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
            # Get dynamic trailing stop from sentiment cycle
            try:
                from alpha_agents.data.sentiment_cycle import get_sentiment_cycle
                _cycle = get_sentiment_cycle()
                _trailing_pct_from_cycle = _cycle["strategy"]["trailing_stop_pct"] / 100
            except Exception:
                _trailing_pct_from_cycle = 0.05
            trailing_pct = min(original_stop_pct, _trailing_pct_from_cycle)

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

        # ── Bearish-signal tightening (V2 原则#2 资金行为优先的卖出侧落地) ──
        # Unlike trailing stop (only profits-based), this applies regardless of
        # P/L. Take the MAX of trailing-stop and bearish-signal-stop — we never
        # loosen a stop that was previously tightened.
        stop_before_bearish = stop_loss
        bearish_stop, bearish_reasons = _check_bearish_signals(
            pos, price, phase_bearish, phase_name,
        )
        if bearish_stop > stop_loss:
            logger.info(
                "Bearish stop: %s %s 止损 %.2f → %.2f [%s]",
                code, pos.get("name", ""), stop_loss, bearish_stop,
                "+".join(bearish_reasons),
            )
            stop_loss = bearish_stop

        with _write_lock:
            conn.execute(
                "UPDATE virtual_portfolio SET peak_return_pct = ?, "
                "max_drawdown_pct = ?, holding_days = ?, stop_loss = ? WHERE id = ?",
                (peak, drawdown, holding_days, stop_loss, pos["id"]),
            )
            conn.commit()

        # Emit a non-closing alert when bearish signals materially tightened
        # the stop. The alert surfaces WHY the stop moved, so the user sees
        # the signals — not just the eventual close. Threshold 0.5% above
        # the pre-bearish stop filters out no-op / sub-cent adjustments.
        if (bearish_reasons and stop_loss > stop_before_bearish * 1.005
                and stop_loss > price * 0.5):  # sanity guard vs zero/garbage prices
            alerts.append({
                "type": "stop_tightened",
                "code": code,
                "name": pos.get("name", ""),
                "price": price,
                "old_stop": stop_before_bearish,
                "new_stop": stop_loss,
                "reason": "预警-" + "+".join(bearish_reasons),
                "current_return": current_return,
            })

        # Check triggers (priority order)
        alert = None
        target_price = pos.get("target_price")

        if stop_loss and price <= stop_loss:
            alert = {"type": "stopped", "reason": f"{'移动' if stop_loss > (pos.get('stop_loss') or 0) else ''}止损触发"}
        elif target_price and price >= target_price:
            alert = {"type": "target_hit", "reason": "止盈触发"}

        # Theme-driven exit — no fixed holding days, ride the theme lifecycle
        if not alert and pos.get("theme"):
            theme = get_theme_by_name(pos["theme"])
            if theme:
                theme_status = theme.get("status", "watching")
                theme_strength = theme.get("strength", 0)

                # Dynamic exit threshold from sentiment cycle
                try:
                    from alpha_agents.data.sentiment_cycle import get_sentiment_cycle
                    _cycle = get_sentiment_cycle()
                    _exit_threshold = _cycle["strategy"]["theme_exit_threshold"]
                except Exception:
                    _exit_threshold = 3

                if theme_status in ("declining", "archived"):
                    # 主线衰退 → 清仓
                    alert = {"type": "expired", "reason": f"主线衰退({pos['theme']}已{theme_status})，持仓{holding_days}天"}
                elif theme_strength <= _exit_threshold:
                    # 主线走弱 → 清仓（不等到 declining，提前走）
                    alert = {"type": "expired", "reason": f"主线走弱({pos['theme']}强度{theme_strength}，阈值{_exit_threshold})，持仓{holding_days}天"}
                # peak/active + strength >= 4 → 继续持有，不设天数上限
                # 靠移动止损保护利润

        # Regime-conditional holding cap — the one exit rule that survived
        # forward testing. A position already up ≥15% bleeds median excess
        # return the longer it is held, and faster the weaker the market:
        # 强势 is flat only through day 3, 震荡 is already negative by day 3,
        # 弱势 loses 1.7% by day 3 and 6.5% by day 20. Riding the theme past
        # that point gives back the catalyst move.
        if not alert:
            try:
                from alpha_agents.tools.exit_signals import check_holding_period
                should_close, hp_reason = check_holding_period(code, holding_days)
                if should_close:
                    alert = {"type": "expired", "reason": hp_reason}
            except Exception as e:
                logger.debug("Holding period check failed for %s: %s", code, e)

        # No theme → fallback to moving stop only (no fixed day limit)
        # The trailing stop + theme lifecycle is the exit mechanism, not calendar days

        # Demote a discretionary trigger to evidence. The position stays
        # open and the agent is told why the rules wanted it closed — a
        # trailing stop that fired on an intraday wick reads very
        # differently next to a theme that is still taking inflow.
        if alert and hard_only and not _is_hard_exit(pos, current_return):
            alerts.append({
                "type": "signal", "code": code, "name": pos.get("name", ""),
                "reason": alert["reason"], "would_have": alert["type"],
                "current_return": current_return, "price": price,
                "holding_days": holding_days,
            })
            alert = None

        if alert:
            success = close_position(pos["id"], close_price=price, close_reason=alert["reason"])
            if not success:
                continue
            shares = pos.get("shares", 0)
            net_result = _estimate_net_close_result(open_price, price, shares)
            alert.update({
                "code": code,
                "name": pos.get("name", ""),
                "shares": shares,
                "open_price": open_price,
                "close_price": price,
                "return_pct": net_result["return_pct"],
                "return_amount": net_result["return_amount"],
                "gross_return_pct": current_return,
                "estimated_costs": net_result["costs"],
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

    # Check theme health (read-only, safe outside lock)
    if theme_name:
        theme = get_theme_by_name(theme_name)
        if theme and theme.get("strength", 0) < 4:
            return None  # Theme too weak, don't throw good money after bad

    with _write_lock:
        open_price = pos.get("open_price", 0)
        existing_shares = pos.get("shares", 0)
        existing_cost = open_price * existing_shares

        # Check position limit (30% with add)
        max_total_cost = TOTAL_CAPITAL * MAX_POSITION_WITH_ADD
        room = max_total_cost - existing_cost
        if room <= 0:
            return None  # Already at max

        # Check available capital (sentiment limit does NOT apply to add-positions —
        # bearish markets are exactly when you want to average down)
        available = get_available_capital()
        room = min(room, available)

        add_shares = _calc_shares(price, room)
        if add_shares == 0:
            return None

        add_cost = add_shares * price

        # Execute add: update shares and recalculate avg open_price
        new_total_shares = existing_shares + add_shares
        new_avg_price = round((existing_cost + add_cost) / new_total_shares, 2)

        # Recalculate stop_loss to maintain original percentage distance from new avg price
        old_stop = pos.get("stop_loss") or 0
        if open_price > 0 and old_stop > 0:
            original_stop_pct = (open_price - old_stop) / open_price  # e.g. 0.10 for 10% distance
            new_stop_loss = round(new_avg_price * (1 - original_stop_pct), 2)
        else:
            new_stop_loss = old_stop

        conn = _get_conn()
        conn.execute(
            "UPDATE virtual_portfolio SET open_price = ?, shares = ?, stop_loss = ? WHERE id = ?",
            (new_avg_price, new_total_shares, new_stop_loss, pos["id"]),
        )
        conn.commit()

    logger.info("Add position: %s %s +%d股 @ %.2f (均价 %.2f→%.2f, 止损 %.2f→%.2f, 总%d股, 总成本%.0f元)",
                code, name, add_shares, price,
                open_price, new_avg_price, old_stop, new_stop_loss,
                new_total_shares, new_avg_price * new_total_shares)

    return {
        "type": "add_position",
        "code": code,
        "name": name,
        "add_shares": add_shares,
        "add_price": price,
        "new_avg_price": new_avg_price,
        "new_stop_loss": new_stop_loss,
        "total_shares": new_total_shares,
        "total_cost": round(new_avg_price * new_total_shares),
    }


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
