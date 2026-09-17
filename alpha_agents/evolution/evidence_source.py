"""Collect closed trades from the live book as analyser input.

``evolution.evidence`` states the proposition and decides the split. What it
deliberately does not know is where the trades came from — it takes
:class:`~alpha_agents.evolution.evidence.Trade` values. This module is the
production half of that seam: it reads the live book and the price corpus and
hands over the trades.

Why it exists as a separate module rather than a function inside
``walk_forward``: until now the only caller of the analyser was the replay
runner, so **production never produced a candidate that could pass D25** —
all five production proposers write empty citation buckets, and the one thing
that can fill them lived in a script. The LEARN half of the loop was alive
only under replay.

The feature is re-read from the corpus at the order's own T-1 rather than
taken from the position row, because it is not stored on the position. That
is deliberate: it makes the claim reproducible from the corpus plus the book,
with nothing in between that only one process knows. It is also the same rule
the replay uses, so a production observation and a replay observation of the
same trade are the same observation.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

from alpha_agents.evolution.evidence import Trade

logger = logging.getLogger(__name__)


def _sessions() -> list[str]:
    """Every session the corpus holds, oldest first."""
    from alpha_agents.data import market_history as mh
    return mh.get_available_dates()


def previous_session(day: str, sessions: list[str] | None = None) -> str | None:
    """The session before ``day``, or None when there is not one.

    ``None`` rather than an earlier guess: a T-1 feature that silently means
    "the closest bar I could find" is a different feature, and the caller
    drops those trades rather than measuring them on the wrong day.
    """
    days = sessions if sessions is not None else _sessions()
    idx = None
    for i, d in enumerate(days):
        if d == day:
            idx = i
            break
    if idx is None or idx == 0:
        return None
    return days[idx - 1]


def collect_closed_trades(*, as_of: str, conn: sqlite3.Connection | None = None
                          ) -> list[Trade]:
    """Every position closed on or before ``as_of``, with its T-1 feature.

    ``episode_id`` is the join that makes a citation possible at all. A
    position with no episode is still returned, with ``episode_id=None``: its
    return is a fact and it counts towards ``n``, but it cannot be cited, so
    the analyser excludes it from the split rather than citing something it is
    not. The count of those is what the bundle reports as ``excluded``.
    """
    from alpha_agents.data import market_history as mh
    from alpha_agents.data.memory_store import _get_conn

    conn = conn if conn is not None else _get_conn()
    rows = conn.execute(
        "SELECT p.id, p.code, p.order_date, p.close_date, p.return_pct,"
        "       p.close_reason, e.id AS episode_id"
        "  FROM virtual_portfolio p"
        "  LEFT JOIN episodes e ON e.position_id = p.id"
        " WHERE p.close_date IS NOT NULL AND p.close_date <= ?"
        "   AND p.return_pct IS NOT NULL"
        " ORDER BY p.close_date, p.id", (as_of,)).fetchall()

    days = _sessions()
    # One bar lookup per session, cached: the loop below would otherwise read
    # the same day's bars once per position closed on it.
    bar_cache: dict[str, dict] = {}

    def _bars(day: str) -> dict:
        if day not in bar_cache:
            bar_cache[day] = {r["code"]: r for r in mh.get_klines_for_date(day)}
        return bar_cache[day]

    out: list[Trade] = []
    for row in rows:
        prev = previous_session(row["order_date"], days)
        chg = None
        if prev is not None:
            bar = _bars(prev).get(row["code"])
            if bar is not None:
                chg = bar.get("change_pct")
        out.append(Trade(
            position_id=int(row["id"]),
            code=row["code"],
            return_pct=float(row["return_pct"]),
            close_date=row["close_date"],
            t1_change=None if chg is None else float(chg),
            episode_id=int(row["episode_id"]) if row["episode_id"] else None,
            close_reason=row["close_reason"],
        ))
    return out


def eligible_window_start(trades: list[Trade]) -> str:
    """The first close date among the trades, or ``as_of`` when there are none.

    Used as the observation's window start. It is read from the data rather
    than passed in, so a caller cannot describe a window the trades did not
    come from.
    """
    dates = [t.close_date for t in trades if t.close_date]
    return min(dates) if dates else datetime.now().strftime("%Y-%m-%d")


def observe(*, as_of: str | None = None, trader: str = "default",
            entry_zone: tuple[float, float] = (0.97, 1.005),
            source: str = "production_evidence",
            conn: sqlite3.Connection | None = None) -> dict | None:
    """Collect, analyse and save one production observation.

    ``None`` when the analyser declines to state anything — too few citable
    trades, or no contrast to state. A caller that gets ``None`` should report
    it as "nothing to state yet" rather than as a failure; the analyser
    refuses to invent a weaker proposition to avoid returning it.

    The write lands in ``observation`` and nothing here advances it. Promotion
    is an audited human act and ``learning_candidates``' own design note says
    the pipeline must not drive ``status``.
    """
    from alpha_agents.evolution import evidence as EV

    when = as_of or datetime.now().strftime("%Y-%m-%d")
    trades = collect_closed_trades(as_of=when, conn=conn)
    if not trades:
        logger.info("Evidence: no closed trades as of %s", when)
        return None

    result = EV.analyse_and_save(
        trades,
        source=source,
        source_date=when,
        trader=trader,
        entry_zone=entry_zone,
        window_start=eligible_window_start(trades),
    )
    if result is None:
        logger.info(
            "Evidence: %d closed trade(s) as of %s, too thin or without "
            "contrast to state anything", len(trades), when)
        return None

    # `save_candidate` dedupes on the evidence, and the proposal fields are
    # deliberately excluded from that fingerprint. So re-observing identical
    # evidence returns the existing row — which is the idempotency the design
    # wants, and is *wrong* to report as a new observation when the row it
    # matched has been retired: the corrected proposal is never written and
    # the caller would believe it was.
    #
    # Reported rather than raised. Returning the existing row is defensible
    # (the same evidence really is the same candidate); what is not
    # defensible is claiming it is fresh. See D42.
    from alpha_agents.data import learning_candidates as LC
    row = LC.get_candidate(result["candidate_id"])
    status = (row or {}).get("status")
    result["status"] = status
    if status != LC.OBSERVATION:
        logger.warning(
            "Evidence: candidate #%d already exists in status %r, so this "
            "observation was NOT written as a new candidate. The fingerprint "
            "is over the evidence, and a retired row still matches it — "
            "corrected proposals over unchanged evidence cannot be recorded "
            "(D42).",
            result["candidate_id"], status)
        result["deduped_into_existing"] = True
    else:
        logger.info(
            "Evidence: candidate #%d over n=%d (%d support / %d oppose)",
            result["candidate_id"], result["n"], result["supporting"],
            result["opposing"])
    return result
