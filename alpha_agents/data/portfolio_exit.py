"""The exit path: A-share friction, and booking a close.

Split out of ``portfolio`` because it is a different job. ``portfolio``
owns the order lifecycle — a pending order is written, a fill converts it
to a position, queries read the book. This module owns what happens when
the book gives something back: the friction model that turns a sale into
a net number, the append-only exit legs that number is derived from, and
the one feedback hop from a realised trade to the rule that produced it.

The friction constants live here because this is the module that applies
them. They are *assumptions*, versioned by hand, not verified market
rules — see the note on ``HARD_STOP_PCT`` in ``portfolio`` for the same
posture. ``LOT_SIZE`` is re-exported by ``portfolio`` because entry sizing
needs it too; it is an A-share market convention, not a strategy choice.

Nothing here grades a forecast. §9 keeps the three outcomes apart, and the
one this module owns is the trade outcome — realised, ledger-derived, net
of costs. A forecast's ``hit`` belongs to the evaluator that matures the
declared horizon.
"""

from __future__ import annotations

import logging

from alpha_agents.data import (
    clock, intent, order_state, settlement, trade_ledger,
)
from alpha_agents.data.memory_store import _get_conn, _write_lock
from alpha_agents.data.trader import DEFAULT_TRADER

logger = logging.getLogger(__name__)

LOT_SIZE = 100                   # A股一手 = 100股

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
            "cost_basis": 0.0,
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
        "cost_basis": round(cost_basis, 2),
        "costs": round(gross_amount - net_amount, 2),
    }


def _blended_return_pct(realized: dict) -> float:
    """Realised P&L over the cost of everything actually sold.

    A single-leg exit equals that leg's own percentage. Once a position has
    been trimmed, no individual leg describes the position any more — the
    label that goes into the review has to be the whole round trip, or the
    trim that locked in a gain becomes invisible.
    """
    basis = realized.get("cost_basis") or 0.0
    if basis <= 0:
        return 0.0
    return round(realized["net_amount"] / basis * 100, 2)


def _status_from_reason(reason: str) -> str:
    if "止损" in reason:
        return "stopped"
    if "止盈" in reason:
        return "target_hit"
    return "expired"


def close_position(
    position_id: int, *, close_price: float, close_reason: str,
    shares: int | None = None, command_id: str | None = None,
) -> bool:
    """Sell part or all of a position. Compatibility wrapper over the
    intent path.

    Same signature and return value as before S5 — ``True`` when the
    exit booked, ``False`` when it was refused. ``shares=None`` means
    sell the whole position and becomes a ``close`` intent; a partial
    quantity becomes a ``trim``. The refusal is recorded as an intent
    row, so "why did nothing sell" is answerable from the book.

    ``command_id`` stays on this signature: it is the retry key the
    ledger matches on, and it is the caller's, not the intent's.
    """
    action = intent.CLOSE if shares is None else intent.TRIM
    result = intent.submit_intent(
        intent.TradeIntent(
            action=action, position_id=position_id, price=close_price,
            reason=close_reason, shares=shares, command_id=command_id),
        conn=_get_conn())
    return bool(result.result)


def _close_position_impl(
    position_id: int, *, close_price: float, close_reason: str,
    shares: int | None = None, command_id: str | None = None,
) -> bool:
    """Atomically book one simulated exit and update its position projection.

    Explicit command IDs are retry-safe, including after a full close. Without
    one, each call is a new command; equal execution terms are not duplicates.
    Partial quantities must obey the existing simulator's lot convention.
    Legacy aggregate P&L is retained, but its unknown sold basis cannot yield
    a trustworthy blended percentage or learning label.
    """
    if (not trade_ledger.positive_price(close_price)
            or not isinstance(close_reason, str)
            or (shares is not None and (type(shares) is not int or shares <= 0))
            or (command_id is not None and
                (not isinstance(command_id, str) or not command_id.strip()))):
        logger.warning("Rejected invalid exit arguments for #%s", position_id)
        return False
    request = trade_ledger.exit_request(close_price, shares, close_reason)
    with _write_lock:
        conn = _get_conn()
        with conn:
            # Serializes competing writers before reading inventory, including
            # writers from another process. The context rolls back both writes.
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            if command_id is not None:
                previous = trade_ledger.find_command(conn, position_id, command_id)
                if previous:
                    matches = previous["request_json"] == request
                    if not matches:
                        logger.warning("Exit command conflict for #%s: %s", position_id, command_id)
                    return matches
            row = conn.execute("SELECT * FROM virtual_portfolio WHERE id=?", (position_id,)).fetchone()
            if not row or row["status"] != "open":
                return False
            held = row["shares"] or 0
            sell = held if shares is None else shares
            if (sell <= 0 or sell > held or (sell < held and sell % LOT_SIZE)
                    or not trade_ledger.positive_price(row["open_price"])):
                return False
            prior = trade_ledger.realized_for_position(conn, position_id)
            legacy = row["legacy_realized_amount"]
            if legacy is None:
                legacy = round((row["return_amount"] or 0) - prior["net_amount"], 2)
            net = _estimate_net_close_result(row["open_price"], close_price, sell)
            today = clock.today()
            # T+1 share-side: take the sold shares out of the
            # position's settled lots FIFO. A position with no lots
            # is a legacy row that pre-dates the S4 cutover; the
            # sell path for those falls back to the old
            # ``open_date < today`` gate the caller has already
            # passed. consume_lots_fifo would refuse a no-lots
            # position, so skip it.
            trader_id = row["trader_id"] or DEFAULT_TRADER
            if settlement.has_lots(conn, position_id):
                try:
                    settlement.consume_lots_fifo(
                        conn, position_id=position_id,
                        shares_to_sell=sell, today=today)
                except ValueError as e:
                    logger.warning(
                        "Exit #%d refused — %s", position_id, e)
                    return False
            exit_id = trade_ledger.record_exit(
                conn, position_id=position_id, trader_id=trader_id,
                code=row["code"], exit_date=today, price=close_price, shares=sell,
                cost_basis=net["cost_basis"], gross_amount=net["gross_amount"],
                costs=net["costs"], net_amount=net["return_amount"],
                return_pct=net["return_pct"], reason=close_reason,
                command_id=command_id, request_json=request,
                # Denormalised onto the leg so the realised result names the
                # idea that produced it even after the position row is
                # recycled or its thesis link is revised.
                thesis_id=row["thesis_id"],
            )
            # T+1 cash-side: the *proceeds* of this leg — the cost basis
            # coming back plus the net gain — are not available for new
            # positions until tomorrow. Proceeds, not the P&L: the whole
            # sale value is what the broker holds, and the cost basis is
            # only "returned" to the pot because ``invested`` drops when
            # the position stops being open. Recording the P&L alone
            # would credit the returned capital a day early.
            #
            # Idempotent on exit_id, so a retry of the same close
            # (matched on command_id) does not double-count the bucket.
            proceeds = round(net["cost_basis"] + net["return_amount"], 2)
            settlement.record_pending(
                conn, exit_id=exit_id, trader_id=trader_id,
                code=row["code"], net_amount=proceeds,
                exit_date=today)
            realized = trade_ledger.realized_for_position(conn, position_id)
            return_pct = None if legacy else _blended_return_pct(realized)
            return_amount = round(legacy + realized["net_amount"], 2)
            full = sell == held
            new_status = (_status_from_reason(close_reason)
                          if full else order_state.OPEN)
            # The earlier "status == open" gate plus BEGIN IMMEDIATE make
            # this near-formal, but the state machine is the contract, not
            # the gate. A partial exit is ``open → open`` (the position
            # shrinks, it does not close); a full close is ``open → stopped
            # / target_hit / expired`` per the reason.
            order_state.assert_transition(row["status"], new_status)
            # Closed rows retain last-sale shares for existing report readers.
            conn.execute(
                "UPDATE virtual_portfolio SET shares=?, status=?, close_date=?, close_price=?, "
                "return_amount=?, return_pct=?, legacy_realized_amount=?, close_reason=? WHERE id=?",
                (held if full else held - sell, new_status,
                 today if full else None, close_price if full else None,
                 return_amount, return_pct, legacy, close_reason[:200], position_id),
            )
        logger.info("Exit #%d: sold %d @ %.2f; cumulative net %.2f; legacy %.2f",
                    position_id, sell, close_price, return_amount, legacy)
    if full and return_pct is not None:
        _feed_close_to_learning(position_id, return_pct, close_reason)
    return True


def _feed_close_to_learning(position_id: int, return_pct: float,
                            close_reason: str) -> None:
    """Feed the realised round trip to the playbook that produced it.

    What this deliberately does **not** do is grade the forecast. A
    prediction's ``hit`` is a fixed-horizon label — "did this beat the
    market over the declared window" — and the old code overwrote it with
    the realised trade return. A stop-out at -8% after a +2% first day is
    not a failed forecast, it is a failed trade, and collapsing the two
    made the forecast score unrecoverable. The realised result lives in
    ``position_exits`` and ``virtual_portfolio.return_amount``, which is
    where a trade outcome belongs; ``review.py`` still fills ``hit`` on
    the declared horizon.

    Attribution is explicit. The order names its prediction, and the
    lookup is by id plus the order's own trader. The old version searched
    for the newest prediction with the same stock code near the fill date,
    which is not an ownership relation: with two traders on the same stock
    it could grade one trader's call with the other's result, and a
    position whose origin was never recorded was matched to whatever
    happened to be nearest. No link means no feedback.

    Best-effort: a failure here must never prevent a position from closing.
    """
    try:
        conn = _get_conn()
        pos = conn.execute(
            "SELECT code, trader_id, prediction_id FROM virtual_portfolio "
            "WHERE id = ?",
            (position_id,),
        ).fetchone()
        if not pos:
            return
        if not pos["prediction_id"]:
            logger.debug("Close #%d has no prediction link; no feedback",
                         position_id)
            return

        trader_id = pos["trader_id"] or DEFAULT_TRADER
        pred = conn.execute(
            "SELECT id, features_json FROM predictions "
            "WHERE id = ? AND trader_id = ? AND code = ?",
            (pos["prediction_id"], trader_id, pos["code"]),
        ).fetchone()
        if not pred:
            logger.warning("Prediction #%s does not belong to %r — no feedback "
                           "for close #%d", pos["prediction_id"], trader_id,
                           position_id)
            return

        hit = 1 if return_pct > 0 else 0
        # The playbook this decision matched, as recorded at decision time.
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

        logger.info("Learning feedback: %s prediction #%d ← 实盘 %+.2f%% (%s)",
                    pos["code"], pred["id"], return_pct, close_reason)
    except Exception as e:
        logger.debug("Close-to-learning feedback failed for #%d: %s",
                     position_id, e)
