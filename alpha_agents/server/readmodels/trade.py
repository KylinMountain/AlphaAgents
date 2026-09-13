"""Trade workspace: the book, its attribution chain, and what each change cost.

Four sections, each with one named source:

* ``book`` — what is pending, open and closed. This is the payload
  ``/api/portfolio`` serves; the route now delegates here rather than
  assembling its own copy, so the page and the workspace cannot disagree.
* ``attribution`` — the chain the design calls ``thesis → order → exits``.
  ``theses.id`` reaches ``virtual_portfolio.thesis_id`` reaches
  ``position_exits.thesis_id``. There is no ``fill_id`` and no
  ``ledger_entry_id`` column in this repository; a fill *is* the position row
  and an exit leg *is* the ledger entry, so the chain is three hops, not four.
* ``intents`` — every accounting change passes one door (``submit_intent``),
  and this is that door's trail. It is also the only place a
  submitted-but-undecided intent shows up.
* ``settlement`` — cash held against live orders and shares still inside
  their T+1 window.

Only ``book`` has rows in production today; the other three are wired and
have never been written, which is what their ``empty`` state reports.
"""

from __future__ import annotations

import logging

from alpha_agents.server.readmodels import Need, Section, section, workspace

logger = logging.getLogger(__name__)


def _read_book() -> tuple[dict, int]:
    """The whole book, one call per table it is built from."""
    from alpha_agents.data.portfolio import (
        account_capital, get_closed_positions, get_open_positions,
        get_pending_orders, get_portfolio_stats,
    )

    pending = get_pending_orders()
    positions = get_open_positions()
    closed = get_closed_positions(50)
    stats = get_portfolio_stats(30)
    total = account_capital()
    invested = sum((p["open_price"] or 0) * (p["shares"] or 0)
                   for p in positions)
    positions = _attach_theses(positions)
    return {
        "pending": pending,
        "positions": positions,
        "closed": closed,
        "stats": stats,
        "theses": _closed_theses(),
        "capital": {"total": total, "available": total - invested,
                    "invested": invested},
        "traders": _trader_books(),
    }, len(pending) + len(positions) + len(closed)


def _trader_books() -> list[dict]:
    """One row per trader: its money, its book, its curve.

    Returned even in the single-trader case so the UI has one shape to
    render; it can collapse the section when the list has one entry.
    """
    from alpha_agents.data.portfolio import (
        get_available_capital, get_open_positions, get_pending_orders,
        trader_capital,
    )
    from alpha_agents.data.portfolio_risk import current_drawdown
    from alpha_agents.data.trader import load_traders

    out = []
    for t in load_traders():
        try:
            positions = get_open_positions(t.id)
            dd = current_drawdown(t.id) or {}
            out.append({
                "id": t.id, "name": t.name,
                "legacy": t.legacy,
                "note": t.note,
                "capital": trader_capital(t.id),
                "available": get_available_capital(t.id),
                "positions": len(positions),
                "pending": len(get_pending_orders(t.id)),
                "drawdown_pct": dd.get("drawdown_pct"),
                "blocked": bool(dd.get("blocked")),
            })
        except Exception as e:
            logger.warning("Trader %s book unavailable: %s", t.id, e)
    return out


def thesis_dict(th) -> dict:
    """One thesis, with its invalidation conditions rendered server-side.

    Rendered from the same table the evaluator reads, so the page cannot
    describe a condition differently from the code that fires it.
    """
    from alpha_agents.data.thesis import describe
    return {
        "id": th.id, "code": th.code, "name": th.name, "theme": th.theme,
        "claim": th.claim, "horizon_days": th.horizon_days,
        "prob": th.prob, "conviction": th.conviction, "status": th.status,
        "created_at": th.created_at, "closed_at": th.closed_at,
        "close_kind": th.close_kind, "close_note": th.close_note,
        "checkpoints": th.checkpoints,
        "conditions": [{"kind": c.kind, "value": c.value, "note": c.note,
                        "text": describe(c)} for c in th.conditions],
    }


def _attach_theses(positions: list[dict]) -> list[dict]:
    """Attach each position's thesis, or an explicit None.

    A position without its thesis is a row of numbers: the thesis is why it
    is held and what would end it.
    """
    try:
        from alpha_agents.data.thesis import get_by_position
    except Exception as e:
        logger.debug("Thesis module unavailable: %s", e)
        return positions
    for pos in positions:
        try:
            th = get_by_position(pos["id"])
            pos["thesis"] = thesis_dict(th) if th else None
        except Exception as e:
            logger.debug("Thesis lookup failed for position %s: %s",
                         pos.get("id"), e)
            pos["thesis"] = None
    return positions


def _closed_theses(days: int = 30) -> list[dict]:
    try:
        from alpha_agents.data.thesis import get_closed
        return [thesis_dict(t) for t in get_closed(days=days)]
    except Exception as e:
        logger.debug("Closed thesis read failed: %s", e)
        return []


def book() -> dict:
    """The book payload, exactly as ``/api/portfolio`` serves it.

    ``/api/portfolio`` delegates here rather than assembling its own copy:
    two assemblies of the same tables drift, and then "what do I hold" has
    two answers.
    """
    return _read_book()[0]


def _read_attribution() -> tuple[dict, int]:
    """Who earned what: realised legs rolled up by the thesis that named them.

    An exit leg with no ``thesis_id`` is counted separately rather than
    dropped. It is not a rounding detail — it is the size of the gap between
    the ledger and the ideas, and a rollup that hides it would make the
    attribution chain look complete when it is not.
    """
    from alpha_agents.data import memory_store
    conn = memory_store._get_conn()
    per_thesis = [dict(r) for r in conn.execute(
        "SELECT e.thesis_id, COUNT(*) legs, "
        "  SUM(e.net_amount) net, SUM(e.return_pct > 0) wins, "
        "  t.code, t.status AS thesis_status "
        "FROM position_exits e LEFT JOIN theses t ON t.id = e.thesis_id "
        "WHERE e.thesis_id IS NOT NULL "
        "GROUP BY e.thesis_id ORDER BY net DESC").fetchall()]
    totals = dict(conn.execute(
        "SELECT COUNT(*) legs, SUM(thesis_id IS NULL) unattributed, "
        "  SUM(net_amount) net FROM position_exits").fetchone() or {})
    orders = dict(conn.execute(
        "SELECT COUNT(*) orders, SUM(thesis_id IS NULL) without_thesis "
        "FROM virtual_portfolio").fetchone() or {})
    return {
        "by_thesis": per_thesis,
        "exits": totals,
        "orders": orders,
    }, int(totals.get("legs") or 0)


def _read_intents() -> tuple[dict, int]:
    """The single door's trail: what was asked, and what came back."""
    from alpha_agents.data import intent
    rows = intent.history(limit=100)
    never = intent.never_decided()
    counts: dict = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {
        "recent": rows,
        "by_status": counts,
        "never_decided": never,
    }, len(rows)


def _read_settlement() -> tuple[dict, int]:
    """Cash committed to live orders, and shares still inside T+1."""
    from alpha_agents.data import memory_store
    conn = memory_store._get_conn()
    reservations = [dict(r) for r in conn.execute(
        "SELECT state, COUNT(*) n, SUM(amount) amount, "
        "  SUM(consumed_amount) consumed FROM reservations "
        "GROUP BY state").fetchall()]
    row = conn.execute(
        "SELECT COUNT(*) lots, SUM(remaining_shares) remaining, "
        "  MIN(settle_date) earliest, MAX(settle_date) latest "
        "FROM settlement_lots").fetchone()
    lots = dict(row) if row is not None else {}
    return {"reservations": reservations, "lots": lots}, (
        sum(int(r["n"] or 0) for r in reservations) + int(lots.get("lots") or 0))


def snapshot() -> dict:
    """The trade workspace."""
    from alpha_agents.data import memory_store
    conn = memory_store._get_conn()
    return workspace("trade", {
        "book": section(conn, Section(
            source="virtual_portfolio (+ theses, traders/)",
            needs=(Need("virtual_portfolio", (
                        "status", "trader_id", "thesis_id", "open_price",
                        "shares")),
                   Need("theses", ("id", "code", "status"))),
            read=_read_book)),
        "attribution": section(conn, Section(
            source="position_exits → theses",
            needs=(Need("position_exits", (
                        "thesis_id", "net_amount", "return_pct")),
                   Need("virtual_portfolio", ("thesis_id",)),
                   Need("theses", ("id", "code", "status"))),
            read=_read_attribution,
            note=("三跳链：theses.id → virtual_portfolio.thesis_id → "
                  "position_exits.thesis_id。本仓库没有 fill_id / "
                  "ledger_entry_id 列，入场成交即持仓行、退出腿即账本条目。"))),
        "intents": section(conn, Section(
            source="intents",
            needs=(Need("intents", (
                "action", "status", "evidence_json", "information_cutoff")),),
            read=_read_intents)),
        "settlement": section(conn, Section(
            source="reservations + settlement_lots",
            needs=(Need("reservations", ("state", "amount", "consumed_amount")),
                   Need("settlement_lots", ("settle_date", "remaining_shares"))),
            read=_read_settlement)),
    }, code={"book_route": "/api/portfolio"})
