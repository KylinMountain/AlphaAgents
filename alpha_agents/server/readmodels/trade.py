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


def _trader_names() -> dict[str, str]:
    """``trader_id`` → display name, so a row can say who owns it."""
    try:
        from alpha_agents.data.trader import load_traders
        return {t.id: t.name for t in load_traders()}
    except Exception as e:
        logger.warning("Trader names unavailable: %s", e)
        return {}


def _last_close(code: str) -> float | None:
    """The latest close this repository has for ``code``, or ``None``.

    Read from the local K-line store and never from the network: a read model
    answers from what is on disk, and reaching for a feed here would turn the
    portfolio page into a scrape. Stale is honest — the card names the day the
    price is from — a hang is not.
    """
    try:
        from alpha_agents.data.market_history import get_local_history
        bars = get_local_history(code, days=1)
    except Exception as e:
        logger.warning("No local price for %s: %s", code, e)
        return None
    if not bars:
        return None
    return bars[-1].get("close")


def _attach_unrealized(positions: list[dict]) -> list[dict]:
    """Give each open position the return the page is asked for.

    ``return_pct`` is a *closed* column: it is written when the position ends,
    so on an open row it is NULL and the card read it as a flat 0.0% — every
    position looked break-even, which on a book holding winners and losers is
    not neutrality, it is a hole. The unrealized figure is computed here, from
    the last close on disk, and carries ``None`` when there is no price rather
    than a confident zero: "no price" and "no change" are different answers.
    """
    names = _trader_names()
    for p in positions:
        last = _last_close(p["code"])
        open_price = p.get("open_price") or 0
        shares = p.get("shares") or 0
        if last and open_price > 0:
            p["last_price"] = last
            p["unrealized_pct"] = round((last - open_price) / open_price * 100, 2)
            p["unrealized_amount"] = round((last - open_price) * shares, 2)
        else:
            p["last_price"] = None
            p["unrealized_pct"] = None
            p["unrealized_amount"] = None
        tid = p.get("trader_id") or ""
        p["trader_name"] = names.get(tid, tid) or None
    return positions


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
    positions = _attach_unrealized(positions)
    names = _trader_names()
    for o in pending:
        tid = o.get("trader_id") or ""
        o["trader_name"] = names.get(tid, tid) or None
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
    """Who earned what: realised P&L rolled up by the thesis that named it.

    Two sources, and both are needed or the chain under-reports by exactly the
    amount it cannot explain. An exit leg carries its own ``thesis_id``; a
    position closed whole carries ``return_amount`` on the position row and no
    leg at all — and the ledger counts both
    (``trade_ledger.realized_total`` = legs + legacy). Reading only the legs
    made this page report a chain worth 0 元 next to a ledger worth
    −5,616.78 元, which is not a neutral empty state, it is a wrong number.

    The headline total is taken from the ledger itself rather than recomputed,
    so the page and the ledger cannot disagree: same function, same expression,
    same number. What the rollup adds is the *split* — how much of that total
    a thesis can be named for.

    An exit with no ``thesis_id``, or a position closed before the thesis
    mechanism existed, is counted separately rather than dropped. That is not
    a rounding detail — it is the size of the gap between the ledger and the
    ideas, and a rollup that hides it would make the chain look complete when
    it is not.
    """
    from alpha_agents.data import memory_store
    conn = memory_store._get_conn()

    ledger_total: float | None
    try:
        from alpha_agents.data import trade_ledger
        from alpha_agents.data.trader import load_traders
        ledger_total = float(sum(
            trade_ledger.realized_total(conn, t.id) for t in load_traders()))
    except Exception as e:
        logger.warning("Ledger total unavailable; attribution is legs-only: %s", e)
        ledger_total = None

    legs_rows = [dict(r) for r in conn.execute(
        "SELECT e.thesis_id, COUNT(*) legs, SUM(e.net_amount) net, "
        "  SUM(e.return_pct > 0) wins, t.code, t.status AS thesis_status "
        "FROM position_exits e LEFT JOIN theses t ON t.id = e.thesis_id "
        "GROUP BY e.thesis_id").fetchall()]
    # Same expression as ``trade_ledger.realized_total``'s legacy term: a row
    # with its own legs contributes only an explicit legacy figure, a row
    # without them contributes ``return_amount``. Copying the expression rather
    # than inventing a simpler one is what keeps the split summing to the
    # headline — the two have to describe the same money. The WHERE keeps out
    # the rows that contribute nothing (every open and pending position): left
    # in, they arrived as zero-value rows and the table read as fourteen
    # theses that had all broken exactly even.
    legacy_rows = [dict(r) for r in conn.execute(
        "SELECT p.thesis_id, COUNT(*) positions, "
        "  SUM(COALESCE(p.legacy_realized_amount, p.return_amount)) legacy_net "
        "FROM virtual_portfolio p "
        "WHERE p.legacy_realized_amount IS NOT NULL "
        "   OR (p.return_amount IS NOT NULL AND NOT EXISTS ("
        "        SELECT 1 FROM position_exits e WHERE e.position_id = p.id)) "
        "GROUP BY p.thesis_id").fetchall()]

    merged: dict = {}
    unattributed = {"legs": 0, "legs_net": 0.0,
                    "positions": 0, "legacy_net": 0.0}
    for r in legs_rows:
        tid = r["thesis_id"]
        if tid is None:
            unattributed["legs"] = r["legs"] or 0
            unattributed["legs_net"] = r["net"] or 0.0
            continue
        row = merged.setdefault(tid, {
            "thesis_id": tid, "code": r.get("code"),
            "thesis_status": r.get("thesis_status"),
            "legs": 0, "wins": 0, "net": 0.0, "legacy_net": 0.0,
            "positions": 0})
        row["legs"] += r["legs"] or 0
        row["wins"] += r["wins"] or 0
        row["net"] += r["net"] or 0.0
    for r in legacy_rows:
        tid = r["thesis_id"]
        if tid is None:
            unattributed["positions"] = r["positions"] or 0
            unattributed["legacy_net"] = r["legacy_net"] or 0.0
            continue
        row = merged.setdefault(tid, {
            "thesis_id": tid, "code": None, "thesis_status": None,
            "legs": 0, "wins": 0, "net": 0.0, "legacy_net": 0.0,
            "positions": 0})
        row["positions"] += r["positions"] or 0
        row["legacy_net"] += r["legacy_net"] or 0.0

    by_thesis = sorted(merged.values(),
                       key=lambda d: d["net"] + d["legacy_net"], reverse=True)
    for d in by_thesis:
        d["total"] = round((d["net"] or 0) + (d["legacy_net"] or 0), 2)
    unattributed["total"] = round(
        (unattributed["legs_net"] or 0) + (unattributed["legacy_net"] or 0), 2)
    attributed = round(sum(d["total"] for d in by_thesis), 2)

    totals = dict(conn.execute(
        "SELECT COUNT(*) legs, SUM(thesis_id IS NULL) unattributed, "
        "  SUM(net_amount) net FROM position_exits").fetchone() or {})
    orders = dict(conn.execute(
        "SELECT COUNT(*) orders, SUM(thesis_id IS NULL) without_thesis "
        "FROM virtual_portfolio").fetchone() or {})
    return {
        "by_thesis": by_thesis,
        "exits": totals,
        "orders": orders,
        "realized": {"total": ledger_total, "attributed": attributed,
                     "unattributed": unattributed},
    }, len(by_thesis) + int(unattributed["positions"])


def _read_intents() -> tuple[dict, int]:
    """The single door's trail: what was asked, and what came back.

    A row whose ``reject_reason`` records a raised exception is counted as a
    **fault**, not as a refusal (D32). The panel used to print the exception
    text under 拒绝原因, so an outage — 68 closes failing on one missing
    column — rendered as 68 ordinary policy verdicts, and a normal verdict is
    exactly the thing nobody re-examines.
    """
    from alpha_agents.data import intent
    rows = intent.history(limit=100)
    never = intent.never_decided()
    counts: dict = {}
    faults = 0
    for row in rows:
        if intent.is_fault(row.get("reject_reason")):
            faults += 1
            row["fault"] = True
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {
        "recent": rows,
        "by_status": counts,
        "never_decided": never,
        "faults": faults,
    }, len(rows)


def _read_settlement() -> tuple[dict, int]:
    """Cash committed to live orders, shares inside T+1, and cash in transit.

    **Cash in transit is reported, not hidden.** Settlement separates 可用 from
    可取 — sale proceeds are spendable the day they arrive and only withdrawable
    the day after — so buying power includes this money while the balance that
    can leave the account does not. It used to be subtracted from available
    capital instead, which applied the withdrawal rule to buying power and so
    forbade selling one name and buying another the same day (tech-debt D19).
    Reporting it here is what keeps the two figures distinguishable.
    """
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
    transit = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(net_amount), 0) amount, "
        "  MIN(settle_date) earliest, MAX(settle_date) latest "
        "FROM pending_settlements WHERE released = 0").fetchone()
    return {"reservations": reservations, "lots": lots,
            "cash_in_transit": dict(transit) if transit is not None else {}}, (
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
            source="reservations + settlement_lots + pending_settlements",
            needs=(Need("reservations", ("state", "amount", "consumed_amount")),
                   Need("settlement_lots", ("settle_date", "remaining_shares")),
                   Need("pending_settlements", ("released", "net_amount",
                                                "settle_date"))),
            read=_read_settlement)),
    }, code={"book_route": "/api/portfolio"})
