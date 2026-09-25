"""The trader's close of day, live: 19:00, once the day's bars are on disk.

The steps are ``evolution.close_day`` — trade reviews, the market review,
the handbook rewrite — shared with the replay. This module assembles what the
live side knows: the day's facts from ``daily_kline`` via
``evolution.session_facts`` (limit-ups, streaks, failed limit-ups, each
concept's move, money and news), which boards the trader tracked or held,
the day's own record (``evolution.day_record``: orders, fills, holdings,
sells) and its exposure against the market from its equity marks.

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


def _morning_inputs(trader_id: str, today: str) -> list[dict]:
    from alpha_agents.data import trader_session
    return [row for row in trader_session.read(trader_id=trader_id,
            kind="morning_input", day=today) if row["observed_at"][11:16] < "09:30"]


def _ours(trader_id: str, today: str) -> dict[str, str]:
    """Visibility is an observed input, not selection or inferred hindsight."""
    return {name: "早盘材料中可见（不等于选中）"
            for row in _morning_inputs(trader_id, today)
            for name in row["payload"].get("themes", [])}


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
    from alpha_agents.evolution import session_facts as SF

    has_today = hist.execute("SELECT 1 FROM daily_kline WHERE date = ? LIMIT 1",
                             (today,)).fetchone()
    if not has_today:
        logger.warning("No %s bars on disk — market review from snapshots", today)
        return market_facts(today)
    members, names = _members()
    prev = hist.execute("SELECT MAX(date) FROM daily_kline WHERE date < ?",
                        (today,)).fetchone()[0]
    try:
        news, news_history = SF.load_news(today, prev)
    except Exception as e:                            # noqa: BLE001
        logger.warning("News for the close review unavailable: %s", e)
        news, news_history = [], []
    try:
        flows = sqlite3.connect(
            f"file:{DATA_DIR / 'market_snapshots.db'}?mode=ro", uri=True)
    except sqlite3.OperationalError as exc:
        logger.warning("Close review flow database unavailable: %s", exc)
        flows = None
    try:
        f = SF.compute(hist, day=today, members=members, names=names,
                       flows=flows, news_titles=news, ours=_ours(trader_id, today),
                       news_history=news_history, search=_news_search(today),
                       unseen_label="是否看到未记录")
    finally:
        if flows is not None:
            flows.close()
    from alpha_agents.data.memory_store import _get_conn
    relevant = {row[0] for row in _get_conn().execute(
        "SELECT DISTINCT code FROM virtual_portfolio WHERE trader_id=? AND "
        "(status='open' OR order_date=? OR close_date=?)", (trader_id, today, today))}
    present = {row[0] for row in hist.execute(
        "SELECT code FROM daily_kline WHERE date=? AND close>0", (today,))}
    missing = sorted(relevant - present)
    coverage = (f"账户相关股票当日行情覆盖 {len(relevant & present)}/{len(relevant)}；"
                f"缺失：{','.join(missing) or '无'}。缺失不表示停牌或零涨跌。")
    return coverage + "\n" + SF.render(f)


def _news_search(today: str):
    """The news index, windowed to the two weeks up to today's close."""
    from datetime import date, timedelta

    from alpha_agents.data import news_index as NI
    from alpha_agents.evolution.session_facts import NEWS_LOOKBACK_DAYS
    back = (date.fromisoformat(today) - timedelta(days=NEWS_LOOKBACK_DAYS)).isoformat()
    try:
        NI.index_window(f"{back} 00:00:00", f"{today} 15:00:00")
    except Exception as e:                            # noqa: BLE001
        logger.warning("News index unavailable, close review uses text match: %s", e)
        return None
    return lambda q, since, until, k: NI.search_window(q, since, until, top_k=k)


def _record(hist: sqlite3.Connection, trader_id: str, today: str) -> str:
    """Use recorded pre-open inputs, never current active themes."""
    from alpha_agents.data.memory_store import _get_conn
    from alpha_agents.evolution import day_record
    rows = _morning_inputs(trader_id, today)
    lines = ["早盘材料快照（仅证明可见，不推定选择或拒绝）："]
    for row in rows:
        names = "、".join(row["payload"].get("themes", [])) or "（主线列表为空）"
        lines.append(f"- {row['observed_at']} {names}")
    if not rows:
        lines = ["早盘材料未记录；不能用收盘主线补写早盘所见。"]
    return day_record.render(_get_conn(), hist, trader_id=trader_id, day=today,
                             directions="\n".join(lines))


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
                got = await close_day.review_day(
                    _get_conn(), hist, trader_id=trader.id, trader=trader,
                    day=today, model=model, facts_text=facts,
                    exposure_text=_exposure(trader.id, hist, today),
                    record_text=_record(hist, trader.id, today),
                    review_market=not trader.legacy)
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
