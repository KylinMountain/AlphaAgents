"""Cross-table reconciliation — verify the book against itself.

This module exists because the trading kernel's cash story used to be a
formula: available = capital + realized − invested, recomputed every
query from scattered columns. A bug in any of those columns — a typo,
a wrong JOIN, a missing reservation — would have surfaced as a wrong
number that nothing else could see. The Phase 2 fix was to give the
formula its own store (reservations, state-machine validated status
updates); reconciliation is the read-back that proves the fix worked.

The check is **read-only against production tables**. It reads
``virtual_portfolio``, ``position_exits`` and ``reservations`` and writes
only to ``reconciliation_runs`` and ``reconciliation_diffs`` — the
audit log of the check itself. Auto-correcting a found inconsistency
would convert a bug we can detect into one we cannot: an undetected
bad write is worse than a noisy alarm.

The eight invariants are the ones that catch money going missing or
to the wrong book:

  1. ``orphan_exit`` — ``position_exits`` row references a position_id
     that does not exist in ``virtual_portfolio``. Major: the realised
     P&L is appended to a phantom position, so it can neither be
     attributed nor reversed cleanly.
  2. ``exit_trader_mismatch`` — exit leg attributed to a different
     trader than the position's owner. Critical: the realised leg would
     be booked against the wrong book, defeating the cross-trader
     comparison the system exists for.
  3. ``exit_math_inconsistent`` — ``net_amount`` does not equal
     ``gross_amount − costs``. Major: the friction model produced a
     number that cannot be reconciled with the raw inputs.
  4. ``orphan_reservation`` — ``reservations`` row references an
     ``order_id`` that does not exist in ``virtual_portfolio``.
     Critical: cash is earmarked for an order that was never written.
  5. ``missing_reservation`` — open position with shares > 0 has no
     reservation in (held, consumed). Critical: the cash earmark is
     gone, so the next order can spend it.
  6. ``stale_reservation`` — reservation still in state='held' but
     the matching order is in a terminal status. Major: the cash has
     already returned to the pot in practice (the order cannot fill),
     but the row still subtracts from ``unconsumed_total``.
  7. ``consumed_amount_mismatch`` — for a consumed reservation,
     ``consumed_amount`` does not equal
     ``shares × open_price × (1 + SLIPPAGE_RATE)``. Critical: the
     reservation was consumed at a different rate than the position
     was opened, which is the kind of drift that erodes the meaning
     of the cash ledger.
  8. ``oversold_position`` — sum of exit legs exceeds the position's
     share count. Major: a position was sold past zero, which is the
     textbook "we invented shares" bug.

The summary in ``reconciliation_runs.summary_json`` records the
per-trader derived totals so a future check can compare itself to
the previous run without re-deriving.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Callable, Iterable

from alpha_agents.data.memory_store import _get_conn, _write_lock
from alpha_agents.data.order_state import is_terminal
from alpha_agents.data.portfolio_exit import SLIPPAGE_RATE

logger = logging.getLogger(__name__)

CRITICAL = "critical"
MAJOR = "major"
MINOR = "minor"

# Tolerance for floating-point comparisons on currency-style amounts.
# 1 fen — small enough to catch a missing fee, large enough not to
# complain about a `round(..., 2)` chain.
TOLERANCE = 0.01


@dataclass
class Diff:
    """One inconsistency found during a reconciliation run."""
    trader_id: str
    invariant: str
    severity: str
    detail: dict = field(default_factory=dict)
    position_id: int | None = None
    reservation_id: int | None = None


@dataclass
class RunResult:
    """The outcome of one ``reconcile`` call."""
    run_id: int
    status: str  # 'clean' | 'dirty'
    diffs: list[Diff] = field(default_factory=list)
    summary: dict = field(default_factory=dict)


# ── Individual checks ───────────────────────────────────────────────────
# Each check reads the production tables directly via SQL and yields Diff
# objects. They never call portfolio.py — a check that goes through the
# thing it is verifying would only ever confirm the implementation is
# self-consistent, not that the implementation is correct.


def _check_orphan_exits(conn: sqlite3.Connection) -> Iterable[Diff]:
    rows = conn.execute(
        """
        SELECT e.id exit_id, e.position_id, e.trader_id, e.code
        FROM position_exits e
        LEFT JOIN virtual_portfolio p ON p.id = e.position_id
        WHERE p.id IS NULL
        """
    ).fetchall()
    for r in rows:
        yield Diff(
            trader_id=r["trader_id"] or "",
            invariant="orphan_exit",
            severity=MAJOR,
            detail={"exit_id": r["exit_id"],
                    "position_id": r["position_id"],
                    "code": r["code"]},
            position_id=r["position_id"],
        )


def _check_exit_trader_mismatch(conn: sqlite3.Connection) -> Iterable[Diff]:
    rows = conn.execute(
        """
        SELECT e.id exit_id, e.position_id, e.trader_id exit_trader,
               p.trader_id portfolio_trader, e.code
        FROM position_exits e
        INNER JOIN virtual_portfolio p ON p.id = e.position_id
        WHERE COALESCE(e.trader_id, '') <> COALESCE(p.trader_id, '')
        """
    ).fetchall()
    for r in rows:
        yield Diff(
            trader_id=r["exit_trader"] or "",
            invariant="exit_trader_mismatch",
            severity=CRITICAL,
            detail={"exit_id": r["exit_id"],
                    "position_id": r["position_id"],
                    "code": r["code"],
                    "exit_trader": r["exit_trader"],
                    "portfolio_trader": r["portfolio_trader"]},
            position_id=r["position_id"],
        )


def _check_exit_math(conn: sqlite3.Connection) -> Iterable[Diff]:
    rows = conn.execute(
        "SELECT id, position_id, trader_id, code, "
        "gross_amount, costs, net_amount FROM position_exits"
    ).fetchall()
    for r in rows:
        expected = round(r["gross_amount"] - r["costs"], 2)
        if abs(r["net_amount"] - expected) > TOLERANCE:
            yield Diff(
                trader_id=r["trader_id"] or "",
                invariant="exit_math_inconsistent",
                severity=MAJOR,
                detail={"exit_id": r["id"],
                        "position_id": r["position_id"],
                        "code": r["code"],
                        "gross_amount": r["gross_amount"],
                        "costs": r["costs"],
                        "expected_net": expected,
                        "actual_net": r["net_amount"]},
                position_id=r["position_id"],
            )


def _check_orphan_reservations(conn: sqlite3.Connection) -> Iterable[Diff]:
    rows = conn.execute(
        """
        SELECT r.id res_id, r.order_id, r.trader_id, r.code, r.amount
        FROM reservations r
        LEFT JOIN virtual_portfolio p ON p.id = r.order_id
        WHERE p.id IS NULL
        """
    ).fetchall()
    for r in rows:
        yield Diff(
            trader_id=r["trader_id"] or "",
            invariant="orphan_reservation",
            severity=CRITICAL,
            detail={"reservation_id": r["res_id"],
                    "order_id": r["order_id"],
                    "code": r["code"],
                    "amount": r["amount"]},
            reservation_id=r["res_id"],
        )


def _check_missing_reservation_for_open(
        conn: sqlite3.Connection) -> Iterable[Diff]:
    rows = conn.execute(
        """
        SELECT p.id, p.code, p.trader_id
        FROM virtual_portfolio p
        WHERE p.status = 'open' AND COALESCE(p.shares, 0) > 0
          AND NOT EXISTS (
            SELECT 1 FROM reservations r
            WHERE r.order_id = p.id
              AND r.state IN ('held', 'consumed')
          )
        """
    ).fetchall()
    for r in rows:
        yield Diff(
            trader_id=r["trader_id"] or "",
            invariant="missing_reservation",
            severity=CRITICAL,
            detail={"position_id": r["id"], "code": r["code"]},
            position_id=r["id"],
        )


def _check_stale_reservations(conn: sqlite3.Connection) -> Iterable[Diff]:
    rows = conn.execute(
        """
        SELECT r.id res_id, r.order_id, r.trader_id, r.code, r.amount,
               p.status position_status
        FROM reservations r
        INNER JOIN virtual_portfolio p ON p.id = r.order_id
        WHERE r.state = 'held'
        """
    ).fetchall()
    for r in rows:
        if is_terminal(r["position_status"]):
            yield Diff(
                trader_id=r["trader_id"] or "",
                invariant="stale_reservation",
                severity=MAJOR,
                detail={"reservation_id": r["res_id"],
                        "order_id": r["order_id"],
                        "code": r["code"],
                        "amount": r["amount"],
                        "position_status": r["position_status"]},
                reservation_id=r["res_id"],
                position_id=r["order_id"],
            )


def _check_consumed_amount(conn: sqlite3.Connection) -> Iterable[Diff]:
    rows = conn.execute(
        """
        SELECT r.id res_id, r.order_id, r.trader_id, r.code,
               r.consumed_amount, r.amount reservation_amount,
               p.shares, p.open_price, p.status position_status
        FROM reservations r
        INNER JOIN virtual_portfolio p ON p.id = r.order_id
        WHERE r.state = 'consumed'
        """
    ).fetchall()
    for r in rows:
        if r["open_price"] is None or r["shares"] is None:
            # A reservation consumed against an unfilled position is a
            # structural mismatch — the consumed row exists but the
            # position has no cost basis to compare against.
            yield Diff(
                trader_id=r["trader_id"] or "",
                invariant="consumed_amount_mismatch",
                severity=CRITICAL,
                detail={"reservation_id": r["res_id"],
                        "order_id": r["order_id"],
                        "code": r["code"],
                        "reason": "consumed reservation references an unfilled position",
                        "shares": r["shares"],
                        "open_price": r["open_price"]},
                reservation_id=r["res_id"],
                position_id=r["order_id"],
            )
            continue
        expected = round(
            r["shares"] * r["open_price"] * (1 + SLIPPAGE_RATE), 2)
        if abs(r["consumed_amount"] - expected) > TOLERANCE:
            yield Diff(
                trader_id=r["trader_id"] or "",
                invariant="consumed_amount_mismatch",
                severity=CRITICAL,
                detail={"reservation_id": r["res_id"],
                        "order_id": r["order_id"],
                        "code": r["code"],
                        "expected_consumed": expected,
                        "actual_consumed": r["consumed_amount"],
                        "shares": r["shares"],
                        "open_price": r["open_price"]},
                reservation_id=r["res_id"],
                position_id=r["order_id"],
            )


def _check_oversold_position(conn: sqlite3.Connection) -> Iterable[Diff]:
    rows = conn.execute(
        """
        SELECT p.id, p.code, p.trader_id, p.shares,
               (SELECT COALESCE(SUM(e.shares), 0)
                FROM position_exits e WHERE e.position_id = p.id) total_sold
        FROM virtual_portfolio p
        """
    ).fetchall()
    for r in rows:
        if r["total_sold"] > (r["shares"] or 0):
            yield Diff(
                trader_id=r["trader_id"] or "",
                invariant="oversold_position",
                severity=MAJOR,
                detail={"position_id": r["id"],
                        "code": r["code"],
                        "shares_held": r["shares"],
                        "total_sold": r["total_sold"]},
                position_id=r["id"],
            )


# All checks in one place so the runner is a flat list and the order of
# diffs is stable across runs.
_CHECKS: tuple[Callable[[sqlite3.Connection], Iterable[Diff]], ...] = (
    _check_orphan_exits,
    _check_exit_trader_mismatch,
    _check_exit_math,
    _check_orphan_reservations,
    _check_missing_reservation_for_open,
    _check_stale_reservations,
    _check_consumed_amount,
    _check_oversold_position,
)


# ── Summary (per-trader derived totals, no diff) ────────────────────────


def _summary(conn: sqlite3.Connection) -> dict:
    """Per-trader breakdown of canonical derived values.

    Not a check — a recording. Captures the numbers as-of the run so a
    later run can compare itself to the previous one without having to
    re-derive.
    """
    out: dict = {}
    rows = conn.execute(
        """
        SELECT COALESCE(NULLIF(trader_id, ''), '') trader_id,
               COUNT(*) FILTER (WHERE status = 'open')    n_open,
               COUNT(*) FILTER (WHERE status = 'pending')  n_pending,
               COUNT(*) FILTER (
                 WHERE status NOT IN ('open', 'pending')) n_closed,
               COALESCE(SUM(CASE WHEN status = 'open'
                            THEN open_price * shares ELSE 0 END), 0) invested
        FROM virtual_portfolio
        GROUP BY COALESCE(NULLIF(trader_id, ''), '')
        """
    ).fetchall()
    for r in rows:
        tid = r["trader_id"] or ""
        out[tid] = {
            "n_open": r["n_open"],
            "n_pending": r["n_pending"],
            "n_closed": r["n_closed"],
            "invested_capital": r["invested"],
        }
    realized_rows = conn.execute(
        """
        SELECT COALESCE(NULLIF(trader_id, ''), '') trader_id,
               COALESCE(SUM(net_amount), 0) net
        FROM position_exits
        GROUP BY COALESCE(NULLIF(trader_id, ''), '')
        """
    ).fetchall()
    for r in realized_rows:
        tid = r["trader_id"] or ""
        out.setdefault(tid, {})
        out[tid]["realized_legs"] = r["net"]
    res_rows = conn.execute(
        """
        SELECT COALESCE(NULLIF(trader_id, ''), '') trader_id,
               COALESCE(SUM(CASE WHEN state = 'held'
                            THEN amount ELSE 0 END), 0) held,
               COALESCE(SUM(CASE WHEN state = 'consumed'
                            THEN amount ELSE 0 END), 0) consumed_total,
               COALESCE(SUM(CASE WHEN state = 'consumed'
                            THEN consumed_amount ELSE 0 END), 0) consumed_used,
               COALESCE(SUM(CASE WHEN state = 'released'
                            THEN amount ELSE 0 END), 0) released
        FROM reservations
        GROUP BY COALESCE(NULLIF(trader_id, ''), '')
        """
    ).fetchall()
    for r in res_rows:
        tid = r["trader_id"] or ""
        out.setdefault(tid, {})
        out[tid]["reservation_held"] = r["held"]
        out[tid]["reservation_consumed_remaining"] = (
            r["consumed_total"] - r["consumed_used"])
        out[tid]["reservation_released"] = r["released"]
    return out


# ── Public entry points ─────────────────────────────────────────────────


def reconcile(conn: sqlite3.Connection | None = None) -> RunResult:
    """Run every invariant, persist the run + diffs, return the summary.

    Read-only against ``virtual_portfolio`` / ``position_exits`` /
    ``reservations``. Writes only to ``reconciliation_runs`` and
    ``reconciliation_diffs`` — the audit log of the check itself.
    """
    if conn is None:
        conn = _get_conn()
    diffs: list[Diff] = []
    for check in _CHECKS:
        try:
            diffs.extend(check(conn))
        except Exception as e:
            # A single failing check must not abort the run: the others
            # still produce useful signal, and the error itself is
            # written to the audit log so the next operator can see
            # *which* invariant went down.
            logger.warning("Reconciliation check %s raised: %s",
                           check.__name__, e)
            diffs.append(Diff(
                trader_id="",
                invariant=f"{check.__name__}_error",
                severity=CRITICAL,
                detail={"error": str(e)[:200]},
            ))
    summary = _summary(conn)
    status = "dirty" if diffs else "clean"
    traders_with_diffs = {d.trader_id for d in diffs
                          if d.trader_id and not d.invariant.endswith("_error")}
    with _write_lock:
        cur = conn.execute(
            "INSERT INTO reconciliation_runs "
            "(status, diff_count, trader_count, summary_json) "
            "VALUES (?, ?, ?, ?)",
            (status, len(diffs), len(traders_with_diffs),
             json.dumps(summary, ensure_ascii=False)))
        run_id = cur.lastrowid
        for d in diffs:
            conn.execute(
                "INSERT INTO reconciliation_diffs "
                "(run_id, trader_id, invariant, severity, detail_json, "
                " position_id, reservation_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (run_id, d.trader_id, d.invariant, d.severity,
                 json.dumps(d.detail, ensure_ascii=False),
                 d.position_id, d.reservation_id))
        conn.commit()
    return RunResult(run_id=run_id, status=status, diffs=diffs,
                     summary=summary)


def get_latest_run(conn: sqlite3.Connection | None = None) -> dict | None:
    if conn is None:
        conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM reconciliation_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def get_diffs_for_run(run_id: int,
                      conn: sqlite3.Connection | None = None) -> list[dict]:
    if conn is None:
        conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM reconciliation_diffs WHERE run_id = ? ORDER BY id",
        (run_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def diff_to_dict(d: Diff) -> dict:
    """Public serializer for the CLI."""
    return {
        "trader_id": d.trader_id,
        "invariant": d.invariant,
        "severity": d.severity,
        "detail": d.detail,
        "position_id": d.position_id,
        "reservation_id": d.reservation_id,
    }