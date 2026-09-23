"""A theme's core stocks: its members that the money has actually been going to.

The fill this replaces took the first matches of a semantic stock search,
which come back in code order — so 存储芯片's "core" was 002138, 002436,
002553, … ascending, and PCB概念's was TCL科技 and 格力电器. Core means where
the money concentrates, so it is measured: the theme's concept members
(``stocks.db:concept_stocks``) ranked by main-force net amount summed over the
last :data:`DAYS` sessions (``stock_fund_flow_daily``). The top one is the
leader.
"""

from __future__ import annotations

import logging
import sqlite3

from alpha_agents import config

logger = logging.getLogger(__name__)

DAYS = 5
N = 10


def _ro(path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def members(theme: str) -> list[tuple[str, str]]:
    """(code, name) of every stock in the concept named ``theme``."""
    try:
        conn = _ro(config.DB_PATH)
    except sqlite3.Error as e:
        logger.warning("Concept membership unavailable: %s", e)
        return []
    try:
        return conn.execute(
            "SELECT cs.stock_code, COALESCE(s.name, cs.stock_code) "
            "FROM concept_stocks cs JOIN concepts c ON c.id = cs.concept_id "
            "LEFT JOIN stocks s ON s.code = cs.stock_code WHERE c.name = ? "
            "AND COALESCE(s.is_st, 0) = 0",
            (theme,)).fetchall()
    except sqlite3.Error as e:
        logger.warning("Concept membership for %s unreadable: %s", theme, e)
        return []
    finally:
        conn.close()


def core_stocks(theme: str, *, days: int = DAYS, n: int = N) -> list[dict]:
    """The top ``n`` members by net main-force money over ``days`` sessions.

    Empty when the theme is not a concept on file or no flow is recorded —
    an empty list is the honest answer, not a search result in code order.
    """
    mem = dict(members(theme))
    if not mem:
        return []
    try:
        conn = _ro(config.DATA_DIR / "market_snapshots.db")
    except sqlite3.Error as e:
        logger.warning("Stock fund flow unavailable: %s", e)
        return []
    try:
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT trade_date FROM stock_fund_flow_daily "
            "ORDER BY trade_date DESC LIMIT ?", (days,)).fetchall()]
        if not dates:
            return []
        marks = ",".join("?" * len(dates))
        rows = conn.execute(
            f"SELECT code, SUM(net_amount) FROM stock_fund_flow_daily "
            f"WHERE trade_date IN ({marks}) GROUP BY code", dates).fetchall()
    except sqlite3.Error as e:
        logger.warning("Stock fund flow unreadable: %s", e)
        return []
    finally:
        conn.close()
    ranked = sorted(((c, v) for c, v in rows if c in mem and v is not None),
                    key=lambda x: -x[1])[:n]
    return [{"code": c, "name": mem[c], "role": "龙头" if i == 0 else "核心",
             "net_5d_wan": round(v, 1)} for i, (c, v) in enumerate(ranked)]
