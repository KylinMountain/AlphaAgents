"""The trader's close of day, live: 19:00, once the day's bars are on disk.

The steps are ``evolution.close_day`` — trade reviews, the market review,
the handbook rewrite — shared with the replay. This module assembles what the
live side knows: the day's facts from ``daily_kline`` via
``evolution.session_facts`` (limit-ups, streaks, failed limit-ups, each
concept's move, money and news), which boards the trader tracked or held,
and its exposure against the market from its equity marks.

When the day's K-line is not on disk yet, :func:`market_facts` reads the
intraday snapshots instead, so the review still has the tape.
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)

#: How many concept boards to show from each end of today's flow ranking.
_BOARDS = 10


def market_facts(today: str) -> str:
    """Today's session as text, from the snapshots on disk."""
    import pandas as pd

    from alpha_agents.data import snapshot_store as S

    lines = []
    b = S.read_latest_breadth()
    if b and str(b.get("captured_at", "")).startswith(today):
        lines.append(
            f"市场宽度（{b['captured_at'][11:16]}）：上涨 {b.get('advances')}、下跌 "
            f"{b.get('declines')}、平 {b.get('flat')}；涨停 {b.get('limit_up')}、"
            f"跌停 {b.get('limit_down')}")
    boards = S.read_latest_sector_flow(scope="concept", limit=5000)
    boards = [x for x in boards if str(x.get("captured_at", "")).startswith(today)]
    if boards:
        # The leader's own move goes with its name: without it the trader
        # wrote "only if today's gain < 4%" about a leader that closed +20%.
        def fmt(x):
            lead = x.get("leader") or "-"
            pct = x.get("leader_change_pct")
            if pct is not None:
                lead += f"({pct:+.1f}%)"
            return (f"{x['sector_name']} {x['change_pct']:+.2f}% "
                    f"净流入{x['net_flow_yi']:+.1f}亿 领涨{lead}")
        lines.append("资金流入最多的概念：" + "；".join(fmt(x) for x in boards[:_BOARDS]))
        lines.append("资金流出最多的概念：" + "；".join(
            fmt(x) for x in boards[-_BOARDS:][::-1]))
    up = S.read_limit_pool("up", today)
    if isinstance(up, pd.DataFrame) and not up.empty:
        ladder = up.sort_values("连板数", ascending=False).head(8)
        lines.append("连板梯队：" + "；".join(
            f"{r['名称']}({r['代码']}) {int(r['连板数'] or 1)}板 {r['所属行业']}"
            for _, r in ladder.iterrows()))
        by_sector = up["所属行业"].value_counts().head(6)
        lines.append("涨停集中的行业：" + "；".join(
            f"{k} {v}只" for k, v in by_sector.items()))
    return "\n".join(lines)


def _members() -> tuple[dict[str, list[str]], dict[str, str]]:
    from alpha_agents.config import DB_PATH
    members: dict[str, list[str]] = {}
    names: dict[str, str] = {}
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        for concept, code in conn.execute(
                "SELECT c.name, cs.stock_code FROM concept_stocks cs "
                "JOIN concepts c ON c.id = cs.concept_id"):
            members.setdefault(concept, []).append(code)
        names = dict(conn.execute("SELECT code, name FROM stocks"))
        conn.close()
    except sqlite3.Error as e:
        logger.warning("Concept membership unavailable: %s", e)
    return members, names


def _ours(trader_id: str, today: str) -> dict[str, str]:
    """Live: a tracked theme was seen; one it holds or ordered today, selected."""
    from alpha_agents.data.memory_store import _get_conn, get_active_themes
    from alpha_agents.evolution.session_facts import SEEN, SELECTED
    out = {t["name"]: SEEN for t in get_active_themes()}
    rows = _get_conn().execute(
        "SELECT DISTINCT theme FROM virtual_portfolio WHERE trader_id = ? AND "
        "(status = 'open' OR order_date = ?)", (trader_id, today)).fetchall()
    for (theme,) in rows:
        if theme:
            out[theme] = SELECTED
    return out


def _exposure(trader_id: str, hist: sqlite3.Connection, today: str) -> str:
    from alpha_agents.data.portfolio_risk import equity_curve
    from alpha_agents.evolution.session_facts import exposure_line, market_move
    marks = [m for m in equity_curve(days=20, trader_id=trader_id)
             if m.get("equity")][-10:]
    if not marks:
        return ""
    pairs = [(m["date"], float(m.get("market_value") or 0) / float(m["equity"]) * 100)
             for m in marks]
    own = (float(marks[-1]["equity"]) / float(marks[0]["equity"]) - 1) * 100
    return exposure_line(pairs, market_move(hist, marks[0]["date"], today), own)


def _day_facts(hist: sqlite3.Connection, today: str, trader_id: str) -> str:
    from alpha_agents.config import DATA_DIR
    from alpha_agents.data.snapshot_store import read_news
    from alpha_agents.evolution import session_facts as SF

    has_today = hist.execute("SELECT 1 FROM daily_kline WHERE date = ? LIMIT 1",
                             (today,)).fetchone()
    if not has_today:
        logger.warning("No %s bars on disk — market review from snapshots", today)
        return market_facts(today)
    members, names = _members()
    try:
        news = [n.get("title", "") for n in read_news(
            None, as_of=f"{today} 15:00:00", since=f"{today} 00:00:00", limit=2000)]
    except Exception as e:                            # noqa: BLE001
        logger.warning("News for the close review unavailable: %s", e)
        news = []
    flows = sqlite3.connect(
        f"file:{DATA_DIR / 'market_snapshots.db'}?mode=ro", uri=True)
    try:
        f = SF.compute(hist, day=today, members=members, names=names,
                       flows=flows, news_titles=news, ours=_ours(trader_id, today))
    finally:
        flows.close()
    return SF.render(f)


def _book(trader_id: str) -> str:
    from alpha_agents.data.portfolio import get_open_positions
    rows = get_open_positions(trader_id)
    if not rows:
        return "你当前空仓。"
    return "你当前持仓：" + "；".join(
        f"{p['code']} {p.get('name', '')} 成本{p.get('open_price')}" for p in rows)


async def run(today: str) -> dict:
    """Every trader's close of day. Returns counts, never raises."""
    import asyncio

    from alpha_agents.config import DATA_DIR
    from alpha_agents.data.memory_store import _get_conn
    from alpha_agents.data.trader import load_traders
    from alpha_agents.evolution import close_day
    from alpha_agents.model_factory import create_model

    try:
        from alpha_agents.data.market_history import update_daily
        await asyncio.to_thread(update_daily)
    except Exception as e:                            # noqa: BLE001
        logger.warning("K-line update before the close review failed: %s", e)
    model = create_model()
    hist = sqlite3.connect(
        f"file:{DATA_DIR / 'market_history.db'}?mode=ro", uri=True)
    totals: dict[str, int] = {}
    try:
        for trader in load_traders():
            try:
                facts = "" if trader.legacy else _day_facts(hist, today, trader.id)
                facts = "\n\n".join(x for x in (facts, _book(trader.id)) if x)
                got = await close_day.review_day(
                    _get_conn(), hist, trader_id=trader.id, trader=trader,
                    day=today, model=model, facts_text=facts,
                    exposure_text=_exposure(trader.id, hist, today),
                    watch=not trader.legacy)
            except Exception as e:                    # noqa: BLE001
                logger.warning("Close review failed for %s: %s", trader.id, e)
                continue
            for k, v in got.items():
                totals[k] = totals.get(k, 0) + v
    finally:
        hist.close()
    logger.info("Close review %s: %s", today, totals)
    return totals


async def run_close_review() -> str | None:
    """Scheduler entry point."""
    from datetime import datetime
    totals = await run(datetime.now().strftime("%Y-%m-%d"))
    return f"收盘复盘：{totals}" if totals else None
