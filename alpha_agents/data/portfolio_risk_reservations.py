"""Pre-trade risk reservations layered on the existing reservation ledger.

This module owns only the arithmetic and accounting for risk reservations.
The portfolio module still owns order creation/fills; reservations still own
the reservation state machine. Keeping those responsibilities separate avoids
turning portfolio.py into a second risk engine.
"""

from __future__ import annotations

import logging
import math
import sqlite3

from alpha_agents.data import attribution, order_theme_exposure, reservations
from alpha_agents.data.theme_gate import resolve_theme

logger = logging.getLogger(__name__)


def related_themes(primary_theme: str,
                   supporting_themes: list[str] | tuple[str, ...] = ()) -> list[str]:
    """Canonical primary + supporting labels, deduplicated in stable order."""
    primary = str(primary_theme or "").strip()
    if not primary:
        raise ValueError("primary theme is required")
    out = [primary]
    for raw in supporting_themes:
        value = str(raw or "").strip()
        if not value:
            continue
        canonical = resolve_theme(value) or value
        if canonical not in out:
            out.append(canonical)
    return out


def primary_theme_exposure(conn: sqlite3.Connection, *,
                           theme: str, trader_id: str) -> float:
    rows = conn.execute(
        "SELECT open_price, shares FROM virtual_portfolio "
        "WHERE status='open' AND theme=? AND trader_id=?",
        (theme, trader_id),
    ).fetchall()
    return sum(
        (row["open_price"] or 0) * (row["shares"] or 0)
        for row in rows)


def committed_theme_exposure(conn: sqlite3.Connection, *,
                             theme: str, trader_id: str) -> float:
    """Open cost plus pending risk holds for one correlated theme.

    New multi-theme positions use the append-only sidecar. Legacy rows without
    a sidecar fall back to virtual_portfolio.theme so old positions continue to
    count exactly once.
    """
    order_theme_exposure.init_schema(conn)
    sidecar = conn.execute(
        "SELECT COALESCE(SUM(v.open_price * v.shares),0) AS amount "
        "FROM virtual_portfolio v JOIN order_theme_exposures e "
        "ON e.order_id=v.id "
        "WHERE v.trader_id=? AND v.status='open' AND e.theme=?",
        (trader_id, theme),
    ).fetchone()
    legacy = conn.execute(
        "SELECT COALESCE(SUM(v.open_price * v.shares),0) AS amount "
        "FROM virtual_portfolio v "
        "WHERE v.trader_id=? AND v.status='open' AND v.theme=? "
        "AND NOT EXISTS (SELECT 1 FROM order_theme_exposures e "
        "                WHERE e.order_id=v.id)",
        (trader_id, theme),
    ).fetchone()
    held = reservations.held_total(
        conn, trader_id, kind=reservations.theme_risk_kind(theme))
    return (
        float(sidecar["amount"] or 0.0)
        + float(legacy["amount"] or 0.0)
        + held
    )


def first_theme_cap_breach(conn: sqlite3.Connection, *,
                           trader_id: str, themes: list[str],
                           reservation_amount: float,
                           theme_cap: float) -> tuple[str, float] | None:
    """First theme whose committed + requested amount exceeds the cap."""
    for theme in themes:
        committed = committed_theme_exposure(
            conn, theme=theme, trader_id=trader_id)
        if committed + reservation_amount > theme_cap + 1e-9:
            return theme, committed
    return None


def refuse_if_theme_cap_breached(
        conn: sqlite3.Connection, *, trader_id: str, code: str,
        order_date: str, primary_theme: str, themes: list[str],
        reservation_amount: float, theme_cap: float,
        thesis_id: int | None, prediction_id: int | None,
        terms: dict) -> bool:
    """Record and return a deterministic pre-trade theme-cap refusal."""
    breach = first_theme_cap_breach(
        conn, trader_id=trader_id, themes=themes,
        reservation_amount=reservation_amount, theme_cap=theme_cap)
    if breach is None:
        return False
    theme, committed = breach
    attribution.record_refusal(
        conn, trader_id=trader_id, code=code, order_date=order_date,
        refused_by=f"theme_risk_cap:{theme}", theme=primary_theme,
        thesis_id=thesis_id, prediction_id=prediction_id, **terms)
    logger.info(
        "Rejected order %s: %s committed %.2f + %.2f > %.2f",
        code, theme, committed, reservation_amount, theme_cap)
    return True


def reserve_theme_risk(conn: sqlite3.Connection, *, order_id: int,
                       trader_id: str, code: str, themes: list[str],
                       amount: float) -> None:
    """Reserve the same worst-case notional against every related theme."""
    for theme in themes:
        reservations.reserve_for_order(
            conn, order_id=order_id, trader_id=trader_id, code=code,
            amount=amount, kind=reservations.theme_risk_kind(theme),
            reason=f"pending theme-risk backstop:{theme}")


def plan_risk_amount_cap(fill_price: float, stop_loss: float | None,
                         capital: float, max_position_pct: float, *,
                         hard_stop_pct: float, lot_size: int) -> float:
    """Maximum notional consistent with the existing hard-risk budget.

    This is a sizing bound, not a promise that a stop can execute there. Risk
    distance is at least the hard-stop percentage; a wider declared stop
    shrinks size further. Gap/limit-blocked loss remains a separate stress
    scenario and may exceed this planned amount.
    """
    if fill_price <= 0 or capital <= 0 or max_position_pct <= 0:
        return 0.0
    hard_distance = fill_price * hard_stop_pct / 100.0
    declared_distance = 0.0
    if isinstance(stop_loss, (int, float)) and 0 < stop_loss < fill_price:
        declared_distance = fill_price - float(stop_loss)
    risk_per_share = max(hard_distance, declared_distance)
    if risk_per_share <= 0:
        return 0.0
    risk_budget = capital * max_position_pct * hard_stop_pct / 100.0
    raw_shares = risk_budget / risk_per_share
    nearest = round(raw_shares)
    shares = (
        int(nearest)
        if math.isclose(raw_shares, nearest, rel_tol=1e-12, abs_tol=1e-9)
        else math.floor(raw_shares)
    )
    shares = shares // lot_size * lot_size
    return max(0.0, shares * fill_price)
