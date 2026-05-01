"""Generate one K-line chart per stock with VPA cfm=True signals annotated.

Output:
    data/charts/<code>.png  — one chart per code in --pool

Usage:
    python scripts/plot_vpa_charts.py CACHE.jsonl POOL_CSV \
        [--start-date 2025-10-17] [--end-date 2026-01-12] [--out data/charts]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties
from matplotlib.patches import Rectangle
import pandas as pd
import mplfinance as mpf

# Locate a CJK-capable font and build a FontProperties for explicit use on
# every text element — mpf.plot's "charles" style otherwise overrides
# rcParams font selections, so passing fontproperties directly is reliable.
_available = {f.name for f in matplotlib.font_manager.fontManager.ttflist}
_CJK_FONT_NAME = None
for cjk in ["PingFang HK", "PingFang SC", "Heiti TC", "Heiti SC", "STHeiti",
            "Hiragino Sans GB", "Songti SC", "Apple SD Gothic Neo",
            "Arial Unicode MS"]:
    if cjk in _available:
        _CJK_FONT_NAME = cjk
        break
if _CJK_FONT_NAME:
    plt.rcParams["font.sans-serif"] = [_CJK_FONT_NAME] + plt.rcParams.get("font.sans-serif", [])
    plt.rcParams["font.family"] = "sans-serif"
plt.rcParams["axes.unicode_minus"] = False
CJK_FP = FontProperties(family=_CJK_FONT_NAME) if _CJK_FONT_NAME else None

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

BULLISH = ("吸筹", "吸筹初期", "吸筹尾声", "吸筹末期", "拉升", "拉升初期",
           "买入高峰", "卖压衰竭")
BEARISH = ("派发", "派发初期", "派发中期", "派发尾声", "抛售高峰",
           "下跌", "下跌初期", "下跌尾声", "下跌末期")


def load_cache(path: Path) -> dict:
    cache = {}
    extract_verdict = None
    try:
        from alpha_agents.tools.vpa import _extract_verdict as extract_verdict  # type: ignore
    except Exception:
        extract_verdict = None
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            res = r["result"]
            need_reparse = any(
                k not in res for k in (
                    "llm_action_confirmed",
                    "llm_confirmation_level",
                    "llm_confirmation_tier",
                    "llm_confirmed_all_signals",
                    "llm_confirmed_any_signal",
                    "llm_decisive_confirmed_signal_count",
                    "llm_structural_phase_change_confirmed",
                    "llm_partial_confirmed",
                )
            )
            if need_reparse:
                report = res.get("llm_report", "")
                if report and extract_verdict is not None:
                    v = extract_verdict(report)
                    res["llm_action_confirmed"] = bool(
                        v.get("action_confirmed", v.get("confirmed", False))
                    )
                    res["llm_partial_confirmed"] = bool(v.get("partial_confirmed", False))
                    res["llm_confirmation_level"] = int(v.get("confirmation_level", 0) or 0)
                    res["llm_confirmation_tier"] = str(v.get("confirmation_tier", "none") or "none")
                    res["llm_confirmed_all_signals"] = bool(v.get("confirmed_all_signals", False))
                    res["llm_confirmed_any_signal"] = bool(v.get("confirmed_any_signal", False))
                    res["llm_decisive_confirmed_signal_count"] = int(
                        v.get("decisive_confirmed_signal_count", 0) or 0
                    )
                    res["llm_structural_phase_change_confirmed"] = bool(
                        v.get("structural_phase_change_confirmed", False)
                    )
                else:
                    res["llm_action_confirmed"] = bool(res.get("llm_confirmed", False))
                    res["llm_partial_confirmed"] = bool(res["llm_action_confirmed"])
                    res["llm_confirmation_level"] = 2 if res["llm_action_confirmed"] else 0
                    res["llm_confirmation_tier"] = "partial" if res["llm_action_confirmed"] else "none"
                    res["llm_decisive_confirmed_signal_count"] = 0
                    res["llm_structural_phase_change_confirmed"] = False
            cache[(r["date"], r["code"])] = res
    return cache


def load_codes(pool_csv: Path) -> list[str]:
    codes = []
    with open(pool_csv, encoding="utf-8") as f:
        for line in f:
            for cell in line.strip().split(","):
                cell = cell.strip()
                if cell.isdigit() and len(cell) == 6:
                    codes.append(cell)
                    break
    return codes


# A-share color convention: red = up, green = down (opposite of Western default).
A_SHARE_MC = mpf.make_marketcolors(
    up="red", down="green",
    edge="inherit", wick="inherit", volume="inherit",
)
A_SHARE_STYLE = mpf.make_mpf_style(
    base_mpf_style="charles", marketcolors=A_SHARE_MC,
    rc={"axes.unicode_minus": False},
)


def plot_stock(code: str, ohlc_rows: list, cache: dict, out_path: Path):
    if not ohlc_rows:
        return False
    df = pd.DataFrame(ohlc_rows, columns=["Date", "Open", "High", "Low", "Close", "Volume"])
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.set_index("Date")

    raw_ret = (df["Close"].iloc[-1] - df["Open"].iloc[0]) / df["Open"].iloc[0] * 100
    title = (f"{code}  raw_ret={raw_ret:+.1f}%  "
             f"({df.index[0].strftime('%Y-%m-%d')} → {df.index[-1].strftime('%Y-%m-%d')})  "
             f"[★=C3, ▲/▼=C2, label=C1/C0]")

    # C3: strong confirmed, C2: partial confirmed, C1/C0: pending/none.
    bull_l3_dates, bull_l3_prices, bull_l3_labels = [], [], []
    bull_l2_dates, bull_l2_prices, bull_l2_labels = [], [], []
    bull_low_dates, bull_low_prices, bull_low_labels = [], [], []
    bear_l3_dates, bear_l3_prices, bear_l3_labels = [], [], []
    bear_l2_dates, bear_l2_prices, bear_l2_labels = [], [], []
    bear_low_dates, bear_low_prices, bear_low_labels = [], [], []
    for d, row in df.iterrows():
        ds = d.strftime("%Y-%m-%d")
        v = cache.get((ds, code), {})
        if not v.get("ok"):
            continue
        ph = v.get("llm_phase", "") or ""
        level = int(v.get("llm_confirmation_level", 0) or 0)
        label = f"C{level} {ph}"
        if ph in BULLISH:
            if level >= 3:
                bull_l3_dates.append(d)
                bull_l3_prices.append(row["High"] * 1.035)
                bull_l3_labels.append(f"★ {label}")
            elif level == 2:
                bull_l2_dates.append(d)
                bull_l2_prices.append(row["High"] * 1.025)
                bull_l2_labels.append(f"▲ {label}")
            else:
                bull_low_dates.append(d)
                bull_low_prices.append(row["High"] * 1.015)
                bull_low_labels.append(label)
        elif ph in BEARISH:
            if level >= 3:
                bear_l3_dates.append(d)
                bear_l3_prices.append(row["High"] * 1.055)
                bear_l3_labels.append(f"★ {label}")
            elif level == 2:
                bear_l2_dates.append(d)
                bear_l2_prices.append(row["High"] * 1.040)
                bear_l2_labels.append(f"▼ {label}")
            else:
                bear_low_dates.append(d)
                bear_low_prices.append(row["High"] * 1.025)
                bear_low_labels.append(label)

    # C2 triangles are drawn by mplfinance. C3 stars are overlaid manually after
    # plotting so they stay above candles/labels and remain visible.
    addplots = []
    if bull_l2_prices:
        lookup = dict(zip(bull_l2_dates, bull_l2_prices))
        addplots.append(mpf.make_addplot(
            pd.Series([lookup.get(d, float("nan")) for d in df.index], index=df.index),
            type="scatter", marker="^", markersize=95, color="#2f9c2f", panel=0,
        ))
    if bear_l2_prices:
        lookup = dict(zip(bear_l2_dates, bear_l2_prices))
        addplots.append(mpf.make_addplot(
            pd.Series([lookup.get(d, float("nan")) for d in df.index], index=df.index),
            type="scatter", marker="v", markersize=95, color="#d44a4a", panel=0,
        ))

    plot_kwargs = dict(
        type="candle",
        style=A_SHARE_STYLE,
        figsize=(18, 9),
        title=title,
        ylabel="Price (RMB)",
        volume=True,
        ylabel_lower="Volume",
        returnfig=True,
        panel_ratios=(3, 1),
        warn_too_much_data=10000,
    )
    if addplots:
        plot_kwargs["addplot"] = addplots

    fig, axes = mpf.plot(df, **plot_kwargs)
    ax = axes[0]

    def _draw_star(dates, prices, color):
        if not dates:
            return
        xs = [df.index.get_loc(d) for d in dates]
        ax.scatter(
            xs, prices, marker="*", s=560, c=color, edgecolors="black",
            linewidths=0.9, zorder=12, alpha=0.98,
        )

    _draw_star(bull_l3_dates, bull_l3_prices, "#00a32a")
    _draw_star(bear_l3_dates, bear_l3_prices, "#e00000")

    # C3: bold + boxed
    for d, y, label in zip(bull_l3_dates, bull_l3_prices, bull_l3_labels):
        x = df.index.get_loc(d)
        label = f"{d.strftime('%m-%d')} {label}"
        ax.annotate(label, xy=(x, y), xytext=(x, y * 1.035),
                    fontsize=9, color="#0b5d0b", ha="center", va="bottom",
                    rotation=90, fontproperties=CJK_FP, fontweight="bold",
                    bbox=dict(boxstyle="round,pad=0.18", fc="#e8f5e8",
                              ec="#1a8a1a", lw=0.6, alpha=0.85))
    for d, y, label in zip(bear_l3_dates, bear_l3_prices, bear_l3_labels):
        x = df.index.get_loc(d)
        label = f"{d.strftime('%m-%d')} {label}"
        ax.annotate(label, xy=(x, y), xytext=(x, y * 1.035),
                    fontsize=9, color="#7a1010", ha="center", va="bottom",
                    rotation=90, fontproperties=CJK_FP, fontweight="bold",
                    bbox=dict(boxstyle="round,pad=0.18", fc="#fce8e8",
                              ec="#c81e1e", lw=0.6, alpha=0.85))

    # C2: medium weight + semi-boxed
    for d, y, label in zip(bull_l2_dates, bull_l2_prices, bull_l2_labels):
        x = df.index.get_loc(d)
        ax.annotate(label, xy=(x, y), xytext=(x, y * 1.025),
                    fontsize=8, color="#146f14", ha="center", va="bottom",
                    rotation=90, fontproperties=CJK_FP, fontweight="bold",
                    bbox=dict(boxstyle="round,pad=0.14", fc="#f0faef",
                              ec="#2f9c2f", lw=0.5, alpha=0.65))
    for d, y, label in zip(bear_l2_dates, bear_l2_prices, bear_l2_labels):
        x = df.index.get_loc(d)
        ax.annotate(label, xy=(x, y), xytext=(x, y * 1.025),
                    fontsize=8, color="#9a2222", ha="center", va="bottom",
                    rotation=90, fontproperties=CJK_FP, fontweight="bold",
                    bbox=dict(boxstyle="round,pad=0.14", fc="#fff2f2",
                              ec="#d44a4a", lw=0.5, alpha=0.65))

    # C1/C0: lighter, no marker
    for d, y, label in zip(bull_low_dates, bull_low_prices, bull_low_labels):
        x = df.index.get_loc(d)
        ax.annotate(label, xy=(x, y), xytext=(x, y * 1.015),
                    fontsize=7, color="#1a8a1a", ha="center", va="bottom",
                    rotation=90, fontproperties=CJK_FP, alpha=0.55)
    for d, y, label in zip(bear_low_dates, bear_low_prices, bear_low_labels):
        x = df.index.get_loc(d)
        ax.annotate(label, xy=(x, y), xytext=(x, y * 1.015),
                    fontsize=7, color="#c81e1e", ha="center", va="bottom",
                    rotation=90, fontproperties=CJK_FP, alpha=0.55)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=110, bbox_inches="tight")
    plt.close(fig)
    return True


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("cache", type=Path)
    p.add_argument("pool_csv", type=Path)
    p.add_argument("--db", type=Path, default=REPO / "data/market_history.db")
    p.add_argument("--start-date", type=str, default="2025-10-17")
    p.add_argument("--end-date", type=str, default="2026-01-12")
    p.add_argument("--out", type=Path, default=REPO / "data/charts")
    args = p.parse_args()

    cache = load_cache(args.cache)
    codes = load_codes(args.pool_csv)
    conn = sqlite3.connect(str(args.db))

    print(f"window: {args.start_date} → {args.end_date}")
    print(f"codes: {len(codes)}  output: {args.out}/")
    print()

    for code in codes:
        rows = conn.execute(
            "SELECT date,open,high,low,close,volume FROM daily_kline "
            "WHERE code=? AND date BETWEEN ? AND ? ORDER BY date",
            (code, args.start_date, args.end_date),
        ).fetchall()
        ohlc = [(d, float(o), float(h), float(l), float(c), int(vol or 0))
                for d, o, h, l, c, vol in rows if o and c]
        if not ohlc:
            print(f"  {code}: no OHLC, skipped")
            continue
        out_path = args.out / f"{code}.png"
        level_counts = {0: 0, 1: 0, 2: 0, 3: 0}
        for row in ohlc:
            level = int(cache.get((row[0], code), {}).get("llm_confirmation_level", 0) or 0)
            if level >= 3:
                level = 3
            elif level <= 0:
                level = 0
            level_counts[level] += 1
        ok = plot_stock(code, ohlc, cache, out_path)
        if ok:
            print(
                f"  {code}: {len(ohlc)} bars, "
                f"C3={level_counts[3]} C2={level_counts[2]} "
                f"C1={level_counts[1]} C0={level_counts[0]} → {out_path.name}"
            )


if __name__ == "__main__":
    main()
