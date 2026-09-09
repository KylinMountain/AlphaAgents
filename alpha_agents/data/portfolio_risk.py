"""Risk that only exists at the portfolio level.

Three things the per-position rules cannot see, however well they work.

**Whether any of it beat doing nothing.** get_portfolio_stats reported win
rate, average return and best/worst trade, and not one field said whether
the account beat the market. Earning 3% in a month the market rose 5% is
a loss. residual_alpha already does this properly per prediction; the
portfolio had no equivalent.

**Correlation.** MAX_THEME_PCT caps one theme at 30%, so two themes can
take 60%. Measured on live data, 金属铜 and 小金属概念 share 6 of their 10
constituents — the same bet under two names, and the cap saw two.

**Drawdown.** A single position stops out at −8%, but five positions each
down 7% is a −35% account with nothing to stop it.

The benchmark is the equal-weight return of the whole market rather than
an index. That is deliberate, not a shortcut: this strategy picks small
and mid caps out of concept themes, so measuring it against a cap-weighted
index would score a size exposure as skill.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime

from alpha_agents.config import DATA_DIR
from alpha_agents.data.memory_store import _get_conn, _write_lock

logger = logging.getLogger(__name__)

_HISTORY_DB = DATA_DIR / "market_history.db"

# Two themes count as one bet above this share of the smaller one's
# constituents. Overlap coefficient, not Jaccard: a broad theme and a
# narrow one that sits entirely inside it are the same bet, and Jaccard
# would call them distinct because the sizes differ.
#
# Calibrated against the live data it was built for: 金属铜/小金属概念
# measure 0.60, 西部大开发/金属铜 measure 0.20. Anything in between is a
# judgement call, and 0.35 leans toward treating near-duplicates as one.
CLUSTER_OVERLAP = 0.35

# A cluster may hold more than one theme's worth, but not two. Setting it
# at MAX_THEME_PCT would punish the system for correctly noticing the
# themes are related; setting it at 2x would mean the check does nothing.
MAX_CLUSTER_PCT = 0.30

# New risk stops here. Deliberately a gate on opening, not a forced
# liquidation: selling everything at a drawdown level sells the bottom,
# and in a system whose whole purpose is learning from resolved theses it
# would destroy the samples before they resolve. Existing positions keep
# their own stops.
MAX_PORTFOLIO_DRAWDOWN_PCT = 12.0


# ── Benchmark ───────────────────────────────────────────────

def benchmark_return(since: str, until: str) -> float | None:
    """Equal-weight whole-market return between two dates, in percent.

    Compounded across sessions rather than summed, since a position held
    over several days earns the compounded path.
    """
    if not since or not until or since > until:
        return None
    try:
        conn = sqlite3.connect(f"file:{_HISTORY_DB}?mode=ro", uri=True)
    except sqlite3.Error as e:
        logger.debug("Benchmark unavailable: %s", e)
        return None
    try:
        rows = conn.execute(
            "SELECT date, AVG(change_pct) FROM daily_kline "
            "WHERE date > ? AND date <= ? GROUP BY date ORDER BY date",
            (since, until)).fetchall()
    except sqlite3.Error as e:
        logger.debug("Benchmark query failed: %s", e)
        return None
    finally:
        conn.close()

    if not rows:
        return None
    growth = 1.0
    for _date, avg in rows:
        growth *= 1 + (avg or 0) / 100
    return round((growth - 1) * 100, 2)


def trade_excess(position: dict) -> float | None:
    """A closed trade's return net of what the market did meanwhile.

    Measured over that trade's own holding window, not a calendar month.
    A strategy holding cash through a crash should not be charged for the
    crash, and one that rode a rising tide should not be paid for it.
    """
    ret = position.get("return_pct")
    if ret is None:
        return None
    bench = benchmark_return(position.get("open_date") or "",
                            position.get("close_date") or "")
    return None if bench is None else round(ret - bench, 2)


# ── Equity curve ────────────────────────────────────────────

def record_equity_mark(date: str, price_map: dict[str, float] | None = None) -> dict:
    """Store one day's account value. Called by the review.

    Without a daily mark there is no curve, so there is no drawdown and no
    time-weighted comparison to the benchmark — only per-trade numbers,
    which say nothing about the days the account sat in cash.
    """
    from alpha_agents.data.portfolio import (
        TOTAL_CAPITAL, get_available_capital, get_open_positions,
    )

    positions = get_open_positions()
    if positions and price_map is None:
        try:
            from alpha_agents.data.market_data import get_realtime_quotes
            quotes = get_realtime_quotes([p["code"] for p in positions])
            price_map = {c: d["price"] for c, d in (quotes or {}).items()}
        except Exception as e:
            logger.warning("Equity mark falling back to cost basis: %s", e)
            price_map = {}
    price_map = price_map or {}

    market_value = 0.0
    unpriced = []
    for pos in positions:
        price = price_map.get(pos["code"]) or pos.get("open_price") or 0
        if not price_map.get(pos["code"]):
            unpriced.append(pos["code"])
        market_value += price * (pos.get("shares") or 0)

    cash = get_available_capital()
    equity = round(cash + market_value, 2)
    mark = {
        "date": date, "equity": equity, "cash": round(cash, 2),
        "market_value": round(market_value, 2), "positions": len(positions),
        # Recorded so a later reader knows which marks are soft. A day
        # marked at cost basis understates both gains and drawdown.
        "unpriced": unpriced,
        "return_pct": round((equity - TOTAL_CAPITAL) / TOTAL_CAPITAL * 100, 2),
    }

    with _write_lock:
        conn = _get_conn()
        conn.execute("DELETE FROM daily_snapshots WHERE date = ? "
                     "AND data_type = 'equity_mark'", (date,))
        conn.execute(
            "INSERT INTO daily_snapshots (date, data_type, data, created_at) "
            "VALUES (?, 'equity_mark', ?, ?)",
            (date, json.dumps(mark, ensure_ascii=False),
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
    logger.info("Equity mark %s: %.0f元 (%+.2f%%, %d 持仓)",
                date, equity, mark["return_pct"], len(positions))
    return mark


def equity_curve(days: int = 90) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT data FROM daily_snapshots WHERE data_type = 'equity_mark' "
        "AND date >= date('now', ?) ORDER BY date", (f"-{days} days",)
    ).fetchall()
    out = []
    for r in rows:
        try:
            out.append(json.loads(r["data"]))
        except json.JSONDecodeError:
            logger.debug("Skipping an unreadable equity mark")
    return out


def current_drawdown() -> dict:
    """Distance from the account's high-water mark.

    Returns {} when there are not two marks to compare — a single day is
    a level, not a drawdown, and reporting 0% off one mark would read as
    "no drawdown" rather than "not enough history".
    """
    curve = equity_curve(days=365)
    if len(curve) < 2:
        return {}
    peak = max(m["equity"] for m in curve)
    now = curve[-1]["equity"]
    dd = 0.0 if peak <= 0 else max(0.0, (peak - now) / peak * 100)
    return {"peak": peak, "now": now, "drawdown_pct": round(dd, 2),
            "blocked": dd >= MAX_PORTFOLIO_DRAWDOWN_PCT, "marks": len(curve)}


# ── Correlated themes ───────────────────────────────────────

def _theme_members() -> dict[str, set[str]]:
    from alpha_agents.data.memory_store import get_active_themes
    out = {}
    for t in get_active_themes():
        try:
            stocks = json.loads(t["core_stocks"] or "[]")
        except json.JSONDecodeError:
            stocks = []
        out[t["name"]] = {s.get("code") for s in stocks if s.get("code")}
    return out


def theme_overlap(a: set[str], b: set[str]) -> float:
    """Overlap coefficient — |A∩B| / min(|A|,|B|)."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def theme_cluster(theme: str, members: dict[str, set[str]] | None = None) -> set[str]:
    """Every theme that is really the same bet as this one, including it.

    Single-link: A clusters with B and B with C puts all three together
    even when A and C barely overlap. That is the right error to make
    here — the cost of treating two related themes as one is a smaller
    position, and the cost of missing it is the concentration this exists
    to catch.

    A theme with no recorded constituents clusters alone. It is not
    evidence of independence, only absence of evidence, so it is logged
    rather than silently trusted.
    """
    members = members if members is not None else _theme_members()
    own = members.get(theme) or set()
    if not own:
        logger.debug("Theme %r has no constituents on file — treated as "
                     "uncorrelated, which may understate concentration", theme)
        return {theme}

    cluster = {theme}
    changed = True
    while changed:
        changed = False
        for name, codes in members.items():
            if name in cluster or not codes:
                continue
            if any(theme_overlap(codes, members[c]) >= CLUSTER_OVERLAP
                   for c in cluster if members.get(c)):
                cluster.add(name)
                changed = True
    return cluster


def cluster_room(theme: str) -> float:
    """Money still available to this theme's correlated cluster, in yuan."""
    from alpha_agents.data.portfolio import TOTAL_CAPITAL, get_theme_exposure

    cluster = theme_cluster(theme)
    used = sum(get_theme_exposure(name) for name in cluster)
    room = TOTAL_CAPITAL * MAX_CLUSTER_PCT - used
    if len(cluster) > 1 and room <= 0:
        logger.info("Cluster %s is full (%.0f元 of %.0f元) — %r gets no more",
                    "+".join(sorted(cluster)), used,
                    TOTAL_CAPITAL * MAX_CLUSTER_PCT, theme)
    return max(0.0, room)


def describe_clusters() -> str:
    """Correlated themes, for a report or the agent's context."""
    members = _theme_members()
    seen, lines = set(), []
    for name in members:
        if name in seen:
            continue
        cluster = theme_cluster(name, members)
        seen |= cluster
        if len(cluster) > 1:
            shared = set.intersection(*(members[c] for c in cluster if members[c]))
            lines.append(f"• {' + '.join(sorted(cluster))}"
                         f"（共用 {len(shared)} 只成分股，视为同一个赌注，"
                         f"合计上限 {MAX_CLUSTER_PCT:.0%}）")
    if not lines:
        return ""
    return "【相关主线】\n" + "\n".join(lines)


# ── Entry quality ───────────────────────────────────────────
#
# A cancelled order is not a non-event. "价格已涨走" means the pick was
# right and the entry price was too greedy — the system waited for a
# pullback that a strengthening theme was never going to give it. That is
# a lesson about *where to buy*, and it is completely separate from
# whether the stock was worth buying.
#
# It was being thrown away. The learning layer only ever saw filled
# orders, so no amount of history could teach it that its entry zones sit
# below where price actually trades. Worse, the bias is systematic:
# a pullback entry only fills when the theme is weakening, so the book
# fills up with the picks that went wrong and misses the ones that went
# right. Today, three fills were all pullbacks and both run-aways were
# themes that strengthened.

_ENTRY_OUTCOMES = {
    "涨走": "介入价太低",
    "资金不足": "资金分配",
    "当日未成交": "价格未到",
    "论点已失效": "论点先于价格失效",
    "主线走弱": "主线先于价格失效",
    "主线衰退": "主线先于价格失效",
    "主线不存在": "主线不存在",
}


def classify_cancellation(reason: str) -> str:
    for key, label in _ENTRY_OUTCOMES.items():
        if key in (reason or ""):
            return label
    return "其他"


def entry_quality(days: int = 30) -> dict:
    """How the orders that never became positions failed.

    The headline is ``missed_right``: how often the pick was right and the
    entry price was wrong. A system that keeps being right and keeps not
    getting filled has an entry problem, not a selection problem, and
    nothing else in the stats will ever say so.
    """
    conn = _get_conn()
    rows = conn.execute(
        "SELECT close_reason FROM virtual_portfolio WHERE status = 'cancelled' "
        "AND order_date >= date('now', ?)", (f"-{days} days",)).fetchall()
    if not rows:
        return {"n": 0}

    counts: dict[str, int] = {}
    for r in rows:
        label = classify_cancellation(r["close_reason"])
        counts[label] = counts.get(label, 0) + 1

    filled = conn.execute(
        "SELECT COUNT(*) FROM virtual_portfolio WHERE open_date >= date('now', ?) "
        "AND status != 'pending' AND open_price > 0", (f"-{days} days",)
    ).fetchone()[0]

    missed = counts.get("介入价太低", 0)
    attempts = filled + len(rows)
    return {
        "n": len(rows), "filled": filled, "by_reason": counts,
        "missed_right": missed,
        "missed_rate": round(missed / attempts * 100, 1) if attempts else 0.0,
    }


def inject_entry_quality(days: int = 30) -> str:
    """The entry-price lesson, for the review and the morning prompt."""
    q = entry_quality(days=days)
    if not q.get("n"):
        return ""
    lines = [f"【介入质量】近{days}天 成交 {q['filled']} 笔、撤单 {q['n']} 笔"]
    for label, n in sorted(q["by_reason"].items(), key=lambda kv: -kv[1]):
        lines.append(f"• {label}: {n} 笔")
    if q["missed_rate"] >= 25:
        lines.append(f"→ **{q['missed_rate']:.0f}% 的介入意图是「选对了但没买到」**（价格涨走）。"
                     "介入区间系统性偏低——你在等一个走强的主线不会给的回调。"
                     "把区间上移，或对高信心的标的直接用市价。")
    elif q["missed_right"]:
        lines.append(f"→ 其中 {q['missed_right']} 笔是选对了但价格涨走，"
                     "属于介入价位问题，不是选股问题。")
    return "\n".join(lines)
