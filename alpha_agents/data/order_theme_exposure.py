"""Theme exposure attribution for one order/position.

A position may belong to several selected themes while still being one stock
order. The ledger keeps one primary theme for ownership; this append-only
sidecar preserves overlapping themes for concentration risk.

For risk, a position's full committed amount counts toward every theme it
belongs to. That intentionally double-counts across themes; the reported
quantity is the *maximum cluster exposure*, not a sum across clusters.
"""

from __future__ import annotations

import json
import sqlite3

from alpha_agents.data import clock, memory_store


_TABLE = """
CREATE TABLE IF NOT EXISTS order_theme_exposures (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL,
    theme TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('primary','supporting')),
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(order_id, theme)
)
"""

_GUARDS = (
    "CREATE TRIGGER IF NOT EXISTS order_theme_exposures_no_update "
    "BEFORE UPDATE ON order_theme_exposures BEGIN "
    "SELECT RAISE(ABORT, 'order_theme_exposures is append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS order_theme_exposures_no_delete "
    "BEFORE DELETE ON order_theme_exposures BEGIN "
    "SELECT RAISE(ABORT, 'order_theme_exposures is append-only'); END",
)


class ThemeExposureError(ValueError):
    pass


def init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_TABLE)
    for statement in _GUARDS:
        conn.execute(statement)


def record(*, order_id: int, primary_theme: str,
           supporting_themes: list[str] | tuple[str, ...] = (),
           source: str,
           conn: sqlite3.Connection | None = None) -> None:
    primary = str(primary_theme or "").strip()
    if not primary:
        raise ThemeExposureError("primary_theme is required")
    supporting = [
        value for value in dict.fromkeys(
            str(theme).strip() for theme in supporting_themes
            if str(theme).strip())
        if value != primary
    ]
    target = conn if conn is not None else memory_store._get_conn()
    init_schema(target)
    rows = [(int(order_id), primary, "primary", source, clock.today())]
    rows.extend(
        (int(order_id), theme, "supporting", source, clock.today())
        for theme in supporting
    )

    def _write():
        target.executemany(
            "INSERT INTO order_theme_exposures "
            "(order_id,theme,role,source,created_at) VALUES (?,?,?,?,?)",
            rows,
        )

    if conn is not None:
        with target:
            _write()
    else:
        with memory_store._write_lock:
            with target:
                _write()


def for_order(order_id: int,
              conn: sqlite3.Connection | None = None) -> list[dict]:
    conn = conn if conn is not None else memory_store._get_conn()
    init_schema(conn)
    rows = conn.execute(
        "SELECT theme,role,source FROM order_theme_exposures "
        "WHERE order_id=? ORDER BY CASE role WHEN 'primary' THEN 0 ELSE 1 END,id",
        (int(order_id),),
    ).fetchall()
    return [dict(row) for row in rows]


def snapshot(*, trader_id: str, price_map: dict[str, float],
             equity: float,
             conn: sqlite3.Connection | None = None) -> dict:
    """Committed open+pending exposure by theme at one mark.

    Open positions use marked market value. Pending orders use the held cash
    reservation because that is capital already committed if the order fills.
    Missing marks/reservations make the snapshot incomplete rather than safe.
    """
    target = conn if conn is not None else memory_store._get_conn()
    init_schema(target)
    if not isinstance(equity, (int, float)) or equity <= 0:
        raise ThemeExposureError("positive equity is required")

    positions = target.execute(
        "SELECT id,code,theme,status,shares,open_price FROM virtual_portfolio "
        "WHERE trader_id=? AND status IN ('pending','open') ORDER BY id",
        (trader_id,),
    ).fetchall()

    cluster_amount: dict[str, float] = {}
    missing = []
    overlap_orders = 0
    for row in positions:
        order_id = int(row["id"])
        themes = for_order(order_id, target)
        if themes:
            labels = [str(item["theme"]) for item in themes]
        else:
            fallback = str(row["theme"] or "").strip()
            labels = [fallback] if fallback else []
        if not labels:
            missing.append({
                "order_id": order_id, "code": row["code"],
                "reason": "missing_theme_attribution"})
            continue
        if len(labels) > 1:
            overlap_orders += 1

        if row["status"] == "open":
            mark = price_map.get(str(row["code"]))
            shares = int(row["shares"] or 0)
            if mark is None or shares <= 0:
                missing.append({
                    "order_id": order_id, "code": row["code"],
                    "reason": "missing_open_mark_or_shares"})
                continue
            amount = float(mark) * shares
        else:
            reservation = target.execute(
                "SELECT amount FROM reservations "
                "WHERE order_id=? AND state='held' ORDER BY id DESC LIMIT 1",
                (order_id,),
            ).fetchone()
            if reservation is None:
                missing.append({
                    "order_id": order_id, "code": row["code"],
                    "reason": "missing_held_reservation"})
                continue
            amount = float(reservation["amount"])

        for theme in labels:
            cluster_amount[theme] = cluster_amount.get(theme, 0.0) + amount

    exposure_pct = {
        theme: round(amount / float(equity) * 100.0, 6)
        for theme, amount in sorted(cluster_amount.items())
    }
    maximum = max(exposure_pct.values()) if exposure_pct else 0.0
    return {
        "complete": not missing,
        "max_theme_cluster_exposure_pct": round(maximum, 6),
        "theme_exposure_pct": exposure_pct,
        "theme_exposure_json": json.dumps(
            exposure_pct, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")),
        "missing": missing,
        "overlap_orders": overlap_orders,
        "active_orders": len(positions),
    }
