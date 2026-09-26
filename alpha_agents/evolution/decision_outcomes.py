"""The market grades each of the Trader's decisions. The model does not.

The close-day review used to ask the reviewing model whether the day's
decisions were good. It said ``good`` 319 times out of 326 over a 30-day
replay whose account trailed the market by four points, and it also wrote
the support and counterexample counts its own lessons were promoted on. That
is a model grading its own output (``AGENTS.md`` invariant 2).

Here every sealed TraderDecision is graded by what the security did next,
relative to the whole market, once its window has closed:

* the base is the last close the decision could see — the previous session
  for a decision taken before the close, the same session for one taken at
  the close;
* the forward return runs ``horizon`` sessions from that base, and the
  benchmark is the **median** return of every name over the same sessions
  (A-share cross-sections are right-skewed; a mean manufactures an edge);
* BUY / ADD / HOLD are right when the excess is positive; SELL / REDUCE /
  REJECT / WAIT are right when it is negative — staying out of, or getting
  out of, something that then lagged the market was the right call.

Every query is capped at ``as_of``: grades feed the next session's context,
so a grade computed from a bar the Trader has not reached yet is lookahead
arriving through the feedback channel. A window that has not closed is simply
not graded.

Each graded decision also carries situation tags computed here from the base
session's bars. They are what lets learning aggregate: a lesson about
"buying after a limit-up day" is tested on every such decision, not on the
wording a model happened to use once.
"""
from __future__ import annotations

import logging
import sqlite3
import statistics
from datetime import datetime, time

from alpha_agents.data import trader_learning as D, trader_state_store
from alpha_agents.evolution.review import (
    _forward_pct, _market_window_median, _sessions_from,
)

logger = logging.getLogger(__name__)

DEFAULT_HORIZON = 5

#: An excess this small is noise, not a verdict either way.
FLAT_PP = 0.5

#: Decisions taken at or after this moment saw the session's own close.
CLOSE_PHASE = time(14, 0)

LONG_ACTIONS = frozenset({"buy", "add", "hold"})
SHORT_ACTIONS = frozenset({"sell", "reduce", "reject", "wait"})

#: The controlled vocabulary. Each tag is a fact about the base session that
#: the Trader could see when it decided, computed the same way for every
#: decision so a cell's count means the same thing on every row.
TAGS = {
    "t1_up_big": "前一交易日涨幅 ≥ 7%",
    "t1_up": "前一交易日上涨但 < 7%",
    "t1_down": "前一交易日下跌或平",
    "run_up_5d": "5 日累计涨幅 ≥ 10%",
    "below_ma5": "收盘低于 5 日均价",
    "off_5d_high": "收盘较 5 日最高回落 ≥ 5%",
}


def verdict(action: str, excess_pct: float) -> str:
    """right / wrong / flat for one action and its market-relative result."""
    if abs(excess_pct) < FLAT_PP:
        return "flat"
    action = action.lower()
    if action in LONG_ACTIONS:
        return "right" if excess_pct > 0 else "wrong"
    if action in SHORT_ACTIONS:
        return "right" if excess_pct < 0 else "wrong"
    raise ValueError(f"no market grade for action {action!r}")


def situation_tags(hist: sqlite3.Connection, code: str,
                   base_date: str) -> list[str]:
    """Tags for ``code`` from the six sessions ending at ``base_date``."""
    rows = hist.execute(
        "SELECT close, high, change_pct FROM daily_kline "
        "WHERE code = ? AND date <= ? ORDER BY date DESC LIMIT 6",
        (code, base_date)).fetchall()
    if not rows or not rows[0]["close"]:
        return []
    tags = []
    last = rows[0]
    change = last["change_pct"]
    if change is not None:
        change = float(change)
        tags.append("t1_up_big" if change >= 7 else
                    "t1_up" if change > 0 else "t1_down")
    close = float(last["close"])
    if len(rows) >= 6 and rows[5]["close"]:
        if close / float(rows[5]["close"]) - 1 >= 0.10:
            tags.append("run_up_5d")
    five = [float(r["close"]) for r in rows[:5] if r["close"]]
    if len(five) == 5 and close < statistics.mean(five):
        tags.append("below_ma5")
    highs = [float(r["high"]) for r in rows[:5] if r["high"]]
    if len(highs) == 5 and close <= max(highs) * 0.95:
        tags.append("off_5d_high")
    return tags


def sealed_decisions(conn: sqlite3.Connection, *, run_id: str,
                     trader_id: str) -> list[dict]:
    """Every decision ever sealed for this run/trader, once each.

    TraderState keeps the most recent 200; the snapshots are append-only, so
    their union is the complete history.
    """
    seen: dict[str, dict] = {}
    for payload in trader_state_store.snapshot_payloads(
            run_id=run_id, trader_id=trader_id, conn=conn):
        for item in payload.get("recent_decisions") or []:
            decision_id = item.get("decision_id")
            if decision_id and decision_id not in seen:
                seen[decision_id] = item
    return list(seen.values())


def _base_date(hist: sqlite3.Connection, made_at: datetime) -> str | None:
    day = made_at.date().isoformat()
    if made_at.time() >= CLOSE_PHASE:
        return day
    row = hist.execute(
        "SELECT MAX(date) FROM daily_kline WHERE date < ?", (day,)).fetchone()
    return row[0] if row and row[0] else None


def grade(conn: sqlite3.Connection, hist: sqlite3.Connection, *,
          run_id: str, trader_id: str, as_of: str,
          horizon: int = DEFAULT_HORIZON) -> dict:
    """Grade every sealed decision whose window closed by ``as_of``.

    The market helpers below read rows by name (row``date``), which
    requires ``sqlite3.Row``. Both callers hand in a bare
    ``sqlite3.connect`` — the live close-review task and the replay
    runner — whose default factory is a tuple, so grade sets the factory
    itself rather than trusting whoever opened the file. Without it every
    decision lands in "pending" behind a caught TypeError: the day's
    learning is zero and the only trace is one warning line.
    """
    hist.row_factory = sqlite3.Row
    graded = {row["decision_id"] for row in D.decision_outcomes(
        run_id=run_id, trader_id=trader_id, conn=conn)}
    medians: dict[tuple[str, str], float | None] = {}
    counts = {"graded": 0, "pending": 0, "ungradeable": 0}
    for item in sealed_decisions(conn, run_id=run_id, trader_id=trader_id):
        decision_id = item["decision_id"]
        action = str(item.get("action") or "").lower()
        code = item.get("code")
        if decision_id in graded:
            continue
        if not code or action not in LONG_ACTIONS | SHORT_ACTIONS:
            counts["ungradeable"] += 1
            continue
        made_at = datetime.fromisoformat(str(item["made_at"]))
        base = _base_date(hist, made_at)
        if base is None or base > as_of:
            counts["pending"] += 1
            continue
        end = _sessions_from(hist, base, horizon, as_of)
        forward = _forward_pct(hist, code, base, horizon, as_of)
        if end is None or forward is None:
            counts["pending"] += 1
            continue
        key = (base, end)
        if key not in medians:
            medians[key] = _market_window_median(hist, base, end)
        market = medians[key]
        if market is None:
            counts["ungradeable"] += 1
            continue
        excess = forward - market
        D.save_decision_outcome(
            run_id=run_id, trader_id=trader_id, decision_id=decision_id,
            action=action, code=code, decided_on=made_at.date().isoformat(),
            base_date=base, end_date=end, horizon=horizon,
            forward_pct=round(forward, 4), market_median_pct=round(market, 4),
            excess_pct=round(excess, 4), verdict=verdict(action, excess),
            tags=situation_tags(hist, code, base),
            evidence_timeframe=str(item.get("timeframe") or "1d"),
            decision_horizon=str(item.get("decision_horizon") or "3-5d"),
            evidence_scope=str(item.get("evidence_scope") or "replay_daily"),
            conn=conn)
        counts["graded"] += 1
    conn.commit()
    return counts


def summary(rows: list[dict]) -> dict[str, dict[str, int]]:
    """right / wrong / flat counts per action."""
    out: dict[str, dict[str, int]] = {}
    for row in rows:
        cell = out.setdefault(row["action"],
                              {"right": 0, "wrong": 0, "flat": 0})
        cell[row["verdict"]] += 1
    return out
