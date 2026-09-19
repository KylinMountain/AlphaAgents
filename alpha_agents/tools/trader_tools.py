"""问题型工具：让 T1 Trader 自己转头看市场。

现在的 T1 决策器 `tools=[]`、`max_turns=1`——它读的是平台预先做好的包
（面板、宽度、新闻、账本），然后从里面选。它没法说"这只票我拿不准，
再查三件事"。这是它和 `morning` 那个 26 工具 / 40 turns 的分析师之间
最大的能力断层。

本模块补的是**问题**，不是数据源。`docs/trader_toolkit.md` 早就列了
六个最像交易员的问题，代码里只落地了一个（`get_limit_ladder`）。
这里把剩下的接上，每个只回答一个问题。

## 三条硬约束

**1. 只输出事实，不输出判断。**
`get_market_regime` 返回 `broken_rate=42.1%`，不返回"承接弱、建议降仓"。
后一句是 policy，它必须能经由 candidate → evidence → 审批 被替换掉
（见 `scripts/lint_policy.py`）。

**2. 每个工具声明它的时间边界，读未来即失败。**
`AS_OF_FIELDS` 说明这个工具按哪个时间列截断。回放目录里
`market_snapshots.db` 是指向生产库的符号链接，库隔离挡不住泄漏——
只有 as-of 截断能。`_cut()` 是唯一的截断入口。

**3. 回放里没有的数据就说没有。**
"这只票今天没行情快照"和"这只票没动"是两件事。缺数据返回
`available: False` 与原因，不返回 0——0 会被读成一个事实。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime

from alpha_agents.config import DATA_DIR

logger = logging.getLogger(__name__)

_SNAPSHOTS = DATA_DIR / "market_snapshots.db"
_HISTORY = DATA_DIR / "market_history.db"

#: 每个工具按哪个时间列截断，以及它回答什么问题。这张表是
#: `tests/test_trader_tools_time_travel.py` 的输入：契约写在数据里，
#: 不写在文档里，否则文档会漂移。
AS_OF_FIELDS = {
    "get_market_regime": ("market_snapshots.db", "captured_at",
                          "今天这个市场能不能做"),
    "get_theme_state": ("market_snapshots.db", "captured_at",
                        "这条线在生命周期哪一段"),
    "get_stock_context": ("market_history.db", "date",
                          "这只票是什么状态"),
    "get_intraday_shape": ("market_snapshots.db", "captured_at",
                           "今天分时怎么走的"),
    "get_event_context": ("market_snapshots.db", "captured_at",
                          "这条消息相对市场原来的预期是什么"),
    "get_stock_memory": ("memory.db", "open_date",
                         "我认识这只票吗"),
    "get_my_state": ("memory.db", "close_date",
                     "我今天手顺不顺"),
}


def _as_of() -> str | None:
    """当前回放时刻，None 表示实盘。"""
    from alpha_agents.evolution.replay_mode import get_replay_as_of
    return get_replay_as_of()


def _snapshot_cut() -> str:
    """快照库的截断点。

    时间索引数据（快照/新闻/宽度）直接按 `captured_at <= as_of` 截断。
    实盘模式下 as_of 是 None，用当前时刻，行为与原来一致。
    """
    as_of = _as_of()
    if as_of:
        # 裸日期 = EOD 语义：那天的快照全部可见。
        return f"{as_of} 23:59:59" if len(as_of) == 10 else as_of
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _eod_cut() -> str:
    """日线（EOD 数据）的截断日期。

    与 `replay_mode.effective_eod_cut_date` 同一口径：盘中时刻看不到当天
    收盘，必须回退到 T-1。
    """
    from alpha_agents.evolution.replay_mode import effective_eod_cut_date
    as_of = _as_of()
    if as_of:
        return effective_eod_cut_date(as_of) or str(as_of)[:10]
    return datetime.now().strftime("%Y-%m-%d")


def _conn(path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _memory_conn() -> sqlite3.Connection:
    from alpha_agents.data.memory_store import _get_conn
    return _get_conn()


def _guard_read(what: str, stamp: str) -> None:
    """读未来的数据是**故障**，不是业务决定。抛错，不返回空。

    返回空会被读成"那天没数据"，而事实是"这段代码试图读未来"——
    两者必须可区分，否则泄漏会以健康日志的形式进入学习数据。
    """
    from alpha_agents.data import clock
    clock.assert_not_from_the_future(stamp, what=what)


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _no_data(what: str, reason: str, **extra) -> str:
    return _json({"available": False, "what": what, "reason": reason, **extra})


# ── ① get_market_regime ────────────────────────────────────────────────


def get_market_regime_fn(as_of: str = "") -> str:
    """今天这个市场能不能做——宽度、梯队、炸板率、赚钱效应。

    只给事实：涨跌家数、涨停/跌停、炸板率、最高连板、昨日涨停今日表现、
    换手水位、板块集中度。**不给仓位建议**——那是 policy。
    """
    cut = as_of or _snapshot_cut()
    _guard_read("market regime snapshot", cut[:10])
    try:
        conn = _conn(_SNAPSHOTS)
    except Exception as e:
        logger.warning("market regime unavailable: %s", e)
        return _no_data("market_regime", f"快照库不可用: {e}")

    try:
        day = cut[:10]
        # 必须限定在**当天**。只用 `captured_at <= cut` 会拿更早某天的宽度
        # 冒充今天：一个 3-20 的查询会返回 3-06 的涨跌家数，还把它标成
        # `as_of: 3-20`。这是 time-travel 契约测试抓到的真泄漏——报告一个
        # 从未存在过的市场状态，比返回"没有数据"危险得多。
        breadth = conn.execute(
            "SELECT * FROM market_breadth_snapshots "
            "WHERE captured_at <= ? AND substr(captured_at,1,10) = ? "
            "ORDER BY captured_at DESC LIMIT 1", (cut, day)).fetchone()
        if breadth is None:
            return _no_data("market_regime",
                            f"{day} 没有宽度快照——非交易日或未采集。"
                            "不要据此判断市场冷清，也不要拿相邻交易日代替。")

        # 涨停池：当日去重后取最后一条，中途炸过又封回去的算封住
        pools = conn.execute(
            "SELECT code, pool_type, consecutive_limits, seal_amount_yi, "
            "       break_count, first_seal_time, sector, "
            "       MAX(captured_at) last_seen "
            "FROM limit_pool_snapshots WHERE captured_at <= ? "
            "  AND substr(captured_at,1,10) = ? "
            "GROUP BY code, pool_type", (cut, day)).fetchall()
        up = [r for r in pools if (r["pool_type"] or "up") == "up"]
        broken = [r for r in pools if r["pool_type"] == "broken"]
        attempts = len(up) + len(broken)

        ladder = Counter((r["consecutive_limits"] or 1) for r in up)
        sectors = Counter(r["sector"] for r in up if r["sector"])
        # 昨日涨停股今日表现：唯一直接回答"跟进能不能赚钱"的数
        money = _money_effect(conn, day, cut)

        return _json({
            "available": True,
            "as_of": cut,
            "breadth": {
                "advances": breadth["advances"],
                "declines": breadth["declines"],
                "flat": breadth["flat"],
                "limit_up": breadth["limit_up"],
                "limit_down": breadth["limit_down"],
                "real_limit_up": breadth["real_limit_up"],
                "ad_ratio": breadth["ad_ratio"],
                "activity_pct": breadth["activity_pct"],
            },
            "ladder": {
                "limit_up": len(up),
                "broken": len(broken),
                "broken_rate_pct": (round(len(broken) / attempts * 100, 1)
                                    if attempts else None),
                "highest_streak": max(ladder) if ladder else 0,
                "by_streak": {f"{k}板": v for k, v in
                              sorted(ladder.items(), reverse=True)},
                "early_seal_pct": (
                    round(sum(1 for r in up
                              if (r["first_seal_time"] or "9999")[:4] <= "0935")
                          / len(up) * 100, 1) if up else None),
            },
            "yesterday_limit_today": money,
            "sector_concentration": [
                {"sector": k, "limit_ups": v} for k, v in sectors.most_common(6)],
            "note": "以上全部是可核验的事实。要不要动手、下多大，是你的判断。",
        })
    except Exception as e:
        logger.warning("market regime failed: %s", e)
        return _no_data("market_regime", f"计算失败: {e}")
    finally:
        conn.close()


def _money_effect(conn, day: str, cut: str) -> dict:
    """昨日涨停的票，今天平均涨跌。

    需要昨日涨停名单 + 今日行情，两个都在快照库里。缺任何一个就诚实
    返回 available=False，而不是拿残缺样本充数。
    """
    prev = conn.execute(
        "SELECT DISTINCT substr(captured_at,1,10) d FROM limit_pool_snapshots "
        "WHERE captured_at <= ? AND substr(captured_at,1,10) < ? "
        "ORDER BY d DESC LIMIT 1", (cut, day)).fetchone()
    if not prev:
        return {"available": False, "reason": "截断点之前没有昨日涨停名单"}
    pd = prev["d"]
    codes = [r["code"] for r in conn.execute(
        "SELECT DISTINCT code FROM limit_pool_snapshots "
        "WHERE captured_at <= ? AND substr(captured_at,1,10)=? "
        "  AND pool_type='up'", (cut, pd))]
    if not codes:
        return {"available": False, "reason": f"{pd} 无涨停记录"}
    marks = ",".join("?" * len(codes))
    rows = conn.execute(
        f"SELECT code, change_pct, MAX(captured_at) FROM all_quote_snapshots "
        f"WHERE captured_at <= ? AND substr(captured_at,1,10)=? "
        f"  AND code IN ({marks}) GROUP BY code",
        (cut, day, *codes)).fetchall()
    vals = [r["change_pct"] for r in rows if r["change_pct"] is not None]
    if not vals:
        return {"available": False,
                "reason": f"{pd} 的涨停股在 {day} 没有行情快照"}
    return {
        "available": True,
        "prev_date": pd,
        "n": len(vals),
        "avg_change_pct": round(sum(vals) / len(vals), 2),
        "positive_pct": round(sum(1 for v in vals if v > 0) / len(vals) * 100, 1),
        "down_over_5pct": sum(1 for v in vals if v < -5),
    }


# ── ② get_theme_state ─────────────────────────────────────────────────


def get_theme_state_fn(theme: str, as_of: str = "") -> str:
    """这条线在生命周期哪一段：龙头、高度、梯队、扩散或收缩。

    `theme` 是板块/概念名（如"固态电池"）。返回该板块的涨停梯队、
    资金流、龙头与其今日表现。**不返回"启动/退潮"这类结论**——那是
    交易员看完这些数自己下的判断。
    """
    if not theme:
        return _no_data("theme_state", "必须给 theme")
    cut = as_of or _snapshot_cut()
    _guard_read("theme state snapshot", cut[:10])
    try:
        conn = _conn(_SNAPSHOTS)
    except Exception as e:
        return _no_data("theme_state", f"快照库不可用: {e}")

    try:
        day = cut[:10]
        rows = conn.execute(
            "SELECT code, name, consecutive_limits, seal_amount_yi, "
            "       break_count, first_seal_time, sector, change_pct, "
            "       MAX(captured_at) last_seen "
            "FROM limit_pool_snapshots "
            "WHERE captured_at <= ? AND substr(captured_at,1,10)=? "
            "  AND sector = ? GROUP BY code, pool_type",
            (cut, day, theme)).fetchall()
        if not rows:
            return _no_data(
                "theme_state",
                f"{day} 的涨停池里没有 '{theme}' 板块的票——可能是这条线今天"
                "没有涨停，也可能是板块名不匹配。两者不是一回事。",
                theme=theme)

        by_streak = Counter((r["consecutive_limits"] or 1) for r in rows)
        leaders = sorted(rows, key=lambda r: -(r["consecutive_limits"] or 0))
        flow = conn.execute(
            "SELECT sector_name, change_pct, net_flow_yi, leader, "
            "       leader_change_pct, company_count, MAX(captured_at) last_seen "
            "FROM sector_flow_snapshots "
            "WHERE captured_at <= ? AND sector_name = ? "
            "ORDER BY captured_at DESC LIMIT 1", (cut, theme)).fetchone()

        return _json({
            "available": True,
            "as_of": cut,
            "theme": theme,
            "limit_up_count": len(rows),
            "highest_streak": max(by_streak) if by_streak else 0,
            "by_streak": {f"{k}板": v for k, v in
                          sorted(by_streak.items(), reverse=True)},
            "leaders": [{
                "code": r["code"], "name": r["name"],
                "streak": r["consecutive_limits"],
                "seal_amount_yi": r["seal_amount_yi"],
                "break_count": r["break_count"],
                "first_seal": r["first_seal_time"],
                "change_pct": r["change_pct"],
            } for r in leaders[:8]],
            "sector_flow": ({
                "change_pct": flow["change_pct"],
                "net_flow_yi": flow["net_flow_yi"],
                "leader": flow["leader"],
                "leader_change_pct": flow["leader_change_pct"],
                "company_count": flow["company_count"],
            } if flow else {"available": False,
                            "reason": "该板块没有资金流快照"}),
            "note": "扩散还是收缩，看 limit_up_count 与 company_count 的关系；"
                    "高度看 highest_streak。判断由你做。",
        })
    except Exception as e:
        logger.warning("theme state failed: %s", e)
        return _no_data("theme_state", f"计算失败: {e}")
    finally:
        conn.close()


# ── ③ get_stock_context ───────────────────────────────────────────────


def get_stock_context_fn(code: str, as_of: str = "") -> str:
    """这只票是什么状态：价格结构、波动、资金、相对板块强弱。

    合并过去散落的碎工具，只给事实：20/60 日结构、ATR、换手、成交额、
    资金流序列、相对板块、涨停史、融资余额。**不给评级、不给买卖点**。
    """
    if not code:
        return _no_data("stock_context", "必须给 code")
    cut_day = _eod_cut()
    if as_of:
        cut_day = as_of[:10]
    _guard_read("stock context kline", cut_day)
    try:
        hist = _conn(_HISTORY)
    except Exception as e:
        return _no_data("stock_context", f"行情库不可用: {e}")

    try:
        bars = hist.execute(
            "SELECT date, open, high, low, close, volume, turnover_rate, "
            "       change_pct FROM daily_kline WHERE code = ? AND date <= ? "
            "ORDER BY date DESC LIMIT 60", (code, cut_day)).fetchall()
        if not bars:
            return _no_data(
                "stock_context",
                f"{code} 在 {cut_day} 及之前没有日线——可能未上市、已退市，"
                "或代码不在本库的覆盖范围内。这三种情况都不是'没波动'。",
                code=code)
        bars = list(reversed(bars))
        closes = [r["close"] for r in bars if r["close"] is not None]
        last = bars[-1]

        # ATR(14)：真实波幅的均值，比标准差更贴近"这根票一天动多少"
        trs = []
        for i in range(1, len(bars)):
            h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
            if None in (h, l, pc):
                continue
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        atr14 = round(sum(trs[-14:]) / len(trs[-14:]), 3) if trs else None

        def _ret(n):
            if len(closes) <= n:
                return None
            base = closes[-1 - n]
            return (round((closes[-1] / base - 1) * 100, 2) if base else None)

        streak = _limit_streak(bars)

        return _json({
            "available": True,
            "as_of": cut_day,
            "code": code,
            "bars_used": len(bars),
            "last_bar": {"date": last["date"], "close": last["close"],
                         "change_pct": last["change_pct"],
                         "turnover_rate": last["turnover_rate"],
                         "volume": last["volume"]},
            "return_pct": {"5d": _ret(5), "20d": _ret(20), "60d": _ret(60)},
            "atr14": atr14,
            "atr_pct_of_price": (round(atr14 / closes[-1] * 100, 2)
                                 if atr14 and closes[-1] else None),
            "high_20d": max((r["high"] for r in bars[-20:]
                             if r["high"] is not None), default=None),
            "low_20d": min((r["low"] for r in bars[-20:]
                            if r["low"] is not None), default=None),
            "limit_streak": streak,
            "fund_flow": _fund_flow_series(code, cut_day),
            "note": "全是历史事实。atr_pct_of_price 是这只票的日常波动幅度，"
                    "你的止损比它窄就会被正常噪音打出去。",
        })
    except Exception as e:
        logger.warning("stock context failed: %s", e)
        return _no_data("stock_context", f"计算失败: {e}")
    finally:
        hist.close()


def _limit_streak(bars: list) -> dict:
    """当前连续涨停天数，以及最近 20 日涨停次数。

    涨停判定用当日涨幅接近 10% 且收盘=最高（封住）。不区分 20cm 板：
    这里只报"连续几根"，具体板性由市场规则决定，不在这层猜。
    """
    def _is_limit(row) -> bool:
        chg = row["change_pct"]
        if chg is None:
            return False
        return chg >= 9.8 and row["close"] is not None and \
            row["high"] is not None and row["close"] >= row["high"] - 1e-9

    streak = 0
    for row in reversed(bars):
        if _is_limit(row):
            streak += 1
        else:
            break
    recent = sum(1 for r in bars[-20:] if _is_limit(r))
    return {"current_streak": streak, "limit_ups_in_20d": recent}


def _fund_flow_series(code: str, cut_day: str) -> dict:
    """最近 5 个交易日的资金流。缺表或没数据都说清楚。"""
    try:
        conn = _conn(_SNAPSHOTS)
    except Exception as e:
        return {"available": False, "reason": f"快照库不可用: {e}"}
    try:
        rows = conn.execute(
            "SELECT date, main_net_yi, main_net_pct, turnover_rate "
            "FROM stock_fund_flow_daily WHERE code = ? AND date <= ? "
            "ORDER BY date DESC LIMIT 5", (code, cut_day)).fetchall()
        if not rows:
            return {"available": False,
                    "reason": f"{code} 在 {cut_day} 及之前没有资金流记录"}
        return {"available": True, "recent": [
            {"date": r["date"], "main_net_yi": r["main_net_yi"],
             "main_net_pct": r["main_net_pct"],
             "turnover_rate": r["turnover_rate"]} for r in rows]}
    except sqlite3.Error as e:
        return {"available": False, "reason": f"资金流表不可用: {e}"}
    finally:
        conn.close()


# ── ④ get_intraday_shape ──────────────────────────────────────────────


def get_intraday_shape_fn(code: str, as_of: str = "") -> str:
    """今天分时怎么走的：VWAP、上下半场量占比、冲高回落、尾盘加速。

    收盘决策（14:55）特别需要这个——否则所谓"收盘决策"只是在读一根
    压扁的 OHLCV。数据来自 5 分钟行情快照，按 as_of 截断。
    """
    if not code:
        return _no_data("intraday_shape", "必须给 code")
    cut = as_of or _snapshot_cut()
    _guard_read("intraday shape snapshot", cut[:10])
    try:
        conn = _conn(_SNAPSHOTS)
    except Exception as e:
        return _no_data("intraday_shape", f"快照库不可用: {e}")

    try:
        day = cut[:10]
        rows = conn.execute(
            "SELECT captured_at, price, open, high, low, prev_close, volume, "
            "       amount_yi, turnover_rate, volume_ratio "
            "FROM all_quote_snapshots "
            "WHERE code = ? AND captured_at <= ? AND substr(captured_at,1,10)=? "
            "ORDER BY captured_at", (code, cut, day)).fetchall()
        if not rows:
            return _no_data(
                "intraday_shape",
                f"{code} 在 {day}（截至 {cut[11:16] or '收盘'}）没有行情快照。"
                "没快照不等于没成交。", code=code)
        if len(rows) < 3:
            return _no_data(
                "intraday_shape",
                f"{code} 在 {day} 只有 {len(rows)} 个快照点，画不出分时形态",
                code=code)

        prices = [r["price"] for r in rows if r["price"] is not None]
        first, last = rows[0], rows[-1]
        prev_close = first["prev_close"]
        day_open = first["open"] if first["open"] is not None else first["price"]
        hi = max((r["high"] for r in rows if r["high"] is not None), default=None)
        lo = min((r["low"] for r in rows if r["low"] is not None), default=None)

        # VWAP：用成交额/成交量近似。快照的 volume 是累计量，取最后一条。
        vwap = None
        amt = last["amount_yi"]
        vol = last["volume"]
        if amt and vol:
            vwap = round(amt * 1e8 / vol, 3)

        # 上下半场成交占比：用累计量的差分，看资金在哪个时段进场
        noon = f"{day} 11:30"
        morning_vol = None
        for r in rows:
            if r["captured_at"] <= noon:
                morning_vol = r["volume"]
        total_vol = last["volume"]
        morning_share = (round(morning_vol / total_vol * 100, 1)
                         if morning_vol and total_vol else None)

        peak = hi
        close_from_peak = (round((last["price"] / peak - 1) * 100, 2)
                           if peak and last["price"] else None)
        open_to_now = (round((last["price"] / day_open - 1) * 100, 2)
                       if day_open and last["price"] else None)

        return _json({
            "available": True,
            "as_of": cut,
            "code": code,
            "snapshots": len(rows),
            "first_snapshot": first["captured_at"],
            "last_snapshot": last["captured_at"],
            "open": day_open,
            "prev_close": prev_close,
            "current": last["price"],
            "high": hi,
            "low": lo,
            "vwap": vwap,
            "change_pct_from_prev_close": (
                round((last["price"] / prev_close - 1) * 100, 2)
                if prev_close and last["price"] else None),
            "open_to_now_pct": open_to_now,
            "close_from_high_pct": close_from_peak,
            "morning_volume_share_pct": morning_share,
            "turnover_rate": last["turnover_rate"],
            "volume_ratio": last["volume_ratio"],
            "note": "close_from_high_pct 为负说明冲高回落；"
                    "morning_volume_share_pct 低说明量能集中在下午。",
        })
    except Exception as e:
        logger.warning("intraday shape failed: %s", e)
        return _no_data("intraday_shape", f"计算失败: {e}")
    finally:
        conn.close()


# ── ⑤ get_stock_memory ────────────────────────────────────────────────


def get_stock_memory_fn(code: str, as_of: str = "") -> str:
    """我认识这只票吗：过去看过它几次、买过几次、结果如何。

    这是"一个交易员越来越熟悉一只股票"和"每天失忆"的区别。数据全部来自
    本交易员自己的账本与论点，**只报它自己做过什么**，不报"这票好不好"。
    """
    if not code:
        return _no_data("stock_memory", "必须给 code")
    cut_day = (as_of or _as_of() or datetime.now().strftime("%Y-%m-%d"))[:10]
    _guard_read("stock memory", cut_day)
    try:
        conn = _memory_conn()
    except Exception as e:
        return _no_data("stock_memory", f"账本不可用: {e}")

    try:
        from alpha_agents.data.trader import DEFAULT_TRADER
        trader = DEFAULT_TRADER
        try:
            from alpha_agents.data.trader import get_trader
            trader = getattr(get_trader(None), "id", DEFAULT_TRADER)
        except Exception as e:
            logger.debug("trader id unavailable, using default: %s", e)

        # 所有碰过这只票的订单（含未成交），按时间正序
        orders = conn.execute(
            "SELECT id, order_date, open_date, status, open_price, shares, "
            "       return_pct, close_date, close_reason, reason "
            "FROM virtual_portfolio WHERE code = ? AND trader_id = ? "
            "  AND COALESCE(open_date, order_date) <= ? "
            "ORDER BY COALESCE(open_date, order_date)", (code, trader, cut_day)
        ).fetchall()

        theses = conn.execute(
            "SELECT id, claim, status, created_at, closed_at, close_kind, "
            "       close_note, conviction FROM theses "
            "WHERE code = ? AND trader_id = ? AND substr(created_at,1,10) <= ? "
            "ORDER BY created_at", (code, trader, cut_day)).fetchall()

        if not orders and not theses:
            return _json({
                "available": True, "as_of": cut_day, "code": code,
                "seen_before": False, "orders": [], "theses": [],
                "note": "这个交易员在截断点之前没有碰过这只票。"
                        "'没碰过'是一个事实，不是'不值得碰'。",
            })

        bought = [o for o in orders if o["open_price"] and o["shares"]]
        closed = [o for o in bought if o["return_pct"] is not None]
        wins = [o for o in closed if (o["return_pct"] or 0) > 0]

        return _json({
            "available": True,
            "as_of": cut_day,
            "code": code,
            "seen_before": True,
            "order_count": len(orders),
            "bought_count": len(bought),
            "closed_count": len(closed),
            "wins": len(wins),
            "losses": len(closed) - len(wins),
            "orders": [{
                "date": o["open_date"] or o["order_date"],
                "status": o["status"],
                "price": o["open_price"],
                "shares": o["shares"],
                "return_pct": o["return_pct"],
                "close_date": o["close_date"],
                "buy_reason": (o["reason"] or "")[:120],
                "close_reason": (o["close_reason"] or "")[:120],
            } for o in orders],
            "theses": [{
                "id": t["id"], "claim": (t["claim"] or "")[:200],
                "status": t["status"], "conviction": t["conviction"],
                "created_at": t["created_at"], "closed_at": t["closed_at"],
                "close_kind": t["close_kind"],
                "close_note": (t["close_note"] or "")[:200],
            } for t in theses],
            "note": "这是你自己的历史，不是别人的评级。"
                    "亏损那一笔的 buy_reason 值得先读。",
        })
    except Exception as e:
        logger.warning("stock memory failed: %s", e)
        return _no_data("stock_memory", f"计算失败: {e}")


# ── ⑥ get_my_state ────────────────────────────────────────────────────


def get_my_state_fn(as_of: str = "") -> str:
    """我今天手顺不顺：今日/本周 P&L、回撤、连亏、今日决策数、敞口。

    回答的是"我最近是不是明显过于激进"。只报事实与计数，**不报"该降频"**
    ——那是 policy，得由证据决定。它读的是本交易员自己的账本。
    """
    cut_day = (as_of or _as_of() or datetime.now().strftime("%Y-%m-%d"))[:10]
    _guard_read("trader state", cut_day)
    try:
        conn = _memory_conn()
    except Exception as e:
        return _no_data("my_state", f"账本不可用: {e}")

    try:
        from alpha_agents.data.trader import DEFAULT_TRADER
        trader = DEFAULT_TRADER
        try:
            from alpha_agents.data.trader import get_trader
            trader = getattr(get_trader(None), "id", DEFAULT_TRADER)
        except Exception as e:
            logger.debug("trader id unavailable, using default: %s", e)

        open_rows = conn.execute(
            "SELECT code, name, theme, open_price, shares, peak_return_pct, "
            "       holding_days, open_date FROM virtual_portfolio "
            "WHERE status='open' AND trader_id=?", (trader,)).fetchall()
        invested = sum((r["open_price"] or 0) * (r["shares"] or 0)
                       for r in open_rows)

        # 已实现：按平仓日聚合，最近 10 笔逐笔列出（连亏要按顺序看）
        exits = conn.execute(
            "SELECT exit_date, code, net_amount, return_pct FROM position_exits "
            "WHERE trader_id=? AND exit_date <= ? "
            "ORDER BY exit_date DESC, id DESC LIMIT 40",
            (trader, cut_day)).fetchall()

        today_pnl = round(sum(e["net_amount"] or 0 for e in exits
                              if e["exit_date"] == cut_day), 2)
        # 本周 = 截断日所在周的周一至今
        try:
            from datetime import date as _d, timedelta as _td
            d0 = _d.fromisoformat(cut_day)
            monday = (d0 - _td(days=d0.weekday())).isoformat()
        except ValueError:
            monday = cut_day
        week_pnl = round(sum(e["net_amount"] or 0 for e in exits
                             if monday <= e["exit_date"] <= cut_day), 2)

        # 连亏：从最近一笔往前数，直到遇到盈利
        streak = 0
        for e in exits:
            if (e["net_amount"] or 0) < 0:
                streak += 1
            else:
                break

        decisions_today = conn.execute(
            "SELECT COUNT(*) c FROM decision_snapshots "
            "WHERE trader_id=? AND substr(decided_at,1,10)=?",
            (trader, cut_day)).fetchone()["c"]

        by_theme = Counter(r["theme"] for r in open_rows if r["theme"])
        total_capital = None
        try:
            from alpha_agents.data.portfolio import trader_capital
            total_capital = trader_capital(trader)
        except Exception as e:
            logger.debug("capital unavailable: %s", e)

        return _json({
            "available": True,
            "as_of": cut_day,
            "realized": {"today": today_pnl, "week": week_pnl,
                         "last_10": [round(e["net_amount"] or 0, 2)
                                     for e in exits[:10]]},
            "losing_streak": streak,
            "decisions_today": decisions_today,
            "open_positions": len(open_rows),
            "invested": round(invested, 2),
            "capital": total_capital,
            "exposure_pct": (round(invested / total_capital * 100, 1)
                             if total_capital else None),
            "theme_concentration": [
                {"theme": k, "positions": v} for k, v in by_theme.most_common()],
            "positions": [{
                "code": r["code"], "name": r["name"], "theme": r["theme"],
                "open_price": r["open_price"], "shares": r["shares"],
                "peak_return_pct": r["peak_return_pct"],
                "holding_days": r["holding_days"],
                "open_date": r["open_date"],
            } for r in open_rows],
            "note": "losing_streak 是连续亏损笔数；decisions_today 是你今天"
                    "已经做过的决策数。要不要收敛，是你自己的判断。",
        })
    except Exception as e:
        logger.warning("my state failed: %s", e)
        return _no_data("my_state", f"计算失败: {e}")


# ── 给 agent 的工具包装 ────────────────────────────────────────────────
#
# 只有 `_fn` 是纯函数、可被测试直接调用；这一层是给模型的接口，负责
# 把"问题"写成它能判断何时该调用的描述。描述里**只写这个工具回答什么
# 问题**，不写"什么时候该买"——那会变成 policy（见 lint_policy.py）。

from agents import function_tool  # noqa: E402

from alpha_agents.tools.budget import with_timeout  # noqa: E402


@function_tool
@with_timeout
def get_market_regime(as_of: str = "") -> str:
    """今天这个市场能不能做——涨跌家数、涨停/跌停、炸板率、最高连板、
    昨日涨停股今日平均表现、板块集中度。

    这是开盘前第一件要知道的事。返回的全是可核验的事实，不含仓位建议。
    `as_of` 留空表示"现在"；回放里由系统自动按决策时刻截断。
    """
    return get_market_regime_fn(as_of)


@function_tool
@with_timeout
def get_theme_state(theme: str, as_of: str = "") -> str:
    """这条线在生命周期哪一段：该板块的涨停梯队、最高连板、龙头及其
    封单与炸板次数、板块资金流。

    当你想知道"这个概念今天还有没有资金在做"时调用。只给事实，
    "启动/加速/分歧/退潮"的判断由你自己下。
    """
    return get_theme_state_fn(theme, as_of)


@function_tool
@with_timeout
def get_stock_context(code: str, as_of: str = "") -> str:
    """这只票是什么状态：20/60 日价格结构、ATR 与日均波动、换手、
    近期资金流序列、连续涨停天数。

    当你看中一只票但拿不准它的波动幅度（止损该放多宽）或它是否已经
    涨了很多时调用。只给历史事实，不给评级、不给买卖点。
    """
    return get_stock_context_fn(code, as_of)


@function_tool
@with_timeout
def get_intraday_shape(code: str, as_of: str = "") -> str:
    """今天分时怎么走的：开高低、VWAP、上下半场成交占比、冲高回落幅度、
    当前相对昨收的涨幅。

    收盘前决策时用它判断"今天这根阳线是真的还是冲高回落"。日线 OHLCV
    是压扁的，这个工具给你路径。
    """
    return get_intraday_shape_fn(code, as_of)




# ── ⑤ get_event_context ───────────────────────────────────────────────


def _event_positioning(code: str, event: dict, cut: str) -> dict:
    """Price path knowable by cut, with an explicit as-of date.

    This is a proxy for positioning, not consensus. The tool reports it beside
    expectation data rather than converting it into a priced-in verdict.
    """
    eod_cut = _eod_cut()
    event_day = str(event.get("scheduled_at") or "")[:10]
    position_day = min(eod_cut, event_day) if event_day else eod_cut
    try:
        conn = _conn(_HISTORY)
    except Exception as exc:
        return {"available": False, "reason": f"行情库不可用: {exc}"}
    try:
        rows = conn.execute(
            "SELECT date, open, high, low, close, volume, turnover_rate, "
            "change_pct FROM daily_kline WHERE code=? AND date<=? "
            "ORDER BY date DESC LIMIT 61", (code, position_day)).fetchall()
        if not rows:
            return {
                "available": False,
                "reason": f"{code} 在 {position_day} 及之前没有日线",
            }
        rows = list(reversed(rows))
        closes = [float(row["close"]) for row in rows
                  if row["close"] is not None]
        if not closes:
            return {"available": False, "reason": "没有可用收盘价"}

        def _ret(n):
            if len(closes) <= n or not closes[-1 - n]:
                return None
            return round((closes[-1] / closes[-1 - n] - 1) * 100, 2)

        recent20 = rows[-20:]
        vols = [float(row["volume"] or 0) for row in recent20]
        turns = [float(row["turnover_rate"] or 0) for row in recent20]
        return {
            "available": True,
            "as_of": rows[-1]["date"],
            "return_pct": {"1d": _ret(1), "5d": _ret(5), "20d": _ret(20)},
            "last_change_pct": rows[-1]["change_pct"],
            "last_turnover_rate": rows[-1]["turnover_rate"],
            "volume_20d_mean": (
                round(sum(vols) / len(vols), 2) if vols else None),
            "turnover_20d_mean": (
                round(sum(turns) / len(turns), 2) if turns else None),
        }
    finally:
        conn.close()


def _event_reaction(code: str, event: dict, cut: str) -> dict:
    """Daily reaction after a realization becomes knowable."""
    realization = event.get("realization")
    if not realization:
        return {"available": False, "reason": "事件结果在截断点尚不可见"}
    announced = str(realization.get("announced_at") or "")
    if not announced:
        return {"available": False, "reason": "事件结果缺少 announced_at"}
    event_day = announced[:10]
    event_time = announced[11:19] if len(announced) >= 19 else ""
    eod_cut = _eod_cut()
    try:
        conn = _conn(_HISTORY)
    except Exception as exc:
        return {"available": False, "reason": f"行情库不可用: {exc}"}
    try:
        prior = conn.execute(
            "SELECT date, close FROM daily_kline "
            "WHERE code=? AND date<=? ORDER BY date DESC LIMIT 1",
            (code, event_day)).fetchone()
        if event_time and event_time < "15:00:00":
            prior = conn.execute(
                "SELECT date, close FROM daily_kline "
                "WHERE code=? AND date<? ORDER BY date DESC LIMIT 1",
                (code, event_day)).fetchone()
            first_cmp = ">="
        else:
            first_cmp = ">"
        if prior is None or prior["close"] is None:
            return {"available": False, "reason": "公告前基准收盘价不可用"}
        prior_close = float(prior["close"])
        rows = conn.execute(
            f"SELECT date, open, high, low, close, volume, turnover_rate "
            f"FROM daily_kline WHERE code=? AND date {first_cmp} ? AND date<=? "
            "ORDER BY date LIMIT 5",
            (code, event_day, eod_cut)).fetchall()
        if not rows:
            return {
                "available": False,
                "reason": "公告后的交易日尚未成熟或行情缺失",
                "announced_at": announced,
            }
        first = rows[0]

        def _close_ret(row):
            return (round((float(row["close"]) / prior_close - 1) * 100, 2)
                    if row["close"] is not None and prior_close else None)

        follow = {}
        for n in (1, 3, 5):
            if len(rows) >= n:
                follow[f"{n}d"] = _close_ret(rows[n - 1])
        return {
            "available": True,
            "announced_at": announced,
            "reaction_start": first["date"],
            "prior_close_date": prior["date"],
            "open_gap_pct": (
                round((float(first["open"]) / prior_close - 1) * 100, 2)
                if first["open"] is not None and prior_close else None),
            "first_close_return_pct": _close_ret(first),
            "follow_through_close_return_pct": follow,
            "convention": (
                "announcement before 15:00 -> same-day daily bar; "
                "15:00 or later/date-only -> next trading day"),
        }
    finally:
        conn.close()


def get_event_context_fn(code: str, as_of: str = "",
                         days_back: int = 30,
                         days_ahead: int = 30) -> str:
    """Event, prior expectation, positioning and realized reaction — facts only."""
    if not code:
        return _no_data("event_context", "必须给 code")
    cut = as_of or _snapshot_cut()
    _guard_read("event expectation snapshot", cut[:10])
    try:
        from alpha_agents.data import event_expectations
        conn = _conn(_SNAPSHOTS)
    except Exception as exc:
        return _no_data("event_context", f"事件库不可用: {exc}", code=code)
    try:
        try:
            rows = event_expectations.context(
                as_of=cut, subject=code, days_back=days_back,
                days_ahead=days_ahead, conn=conn)
        except Exception as exc:
            return _no_data(
                "event_context", f"事件数据不可用: {exc}", code=code)
        if not rows:
            return _no_data(
                "event_context",
                f"{code} 在截断点前后没有已知事件/预期快照",
                code=code, as_of=cut)
        enriched = []
        for event in rows[-8:]:
            enriched.append({
                **event,
                "positioning": _event_positioning(code, event, cut),
                "reaction": _event_reaction(code, event, cut),
            })
        return _json({
            "available": True,
            "as_of": cut,
            "code": code,
            "events": enriched,
            "note": (
                "positioning 是价格/量能事实，不等于 priced-in 判断；"
                "expectation 缺失时不要自行补 consensus。"),
        })
    finally:
        conn.close()


@function_tool
@with_timeout
def get_event_context(code: str, as_of: str = "",
                      days_back: int = 30, days_ahead: int = 30) -> str:
    """这只票附近有什么可知的事件：披露日历、当时 consensus/隐含预期、
    公司实际 guidance/结果、事件前价格位置，以及结果可见后的价格反应。

    只返回事实，不判断“利好兑现”“利空出尽”或是否 priced in。没有可靠
    expectation snapshot 时会明确缺失，不会替市场编一个预期。
    """
    return get_event_context_fn(code, as_of, days_back, days_ahead)


@function_tool
@with_timeout
def get_stock_memory(code: str) -> str:
    """我认识这只票吗：我自己过去看过它几次、买过几次、每次的理由与结果、
    我对它写过什么论点。

    每次考虑一只票之前都该看一眼——否则你每天都在从零开始。亏损那一笔的
    买入理由最值得先读。
    """
    return get_stock_memory_fn(code)


@function_tool
@with_timeout
def get_my_state() -> str:
    """我今天手顺不顺：今日/本周已实现盈亏、最近 10 笔、连续亏损笔数、
    今天已经做了几个决策、当前敞口与主线集中度。

    当你怀疑"我今天是不是出手太频繁/太激进"时调用。它只报计数与事实，
    要不要收敛是你自己的判断。
    """
    return get_my_state_fn()


#: 接进 T1 Trader 的七个问题型工具。**不是** registry 里那 30 个——
#: 工具越多，模型越容易按名字的形状挑，而不是按它需要知道什么挑。
TRADER_TOOLS = [
    get_market_regime,
    get_theme_state,
    get_stock_context,
    get_intraday_shape,
    get_event_context,
    get_stock_memory,
    get_my_state,
]
