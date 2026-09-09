"""Deciding what to do about a position that is already open.

Split out of portfolio.py when it crossed the 1200-line lint ceiling, on
a seam that was already there: this half only *reads* positions and
decides, while the other half creates and resizes them. Nothing here
opens an order or picks a size.

What lives here is the rule layer that runs underneath the trading agent
— the trailing stop, the bearish-signal tightening, the theme exit, the
regime holding cap, and the hard floor the agent may not overrule. With
``AGENT_EXIT_DECISIONS`` on these are demoted to evidence; with it off
they are the whole exit policy.
"""

import logging
from datetime import datetime

from alpha_agents.data.memory_store import (
    _get_conn, _write_lock, get_theme_by_name,
)
from alpha_agents.data.portfolio import (
    ADD_POSITION_DROP_PCT, DEFAULT_POSITION_PCT, HARD_STOP_PCT, LOT_SIZE,
    MAX_POSITION_WITH_ADD, MAX_THEME_PCT,
    _calc_shares, _estimate_net_close_result, close_position,
    get_available_capital, get_open_positions, get_sentiment_exposure_limit,
    get_theme_exposure, trader_capital,
)
from alpha_agents.data.trader import DEFAULT_TRADER

logger = logging.getLogger(__name__)


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
    trader_id: str | None = None,
) -> list[dict]:
    """Check open positions for stop-loss/take-profit/expiry.

    ``trader_id=None`` checks every book; the intraday cycle passes one id
    so each trader's alerts reach its own exit decision.

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
        "SELECT * FROM virtual_portfolio WHERE status = 'open' AND open_date < ?"
        + (" AND trader_id = ?" if trader_id else ""),
        [today, *([trader_id] if trader_id else [])],
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

    trader_id = pos.get("trader_id") or DEFAULT_TRADER

    with _write_lock:
        open_price = pos.get("open_price", 0)
        existing_shares = pos.get("shares", 0)
        existing_cost = open_price * existing_shares

        # Check position limit (30% with add), against this trader's own pot
        max_total_cost = trader_capital(trader_id) * MAX_POSITION_WITH_ADD
        room = max_total_cost - existing_cost
        if room <= 0:
            return None  # Already at max

        # Check available capital (sentiment limit does NOT apply to add-positions —
        # bearish markets are exactly when you want to average down)
        available = get_available_capital(trader_id)
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


