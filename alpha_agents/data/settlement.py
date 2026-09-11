"""T+1 settlement: lots (share-side) and pending cash (cash-side).

Two clocks were missing before this slice.

The share-side one: T+1 in A-shares says the shares you buy today are
not sellable until tomorrow. The kernel used ``open_date < today`` as a
single-position proxy: it conflated "bought today" with "the entire
position is one batch". Adding to a position kept the original
``open_date``, so a position bought last week and topped up today looked
like one old batch — its today-bought shares were already
"sellable" even though the market would refuse the order. The lot
table makes each fill its own settlement batch: lots are consumed FIFO
by ``settle_date`` on a sell, and ``sellable_shares`` is a sum over
lots, not a date comparison against a position row that mixes dates.

The cash-side one: when you sell shares today the proceeds arrive in
your account tomorrow. The old ``get_available_capital`` added the
sale's net P&L to spendable cash immediately, which let a position
liquidate on Day T and redeploy the cash on Day T — the same day the
broker would still have it frozen. ``pending_settlements`` records each
leg's net cash with its own settle_date; ``release_due_settlements``
flips ``released=1`` once that date has passed, and
``unreleased_pending_total`` is what ``get_available_capital``
subtracts.

Both tables are append-only for the rows they create; only the two
"this just got consumed / released" UPDATEs move state. Legacy rows in
``virtual_portfolio`` have no lots: they fall back to the
``open_date < today`` semantic the rest of the system still understands.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Day count between fill and settle for both clocks. A-share T+1 means
# the next calendar day; weekends are fine because Saturday ≤ Monday
# is True and Sunday ≤ Monday is True, so Friday-buy is sellable on
# Monday the same way the broker would treat it.
T_PLUS_ONE_DAYS = 1


def next_settle_date(fill_date: str) -> str:
    """The date on which shares / cash from a Day-T fill become spendable.

    Pure calendar +1, not a trading-day calendar. That is correct for
    A-share T+1 because the comparison is ``settle_date <= today``: a
    Friday fill's Saturday settle_date is ≤ Monday, so Monday the
    position is sellable, exactly as the broker reports.
    """
    return (datetime.strptime(fill_date, "%Y-%m-%d")
            + timedelta(days=T_PLUS_ONE_DAYS)).strftime("%Y-%m-%d")


# ── Share-side (lots) ──────────────────────────────────────────────────


def create_lot(conn: sqlite3.Connection, *, position_id: int, trader_id: str,
               code: str, shares: int, open_date: str, open_price: float,
               source: str = "initial") -> int:
    """Append one settlement lot. ``shares`` is both the original and
    remaining count — the caller has not yet sold any of these shares.

    Returns the new lot id. Raises on non-positive shares or unparseable
    date, because a zero-share lot is a different bug from a no-fill.
    """
    if type(shares) is not int or shares <= 0:
        raise ValueError(
            f"lot shares must be a positive int, got {shares!r}")
    settle = next_settle_date(open_date)
    cur = conn.execute(
        "INSERT INTO settlement_lots "
        "(position_id, trader_id, code, shares, remaining_shares, "
        " open_date, settle_date, open_price, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (position_id, trader_id, code, shares, shares,
         open_date, settle, open_price, source))
    return int(cur.lastrowid)


def consume_lots_fifo(conn: sqlite3.Connection, *, position_id: int,
                      shares_to_sell: int, today: str) -> list[dict]:
    """Consume up to ``shares_to_sell`` from the position's settled lots FIFO.

    "FIFO" here is by ``settle_date`` ascending, breaking ties on lot
    id. This is the standard accounting order for share-settlement: the
    earliest-settled lot is sold first, so any lot still frozen by T+1
    stays frozen until its turn.

    Only lots whose ``settle_date <= today`` are eligible — T+1 means
    unsettled lots cannot be sold no matter how early their row sits in
    the table. ``today`` is the exit date the caller is operating on,
    not wall-clock time, so a backtest can replay yesterday's exit
    against yesterday's lots.

    Updates ``remaining_shares`` in place. Returns a list of
    ``{lot_id, shares, settle_date}`` for the lots that were drawn down,
    in the order drawn — so the caller can record one
    ``pending_settlements`` row per exit and one credit per consumed
    lot if it needs to.

    Raises ``ValueError`` if there are no settled lots or if the
    position's settled shares are insufficient. ``check_positions``
    already enforces the T+1 rule on the read side; this is the
    write-side backstop so a misbehaving caller cannot sell unsettled
    lots in spite of that gate.
    """
    if shares_to_sell <= 0:
        raise ValueError(
            f"shares_to_sell must be positive, got {shares_to_sell!r}")
    # Take from oldest settle_date first; tie-break on lot id so the
    # order is stable across runs even with same-day adds.
    lots = conn.execute(
        """
        SELECT id, settle_date, remaining_shares
        FROM settlement_lots
        WHERE position_id = ?
          AND remaining_shares > 0
          AND settle_date <= ?
        ORDER BY settle_date ASC, id ASC
        """,
        (position_id, today)).fetchall()
    consumed: list[dict] = []
    remaining_to_sell = shares_to_sell
    for lot in lots:
        if remaining_to_sell == 0:
            break
        take = min(lot["remaining_shares"], remaining_to_sell)
        conn.execute(
            "UPDATE settlement_lots SET remaining_shares = ? WHERE id = ?",
            (lot["remaining_shares"] - take, lot["id"]))
        consumed.append({"lot_id": lot["id"], "shares": take,
                         "settle_date": lot["settle_date"]})
        remaining_to_sell -= take
    if remaining_to_sell > 0:
        raise ValueError(
            f"position #{position_id} has only "
            f"{shares_to_sell - remaining_to_sell} settled shares today "
            f"({today}); requested {shares_to_sell}")
    return consumed


def sellable_shares(conn: sqlite3.Connection, position_id: int,
                    today: str) -> int:
    """Shares sellable today from one position.

    A-share rule: shares become sellable on ``settle_date`` (open_date
    + 1 calendar day). ``settle_date <= today`` is "already settled";
    a Saturday settle on a Monday today is settled and counted.

    Returns 0 for positions with no lots at all — caller should fall
    back to ``open_date < today`` semantics for legacy rows.
    """
    row = conn.execute(
        """
        SELECT COALESCE(SUM(remaining_shares), 0) sellable
        FROM settlement_lots
        WHERE position_id = ? AND remaining_shares > 0
          AND settle_date <= ?
        """,
        (position_id, today)).fetchone()
    return int(row["sellable"])


def has_lots(conn: sqlite3.Connection, position_id: int) -> bool:
    """True iff this position has at least one settlement_lots row.

    Used to distinguish legacy positions (no lots → fall back to
    open_date < today) from positions created after the S4 cutover
    (have lots → use lot-based sellability).
    """
    row = conn.execute(
        "SELECT 1 FROM settlement_lots WHERE position_id = ? LIMIT 1",
        (position_id,)).fetchone()
    return row is not None


def lot_count(conn: sqlite3.Connection, position_id: int) -> int:
    """How many lots a position has — diagnostic."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM settlement_lots WHERE position_id = ?",
        (position_id,)).fetchone()
    return int(row["n"])


# ── Cash-side (pending_settlements) ─────────────────────────────────────


def record_pending(conn: sqlite3.Connection, *, exit_id: int,
                   trader_id: str, code: str, net_amount: float,
                   exit_date: str) -> int:
    """Append a pending settlement row for one exit leg.

    Idempotent on ``exit_id``: a retry of the same exit returns the
    existing row id, which matters because partial-exit retries go
    through the same exit id and double-counting would inflate the
    pending bucket.
    """
    existing = conn.execute(
        "SELECT id FROM pending_settlements WHERE exit_id = ?",
        (exit_id,)).fetchone()
    if existing:
        return int(existing["id"])
    settle = next_settle_date(exit_date)
    cur = conn.execute(
        "INSERT INTO pending_settlements "
        "(exit_id, trader_id, code, net_amount, exit_date, settle_date) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (exit_id, trader_id, code, net_amount, exit_date, settle))
    return int(cur.lastrowid)


def unreleased_pending_total(conn: sqlite3.Connection,
                             trader_id: str) -> float:
    """Cash the trader has earned but cannot yet deploy.

    This is what the cash-side T+1 rule adds to the pot the day after a
    sale — the subtraction is what ``get_available_capital`` does not
    include until ``release_due_settlements`` flips the row to
    ``released=1``.
    """
    row = conn.execute(
        """
        SELECT COALESCE(SUM(net_amount), 0) pending
        FROM pending_settlements
        WHERE trader_id = ? AND released = 0
        """,
        (trader_id,)).fetchone()
    return float(row["pending"])


def release_due_settlements(conn: sqlite3.Connection, today: str) -> int:
    """Flip released=1 on every pending_settlements row whose settle_date
    has passed. Returns the count released. Called once per EOD
    settlement pass; safe to call more than once (rows already released
    are skipped by the WHERE clause).

    Idempotent: ``released_at`` is set to a fresh timestamp every call,
    which is fine because the row's release fact is already recorded
    in ``released=1`` — the timestamp is for audit only.
    """
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    cur = conn.execute(
        "UPDATE pending_settlements "
        "SET released = 1, released_at = ? "
        "WHERE released = 0 AND settle_date <= ?",
        (now, today))
    return int(cur.rowcount)


def pending_summary(conn: sqlite3.Connection, trader_id: str) -> dict:
    """Snapshot of pending cash state per trader — for reconciliation."""
    row = conn.execute(
        """
        SELECT
          COALESCE(SUM(CASE WHEN released = 0 THEN net_amount ELSE 0 END), 0)
              unreleased_total,
          COALESCE(SUM(CASE WHEN released = 1 THEN net_amount ELSE 0 END), 0)
              released_total,
          COUNT(*) FILTER (WHERE released = 0) unreleased_count,
          COUNT(*) FILTER (WHERE released = 1) released_count
        FROM pending_settlements
        WHERE trader_id = ?
        """,
        (trader_id,)).fetchone()
    return {
        "unreleased_total": float(row["unreleased_total"]),
        "released_total": float(row["released_total"]),
        "unreleased_count": int(row["unreleased_count"]),
        "released_count": int(row["released_count"]),
    }