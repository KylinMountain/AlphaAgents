"""Concept- and industry-level fund flow, aggregated from per-stock flow.

Why this exists. ``scripts/replay_evolution.py`` picks each day's directions
from ``daily_snapshots['concept_fund_flow_hist']``. That key had **no rows**:
its only writer, ``scripts/backfill_sector_flow.py``, needs Eastmoney, and
Eastmoney refuses the datacentre IP ranges we can reach (measured 2026-09-21:
the worker fetches httpbin at 200 and forwards THS at 401, while Eastmoney
answers 520/526/timeout). ``_top_sectors`` therefore returned ``[]`` every
day and the replay silently fell back to its liquid-stock pool — it never
chose a direction from fund flow at all.

``scripts/backfill_daily_snapshots.py`` already named the way out in its own
docstring — "synthesized from stock_individual_fund_flow (not yet
implemented)". This is that.

**How faithful is the proxy.** Summing member ``net_amount`` reproduces the
THS official concept ranking closely: over the ten sessions where both exist
(2026-09-08..09-21), cross-sectional Spearman ran 0.942–0.984, median 0.974,
with three of those sessions absent from the local table entirely and so
genuinely out of sample. Pearson ran 0.963–0.997. The aggregate is about a
tenth larger in magnitude than the official figure, which is why rank, not
level, is what callers should lean on.

**Two look-aheads, one of which this module closes.** Membership is a current
snapshot (see ``ths_local``), so a name that joined a concept last week is in
it for every replayed day — that one stays open and is measured in
``docs/exec-plans/active/2026-09-19-concept-lookahead-ablation.md``. The
sharper one is the concept itself: THS creates a concept *after* its stocks
begin moving together, so a series for a concept that did not exist yet is a
grouping chosen with hindsight. :func:`synth_concept_flow` drops those days
via ``concept_dates``.
"""

from __future__ import annotations

import logging
import sqlite3
from statistics import median

logger = logging.getLogger(__name__)

#: Below this many members priced on the day, the sum is noise rather than a
#: sector reading. Matches the floor used when the proxy was calibrated.
MIN_MEMBERS = 5

#: ``net_amount`` is 万元; the snapshot contract (and Eastmoney's own
#: backfill) stores 元, which ``market_data._historical_sector_flow`` divides
#: by 1e8 to show 亿.
_WAN_TO_YUAN = 1e4


def _member_net(snap_conn: sqlite3.Connection, trade_date: str) -> dict[str, float]:
    """``{6-digit code: 主力净额 in 万元}`` for one session."""
    return {code: amount for code, amount in snap_conn.execute(
        "SELECT code, net_amount FROM stock_fund_flow_daily "
        "WHERE trade_date = ? AND net_amount IS NOT NULL", (trade_date,))}


def _member_pct(hist_conn: sqlite3.Connection, iso_date: str) -> dict[str, float]:
    """``{6-digit code: pct change}`` for one session, from daily K-lines.

    ``stock_fund_flow_daily.pct_change`` is NULL throughout — the Tushare
    ``moneyflow`` endpoint carries flow only — so the move comes from the
    K-line table instead of being reported as zero.
    """
    return {code: pct for code, pct in hist_conn.execute(
        "SELECT code, change_pct FROM daily_kline "
        "WHERE date = ? AND change_pct IS NOT NULL", (iso_date,))}


def _groups_concept(stocks_conn: sqlite3.Connection,
                    allowed: set[str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for name, code in stocks_conn.execute(
            "SELECT c.name, cs.stock_code FROM concept_stocks cs "
            "JOIN concepts c ON c.id = cs.concept_id"):
        if name in allowed:
            groups.setdefault(name, []).append(code)
    return groups


def _groups_industry(stocks_conn: sqlite3.Connection) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for industry, code in stocks_conn.execute(
            "SELECT industry, code FROM stocks "
            "WHERE industry IS NOT NULL AND industry <> ''"):
        groups.setdefault(industry, []).append(code)
    return groups


def _aggregate(groups: dict[str, list[str]], net: dict[str, float],
               pct: dict[str, float]) -> list[dict]:
    """One snapshot's ``sectors`` list, ranked by net inflow."""
    rows = []
    for name, codes in groups.items():
        hits = [net[c] for c in codes if c in net]
        if len(hits) < MIN_MEMBERS:
            continue
        moves = [pct[c] for c in codes if c in pct]
        rows.append({
            "code": name,          # no Eastmoney BK code here; the name is the key
            "name": name,
            "main_net": sum(hits) * _WAN_TO_YUAN,
            # Median, not mean: A-share cross-sections are right-skewed and
            # AGENTS.md makes this the house rule.
            "close_pct_change": round(median(moves), 4) if moves else 0.0,
            "members_priced": len(hits),
            "members_total": len(codes),
        })
    rows.sort(key=lambda r: r["main_net"], reverse=True)
    for rank, row in enumerate(rows, 1):
        row["rank"] = rank
    return rows


def synth_concept_flow(stocks_conn: sqlite3.Connection,
                       snap_conn: sqlite3.Connection,
                       hist_conn: sqlite3.Connection,
                       trade_date: str) -> list[dict]:
    """Concept-level flow for ``trade_date`` (YYYYMMDD), newest concepts gated.

    Concepts whose ``created_date`` is after the session are excluded — see
    the module docstring.
    """
    from alpha_agents.data.concept_dates import concepts_as_of
    iso = "%s-%s-%s" % (trade_date[:4], trade_date[4:6], trade_date[6:])
    allowed = concepts_as_of(stocks_conn, iso)
    groups = _groups_concept(stocks_conn, allowed)
    return _aggregate(groups, _member_net(snap_conn, trade_date),
                      _member_pct(hist_conn, iso))


def synth_industry_flow(stocks_conn: sqlite3.Connection,
                        snap_conn: sqlite3.Connection,
                        hist_conn: sqlite3.Connection,
                        trade_date: str) -> list[dict]:
    """Industry-level flow for ``trade_date``, over the baostock CSRC classes.

    These are not the THS 90 industries the live path reads, so the names
    differ; the replay already fuzzy-matches sector names to concepts.
    """
    iso = "%s-%s-%s" % (trade_date[:4], trade_date[4:6], trade_date[6:])
    return _aggregate(_groups_industry(stocks_conn),
                      _member_net(snap_conn, trade_date),
                      _member_pct(hist_conn, iso))
