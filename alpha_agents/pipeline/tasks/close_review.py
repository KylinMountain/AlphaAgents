"""After the close: each trader reviews its trades, its rules and the market.

Three steps per trader, in this order because each reads the one before:

1. the per-trade reviews of everything it closed (``evolution.trade_review``);
2. its handbook rewritten from those reviews (``evolution.handbook``);
3. its read of today's market and a watchlist for tomorrow
   (``evolution.market_review``), written with its handbook in front of it.

The market facts are assembled here from the day's snapshots and handed to
the model as text. The analyst review that runs before this used tools to
fetch them and timed out at 300 s every day from 09-18 to 09-23; a read of
data already on disk cannot.
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


def _book(trader_id: str) -> str:
    from alpha_agents.data.portfolio import get_open_positions
    rows = get_open_positions(trader_id)
    if not rows:
        return "你当前空仓。"
    return "你当前持仓：" + "；".join(
        f"{p['code']} {p.get('name', '')} 成本{p.get('open_price')}" for p in rows)


async def run(today: str) -> dict:
    """All three steps for every trader. Returns counts, never raises."""
    from alpha_agents.config import DATA_DIR
    from alpha_agents.data.memory_store import _get_conn
    from alpha_agents.data.trader import load_traders
    from alpha_agents.evolution import handbook, market_review, trade_review
    from alpha_agents.model_factory import create_model

    model = create_model()
    hist = sqlite3.connect(
        f"file:{DATA_DIR / 'market_history.db'}?mode=ro", uri=True)
    counts = {"trade_reviews": 0, "handbooks": 0, "market_reviews": 0}
    try:
        facts = market_facts(today)
    except Exception as e:                            # noqa: BLE001
        logger.warning("Market facts unavailable: %s", e)
        facts = ""
    try:
        for trader in load_traders():
            conn = _get_conn()
            try:
                n = await trade_review.review_closed(
                    conn, hist, trader_id=trader.id, as_of=today,
                    model=model, trader=trader)
                counts["trade_reviews"] += n
                if n and await handbook.consolidate(
                        conn, trader.id, as_of=today, model=model, trader=trader):
                    counts["handbooks"] += 1
            except Exception as e:                    # noqa: BLE001
                logger.warning("Trade review step failed for %s: %s", trader.id, e)
            if trader.legacy:
                continue  # winding down: nothing new to watch for
            context = "\n\n".join(x for x in (
                _book(trader.id),
                market_review.grade_line(
                    market_review.grade(conn, hist, trader.id)),
                handbook.load(trader.id)) if x)
            if await market_review.write(conn, trader_id=trader.id, date=today,
                                         facts=facts, model=model,
                                         context=context):
                counts["market_reviews"] += 1
    finally:
        hist.close()
    logger.info("Close review: %s", counts)
    return counts
