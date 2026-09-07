"""VPA Confirmed Signal Backtest — Anna Coulling's "wait for confirmation".

Core idea: VPA signal on Day T is NOT an entry signal. Entry only happens
when a CONFIRMATION K-line appears within the next 1-3 days.

Confirmation rules (from Anna Coulling):
  Bullish signal confirmation:
    - 阳线 (close > open)
    - 量比 > 1.0 (volume above average — real buying, not dead cat bounce)
    → Both conditions met = confirmed, enter at that day's close

  Bearish signal confirmation:
    - 阴线 (close < open)
    - 量比 > 1.0 (volume above average — real selling, not profit taking)
    → Both conditions met = confirmed, enter short / avoid

  No confirmation within 3 days → signal expired, discard.

Measures: returns from CONFIRMATION day (not signal day) forward T+1..T+5.

Usage:
  uv run python scripts/backtest_vpa_confirmed.py
  uv run python scripts/backtest_vpa_confirmed.py --max-stocks 500
"""

import argparse
import csv
import logging
import statistics
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")
except ImportError:
    pass

import numpy as np
import pandas as pd

from alpha_agents.data.market_history import _get_conn as _history_conn
from alpha_agents.tools.vpa import (
    _compute_derived, _detect_patterns, _obv_trend, get_pattern_scores,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backtest_confirmed")

HORIZONS = [1, 2, 3, 4, 5]
WINDOW = 20
MIN_HISTORY = 60
CONFIRM_WINDOW = 3  # Max days to wait for confirmation


def compute_vpa_signal(df_slice, regime=""):
    """Detect VPA signal from historical slice. Returns signal dict or None."""
    if len(df_slice) < WINDOW + 5:
        return None
    df = _compute_derived(df_slice.reset_index(drop=True), window=WINDOW)
    patterns = _detect_patterns(df, regime=regime)
    obv = _obv_trend(df)
    net_score = sum(p["strength"] for p in patterns)
    if obv == "上升":
        net_score += 1
    elif obv == "下降":
        net_score -= 1

    # Use regime-adaptive thresholds
    if regime == "falling":
        bt, brt = 2, -3
    elif regime == "rising":
        bt, brt = 4, -3
    else:
        bt, brt = 2, -4

    if net_score >= bt:
        signal = "bullish"
    elif net_score <= brt:
        signal = "bearish"
    else:
        return None  # No signal = nothing to confirm

    primary = ""
    if patterns:
        primary = max(patterns, key=lambda p: abs(p["strength"]))["pattern"]

    return {
        "signal": signal,
        "net_score": net_score,
        "primary_pattern": primary,
    }


def check_confirmation(df_full, signal_idx, signal_type, pattern, max_wait=CONFIRM_WINDOW):
    """Anna Coulling confirmation rules — per signal type.

    Each pattern has its own confirmation logic from the book:

    ── BEARISH confirmations ──

    top_divergence (顶部背离: 价涨量缩):
      Book: "上涨中连续多根K线成交量逐步萎缩 → 趋势减弱"
      Confirm: 后续出现 阴线 + 收盘跌破信号日低点 (breakdown)

    healthy_uptrend used as bearish (追高信号):
      Book: "短阳/短阴 + 高成交量 → 拉锯反转"
      Confirm: 后续出现 阴线 + 量比>0.8 (不需要巨量，正常确认即可)

    buying_climax (买入高潮: 冲高回落):
      Book: "2~3根带长上影线 + 极高成交量"
      Confirm: 后续出现 第2根长上影阴线 或 放量阴线跌破前日低点

    distribution (放量滞涨):
      Book: "低实体 + 高成交量 → 买卖拉锯"
      Confirm: 后续出现 阴线 (方向选择向下)

    no_demand (无需求反弹):
      Book: "缩量阳线 = 虚假反弹"
      Confirm: 后续出现 阴线 (反弹结束回落)

    ── BULLISH confirmations ──

    bottom_volume_spike (底部放量):
      Book: "暴跌中长下影线 + 极高成交量 → 买入高峰"
      Confirm: 后续出现 2根连续阳线 或 阳线+量比>1.0 突破信号日高点

    selling_climax (卖出高潮):
      Book: "急跌巨量收回过半 → 恐慌见底"
      Confirm: 供给测试 — 缩量回踩不破信号日低点 (low volume test)

    bottom_exhaustion (卖压衰竭):
      Book: "下跌中成交量逐步萎缩 → 卖压枯竭"
      Confirm: 后续出现 放量阳线 (新买盘入场)

    no_supply (无供给回调):
      Book: "缩量阴线 = 卖方枯竭"
      Confirm: 后续出现 阳线 (回调结束恢复上涨)

    absorption (吸筹):
      Confirm: 放量突破近期高点

    Returns (confirmed_idx, confirm_day_offset, confirm_method) or (None, None, None).
    """
    if signal_idx + 1 >= len(df_full):
        return None, None, None

    signal_row = df_full.iloc[signal_idx]
    signal_high = signal_row["high"]
    signal_low = signal_row["low"]
    signal_close = signal_row["close"]

    def _row(offset):
        ci = signal_idx + offset
        if ci >= len(df_full):
            return None, ci
        return df_full.iloc[ci], ci

    def _is_yang(r):
        return r["close"] > r["open"]

    def _is_yin(r):
        return r["close"] < r["open"]

    def _vol_ratio(r):
        v = r.get("volume_ratio", 0)
        return 0 if pd.isna(v) else v

    def _upper_shadow(r):
        v = r.get("upper_shadow", 0)
        return 0 if pd.isna(v) else v

    # ── BEARISH pattern confirmations ──

    if pattern == "top_divergence":
        # Confirm: 阴线 + 收盘跌破信号日低点
        for off in range(1, max_wait + 1):
            r, ci = _row(off)
            if r is None:
                break
            if _is_yin(r) and r["close"] < signal_low:
                return ci, off, "breakdown_below_signal_low"
        return None, None, None

    if pattern == "healthy_uptrend":  # used as bearish
        # Confirm: 阴线 + 量比>0.8
        for off in range(1, max_wait + 1):
            r, ci = _row(off)
            if r is None:
                break
            if _is_yin(r) and _vol_ratio(r) > 0.8:
                return ci, off, "yin_with_volume"
        return None, None, None

    if pattern == "buying_climax":
        # Confirm: 放量阴线 或 第2根长上影线
        for off in range(1, max_wait + 1):
            r, ci = _row(off)
            if r is None:
                break
            if _is_yin(r) and _vol_ratio(r) > 1.0:
                return ci, off, "volume_yin_after_climax"
            if _upper_shadow(r) > 0.4 and _vol_ratio(r) > 1.0:
                return ci, off, "second_shooting_star"
        return None, None, None

    if pattern == "distribution":
        # Confirm: 阴线 (方向选择向下)
        for off in range(1, max_wait + 1):
            r, ci = _row(off)
            if r is None:
                break
            if _is_yin(r):
                return ci, off, "direction_chosen_down"
        return None, None, None

    if pattern == "no_demand":
        # Confirm: 阴线 (反弹结束)
        for off in range(1, max_wait + 1):
            r, ci = _row(off)
            if r is None:
                break
            if _is_yin(r):
                return ci, off, "bounce_failed"
        return None, None, None

    if pattern == "selling_climax":
        # Confirm: 缩量回踩不破信号日低点 (supply test)
        for off in range(1, max_wait + 1):
            r, ci = _row(off)
            if r is None:
                break
            # Supply test: price dips but doesn't break low, on LOW volume
            if r["low"] >= signal_low * 0.99 and _vol_ratio(r) < 1.0:
                return ci, off, "supply_test_passed"
        return None, None, None

    if pattern == "bottom_volume_spike":
        # Confirm: 阳线 + 量比>1.0 (real buying follow-through)
        # Or: 2 consecutive 阳线
        consecutive_yang = 0
        for off in range(1, max_wait + 1):
            r, ci = _row(off)
            if r is None:
                break
            if _is_yang(r):
                consecutive_yang += 1
                if _vol_ratio(r) > 1.0:
                    return ci, off, "volume_yang_followthrough"
                if consecutive_yang >= 2:
                    return ci, off, "two_consecutive_yang"
            else:
                consecutive_yang = 0
        return None, None, None

    if pattern == "bottom_exhaustion":
        # Confirm: 放量阳线 (new buying enters)
        for off in range(1, max_wait + 1):
            r, ci = _row(off)
            if r is None:
                break
            if _is_yang(r) and _vol_ratio(r) > 1.0:
                return ci, off, "volume_yang_new_buying"
        return None, None, None

    if pattern == "no_supply":
        # Confirm: 阳线 (bounce resumes)
        for off in range(1, max_wait + 1):
            r, ci = _row(off)
            if r is None:
                break
            if _is_yang(r):
                return ci, off, "bounce_resumes"
        return None, None, None

    if pattern == "absorption":
        # Confirm: 放量突破近期高点
        for off in range(1, max_wait + 1):
            r, ci = _row(off)
            if r is None:
                break
            if r["close"] > signal_high and _vol_ratio(r) > 1.2:
                return ci, off, "volume_breakout"
        return None, None, None

    # Default: generic confirmation (fallback)
    for off in range(1, max_wait + 1):
        r, ci = _row(off)
        if r is None:
            break
        if signal_type == "bullish" and _is_yang(r) and _vol_ratio(r) > 1.0:
            return ci, off, "generic_bullish"
        if signal_type == "bearish" and _is_yin(r) and _vol_ratio(r) > 1.0:
            return ci, off, "generic_bearish"

    return None, None, None


def load_all_histories(conn, max_stocks=None):
    rows = conn.execute(
        "SELECT code, COUNT(*) n FROM daily_kline GROUP BY code HAVING n >= ? ORDER BY code",
        (MIN_HISTORY + CONFIRM_WINDOW + max(HORIZONS) + 5,),
    ).fetchall()
    codes = [r["code"] for r in rows]
    if max_stocks:
        codes = codes[:max_stocks]
    histories = {}
    for code in codes:
        rows = conn.execute(
            "SELECT date, open, high, low, close, volume FROM daily_kline WHERE code = ? ORDER BY date",
            (code,),
        ).fetchall()
        if not rows:
            continue
        df = pd.DataFrame([dict(r) for r in rows])
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["open", "high", "low", "close", "volume"]).reset_index(drop=True)
        if len(df) >= MIN_HISTORY + CONFIRM_WINDOW + max(HORIZONS) + 5:
            histories[code] = df
    return histories


def build_regime_map(conn):
    rows = conn.execute(
        "SELECT date, AVG(change_pct) AS avg_chg FROM daily_kline GROUP BY date ORDER BY date"
    ).fetchall()
    dates = [r["date"] for r in rows]
    avg_chg = {r["date"]: float(r["avg_chg"] or 0) for r in rows}
    regime_map = {}
    for i, date in enumerate(dates):
        if i < 10:
            regime_map[date] = "unknown"
            continue
        vals = [avg_chg[d] for d in dates[i - 10: i]]
        m = sum(vals) / len(vals)
        regime_map[date] = "rising" if m > 0.3 else ("falling" if m < -0.3 else "ranging")
    return regime_map


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-stocks", type=int, default=None)
    parser.add_argument("--out", type=str, default="data/vpa_confirmed.csv")
    args = parser.parse_args()

    conn = _history_conn()
    regime_map = build_regime_map(conn)
    histories = load_all_histories(conn, max_stocks=args.max_stocks)
    logger.info("Loaded %d histories", len(histories))

    all_dates = sorted(set(d for df in histories.values() for d in df["date"].tolist()))
    max_h = max(HORIZONS)
    # Need: MIN_HISTORY before, CONFIRM_WINDOW + max_h after
    eval_dates = all_dates[MIN_HISTORY: len(all_dates) - CONFIRM_WINDOW - max_h]
    logger.info("Eval dates: %d (%s → %s)", len(eval_dates), eval_dates[0], eval_dates[-1])

    results = []
    stats = {"signals": 0, "confirmed": 0, "expired": 0}

    for di, date in enumerate(eval_dates):
        if di % 10 == 0:
            logger.info("Day %d/%d (%s) — %d signals, %d confirmed, %d expired",
                        di, len(eval_dates), date,
                        stats["signals"], stats["confirmed"], stats["expired"])

        regime = regime_map.get(date, "")

        for code, df_raw in histories.items():
            idx_matches = df_raw.index[df_raw["date"] == date]
            if len(idx_matches) == 0:
                continue
            idx = idx_matches[0]
            if idx < MIN_HISTORY:
                continue
            if idx + CONFIRM_WINDOW + max_h >= len(df_raw):
                continue

            # Step 1: Compute VPA signal from data up to date
            df_slice = df_raw.iloc[:idx + 1]
            signal = compute_vpa_signal(df_slice, regime=regime)
            if not signal:
                continue
            stats["signals"] += 1

            # Step 2: Compute derived metrics for full df (need volume_ratio for confirmation)
            # We need _compute_derived on the full range including confirmation window
            df_extended = df_raw.iloc[:idx + CONFIRM_WINDOW + max_h + 1].copy()
            df_extended = _compute_derived(df_extended, window=WINDOW)

            # Step 3: Check Anna Coulling confirmation (per-pattern rules)
            confirmed_idx, confirm_offset, confirm_method = check_confirmation(
                df_extended, idx, signal["signal"], signal["primary_pattern"],
                max_wait=CONFIRM_WINDOW,
            )

            if confirmed_idx is None:
                stats["expired"] += 1
                continue

            stats["confirmed"] += 1

            # Step 4: Measure returns from CONFIRMATION day forward
            # Map confirmed_idx back to df_raw index
            entry_idx_raw = idx + confirm_offset
            entry_close = df_raw.iloc[entry_idx_raw]["close"]
            if not entry_close or entry_close <= 0:
                continue

            fwds = {}
            for h in HORIZONS:
                fwd_idx = entry_idx_raw + h
                if fwd_idx >= len(df_raw):
                    break
                exit_close = df_raw.iloc[fwd_idx]["close"]
                fwds[h] = (exit_close - entry_close) / entry_close * 100.0

            if not fwds:
                continue

            # Also compute "no confirmation" baseline: return from signal day
            signal_close = df_raw.iloc[idx]["close"]
            fwds_no_confirm = {}
            for h in HORIZONS:
                fwd_idx = idx + h
                if fwd_idx >= len(df_raw):
                    break
                exit_close = df_raw.iloc[fwd_idx]["close"]
                fwds_no_confirm[h] = (exit_close - signal_close) / signal_close * 100.0

            results.append({
                "signal_date": date,
                "confirm_date": df_raw.iloc[entry_idx_raw]["date"],
                "confirm_offset": confirm_offset,
                "confirm_method": confirm_method,
                "code": code,
                "regime": regime,
                "signal": signal["signal"],
                "net_score": signal["net_score"],
                "primary_pattern": signal["primary_pattern"],
                **{f"confirmed_t{h}": round(fwds.get(h, float("nan")), 3) for h in HORIZONS},
                **{f"raw_t{h}": round(fwds_no_confirm.get(h, float("nan")), 3) for h in HORIZONS},
            })

    logger.info("Done: %d signals, %d confirmed (%.1f%%), %d expired",
                stats["signals"], stats["confirmed"],
                stats["confirmed"] / stats["signals"] * 100 if stats["signals"] else 0,
                stats["expired"])

    # Save
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if results:
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
            w.writeheader()
            w.writerows(results)
        logger.info("Saved %d rows to %s", len(results), out)

    print_summary(results, stats)


def print_summary(results, stats):
    if not results:
        print("No results.")
        return

    total_signals = stats["signals"]
    total_confirmed = stats["confirmed"]
    total_expired = stats["expired"]
    confirm_rate = total_confirmed / total_signals * 100 if total_signals else 0

    print(f"\n{'=' * 120}")
    print(f"VPA Confirmed Signal Backtest")
    print(f"Signals: {total_signals:,}  Confirmed: {total_confirmed:,} ({confirm_rate:.1f}%)  Expired: {total_expired:,}")
    print(f"{'=' * 120}")

    df = pd.DataFrame(results)

    # ── Confirmed vs Raw (no-confirmation baseline) ──
    print(f"\n── CONFIRMED entry vs RAW entry (signal day) — T+1 through T+5 ──\n")
    print(f"  {'signal':10s} {'method':12s} {'N':>6s}   T+1        T+2        T+3        T+4        T+5")

    for signal in ("bullish", "bearish"):
        sub = df[df["signal"] == signal]
        if len(sub) < 20:
            continue

        for method, prefix in [("CONFIRMED", "confirmed_t"), ("RAW", "raw_t")]:
            cells = [f"  {signal:10s} {method:12s} {len(sub):>6d}"]
            for h in HORIZONS:
                col = f"{prefix}{h}"
                vals = sub[col].dropna()
                if len(vals) == 0:
                    cells.append("    N/A   ")
                    continue
                if signal == "bullish":
                    hit = (vals > 0).mean() * 100
                else:
                    hit = (vals < 0).mean() * 100
                mean = vals.mean()
                cells.append(f"  {hit:5.1f}%/{mean:+5.2f}%")
            print("".join(cells))
        print()

    # ── Confirmation rate by signal type ──
    print(f"── Confirmation rate ──\n")
    for signal in ("bullish", "bearish"):
        sig_count = len([r for r in results if r["signal"] == signal])
        # We only have confirmed samples in results, need total from stats
        # Actually we only saved confirmed ones. Let me compute from the ratio.
        print(f"  {signal}: {sig_count:,} confirmed samples")

    # ── Confirmation offset distribution ──
    print(f"\n── Confirmation day offset (1=next day, 2=two days, 3=three days) ──\n")
    offsets = df["confirm_offset"].value_counts().sort_index()
    for off, count in offsets.items():
        pct = count / len(df) * 100
        bar = "█" * int(pct / 2)
        print(f"  Day+{off}: {count:>6,} ({pct:5.1f}%) {bar}")

    # ── By regime ──
    print(f"\n── By regime (CONFIRMED T+1) ──\n")
    for regime in ("rising", "ranging", "falling"):
        for signal in ("bullish", "bearish"):
            sub = df[(df["regime"] == regime) & (df["signal"] == signal)]
            if len(sub) < 20:
                continue
            col = "confirmed_t1"
            vals = sub[col].dropna()
            if signal == "bullish":
                hit = (vals > 0).mean() * 100
            else:
                hit = (vals < 0).mean() * 100
            mean = vals.mean()
            print(f"  {regime:8s} × {signal:8s}  N={len(sub):5d}  T+1 hit={hit:.1f}%  mean={mean:+.2f}%")

    # ── By pattern ──
    print(f"\n── By pattern (CONFIRMED T+1, N≥50) ──\n")
    for pat, sub in df.groupby("primary_pattern"):
        if len(sub) < 50:
            continue
        for signal in sub["signal"].unique():
            ssub = sub[sub["signal"] == signal]
            if len(ssub) < 30:
                continue
            col = "confirmed_t1"
            vals = ssub[col].dropna()
            if signal == "bullish":
                hit = (vals > 0).mean() * 100
            else:
                hit = (vals < 0).mean() * 100
            mean = vals.mean()
            print(f"  {pat:25s} {signal:8s}  N={len(ssub):5d}  confirmed T+1 hit={hit:.1f}%  mean={mean:+.2f}%")

    print(f"\n{'=' * 120}\n")


if __name__ == "__main__":
    main()
