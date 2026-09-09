"""The raw material for deciding where to buy — nothing more.

The system was placing 109 of its 113 orders at ``price * 0.97``. Not
because anyone decided a 3% pullback was right, but because that constant
lived in a code path with no agent in it: a scoring function picked the
stock and then priced the order itself. Naming the constant "entry style"
and putting it in a config file, which is what this replaces, only made
the absence of judgement look like a decision.

The morning agent *could* already state its own entry price and did — all
four of its orders carried a level it wrote. But the prompt told it that
``get_institutional_position`` returns "介入区间和止损位", and that tool
returns no such fields. So the agent was told a number would be handed to
it, and it never was: every one of those four orders set only an upper
bound and none ever set a lower one.

This tool gives it the numbers instead of a verdict. Deliberately: a
"suggested entry price" would just be this file's opinion replacing the
constant, and the agent would learn nothing from following it. Levels are
facts about where the stock has traded; which one to buy at is the
judgement, it is the agent's, and the review can grade it because the
thesis records what was chosen.

**Everything here is a close-based level.** Intraday highs and lows are
not used: a wick through a level is not the market trading there, and the
fill simulation matches on the close-to-close path anyway.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# Enough history for a 20-day range and a 20-day mean with room to spare.
# Longer would smooth away exactly the swing structure that decides an
# entry over a 3-5 day horizon.
_LOOKBACK = 60

# A swing low is a close lower than this many closes on each side of it.
# Two is loose enough to find levels in a five-day window and strict
# enough that noise does not qualify.
_SWING_WIDTH = 2


def _sma(values: list[float], n: int) -> float | None:
    return round(sum(values[-n:]) / n, 2) if len(values) >= n else None


def _atr_pct(bars: list[dict], n: int = 14) -> float | None:
    """Average true range as a share of price — how far it moves in a day.

    The number an entry zone should be sized against. A 1% band on a stock
    that swings 6% a day is not a zone, it is a coin flip on whether the
    order fills at all; the same 1% on a 1.5% mover is most of a session.
    """
    if len(bars) < n + 1:
        return None
    trs = []
    for prev, cur in zip(bars[-n - 1:-1], bars[-n:]):
        pc = prev.get("close") or 0
        hi = cur.get("high") or cur.get("close") or 0
        lo = cur.get("low") or cur.get("close") or 0
        if not pc:
            continue
        trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))
    if not trs:
        return None
    last = bars[-1].get("close") or 0
    return round(sum(trs) / len(trs) / last * 100, 2) if last else None


def _swings(closes: list[float], want_low: bool) -> list[float]:
    """Closes that are local extremes — where the market turned before."""
    out = []
    w = _SWING_WIDTH
    for i in range(w, len(closes) - w):
        window = closes[i - w:i + w + 1]
        pivot = closes[i]
        if (pivot == min(window) if want_low else pivot == max(window)):
            out.append(pivot)
    return out


def get_price_levels_fn(code: str, realtime_price: float | None = None) -> str:
    """Where this stock has actually traded, for pricing an entry.

    Returns levels and no recommendation. See the module docstring for why
    that separation is the point rather than an omission.
    """
    from alpha_agents.data.market_data import get_stock_history

    try:
        bars = get_stock_history(code, days=_LOOKBACK)
    except Exception as e:
        logger.warning("Price levels unavailable for %s: %s", code, e)
        return json.dumps({"error": f"{code} 历史数据获取失败: {e}"},
                          ensure_ascii=False)

    if not bars or len(bars) < 10:
        return json.dumps(
            {"error": f"{code} 历史数据不足（{len(bars or [])}根），无法给出价位。"
                      f"不要据此瞎猜介入价，说明缺数据即可。"},
            ensure_ascii=False)

    closes = [b["close"] for b in bars if b.get("close")]
    last = realtime_price or closes[-1]

    hi20, lo20 = max(closes[-20:]), min(closes[-20:])
    rng = hi20 - lo20
    pos_pct = round((last - lo20) / rng * 100, 1) if rng > 0 else 50.0

    # Swing lows below the current price are where buyers showed up before;
    # swing highs above it are where sellers did. Both are only candidates
    # — which one is the entry depends on the thesis, not on this file.
    lows = sorted({round(x, 2) for x in _swings(closes, True) if x < last},
                  reverse=True)[:3]
    highs = sorted({round(x, 2) for x in _swings(closes, False) if x > last})[:3]

    vol5 = [b.get("volume") or 0 for b in bars[-5:]]
    if sum(vol5) > 0:
        vwap5 = round(sum(b["close"] * (b.get("volume") or 0)
                          for b in bars[-5:]) / sum(vol5), 2)
    else:
        vwap5 = _sma(closes, 5)

    atr = _atr_pct(bars)
    out = {
        "code": code,
        "price": round(last, 2),
        "bars": len(closes),
        # Trend reference. A price under its own 20-day mean and a price
        # above it are different trades even on the same thesis.
        "ma": {"ma5": _sma(closes, 5), "ma10": _sma(closes, 10),
               "ma20": _sma(closes, 20), "ma60": _sma(closes, 60)},
        "vwap_5d": vwap5,
        "range_20d": {"high": round(hi20, 2), "low": round(lo20, 2),
                      "position_pct": pos_pct},
        "range_60d": {"high": round(max(closes), 2),
                      "low": round(min(closes), 2)},
        # How far it moves in a normal day. An entry band narrower than
        # this will rarely fill; one much wider is not a level, it is a
        # shrug.
        "atr_pct": atr,
        "support_below": lows,
        "resistance_above": highs,
        "gap_to_support_pct": (round((last - lows[0]) / last * 100, 2)
                               if lows else None),
        "gap_to_resistance_pct": (round((highs[0] - last) / last * 100, 2)
                                  if highs else None),
        "note": ("以上都是收盘价口径的事实，不含任何建议。介入价由你根据"
                 "论点决定：等回调就挂在支撑上方一点，追突破就挂在阻力上方"
                 "一点，band 宽度参考 atr_pct——比日均波动还窄的区间基本"
                 "不会成交。"),
    }
    return json.dumps(out, ensure_ascii=False)
