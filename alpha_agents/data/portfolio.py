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
from datetime import datetime

from alpha_agents.data import (
    attribution, clock, episodes, order_state, reservations, settlement,
    thesis, trade_ledger,
)
from alpha_agents.data import portfolio_risk_reservations as risk_reservations
from alpha_agents.data.memory_store import _get_conn, _write_lock, get_theme_by_name
from alpha_agents.data.portfolio_entry import (
    order_conviction as _order_conviction,
    thesis_already_broken as _thesis_already_broken,
)
# Whether a theme may carry an order is its own question, and it now has its
# own module: the two-bar hysteresis that replaced the single `strength >= 4`
# veto did not fit under this file's line ceiling. `resolve_theme` comes from
# there too and is re-exported here, because callers have always reached for
# it through this module.
from alpha_agents.data.theme_gate import resolve_theme, theme_admits, theme_gate
from alpha_agents.data.trader import DEFAULT_TRADER
# The entry-zone predicate is shared with the T+1 walk-forward settlement: one
# reading of a zone, asked of a live quote here and of a session's open there.
# ``t1_execution`` imports nothing but ``dataclasses``, so this adds no cycle.
from alpha_agents.data.t1_execution import in_entry_zone
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
    _cluster_room,
    _sizing_policy,
    _trader_pct,
    _wanted_pct,
    get_sentiment_exposure_limit,
)
# The book's rows — pending / open / finished — and the one way a pending
# row leaves without trading. Moved out with the 2026-09-14 pending-order
# work, which pushed this file past the ceiling: adopting orders written
# before reservations existed, re-reading every order's thesis each cycle,
# and honouring expire_days all live in check_pending_orders above.
# Imported at module scope because check_pending_orders and _fill_order
# call into it directly, and re-exported to callers by being in this
# namespace — chat.py still does `from portfolio import _cancel_order`.
from alpha_agents.data import portfolio_book
from alpha_agents.data.portfolio_book import (
    _cancel_order,
    _cancel_order_impl,
    _cancel_order_unlocked,
    entry_stop,
    get_closed_positions,
    get_open_positions,
    get_pending_orders,
)

logger = logging.getLogger(__name__)

# 挂单有效期。曾经是「仅用于无主线的挂单，有主线的跟随主线生命周期」——
# 而无主线的挂单在更早一行就被撤了,所以它从未生效,有主线的挂单则永不
# 到期。现在它只是最后的兜底:期限先取论点自己的 horizon_days,再取交易员
# 的 default_horizon_days,两者都没有才落到这个数。见 `_expire_days_for`。
PENDING_EXPIRE_DAYS = 2

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

# The `MIN_THEME_STRENGTH = 4` veto that used to sit here is gone. It read the
# same constant at creation and on every pending check "so the two cannot drift
# apart" — and they did not drift, which was the problem: it vetoed candidates
# before the model saw them, then re-vetoed the order every five minutes while it
# waited to fill (106 of 117 orders cancelled, ~100 of them as 主线走弱). The bar
# is now a cross-sectionally normalised score with two levels — admission above
# cancellation — and it lives in `data/theme_gate.py` with that history.


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
    """Cash this trader can **spend right now**, after every commitment speaks.

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

    **Sale proceeds are deliberately not subtracted, because that is the
    A-share rule.** Settlement separates 可用 from 可取: selling on T makes the
    proceeds usable *immediately* — spendable on a further purchase the same
    day, repeatedly — and only withdrawable on T+1. This function answers "what
    can I spend", so cash in transit counts. The part that cannot yet leave the
    account is ``settlement.unreleased_pending_total``, and the trade read model
    reports it separately.

    Subtracting it here is what this function did until 2026-09-16, and it was
    wrong: it applied the *withdrawal* rule to *buying power*, so a day with any
    sale could not also buy, and turnover, exposure, the cash curve and drawdown
    all shifted. Citations and the replacement contract are in tech-debt D19 and
    ``tests/test_cash_settlement_semantics.py``.

    Legacy aggregate results are included as compatibility estimates,
    never reconstructed fills. This is not yet a fee-at-fill cash ledger.
    """
    return (trader_capital(trader_id)
            + trade_ledger.realized_total(_get_conn(), trader_id)
            - get_invested_capital(trader_id)
            - reservations.unconsumed_total(_get_conn(), trader_id))


def get_total_capital(trader_id: str = DEFAULT_TRADER) -> float:
    """What the trader owns, whether or not it can deploy it yet.

    ``capital + realized`` — the mandate plus everything won or lost.
    The decomposition against ``get_available_capital`` is::

        total = available + invested + reservations

    i.e. the only things separating "owns" from "can spend" are the open
    book (cash converted to shares at cost) and the cash earmarked against
    pending orders. Sale proceeds are **not** part of that separation: they are
    spendable the day they arrive and merely become *withdrawable* a day later
    (the A-share 可用/可取 split described in ``get_available_capital``), so the
    in-transit figure is not a difference between owning and spending and does
    not appear above.

    Reports that say "what do I actually have" read this. Trading
    decisions must read ``get_available_capital`` instead: sizing
    against ``total`` would spend money already earmarked for a pending
    order.
    """
    return (trader_capital(trader_id)
            + trade_ledger.realized_total(_get_conn(), trader_id))


def position_cost_basis(open_price: float, shares: int) -> float:
    """What a position actually cost to open, buy-side slippage included.

    ``open_price`` is the *fill price* and stays that: ``reconciliation``
    checks ``consumed_amount`` against ``shares × open_price × (1 + slip)``
    and settlement stores the raw price, so folding the slippage into the
    stored price would make both of them charge it a second time.

    But the cash identity has to count it. Until 2026-09-21 the buy leg was
    **recorded and never charged**: a fill consumed a reservation of
    ``shares × price × (1 + slip)`` (reconciliation agreed with that number)
    while ``get_available_capital`` subtracted ``shares × price``, and
    ``unconsumed_total`` reads a consumed row as 0. Measured on a 30,000
    notional fill, 15.00 of cash vanished from the identity — so every day
    the position was open, equity was overstated by exactly one buy-side
    slippage leg. The round trip was right once it closed (the close path
    puts slippage in ``cost_basis``); the daily marks the arm comparisons
    read were not.
    """
    return float(open_price) * int(shares) * (1 + SLIPPAGE_RATE)


def get_invested_capital(trader_id: str = DEFAULT_TRADER) -> float:
    """Open cost exposure, independent of realized gains or losses.

    Includes the buy-side slippage leg, through :func:`position_cost_basis`,
    because that is the cash the fill actually took. It has to be the same
    basis ``get_available_capital`` subtracts or the identity that function
    documents stops holding — see ``position_cost_basis``.
    """
    row = _get_conn().execute(
        "SELECT COALESCE(SUM(open_price * shares * ?), 0) FROM virtual_portfolio "
        "WHERE status='open' AND trader_id=?", (1 + SLIPPAGE_RATE, trader_id),
    ).fetchone()
    return float(row[0])


def get_theme_exposure(theme: str, trader_id: str = DEFAULT_TRADER) -> float:
    """Compatibility read; risk accounting is owned by the risk module."""
    return risk_reservations.primary_theme_exposure(
        _get_conn(), theme=theme, trader_id=trader_id)


# ── Pending Orders (挂单) ───────────────────────────────────


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
    risk_themes: list[str] | None = None,
    wake_agent: bool = False,
) -> int | None:
    """Create a pending order (挂单). Triggered when price enters entry zone.

    ``wake_agent`` with a ``thesis_id``: the theme admission bar is noted on
    the thesis instead of refusing the order. The agent chose this line with
    a stated plan; the lifecycle's view reaches it on the first cycle as an
    ``order_signal`` (see ``check_pending_orders``), where it is asked.

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
    admission_note = None
    with _write_lock:
        conn = _get_conn()
        if not portfolio_book._valid_prediction(
                conn, prediction_id, code, trader_id):
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
        related_themes = risk_reservations.related_themes(
            theme, risk_themes or [])

        # The check and reservations run under _write_lock, so two concurrent
        # individually-legal orders cannot both spend the last theme headroom.
        capital = trader_capital(trader_id)
        # The backstop is what *this* order could spend at fill, which is
        # what the thesis asked for. It was a flat MAX_POSITION_PCT, and
        # once sizing became the agent's that stopped being an upper bound:
        # an order stating 22.8% reserved 10% and spent 22.8%, so two pending
        # orders could commit the same cash — the exact failure reservations
        # exist to prevent, arrived at from the other side.
        reservation_amount = (
            capital * _wanted_pct(code, trader_id) * (1 + SLIPPAGE_RATE))
        terms = {"entry_low": entry_low, "entry_high": entry_high,
                 "stop_loss": stop_loss, "target_price": target_price,
                 "source": source, "reason": reason}
        # The per-theme ceiling is policy, like every other sizing bound —
        # `_fill_order`已经这样读它了，这道门漏了。Off by default: how much
        # to concentrate is the agent's, and an envelope that binds has to
        # earn its place through the policy registry rather than by being a
        # module constant.
        #
        # It mattered more than it looks. A replay hangs every order off one
        # synthetic theme, so a 30% per-theme cap was a 30% cap on the whole
        # replay book — and with a flat 10% reservation that is three pending
        # orders for the entire run, whatever the agent decided.
        theme_pct = _sizing_policy().get("max_theme_pct")
        if theme_pct is not None and risk_reservations.refuse_if_theme_cap_breached(
                conn, trader_id=trader_id, code=code, order_date=order_date,
                primary_theme=theme, themes=related_themes,
                reservation_amount=reservation_amount,
                theme_cap=capital * float(theme_pct), thesis_id=thesis_id,
                prediction_id=prediction_id, terms=terms):
            return None

        # The same admission bar check_pending_orders applies, applied at
        # creation instead of one cycle later — see `data/theme_gate.py` for
        # what it is now and why the old one cost 106 of 117 orders.
        #
        # A refusal is recorded, not just logged: it is the decision a
        # counterfactual has to see. Built from the same terms as the
        # placement below, because "what it would have ordered" and "what it
        # refused to order" are the same intent with a different action.
        weak = theme_admits(theme)
        if weak and wake_agent and thesis_id:
            # Not refused, and not silent either: the note goes on the thesis
            # once the order exists, below, so the review can read the
            # lifecycle's call against what the order went on to do.
            admission_note = weak
            logger.info("Order %s %s placed over the admission bar (%s) — "
                        "the thesis is the agent's; it will be asked", code,
                        name, weak)
        elif weak:
            attribution.record_refusal(
                conn, trader_id=trader_id, code=code, order_date=order_date,
                refused_by=weak, theme=theme, thesis_id=thesis_id,
                prediction_id=prediction_id, **terms)
            logger.info("Rejected order %s %s: %s — 下一轮也会被撤，不如不建",
                        code, name, weak)
            return None

        expire_days = portfolio_book._expire_days_for(
            thesis_id, trader_id, thesis_mod=thesis,
            trader_pct=_trader_pct, default=PENDING_EXPIRE_DAYS)
        cursor = conn.execute(
            "INSERT INTO virtual_portfolio "
            "(code, name, theme, order_date, open_date, open_price, entry_low, entry_high, "
            " stop_loss, target_price, expire_days, status, source, reason, trader_id, "
            " prediction_id, thesis_id) "
            "VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)",
            (code, name, theme, order_date, order_date, entry_low, entry_high,
             stop_loss, target_price, expire_days, source, reason,
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
                payload={"action": "open", "expire_days": expire_days,
                         **terms},
                thesis_id=thesis_id, order_id=order_id,
                prediction_id=prediction_id,
                sources=[source] if source else [],
            )
        except clock.LookAheadError:
            # Not the same failure as the one below. A boundary that
            # cannot be written is a missing audit; a boundary dated in
            # the future is the *content* being wrong, and it must not be
            # demoted to a warning — that is how a look-ahead reaches the
            # learning data while every log line looks healthy. Re-raise
            # before the tolerant handler can catch it.
            raise
        except Exception as e:
            # The order is the trade; the boundary is the audit. Losing the
            # audit must not stop the trade — but it must be visible.
            logger.warning("Could not freeze decision boundary for order "
                           "#%s: %s", order_id, e)
        # Reserve the worst-case fill cost against the order. The held row
        # is what keeps a second pending order from spending the same
        # cash; computing it here, before commit, means the order and
        # the reservation are written together or not at all.
        reservations.reserve_for_order(
            conn, order_id=order_id, trader_id=trader_id,
            code=code, amount=reservation_amount,
            reason="pending-order backstop")
        risk_reservations.reserve_theme_risk(
            conn, order_id=order_id, trader_id=trader_id,
            code=code, themes=related_themes, amount=reservation_amount)
        conn.commit()

        zone = (f"{entry_low:.2f}-{entry_high:.2f}" if entry_low and entry_high
                else f"≤{entry_high:.2f}" if entry_high
                else f"≥{entry_low:.2f}" if entry_low else "市价")
        logger.info("Pending order: %s %s 介入区间%s 止损%s (%s)",
                     code, name, zone, stop_loss or "无", source)
        placed = cursor.lastrowid
    # Outside the lock: add_checkpoint takes the same non-reentrant lock.
    if admission_note and placed:
        try:
            thesis.add_checkpoint(int(thesis_id),
                                  f"下单时主线系统判断: {admission_note}；按 agent 的论点照下",
                                  thesis.ACTIVE, kind="theme_gate")
        except Exception as e:
            logger.warning("Could not note the admission bar on thesis #%s: %s",
                           thesis_id, e)
    return placed


def check_pending_orders(
    realtime_prices: dict[str, float],
    today: str,
    trader_id: str | None = None,
    max_shares_by_code: dict[str, int] | None = None,
    wake_agent: bool = False,
) -> list[dict]:
    """Check pending orders against realtime prices. Fill if price in entry zone.

    ``wake_agent``: a themed order that carries the agent's own thesis is not
    pulled by the lifecycle gate; it comes back as an ``order_signal`` for
    ``order_review`` to put to the agent. Without a thesis there is no plan to
    consult, so those orders are still cancelled as before.

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

        # A pending order must hold a cash reservation before anything below
        # touches it. Both ends of an order require one — the fill path
        # (``consume_reservation``) and the cancel path (``release_reservation``)
        # refuse to run without a ``held`` row, and both raise rather than
        # degrade, which is the right call: a missing reservation means the cash
        # was never earmarked, and guessing at one would hide the double-spend
        # this module exists to prevent.
        #
        # But an order written before reservations existed holds none — #117
        # was created 13 hours before the module landed in 0fbe89a — and such an
        # order can be neither filled nor cancelled. Whichever came first raised
        # out of this loop, ``manage_book`` filed it as "组合管理失败", and the
        # rest of that book's cycle was skipped with it. One unfinishable row
        # became an outage of everything queued behind it.
        #
        # Repair it instead of finishing it. The reservation's job is to stop two
        # pending orders spending the same cash, so a missing row means
        # ``get_available_capital`` is over-reporting by this order's backstop —
        # the exact bug the module was written to fix, still live. Adopting puts
        # the constraint back. Cancelling would destroy a bet over a bookkeeping
        # omission. ``reserve_for_order`` is idempotent, so a later cycle that
        # finds the row already in place does nothing.
        reservation = reservations.reservation_for_order(conn, order["id"])
        if reservation is None:
            with _write_lock:
                reservations.reserve_for_order(
                    conn, order_id=order["id"],
                    trader_id=order.get("trader_id") or DEFAULT_TRADER,
                    code=code,
                    # What the order would spend, read the same way the
                    # door and the fill read it.
                    amount=(trader_capital(order.get("trader_id") or DEFAULT_TRADER)
                            * _wanted_pct(code, order.get("trader_id")
                                          or DEFAULT_TRADER)
                            * (1 + SLIPPAGE_RATE)),
                    reason="adopted: order predates reservations")
                conn.commit()
            logger.warning(
                "Order #%d (%s) carried no cash reservation — it predates the "
                "reservation module, so it could be neither filled nor cancelled. "
                "Adopted one now; the create path is supposed to write it.",
                order["id"], code)
        elif reservation["state"] != reservations.HELD:
            # Its reservation is already consumed or released while the order is
            # still pending: the same order was finished twice. Nothing here can
            # be repaired without guessing which half is real, and both finish
            # paths would raise on it, so leave it alone and say so every cycle
            # until someone reconciles it.
            logger.error(
                "Order #%d (%s) is pending but its reservation is %s — it can "
                "neither fill nor cancel. Skipping it this cycle.",
                order["id"], code, reservation["state"])
            continue

        # Theme health. This used to be the whole of an order's lifetime rule —
        # the comment said a themed order was kept alive "regardless of days",
        # which is how a zone the price never reached stayed armed forever. The
        # gate still asks its own question (has the line weakened, been retired,
        # or is there no line to hang the thesis on); expiry below asks the
        # separate one (has this setup had its time).
        theme_name = order.get("theme", "")
        if theme_name:
            theme = get_theme_by_name(theme_name)
            # The cancel bar is deliberately lower than the admission bar: an
            # order that was admitted on a strong theme must not be pulled for
            # a wobble back to the level it was admitted at.
            reason = (f"关联主线'{theme_name}'不存在" if theme is None
                      else theme_gate(theme_name, "cancel"))
            if reason and wake_agent and order.get("thesis_id"):
                # Evidence, not a verdict. Live 2026-09-10 → 09-23 about 25
                # orders died here, some minutes after placement: the direction
                # stage chose the line on fund flow while the lifecycle table
                # had already archived a theme of the same name, and the rule
                # nobody asked won. The agent's own theme invalidation still
                # runs below, in its own words.
                alerts.append({"type": "order_signal", "order_id": order["id"],
                               "thesis_id": order["thesis_id"], "code": code,
                               "name": order.get("name", ""),
                               "theme": theme_name, "reason": reason})
            elif reason:
                _cancel_order(order["id"], reason)
                alerts.append({"type": "cancelled", "code": code,
                               "name": order.get("name", ""),
                               "reason": "主线不存在" if theme is None else reason})
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

        # The order's own invalidation conditions run every cycle, not only at
        # the moment the price arrives. They were nested inside ``if triggered``,
        # so the one situation they exist for — an order sitting there while the
        # world moved away from it — was the one situation in which they could
        # not fire. Order #117 declared "跌破 94.00 则突破失败" and traded at
        # 85.49 for four sessions with nobody looking, because 85.49 is not
        # inside 95.5–98.0 and the check only ever ran on the way in.
        #
        # Filling into a dead thesis costs a full round trip — buy and sell
        # commission, stamp duty, slippage, ~0.15% — for a position the monitor
        # closes on its next pass. Seen on the first day of real fills:
        # 东方明珠 filled while 国企改革 was already scoring −1 for the day.
        dead = _thesis_already_broken(code, price, order)
        if dead:
            _cancel_order(order["id"], f"论点在成交前已失效: {dead}")
            alerts.append({"type": "cancelled", "code": code,
                           "name": order.get("name", ""),
                           "reason": f"论点已失效({dead})"})
            continue

        # Expiry, read at last. ``expire_days`` was written at creation and
        # ``days_pending`` computed at the top of this loop, and neither was ever
        # consulted: the theme branch skipped past the expiry check for every
        # themed order, and the only orders that reached it were the theme-less
        # ones cancelled a few lines above. The field now means what it says —
        # how long this idea is worth waiting for — and the agent sets it by
        # declaring the thesis horizon. "有效 3 天" covers days 0, 1 and 2; the
        # third day after placement is when the setup is stale.
        #
        # It is the *second* question, asked only when the price did not come.
        # Two reasons, both about the record rather than about the trade: the
        # reason this writes names the price (未到价), so writing it on a cycle
        # whose price sat inside the zone would put a label in the learning data
        # that its own row contradicts — and every reading of ``entry_quality``
        # would inherit the lie. And it is asked only once the live price is
        # known at all (the ``price is None`` guard above), on the same principle
        # the scoring side uses for a missing kline: no assertion about the
        # market without the market's data. A suspended stock's order therefore
        # waits instead of expiring, which is a hole, and a smaller one than
        # "未到价" meaning "we could not look".
        expire_days = order.get("expire_days")

        # Check if price is in entry zone. The predicate is imported rather
        # than inlined: the walk-forward runner asks the same question of a
        # session's open, and two copies of one rule is how an entry rule and
        # a settlement rule drift apart while both still look right.
        triggered = in_entry_zone(price, entry_low, entry_high)

        if triggered:
            fill_alert = _fill_order(
                order, fill_price=price, fill_date=today,
                max_shares=(
                    max_shares_by_code.get(code)
                    if max_shares_by_code is not None else None))
            if fill_alert:
                alerts.append(fill_alert)
        elif expire_days is not None and days_pending >= expire_days:
            # The price never arrived inside the idea's own window. This is the
            # one cancel whose reason is about the zone rather than the world —
            # and it is the producer the "价格未到" bucket never had.
            _cancel_order(order["id"],
                          f"挂单到期未到价（挂{days_pending}天，期限{expire_days}天）")
            alerts.append({"type": "cancelled", "code": code,
                           "name": order.get("name", ""),
                           "reason": f"到期未到价（{days_pending}天）"})

    return alerts


def _fill_order(
        order: dict, fill_price: float, fill_date: str,
        max_shares: int | None = None) -> dict | None:
    """Convert a pending order to an open position at fill_price.

    Every limit below is measured against the order's own trader: its
    capital, its exposure, its drawdown. A limit read off the pooled book
    would let one strategy's positions decide whether another gets to
    open one.

    Two time guards run before anything else (S6). The fill may not be
    dated after the kernel clock, and it may not act on information the
    decision behind the order could not have had. Both *raise* rather
    than return: a look-ahead is a fault in the simulation, not a trade
    that business rules turned down, and returning ``None`` would file it
    as the latter — which is how a leak stays invisible for months.
    """
    code = order["code"]
    name = order.get("name", "")
    theme = order.get("theme", "")
    trader_id = order.get("trader_id") or DEFAULT_TRADER
    capital = trader_capital(trader_id)

    clock.guard_fill(fill_date, order_id=order["id"], conn=_get_conn())

    # The market's exposure cap is read **before** the write lock, and that
    # placement is the fix for a deadlock rather than a style choice.
    #
    # The phase is a fact about the market, not about this book, so it needs no
    # lock. Reaching for it under the lock hangs the fill: ``get_sentiment_cycle``
    # computes and *writes* its phase on a cache miss, that write takes
    # ``_write_lock``, and ``_write_lock`` is a plain ``threading.Lock`` — so it
    # waits forever on a lock this frame is already holding. Production hid this
    # because the 15:30 review pre-computes tomorrow's phase; a fresh walk-forward
    # directory has no phase, and M1's first fill hung on it. The failure is
    # silent and unbounded, which is why it is a fix and not a note.
    # Computed outside the lock for the deadlock reason above; whether it
    # *binds* is the policy's call and is decided below.
    sentiment_cap = get_sentiment_exposure_limit(trader_id)

    with _write_lock:
        # Capital checks must be inside lock to prevent race conditions with
        # concurrent fills.
        available = get_available_capital(trader_id)
        invested = get_invested_capital(trader_id)
        sentiment_room = (max(0, sentiment_cap - invested)
                          if _sizing_policy().get("sentiment_scaling")
                          else float("inf"))

        # Size by conviction rather than filling every position to the cap.
        # A flat 15% everywhere throws away half of what a trader is for:
        # being right more often is worth less than being bigger when
        # right. The thesis carries a conviction the agent stated when it
        # opened the idea; with no thesis this falls back to the cap and
        # behaves exactly as before.
        # What the agent asked for, capped by the backstop. Conviction no
        # longer scales this behind its back — it says the number itself.
        sizing = _sizing_policy()
        wanted = capital * _wanted_pct(code, trader_id)
        # The agent's number, and by default nothing trims it. Each term
        # below is added only when the policy in force asks for it.
        max_pos_pct = sizing.get("max_position_pct")
        if max_pos_pct is None:
            max_pos_pct = _trader_pct(trader_id, "max_position_pct",
                                      MAX_POSITION_PCT)
            max_per_stock = wanted
        else:
            max_per_stock = min(wanted, capital * float(max_pos_pct))
        plan_risk_cap = (
            risk_reservations.plan_risk_amount_cap(
                fill_price, order.get("stop_loss"), capital,
                float(max_pos_pct), hard_stop_pct=HARD_STOP_PCT,
                lot_size=LOT_SIZE)
            if sizing.get("risk_budget_sizing") else float("inf"))
        theme_pct = sizing.get("max_theme_pct")
        max_for_theme = (
            capital * float(theme_pct) - get_theme_exposure(theme, trader_id)
            if theme_pct is not None else float("inf"))
        # Themes that share most of their constituents are one bet. The
        # per-theme cap counted 金属铜 and 小金属概念 as two and would let
        # them take 60% between them while sharing 6 of 10 names.
        cluster_cap = (_cluster_room(theme, trader_id)
                       if sizing.get("cluster_cap") else float("inf"))
        # Floor at zero, and that is a fix rather than formatting. Every term
        # above can be negative — ``available`` after a realized loss,
        # ``max_for_theme`` once the line is over its cap, ``cluster_cap`` once
        # a correlated cluster is — and ``min()`` over them is not a budget.
        # Unfloored, the negative value reached ``_calc_shares``, produced a
        # negative share count, and ``consume_reservation`` refused a negative
        # cost by **raising** — which aborted the whole pending-order cycle for
        # that trader instead of skipping one order. Found by the walk-forward
        # on 2025-07-08; no room is a refusal, and ``shares == 0`` below is the
        # code that already knows how to say so.
        capacity_amount = (
            float("inf")
            if max_shares is None
            else max(0, int(max_shares)) * fill_price
        )
        # The other five terms, before capacity joins them. Kept apart so the
        # fill can say whether *capacity* was what limited it:
        # ``capacity_amount`` is inside the ``min`` below, so ``shares`` can
        # never exceed it — a "did the fill oversize its cap" test is therefore
        # always False and measures nothing. "Was liquidity the binding
        # constraint" is the fact a reader can act on, and it is only
        # answerable against the terms capacity competed with.
        other_room = max(0.0, min(
            available, max_per_stock, plan_risk_cap,
            max(0, max_for_theme), sentiment_room, cluster_cap))
        capacity_bound = capacity_amount < other_room
        max_amount = max(0.0, min(other_room, capacity_amount))

        # Portfolio drawdown gates *new* risk and never forces an exit.
        # Liquidating at a drawdown level sells the bottom, and in a system
        # built to learn from resolved theses it would destroy the samples
        # before they resolve. Positions already open keep their own stops.
        if sizing.get("drawdown_gate") and _drawdown_blocks_new_risk(trader_id):
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
            ceiling = max(0.0, min(
                available, capital * max_pos_pct, plan_risk_cap,
                max(0, max_for_theme), sentiment_room, cluster_cap,
                capacity_amount))
            # The one-lot fallback can also be what the cap refuses, so the
            # binding flag is re-derived against the ceiling actually used.
            capacity_bound = capacity_amount < max(0.0, min(
                available, capital * max_pos_pct, plan_risk_cap,
                max(0, max_for_theme), sentiment_room, cluster_cap))
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
            "status = ?, open_date = ?, open_price = ?, shares = ?, "
            "initial_stop_loss = ? "
            "WHERE id = ?",
            (target, fill_date, fill_price, shares, order.get("stop_loss"),
             order["id"]),
        )
        # The held reservation is now the actual cost. Entry_high
        # over-estimated; the over-reserve is returned to available —
        # for real, not by promise: the consume call shrinks the row to
        # the actual cost, and the first version left the full backstop
        # bound until a reconciliation that never ran (eight fills locked
        # 647k of a 1M pot in one replay window).
        actual_cost = shares * fill_price * (1 + SLIPPAGE_RATE)
        reservations.consume_reservation(
            conn, order_id=order["id"], actual_cost=actual_cost)
        # Pending theme-risk holds become redundant once the position is open;
        # the open position itself is now counted by _theme_committed_exposure.
        reservations.release_all_held_for_order(
            conn, order_id=order["id"],
            reason="filled: risk now represented by open position",
            include_cash=False)
        # T+1 share-side: each fill is its own settlement lot. settle_date
        # is fill_date + 1 calendar day, so the position cannot be sold
        # back the same day the order fills. The legacy open_date check
        # keeps working for rows that pre-date the S4 cutover (no lot,
        # fall back to open_date < today).
        settlement.create_lot(
            conn, position_id=order["id"], trader_id=trader_id,
            code=code, shares=shares, open_date=fill_date,
            open_price=fill_price, source="initial")
        # The decision's learning unit gained its outcome: this one traded.
        episodes.note_fill(conn, order["id"], fill_date)
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
        # Liquidity, not the other five limits, is what sized this fill.
        "capacity_bound": bool(capacity_bound),
    }


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
        if not portfolio_book._valid_prediction(
                conn, prediction_id, code, trader_id):
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
            " stop_loss, initial_stop_loss, target_price, status, source, reason, "
            " trader_id, prediction_id, thesis_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)",
            (code, name, theme, open_date, open_date, open_price, shares,
             stop_loss, stop_loss, target_price, source, reason, trader_id,
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
        except clock.LookAheadError:
            # See the note on the pending-order path: a boundary that is
            # *wrong* is not a boundary that is merely missing.
            raise
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


# Topping up an open position. Imported here rather than at the top because
# portfolio_add reaches back into this namespace for the capital readings —
# the same one-way shape portfolio_intent has. `portfolio_intent.add_to_position`
# calls `P._add_to_position_impl`, so it has to be in this namespace.
from alpha_agents.data.portfolio_add import (  # noqa: E402
    _add_to_position_impl, _round_lot,
)


# Re-exported so callers keep importing check_positions from portfolio.
# The split is about file size, not about the API.
from alpha_agents.data.position_monitor import (  # noqa: E402
    _check_add_position, _check_bearish_signals,
    _is_phase_bearish, check_positions,
)
