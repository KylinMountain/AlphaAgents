"""复盘 — 它看见的全部，不只是它买到的那几笔。

`evidence.analyse` 问一个问题（T-1 涨幅高的那批，平仓后是不是更好），样本
是已平仓的仓位。2026-01-05 起 20 天回放里那个样本是 **3**，低于
``evidence.MIN_TRADES``，于是一条观察都没写。拒绝是对的；只问一个问题
不是。同一个窗口在盘里躺着 8,599 行它自己的决策记录：

    theme_opportunity_items 7,800   opportunity_items 799
    episodes 17   episode_events 83   position_exits 10

这个模块把复盘拆成交易员真的会问自己的五段——主线选得对吗、主线对了
股票选得对吗、挂单价要得合理吗、卖得是不是太早、不上场的那天市场在做
什么——每段各自算一个**当天截面里的分位数**，各自申报样本量。

**数由这里算，话由 agent 写。** `GOLDEN_PRINCIPLES` 第 2 条：没有 LLM 给
自己的产出打分。模型读 7,800 行然后说「我主线选得不错」，这句话没有任何
东西能反驳它。所以百分位来自 ``daily_kline``，模型拿到的是一组它无法
篡改的数，它的工作是解释这些数——而它的解释会被下一次的数检验。

分位数而不是绝对收益，因为当天截面就是天然的对照组：「选中的主线涨了
2%」没有信息，「在它当天看过的 5 条里排第 20%」有。中位对中位，因为
A 股截面右偏（`AGENTS.md` 证据规矩）。

样本不够时申报 ``too_thin``，不猜。「没学到」和「没发生」是两件事，
``evidence.MIN_TRADES`` 的注释已经写下这条原则，这里沿用。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import statistics
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: 前向窗口，交易日。5 天与 `scoring.DEFAULT_HORIZON_DAYS` 一致，也与
#: 交易员 yaml 的 `default_horizon_days` 一致——复盘量的是它自己声明的
#: 持有尺度，换一个尺度就是在回答另一个问题。
DEFAULT_HORIZON = 5

#: 每段各自的下限。不是一个数，因为五段的样本来源根本不同：主线选择
#: 每天贡献一条，选股每笔成交贡献一条，挂单每单贡献一条。用一个统一
#: 阈值会让样本最密的那段陪着最稀的那段一起沉默。
#:
#: 都远低于晋升门槛（`GOLDEN_PRINCIPLES` §7 的 20 对）。这里的下限是
#: **记下一条观察**的下限，写入状态恒为 ``observation``。
MIN_N = {
    "direction": 10,
    "stock": 8,
    "entry": 8,
    "exit": 6,
    "absence": 3,
}

_SELECTED = "agent_selected"
#: 「看过但没选」与「根本没端上来」是两件事，而只有前者是对照组：
#: 它当时可以选而没选。`evaluated_not_offered` 是 7,640 行的全域，拿它
#: 当对照会把「没被推荐」算成「被拒绝」。
_OFFERED = ("agent_selected", "offered_not_researched", "researched_not_selected")


@dataclass(frozen=True)
class Finding:
    """一段复盘的结论：一个数、它的样本量、以及支撑它的具体条目。

    ``detail`` 不是可选的装饰。agent 写下的每一句都要能回溯到行，否则
    它写的是印象而不是观察——而印象无法被下一轮的数推翻。
    """

    dimension: str
    question: str
    n: int
    value: float | None
    unit: str
    too_thin: bool
    detail: list[dict] = field(default_factory=list)
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "dimension": self.dimension, "question": self.question,
            "n": self.n, "value": self.value, "unit": self.unit,
            "too_thin": self.too_thin, "note": self.note,
            "detail": self.detail,
        }


# ── 行情算子 ────────────────────────────────────────────────────────────


def _forward_pct(hist: sqlite3.Connection, code: str, day: str,
                 horizon: int, as_of: str | None = None) -> float | None:
    """``day`` 之后 ``horizon`` 个交易日的收益（%），窗口没走完返回 None。

    与 ``scoring._forward_return`` 同一口径。窗口未闭合时返回 None 而不是
    用现有的最后一根 K 线补齐：一个走了 2 天的 5 日窗口和一个走完的不是
    同一个量，混在一起会让最近的样本系统性偏向小幅波动。

    ``as_of`` 是硬上限，而且不是可选的谨慎——复盘的结果会进次日提示词，
    所以它必须是 PIT 的。没有这个上限，第 10 天的复盘会用第 14 天的
    K 线给第 9 天的选择打分，然后把那个分数交给第 11 天的 agent。窗口
    未闭合时行数不足，自然返回 None，这正是想要的行为。
    """
    rows = hist.execute(
        "SELECT close FROM daily_kline WHERE code = ? AND date >= ?"
        + (" AND date <= ?" if as_of else "")
        + " ORDER BY date LIMIT ?",
        ((code, day, as_of, horizon + 1) if as_of
         else (code, day, horizon + 1))).fetchall()
    if len(rows) < horizon + 1:
        return None
    start, end = rows[0]["close"], rows[horizon]["close"]
    if not start or start <= 0 or not end:
        return None
    return (end - start) / start * 100


def _market_median_pct(hist: sqlite3.Connection, day: str,
                       as_of: str | None = None) -> float | None:
    """当天全市场涨跌幅中位数。均值会被右偏截面拉高。"""
    if as_of and day > as_of:
        return None
    rows = hist.execute(
        "SELECT change_pct FROM daily_kline WHERE date = ? "
        "AND change_pct IS NOT NULL", (day,)).fetchall()
    vals = [float(r["change_pct"]) for r in rows]
    return statistics.median(vals) if len(vals) >= 50 else None


def _prev_close(hist: sqlite3.Connection, code: str, day: str) -> float | None:
    row = hist.execute(
        "SELECT close FROM daily_kline WHERE code = ? AND date < ? "
        "ORDER BY date DESC LIMIT 1", (code, day)).fetchone()
    return float(row["close"]) if row and row["close"] else None


def _bar(hist: sqlite3.Connection, code: str, day: str) -> sqlite3.Row | None:
    return hist.execute(
        "SELECT open, high, low, close FROM daily_kline "
        "WHERE code = ? AND date = ?", (code, day)).fetchone()


def _sessions_from(hist: sqlite3.Connection, day: str, n: int,
                   as_of: str | None = None) -> str | None:
    """The market's ``n``-th session at or after ``day``, or None."""
    rows = hist.execute(
        "SELECT DISTINCT date FROM daily_kline WHERE date >= ?"
        + (" AND date <= ?" if as_of else "")
        + " ORDER BY date LIMIT ?",
        ((day, as_of, n + 1) if as_of else (day, n + 1))).fetchall()
    return rows[n]["date"] if len(rows) > n else None


def _market_window_median(hist: sqlite3.Connection, d0: str,
                          dn: str) -> float | None:
    """Median return of every name that traded on both dates. The benchmark
    leg, taken as a median for the reason stated at the top of this file."""
    rows = hist.execute(
        "SELECT a.close AS c0, b.close AS cn FROM daily_kline a "
        "JOIN daily_kline b ON a.code = b.code AND b.date = ? "
        "WHERE a.date = ? AND a.close > 0", (dn, d0)).fetchall()
    rets = [(r["cn"] - r["c0"]) / r["c0"] * 100
            for r in rows if r["c0"] and r["cn"]]
    return statistics.median(rets) if len(rets) >= 50 else None


def _pct_rank(value: float, population: list[float]) -> float | None:
    """value 在 population 里的百分位，0 = 最差，100 = 最好。

    对照组少于 3 个就返回 None：两个样本的「分位数」只有 0 和 100，
    读起来像一个结论，其实是一次抛硬币。
    """
    others = [x for x in population if x is not None]
    if len(others) < 3:
        return None
    below = sum(1 for x in others if x < value)
    ties = sum(1 for x in others if x == value)
    return (below + ties / 2) / len(others) * 100


def _median_or_none(values: list[float]) -> float | None:
    vals = [v for v in values if v is not None]
    return statistics.median(vals) if vals else None


def _finding(dimension: str, question: str, unit: str,
             samples: list[float], detail: list[dict],
             note: str = "") -> Finding:
    """一段的收口：够样本就报中位数，不够就照实说不够。"""
    n = len(samples)
    thin = n < MIN_N[dimension]
    return Finding(
        dimension=dimension, question=question, n=n,
        value=None if thin else round(statistics.median(samples), 1),
        unit=unit, too_thin=thin, detail=detail,
        note=note or (f"样本 {n} < 下限 {MIN_N[dimension]}，只记不判"
                      if thin else ""),
    )


# ── ① 主线 ──────────────────────────────────────────────────────────────


def direction_choice(book: sqlite3.Connection, hist: sqlite3.Connection,
                     members: dict[str, list[str]], *,
                     run_id: str | None = None,
                     horizon: int = DEFAULT_HORIZON,
                     as_of: str | None = None,
                     member_cap: int = 60) -> Finding:
    """选中的主线，在它当天看过的那几条里排第几。

    对照组是当天的 shortlist——它当时真的可以选的那些——而不是全市场
    的主线。「你没选中今天最强的那条」在 390 条里永远成立，没有信息量；
    「在你自己看过的 5 条里你选了第 2 弱的」才是可行动的。
    """
    rows = book.execute(
        "SELECT s.day, i.sector_id, i.status FROM theme_opportunity_items i "
        "JOIN theme_opportunity_sets s ON s.id = i.theme_opportunity_set_id "
        + ("WHERE s.run_id = ? " if run_id else "")
        + "ORDER BY s.day", (run_id,) if run_id else ()).fetchall()

    by_day: dict[str, dict[str, str]] = {}
    for r in rows:
        if r["status"] in _OFFERED:
            by_day.setdefault(r["day"], {})[r["sector_id"]] = r["status"]

    fwd_cache: dict[tuple[str, str], float | None] = {}

    def theme_forward(theme: str, day: str) -> float | None:
        key = (theme, day)
        if key not in fwd_cache:
            codes = (members.get(theme) or [])[:member_cap]
            fwd_cache[key] = _median_or_none(
                [_forward_pct(hist, c, day, horizon, as_of) for c in codes])
        return fwd_cache[key]

    samples, detail = [], []
    for day, themes in sorted(by_day.items()):
        scored = {t: theme_forward(t, day) for t in themes}
        pool = [v for v in scored.values() if v is not None]
        for theme, status in themes.items():
            if status != _SELECTED or scored[theme] is None:
                continue
            rank = _pct_rank(scored[theme], pool)
            if rank is None:
                continue
            samples.append(rank)
            detail.append({"day": day, "theme": theme,
                           "forward_pct": round(scored[theme], 2),
                           "percentile": round(rank, 1),
                           "offered": len(pool)})
    return _finding(
        "direction",
        "选中的主线，在当天看过的那几条里排第几（0=最差，100=最好）",
        "百分位", samples, detail)


# ── ② 选股 ──────────────────────────────────────────────────────────────


def stock_choice(book: sqlite3.Connection, hist: sqlite3.Connection, *,
                 run_id: str | None = None,
                 horizon: int = DEFAULT_HORIZON,
                 as_of: str | None = None) -> Finding:
    """主线选对了，这条线里的票选对了吗。

    对照组是**同一天、同一条主线下**的其它候选。这是用户问的那句「选中
    主线了，为什么没选对股票」——把主线的贡献和选股的贡献分开，否则一
    笔赚钱的交易说不清是踩对了线还是挑对了票。
    """
    rows = book.execute(
        "SELECT s.day, i.code, i.status, i.panel_row_json "
        "FROM opportunity_items i "
        "JOIN opportunity_sets s ON s.id = i.opportunity_set_id "
        + ("WHERE s.run_id = ? " if run_id else "")
        + "ORDER BY s.day", (run_id,) if run_id else ()).fetchall()

    by_day: dict[str, list[dict]] = {}
    for r in rows:
        try:
            panel = json.loads(r["panel_row_json"] or "{}")
        except (TypeError, ValueError):
            panel = {}
        by_day.setdefault(r["day"], []).append({
            "code": r["code"], "status": r["status"],
            "theme": (panel.get("primary_theme") or "").strip(),
        })

    samples, detail = [], []
    for day, items in sorted(by_day.items()):
        for item in items:
            if item["status"] != _SELECTED or not item["theme"]:
                continue
            peers = [x for x in items
                     if x["theme"] == item["theme"] and x["code"] != item["code"]]
            mine = _forward_pct(hist, item["code"], day, horizon, as_of)
            if mine is None or not peers:
                continue
            pool = [_forward_pct(hist, p["code"], day, horizon, as_of)
                    for p in peers]
            rank = _pct_rank(mine, [v for v in pool if v is not None])
            if rank is None:
                continue
            samples.append(rank)
            best = max((v for v in pool if v is not None), default=None)
            detail.append({"day": day, "code": item["code"],
                           "theme": item["theme"],
                           "forward_pct": round(mine, 2),
                           "best_peer_pct": None if best is None else round(best, 2),
                           "percentile": round(rank, 1),
                           "peers": sum(1 for v in pool if v is not None)})
    return _finding(
        "stock",
        "同一条主线里，选中的票比同线其它候选好多少（0=最差，100=最好）",
        "百分位", samples, detail)


# ── ③ 买入 ──────────────────────────────────────────────────────────────


def entry_pricing(book: sqlite3.Connection, hist: sqlite3.Connection, *,
                  trader_id: str | None = None,
                  as_of: str | None = None,
                  horizon: int = DEFAULT_HORIZON) -> Finding:
    """要的价，对得上当天实际开出来的价吗。

    单位是**百分点**，不是百分位：这一段问的不是排名而是距离——挂单
    上沿离当天开盘有多远。负数表示要价低于开盘，也就是买不到。

    这条量的是一个不需要任何盈亏就能知道的事实。一张没成交的单同样是
    证据，而且没有幸存者偏差——被筛掉的恰恰是幸存者偏差挡在门外的那批。
    """
    rows = book.execute(
        "SELECT code, order_date, entry_high, status, open_date, shares "
        "FROM virtual_portfolio "
        "WHERE order_date IS NOT NULL AND entry_high IS NOT NULL"
        + (" AND trader_id = ?" if trader_id else "")
        + (" AND order_date <= ?" if as_of else "")
        + " ORDER BY order_date",
        tuple(x for x in (trader_id, as_of) if x)).fetchall()

    samples, detail, missed = [], [], []
    filled = 0
    for r in rows:
        prev = _prev_close(hist, r["code"], r["order_date"])
        bar = _bar(hist, r["code"], r["order_date"])
        if not prev or not bar or not bar["open"]:
            continue
        ask = float(r["entry_high"]) / prev
        opened = float(bar["open"]) / prev
        gap_pp = (ask - opened) * 100
        samples.append(gap_pp)
        got_in = bool(r["open_date"]) and (r["shares"] or 0) > 0
        filled += 1 if got_in else 0
        # What the pass cost, measured from the price it was looking at when
        # it priced the order — the T-1 close, not the gap-up open. Measuring
        # from the open would build the answer into the question: a name that
        # is missed *because* it gapped up is scored from the top of that gap.
        excess = None
        if not got_in:
            dn = _sessions_from(hist, r["order_date"], horizon, as_of)
            end = (hist.execute(
                "SELECT close FROM daily_kline WHERE code = ? AND date = ?",
                (r["code"], dn)).fetchone() if dn else None)
            mkt = _market_window_median(hist, r["order_date"], dn) if dn else None
            if end and end["close"] and mkt is not None:
                excess = (float(end["close"]) - prev) / prev * 100 - mkt
                missed.append(excess)
        detail.append({"day": r["order_date"], "code": r["code"],
                       "ask_x_prev_close": round(ask, 3),
                       "open_x_prev_close": round(opened, 3),
                       "gap_pp": round(gap_pp, 2),
                       "status": r["status"], "filled": got_in,
                       "missed_excess_pct": (None if excess is None
                                             else round(excess, 2))})
    out = _finding(
        "entry",
        "挂单上沿减当天开盘，占 T-1 收盘的百分点（负 = 要价低于开盘，买不到）",
        "百分点", samples, detail)
    # A gap with no consequence attached reads as a statistic, and the trader
    # persona on the other side of the prompt says 宁可错过. The trade-off is
    # the thing it can weigh: this many never filled, and this is what they
    # then did. If the misses had outperformed, the same line would argue the
    # other way — which is what makes it a measurement and not an instruction.
    if samples:
        cost = (f"，未成交那批 {horizon} 日超额中位 "
                f"{statistics.median(missed):+.2f}%（n={len(missed)}）"
                if missed else "")
        note = (f"{len(samples)} 单里成交 {filled} 单{cost}")
        out = Finding(**{**out.as_dict(),
                         "note": out.note + ("；" if out.note else "") + note})
    return out


# ── ④ 卖出 ──────────────────────────────────────────────────────────────


def exit_timing(book: sqlite3.Connection, hist: sqlite3.Connection, *,
                horizon: int = DEFAULT_HORIZON,
                as_of: str | None = None) -> Finding:
    """卖出价落在「卖出当天到之后 N 日」那段区间的什么位置。

    100 = 卖在了这段的最高点，0 = 最低点。用**卖出之后**的区间，不是
    持有期内的：持有期的高点是它当时看得见的，事后指着它说「你没卖在
    那里」是后见之明；卖出之后的走势才回答「再拿几天会怎样」，而那正是
    减仓这个动作赌的东西。
    """
    rows = book.execute(
        "SELECT code, exit_date, price, shares, return_pct, reason "
        "FROM position_exits ORDER BY exit_date, id").fetchall()

    samples, detail = [], []
    for r in rows:
        bars = hist.execute(
            "SELECT high, low FROM daily_kline WHERE code = ? AND date >= ?"
            + (" AND date <= ?" if as_of else "")
            + " ORDER BY date LIMIT ?",
            ((r["code"], r["exit_date"], as_of, horizon + 1) if as_of
             else (r["code"], r["exit_date"], horizon + 1))).fetchall()
        if len(bars) < horizon + 1:
            continue
        highs = [float(b["high"]) for b in bars if b["high"]]
        lows = [float(b["low"]) for b in bars if b["low"]]
        if not highs or not lows:
            continue
        hi, lo, px = max(highs), min(lows), float(r["price"])
        if hi <= lo:
            continue
        pos = (px - lo) / (hi - lo) * 100
        samples.append(pos)
        detail.append({"day": r["exit_date"], "code": r["code"],
                       "exit_price": px, "window_high": round(hi, 2),
                       "window_low": round(lo, 2),
                       "position_pct": round(pos, 1),
                       "left_on_table_pct": round((hi - px) / px * 100, 2),
                       "reason": (r["reason"] or "")[:80]})
    return _finding(
        "exit",
        f"卖出价在「卖出日至之后 {horizon} 日」区间的位置（100=卖在最高）",
        "百分位", samples, detail)


# ── ⑤ 不作为 ────────────────────────────────────────────────────────────


def absence(book: sqlite3.Connection, hist: sqlite3.Connection, *,
            run_id: str | None = None,
            as_of: str | None = None) -> Finding:
    """没上场的那些天，市场在做什么。

    空仓不是中性的。在一个上行窗口里，不作为和做错一样要付代价，而
    盈亏表看不见这笔——它只记录发生过的交易。
    """
    days = [r["day"] for r in book.execute(
        "SELECT DISTINCT day FROM opportunity_sets "
        + ("WHERE run_id = ? " if run_id else "")
        + "ORDER BY day", (run_id,) if run_id else ()).fetchall()]
    held = book.execute(
        "SELECT open_date, close_date FROM virtual_portfolio "
        "WHERE open_date IS NOT NULL AND shares > 0").fetchall()

    samples, detail = [], []
    for day in days:
        on_field = any(
            h["open_date"] <= day and (h["close_date"] is None
                                       or day <= h["close_date"])
            for h in held)
        if on_field:
            continue
        mkt = _market_median_pct(hist, day, as_of)
        if mkt is None:
            continue
        samples.append(mkt)
        detail.append({"day": day, "market_median_pct": round(mkt, 2)})
    return _finding(
        "absence", "空仓那天，全市场涨跌幅中位数", "%", samples, detail,
        note="" if len(samples) >= MIN_N["absence"] else
             f"空仓 {len(samples)} 天，不足以判断")


# ── 收口 ────────────────────────────────────────────────────────────────


def review(book: sqlite3.Connection, hist: sqlite3.Connection,
           members: dict[str, list[str]], *,
           run_id: str | None = None, trader_id: str | None = None,
           horizon: int = DEFAULT_HORIZON,
           as_of: str | None = None) -> list[Finding]:
    """五段复盘。

    不发起任何模型调用——这是它可信的前提；``as_of`` 封住未来——这是它
    可以被喂回提示词的前提。两条缺一条，这个模块就不该存在。
    """
    return [
        direction_choice(book, hist, members, run_id=run_id, horizon=horizon,
                         as_of=as_of),
        stock_choice(book, hist, run_id=run_id, horizon=horizon, as_of=as_of),
        entry_pricing(book, hist, trader_id=trader_id, as_of=as_of,
                      horizon=horizon),
        exit_timing(book, hist, horizon=horizon, as_of=as_of),
        absence(book, hist, run_id=run_id, as_of=as_of),
    ]


def as_text(findings: list[Finding]) -> str:
    """喂给 agent 的那一段。数在这里，话由它写。"""
    if not findings:
        return ""
    lines = ["【你自己的复盘（数由行情算出，不是评价；请针对这些数写观察）】"]
    for f in findings:
        if f.too_thin:
            lines.append(f"· {f.question}\n    样本 {f.n}，不足以判断（{f.note}）")
            continue
        tail = f"；{f.note}" if f.note else ""
        lines.append(f"· {f.question}\n    中位 {f.value}{f.unit}（n={f.n}）{tail}")
    lines.append("每一条都可以回溯到具体的日期与代码；说不清的就说不清，"
                 "不要把样本不足写成结论。")
    return "\n".join(lines)
