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

PENDING_EXPIRE_DAYS = 2  # 挂单有效期（仅用于无主线的挂单，有主线的跟随主线生命周期）

# ── Capital Management ──────────────────────────────────────
TOTAL_CAPITAL = 100_000          # 总资金 10万
MAX_POSITION_PCT = 0.15          # 单票初始建仓最多占总资金 15%
MAX_POSITION_WITH_ADD = 0.30     # 补仓后单票最多占总资金 30%
MAX_THEME_PCT = 0.30             # 同主线最多占总资金 30%
LOT_SIZE = 100                   # A股一手 = 100股
ADD_POSITION_DROP_PCT = 5.0      # 持仓跌超5%才考虑补仓

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

def get_vpa_target_for_code(code: str) -> float | None:
    """Look up the latest VPA-derived target price (take-profit) for a code.

    Reads vpa_analysis_history.target_low/target_high and returns their
    midpoint — a balance between conservative (target_low: first profit
    pause) and stretch (target_high: full Wyckoff projection).

    Returns None if no recent VPA target zone is available. Callers should
    treat this as best-effort; positions without a target_price still work
    with trailing-stop only.
    """
    try:
        from alpha_agents.data.memory_store import get_latest_vpa_analysis
        vpa = get_latest_vpa_analysis(code)
        if not vpa:
            return None
        tl = vpa.get("target_low")
        th = vpa.get("target_high")
        if tl and th and tl > 0 and th > 0:
            return round((tl + th) / 2, 2)
        # Partial data: use whichever is present
        if th and th > 0:
            return float(th)
        if tl and tl > 0:
            return float(tl)
        return None
    except Exception:
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

        max_per_stock = TOTAL_CAPITAL * MAX_POSITION_PCT
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
        return True


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


def _vpa_phase_action(phase: str, verdict: str) -> tuple[float | None, str]:
    """Map VPA phase to a stop-tightening factor (Anna Coulling phase priority).

    Phase is more decisive than verdict in Wyckoff theory:
      - 派发 (initial/middle): insiders are selling → tighten aggressively (96%)
      - 派发尾声 / 抛售高峰: terminal exhaustion → DO NOT tighten
        (these are reversal/buy signals, not sell signals — even if verdict
         appears bearish because the panic isn't done)
      - 下跌: trend in progress → tighten moderately (97%)
      - 吸筹 / 拉升: bullish phases → never tighten on phase alone
      - 震荡: ambiguous → defer to verdict-based logic (return None)
      - 买入高峰: bottom reversal → DO NOT tighten

    Returns (factor, reason) or (None, "") if phase is non-decisive.
    factor is what to multiply current price by; lower = tighter stop.
    """
    if not phase:
        return None, ""
    # Order matters — check more specific labels first
    if "买入高峰" in phase:
        return None, ""  # Strong bottom reversal; do not tighten
    if "抛售高峰" in phase or "派发尾声" in phase:
        return None, ""  # Terminal exhaustion; possible reversal up
    if "派发" in phase:
        # Catches "派发", "派发初期", "派发中期"
        return 0.96, f"VPA派发({phase})"
    if "下跌" in phase:
        return 0.97, f"VPA下跌phase"
    if "吸筹" in phase or "拉升" in phase:
        return None, ""  # Bullish phases — let trailing stop work normally
    # 震荡 or unrecognized — fall through to verdict-based logic
    return None, ""


def _check_bearish_signals(
    pos: dict, price: float, phase_bearish: bool, phase_name: str
) -> tuple[float, list[str]]:
    """Detect bearish signals on a position and compute a tightened stop.

    Implements V2 principle #2 (资金行为优先) on the sell side. Five signals,
    in priority order:
      1. **VPA phase (NEW v2.5)**: Wyckoff phase decides direction first.
         派发 → 96%; 下跌 → 97%; 派发尾声/抛售高峰/吸筹/拉升 → DO NOT tighten.
         Only fall through to verdict if phase is ambiguous (震荡).
      2. P0 资金流：主力连续净流出 ≥3天 (3-day: 97%, 5+ day: 98%)
      3. P1 VPA verdict (fallback when phase didn't decide)
      4. P2 情绪周期：分歧/退潮 phase → 97% of current price
      5. P3 主线速率：2日内强度下降≥3 → 98% of current price

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

    # ── Signal 1 (highest priority): VPA phase + verdict ──
    # Anna Coulling: phase IS the direction. A "中性" verdict in 派发 phase is
    # actually a sell signal — insiders are unwinding even if the bar-by-bar
    # indicators look ambiguous. Conversely, "偏空" verdict in 抛售高峰 is a
    # buy signal — the panic isn't done but the bottom is forming.
    try:
        from alpha_agents.data.memory_store import get_latest_vpa_analysis
        vpa = get_latest_vpa_analysis(code)
        if vpa:
            verdict = vpa.get("verdict", "") or ""
            phase = vpa.get("phase", "") or ""
            analysis_date = vpa.get("analysis_date", "")
            analysis_fresh = False
            try:
                a_dt = datetime.strptime(analysis_date, "%Y-%m-%d")
                now_dt = datetime.now()
                analysis_fresh = (now_dt - a_dt).days <= 3
            except (ValueError, TypeError):
                pass
            if analysis_fresh:
                # Phase first
                phase_factor, phase_reason = _vpa_phase_action(phase, verdict)
                if phase_factor is not None:
                    _apply(phase_factor, phase_reason)
                else:
                    # Phase didn't decide (震荡 or 吸筹/拉升 or terminal phase) —
                    # fall back to verdict-based check, but ONLY in 震荡.
                    # In 吸筹/拉升 or terminal phases, even bearish verdict is
                    # likely noise — skip.
                    if (not phase or "震荡" in phase) and verdict in ("看空", "偏空"):
                        _apply(0.97, f"VPA{verdict}(震荡)")
    except Exception as e:
        logger.debug("VPA phase check failed for %s: %s", code, e)

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


def check_positions(
    realtime_prices: dict[str, float],
    today: str,
) -> list[dict]:
    """Check open positions for stop-loss/take-profit/expiry.

    Respects T+1: skips positions where open_date == today.

    Sell triggers (priority order):
      1. Hard stop / trailing stop triggered → close
      2. Target price hit → close
      3. Theme declining/weakening → close
      4. Bearish signals (fund flow / VPA / phase / theme velocity) → tighten
         stop (may trigger #1 on next tick)

    The bearish-signal mechanism implements V2 principle #2 (资金行为优先) on
    the sell side: we no longer wait for the price to touch the original
    stop — we proactively raise it whenever the fund-flow / VPA / phase /
    theme signals say the position is at risk.
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

        # No theme → fallback to moving stop only (no fixed day limit)
        # The trailing stop + theme lifecycle is the exit mechanism, not calendar days

        if alert:
            success = close_position(pos["id"], close_price=price, close_reason=alert["reason"])
            if not success:
                continue
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
