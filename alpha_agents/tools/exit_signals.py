"""Exit timing — when to sell a position we already hold.

News is the entry catalyst; it says nothing about when to leave. This
module answers the exit question from price and volume alone, with no LLM
call, so every decision is cheap and reproducible.

WHAT THE DATA SAID
------------------
Both mechanisms below were forward-tested on data/market_history.db
(5,699 stocks, 2020-01..2026-09), measuring median excess return against
the median stock over the same window — median-vs-median, because A-share
returns are right-skewed and comparing a bucket median against a market
*mean* manufactures a fake negative edge.

1. Classic distribution patterns — bearish RSI divergence, 放量滞涨,
   高位缩量 — DO NOT WORK. On the population that matters (stocks already
   up ≥8% in 20 days, i.e. what we would actually be holding), all three
   were counter-indicators: +0.51pp, +1.52pp and +0.51pp of excess return
   versus non-firing controls, and the sign was consistent across five of
   six time windows. They are kept below for display only — never wire
   them into a sell decision. RSI is computed for the same reason.

2. Regime-conditional holding period WORKS. For stocks already up ≥15%,
   median excess return by market regime (24 dense windows):

       大盘20日      持3日    持5日   持10日   持20日     n
       强势 >+3%    +0.11%   -0.95%  -0.75%   -2.76%   1171
       震荡 ±3%     -1.04%   -0.81%  -1.58%   -2.89%    680
       弱势 <-3%    -1.73%   -2.85%  -4.14%   -6.48%    250

   Monotonic in both directions: weaker market → worse at every horizon;
   longer hold → worse in every regime. The gradient survives excluding
   2026-05 onwards (the crash window), where 弱势 goes -1.30/-2.10/
   -3.83/-7.18 on n=170 — so it is not a single-crash artifact.

   Only one cell is positive: 强势 held 3 days. Everything else bleeds.
   Hence the caps in MAX_HOLDING_DAYS.
"""

import logging
import sqlite3

from alpha_agents.config import DATA_DIR

logger = logging.getLogger(__name__)

DB_PATH = DATA_DIR / "market_history.db"

RSI_PERIOD = 14
LOOKBACK_BARS = 60

# Trading days a position may be held, by market regime. Derived from the
# table above: hold only as long as the median excess return is not yet
# clearly negative.
MAX_HOLDING_DAYS = {
    "strong": 3,   # 大盘20日 > +3% — the one profitable cell
    "neutral": 2,  # 震荡 — already -1.04% by day 3, so leave sooner
    "weak": 0,     # 弱势 — -1.73% by day 3; exit at the next opportunity
}

# Regime thresholds on the market index's 20-day return.
REGIME_STRONG_PCT = 3.0
REGIME_WEAK_PCT = -3.0

# Below this 20-day run-up the decay is inside the noise band (±0.5%), so
# the holding cap does not apply — those positions are governed by the
# ordinary stop/target machinery.
RUNUP_THRESHOLD_PCT = 15.0


def _connect() -> sqlite3.Connection | None:
    if not DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as e:
        logger.debug("exit_signals: cannot open %s: %s", DB_PATH, e)
        return None


def _load_bars(code: str, limit: int = LOOKBACK_BARS) -> list[dict]:
    """Most recent daily bars for one stock, oldest first."""
    conn = _connect()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT date, open, high, low, close, volume FROM daily_kline "
            "WHERE code = ? ORDER BY date DESC LIMIT ?",
            (code, limit),
        ).fetchall()
    except sqlite3.Error as e:
        logger.debug("exit_signals: kline read failed for %s: %s", code, e)
        return []
    finally:
        conn.close()
    return [dict(r) for r in reversed(rows)]


# ── Regime-conditional holding cap (the mechanism that works) ─────────

def get_market_regime() -> tuple[str, float]:
    """Classify the market by the index's 20-day return.

    Uses ChiNext (399006) when present — it is the higher-beta index and
    turns first — falling back to an equal-weighted mean across the whole
    daily_kline table.

    Returns (regime, pct). Regime is "strong" / "neutral" / "weak", or
    "unknown" with 0.0 when there is not enough data, in which case
    callers should not apply a holding cap.
    """
    conn = _connect()
    if conn is None:
        return "unknown", 0.0
    try:
        rows = conn.execute(
            "SELECT close FROM daily_kline WHERE code = '399006' "
            "ORDER BY date DESC LIMIT 21"
        ).fetchall()
        if len(rows) < 21:
            # Equal-weighted proxy: mean close per date over recent dates.
            rows = conn.execute(
                "SELECT AVG(close) AS close FROM daily_kline "
                "WHERE date IN (SELECT DISTINCT date FROM daily_kline "
                "               ORDER BY date DESC LIMIT 21) "
                "GROUP BY date ORDER BY date DESC"
            ).fetchall()
        if len(rows) < 21:
            return "unknown", 0.0
        latest = rows[0]["close"]
        prior = rows[20]["close"]
    except sqlite3.Error as e:
        logger.debug("exit_signals: regime query failed: %s", e)
        return "unknown", 0.0
    finally:
        conn.close()

    if not latest or not prior or prior <= 0:
        return "unknown", 0.0

    pct = (latest - prior) / prior * 100
    if pct > REGIME_STRONG_PCT:
        return "strong", round(pct, 2)
    if pct < REGIME_WEAK_PCT:
        return "weak", round(pct, 2)
    return "neutral", round(pct, 2)


def get_runup_pct(code: str) -> float | None:
    """The stock's 20-day run-up, or None when history is too short."""
    bars = _load_bars(code, limit=21)
    if len(bars) < 21:
        return None
    latest, prior = bars[-1]["close"], bars[0]["close"]
    if not latest or not prior or prior <= 0:
        return None
    return round((latest - prior) / prior * 100, 2)


def check_holding_period(code: str, holding_days: int) -> tuple[bool, str]:
    """Should this position be closed on holding period alone?

    Applies only to positions already up ≥15% over 20 days — below that
    the measured decay is inside the noise band. Returns (should_close,
    reason).
    """
    runup = get_runup_pct(code)
    if runup is None or runup < RUNUP_THRESHOLD_PCT:
        return False, ""

    regime, pct = get_market_regime()
    if regime == "unknown":
        return False, ""

    cap = MAX_HOLDING_DAYS[regime]
    if holding_days < cap:
        return False, ""

    label = {"strong": "强势", "neutral": "震荡", "weak": "弱势"}[regime]
    return True, (
        f"持有期到限(涨{runup:.0f}%/大盘{label}{pct:+.1f}%/"
        f"持{holding_days}日≥{cap}日)"
    )


# ── Display-only indicators (tested, no edge — do not gate on these) ──

def compute_rsi(closes: list[float], period: int = RSI_PERIOD) -> list[float | None]:
    """Wilder's RSI. Returns one value per close; None until seeded.

    Wilder smoothing (not a simple moving average) is what every charting
    package means by "RSI", so values here match what a human would see.
    Shown in reports for context; it carries no exit edge on its own.
    """
    n = len(closes)
    out: list[float | None] = [None] * n
    if n <= period:
        return out

    gains = losses = 0.0
    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        gains += max(delta, 0.0)
        losses += max(-delta, 0.0)
    avg_gain = gains / period
    avg_loss = losses / period

    def _rsi(g: float, l: float) -> float:
        if l == 0:
            return 100.0
        return 100.0 - (100.0 / (1.0 + g / l))

    out[period] = _rsi(avg_gain, avg_loss)
    for i in range(period + 1, n):
        delta = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(delta, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-delta, 0.0)) / period
        out[i] = _rsi(avg_gain, avg_loss)
    return out


def _swing_highs(highs: list[float], window: int = 3) -> list[int]:
    """Indices of local maxima with `window` bars clear on each side."""
    return [
        i for i in range(window, len(highs) - window)
        if highs[i] > max(highs[i - window:i])
        and highs[i] > max(highs[i + 1:i + 1 + window])
    ]


def detect_bearish_divergence(bars: list[dict]) -> tuple[float, str] | None:
    """Price higher high while RSI makes a lower high.

    Display only — forward-tested at +0.51pp excess versus controls, i.e.
    the opposite of a sell signal.
    """
    if len(bars) < RSI_PERIOD + 15:
        return None
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    if not all(closes) or not all(highs):
        return None

    rsi = compute_rsi(closes)
    swings = [i for i in _swing_highs(highs) if rsi[i] is not None]
    if len(swings) < 2:
        return None

    recent, prior = swings[-1], swings[-2]
    if recent < len(bars) - 10:
        return None
    if highs[recent] <= highs[prior] or rsi[recent] >= rsi[prior]:
        return None

    gap = rsi[prior] - rsi[recent]
    return (0.95 if gap >= 10 else 0.97,
            f"顶背离(价新高/RSI {rsi[prior]:.0f}→{rsi[recent]:.0f})")


def detect_churning(bars: list[dict]) -> tuple[float, str] | None:
    """放量滞涨 — heavy volume that fails to move price.

    Display only — forward-tested at +1.52pp excess versus controls.
    """
    if len(bars) < 21:
        return None
    last = bars[-1]
    vols = [b["volume"] for b in bars[-21:-1] if b["volume"]]
    if len(vols) < 15 or not last.get("volume"):
        return None
    avg_vol = sum(vols) / len(vols)
    prev_close = bars[-2]["close"]
    if avg_vol <= 0 or not prev_close:
        return None

    ratio = last["volume"] / avg_vol
    change = (last["close"] - prev_close) / prev_close * 100
    if ratio >= 2.0 and change <= 1.0:
        return 0.96, f"放量滞涨(量{ratio:.1f}x/涨{change:+.1f}%)"
    if ratio >= 1.5 and change <= 0.0:
        return 0.97, f"放量下跌(量{ratio:.1f}x/跌{change:.1f}%)"
    return None


def detect_no_demand_at_high(bars: list[dict]) -> tuple[float, str] | None:
    """高位缩量 — near the 60-bar high while volume dries up.

    Display only — forward-tested at +0.51pp excess versus controls.
    """
    if len(bars) < 21:
        return None
    last = bars[-1]
    highs = [b["high"] for b in bars if b["high"]]
    if not highs or not last.get("volume") or not last.get("close"):
        return None

    peak = max(highs)
    if peak <= 0 or last["close"] < peak * 0.93:
        return None

    vols = [b["volume"] for b in bars[-21:-1] if b["volume"]]
    if len(vols) < 15:
        return None
    avg_vol = sum(vols) / len(vols)
    if avg_vol <= 0:
        return None

    ratio = last["volume"] / avg_vol
    if ratio <= 0.6:
        return (0.98,
                f"高位缩量(量{ratio:.1f}x/距顶{(1 - last['close'] / peak) * 100:.1f}%)")
    return None


def describe_exit_state(code: str, holding_days: int = 0) -> dict:
    """Full exit picture for one stock — for reports and the dashboard.

    `patterns` are informational; `should_close` is the decision, and it
    comes from the holding-period rule alone.
    """
    bars = _load_bars(code)
    if len(bars) < RSI_PERIOD + 15:
        return {"ok": False, "error": "历史数据不足"}

    rsi = compute_rsi([b["close"] for b in bars])
    patterns = []
    for fn in (detect_bearish_divergence, detect_churning, detect_no_demand_at_high):
        try:
            hit = fn(bars)
        except Exception as e:
            logger.debug("exit_signals: %s failed for %s: %s", fn.__name__, code, e)
            continue
        if hit:
            patterns.append(hit[1])

    regime, regime_pct = get_market_regime()
    runup = get_runup_pct(code)
    should_close, reason = check_holding_period(code, holding_days)

    return {
        "ok": True,
        "code": code,
        "date": bars[-1]["date"],
        "close": bars[-1]["close"],
        "rsi14": round(rsi[-1], 1) if rsi[-1] is not None else None,
        "runup_20d": runup,
        "regime": regime,
        "regime_pct": regime_pct,
        "max_holding_days": MAX_HOLDING_DAYS.get(regime),
        "patterns": patterns,          # informational only
        "should_close": should_close,
        "reason": reason,
    }
