"""Anna Coulling 选股扫描器（多周期版本）

Anna 第一原则："VPA is not for every stock. It works on stocks where smart
money is operating with conviction." 选股不是分析每只股票，是先筛掉**没故事**
的股票。

筛选层次（**纯代码筛**，不调用 LLM——LLM 留给 Step 2 深度分析）：

  Layer 1: 流动性
    日均成交额 ≥ 1 亿（A 股流动性下限，VPA 在低流动性股不可靠）

  Layer 2: 多周期 confluence（Anna 第一原则）
    周线必须有方向 OR 月线必须有方向
    震荡股（周线 ranging + 月线 ranging）→ 直接淘汰

  Layer 3: 当下量价异常（Anna 第二原则）
    近 5 日内 candidate scanner 触发
    （即股票自身近 60 日的 vol/abspct/spread 任一项 ≥ 0.85 分位）

  Layer 4: 位置过滤（risk control）
    避开月线极端高位 + 月线极端低位（待确认反转才入）

  Layer 5: 评分排序
    优先 Anna 经典进场点：
    - 周线 markup 起步 + 日线 SOS 候选 = 高分
    - 周线 distribution 末段 + 日线 SC 候选 = 高分（做多反转）
    - 周线 markdown 末段 + 日线 Spring 候选 = 高分（做多反转）

输出：top N 候选 + 详细评分 + 推荐进场逻辑

用法：
    python scripts/anna_screener.py [--as-of YYYY-MM-DD] [--top N] [--min-amount 1.0]
        [--include-board 300,301,688] [--out data/anna_picks_YYYYMMDD.csv]
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import pandas as pd

from alpha_agents.tools.vpa.data import (
    _compute_derived,
    _load_ohlcv,
    _load_ohlcv_monthly,
    _load_ohlcv_weekly,
    _summarize_higher_timeframe,
)
from alpha_agents.tools.vpa.scanner import _scan_climax_candidates


# ── Anna phase 分类（基于多周期 summary，纯代码） ────────────

def _classify_higher_tf(summary: dict, ranging_threshold: float = 8.0) -> str:
    """Classify higher-TF phase: markup / markdown / ranging / unclear.

    No LLM call — purely structural.
    - |trend| < ranging_threshold% AND consistency ∈ [-0.2, +0.2] → ranging
    - trend > +ranging AND consistency > +0.1 → markup
    - trend < -ranging AND consistency < -0.1 → markdown
    - 否则 → unclear（混合特征，慎判断）
    """
    if not summary:
        return "unknown"
    trend = summary.get("trend_pct", 0)
    consist = summary.get("direction_consistency", 0)

    if abs(trend) < ranging_threshold and abs(consist) < 0.20:
        return "ranging"
    if trend > ranging_threshold and consist > 0.10:
        return "markup"
    if trend < -ranging_threshold and consist < -0.10:
        return "markdown"
    return "unclear"


def _is_strict_SC(candidate: dict, prior_markdown_weeks: int) -> bool:
    """Anna 严格 SC 判定：4 条硬性条件 + 之前 markdown ≥ 12 周"""
    if prior_markdown_weeks < 12:
        return False
    vol_pct = candidate.get("vol_ratio_pct", 0)
    if vol_pct < 0.95:  # 巨量
        return False
    range_x = candidate.get("range_vs_5d_avg", 0)
    if range_x < 1.5:  # 宽幅
        return False
    lower_shadow = candidate.get("lower_shadow", 0)
    if lower_shadow < 0.4:  # 长下影（panic 后买盘进场）
        return False
    cp = candidate.get("close_position", 0.5)
    if cp < 0.4:  # 收盘必须在中位以上（卖盘 absorbed）
        return False
    return True


def _is_strict_BC(candidate: dict, prior_markup_weeks: int) -> bool:
    """Anna 严格 BC 判定：4 条硬性条件 + 之前 markup ≥ 12 周"""
    if prior_markup_weeks < 12:
        return False
    vol_pct = candidate.get("vol_ratio_pct", 0)
    if vol_pct < 0.95:
        return False
    range_x = candidate.get("range_vs_5d_avg", 0)
    if range_x < 1.5:
        return False
    upper_shadow = candidate.get("upper_shadow", 0)
    if upper_shadow < 0.4:
        return False
    cp = candidate.get("close_position", 0.5)
    if cp > 0.6:  # 收盘必须中位或下半区（买盘 exhausted）
        return False
    return True


def _count_higher_tf_phase_duration(df_higher: "pd.DataFrame", current_phase: str, ranging_threshold: float) -> int:
    """从尾向前回算: 当前 phase 已持续多少周/月。"""
    if df_higher is None or len(df_higher) < 4:
        return 0
    closes = df_higher["close"].values
    weeks = 0
    # 从倒数第 4 个起，每往前 4 期算一个滚动 trend
    for i in range(len(closes) - 1, 3, -1):
        sub = closes[max(0, i - 4):i + 1]
        if len(sub) < 4:
            break
        sub_trend = (sub[-1] - sub[0]) / sub[0] * 100 if sub[0] > 0 else 0
        sub_consist = sum(1 for j in range(1, len(sub)) if sub[j] > sub[j - 1]) - sum(1 for j in range(1, len(sub)) if sub[j] < sub[j - 1])
        sub_consist_norm = sub_consist / max(1, len(sub) - 1)
        if current_phase == "markup":
            if sub_trend > ranging_threshold and sub_consist_norm > 0.10:
                weeks += 1
            else:
                break
        elif current_phase == "markdown":
            if sub_trend < -ranging_threshold and sub_consist_norm < -0.10:
                weeks += 1
            else:
                break
        elif current_phase == "ranging":
            if abs(sub_trend) < ranging_threshold and abs(sub_consist_norm) < 0.20:
                weeks += 1
            else:
                break
    return weeks


def _interpret_signal(
    weekly_phase: str,
    monthly_phase: str,
    candidate: dict,
    pos_in_range: float,
    prior_markdown_weeks: int = 0,
    prior_markup_weeks: int = 0,
    prior_base_weeks: int = 0,
    relative_strength: float = 0.0,
) -> tuple[str, float]:
    """Anna 经典进场点识别 → 返回 (signal_type, score)

    v3 升级（基于 realized P&L 实证修正 v2）：
    - 不再因 pos>0.85 一律扣分。改为基于 K 线字符判断:
        SOS 延续 (放量阳 + 收上半区 + 短上影 + 宽幅) → 加分
        No Demand 顶 (缩量 + 窄幅) → 重扣
        其他高位 → 中性或轻扣
    - 双 markup + 高位 = 强趋势加分,单 markup + 高位 = 中性轻扣
    - 因果定律 / SC/BC 严格 / 相对强度 (保留 v2)

    实证依据 (181 hy3 confirmed bullish 上的 realized P&L):
        v2 score < -30 子集 avg = +3.42% (反向最强)
        v2 score >= 30 子集 avg = +1.37%
        → v2 评分严重反向, v3 修正
    """
    cand_type = candidate.get("bar_type", "")
    cand_close_pos = candidate.get("close_position", 0.5)
    cand_max_pct = max(
        candidate.get("vol_ratio_pct", 0),
        candidate.get("abspct_pct", 0),
        candidate.get("spread_pct", 0),
    )

    # 因果定律加分: base 越深越好
    base_bonus = 0
    if prior_base_weeks >= 12:
        base_bonus = 20
    elif prior_base_weeks >= 4:
        base_bonus = 10
    elif prior_base_weeks > 0:
        base_bonus = 0
    else:
        base_bonus = -10  # 没 base 就涨 = 弱

    # 相对强度: > 0 加分, < 0 减分
    rs_bonus = max(-15, min(15, relative_strength * 0.3))

    # ===== 顶部风险类（v3: 仅严格 BC 是硬扣分，其他看 K 线字符） =====

    # 场景 R1: 严格 BC 信号（顶部派发，不进场）— 唯一硬扣分场景
    if _is_strict_BC(candidate, prior_markup_weeks):
        return ("strict_BC_top_warning", -80 + rs_bonus)

    # ===== v3 新增: markup 高位的字符化判断 =====
    upper_shadow = candidate.get("upper_shadow", 0)
    range_x = candidate.get("range_vs_5d_avg", 1.0)

    # 场景 R2 (v3): SOS 延续 (放量阳 + 收上半区 + 短上影 + 宽幅) → 趋势确认加分
    if (weekly_phase == "markup" or monthly_phase == "markup") and pos_in_range > 0.85 \
       and cand_type == "阳线" and cand_close_pos > 0.6 and upper_shadow < 0.3 \
       and cand_max_pct >= 60 and range_x >= 1.2:
        if weekly_phase == "markup" and monthly_phase == "markup":
            return ("markup_SOS_continuation_dual", 50 + cand_max_pct * 0.3 + base_bonus + rs_bonus)
        else:
            return ("markup_SOS_continuation_single", 25 + cand_max_pct * 0.2 + base_bonus + rs_bonus)

    # 场景 R3 (v3): No Demand 顶 (缩量 + 窄幅) → 派发开始重扣
    if (weekly_phase == "markup" or monthly_phase == "markup") and pos_in_range > 0.85 \
       and cand_max_pct < 30 and range_x < 0.8:
        return ("markup_no_demand_top", -50 + rs_bonus)

    # 场景 R4 (v3): 其他 markup 高位 — 趋势内中性
    if weekly_phase == "markup" and monthly_phase == "markup" and pos_in_range > 0.85:
        return ("markup_dual_high_neutral", 5 + base_bonus + rs_bonus)
    if (weekly_phase == "markup" or monthly_phase == "markup") and pos_in_range > 0.85:
        return ("markup_single_high_caution", -10 + rs_bonus)

    # ===== 真进场点类 =====

    # 场景 G1: 吸筹尾声 + 严格 SC 后的反转 (Anna 教科书底部进场, 最佳)
    if _is_strict_SC(candidate, prior_markdown_weeks):
        return ("strict_SC_reversal_buy", 90 + base_bonus + rs_bonus)

    # 场景 G2: 周线 markup 健康回踩 (markup + pos < 0.40) + 日线 SOS
    if weekly_phase == "markup" and pos_in_range < 0.40 and cand_type == "阳线" and cand_close_pos > 0.6:
        return ("markup_pullback_SOS", 80 + cand_max_pct * 10 + base_bonus + rs_bonus)

    # 场景 G3: 周线 ranging + 月线 markup + 低位 = 突破前买点
    if weekly_phase == "ranging" and monthly_phase == "markup" and cand_type == "阳线" and pos_in_range < 0.40:
        return ("ranging_breakout_imminent", 70 + cand_max_pct * 10 + base_bonus + rs_bonus)

    # 场景 G4: 月线 markdown 末段 + 日线 SC-like 阴线 + 长下影 = 反转候选 (弱版本)
    if monthly_phase == "markdown" and cand_type == "阴线" and cand_close_pos < 0.30 and \
       candidate.get("lower_shadow", 0) >= 0.3 and candidate.get("post_bar_reverse_3d", 0) > 0.03:
        return ("markdown_reversal_candidate", 50 + cand_max_pct * 10 + rs_bonus)

    # ===== 中性/不明朗类 =====

    # 场景 N1: 周线 markup 中段 (pos 0.40-0.70) — 还能进
    if weekly_phase == "markup" and pos_in_range < 0.70 and cand_type == "阳线":
        return ("markup_midrange", 40 + cand_max_pct * 10 + base_bonus + rs_bonus)

    # 场景 N2: 周线 markup 中后段 (pos 0.70-0.85) — 谨慎
    if weekly_phase == "markup" and pos_in_range <= 0.85:
        return ("markup_late_caution", 10 + rs_bonus)

    # 场景 N3: 双 ranging
    if weekly_phase == "ranging" and monthly_phase == "ranging":
        return ("dual_ranging_avoid", -30)

    # 默认
    return ("unclear", cand_max_pct * 20 + rs_bonus)


# ── 单股筛选 ──────────────────────────────────────────

def screen_one(
    code: str,
    as_of: str,
    min_amount: float = 1.0,
    market_baseline_60d: float = 0.0,
) -> Optional[dict]:
    """Run all layers on one stock. Return picked dict or None.

    market_baseline_60d: 市场中位数 60d 回报 (%)，用于相对强度计算。
    """
    df = _load_ohlcv(code, days=120, as_of=as_of)
    if df is None or len(df) < 60:
        return None
    df = _compute_derived(df, window=20)

    today = df.iloc[-1]
    last_5d = df.tail(5)
    last_60d = df.tail(60)

    # Layer 1: 流动性
    avg_amount_yi = (last_5d["close"] * last_5d["volume"]).mean() / 1e8
    if avg_amount_yi < min_amount:
        return None

    # Layer 2: 多周期 confluence
    df_w = _load_ohlcv_weekly(code, weeks=26, as_of=as_of)
    df_m = _load_ohlcv_monthly(code, months=6, as_of=as_of)
    if df_w is None and df_m is None:
        return None
    weekly = _summarize_higher_timeframe(df_w, periods=12, label="周") if df_w is not None else {}
    monthly = _summarize_higher_timeframe(df_m, periods=6, label="月") if df_m is not None else {}
    weekly_phase = _classify_higher_tf(weekly, ranging_threshold=8.0)
    monthly_phase = _classify_higher_tf(monthly, ranging_threshold=15.0)

    # 双 ranging → 直接淘汰
    if weekly_phase == "ranging" and monthly_phase == "ranging":
        return None

    # Layer 3: 量价异常
    candidates = _scan_climax_candidates(df)
    if not candidates:
        return None
    recent_dates = set(last_5d["date"].astype(str))
    recent_cands = [c for c in candidates if c.get("date") in recent_dates]
    if not recent_cands:
        return None

    # Layer 4: 位置筛
    high_60 = last_60d["high"].max()
    low_60 = last_60d["low"].min()
    if high_60 == low_60:
        return None
    pos_in_range = (today["close"] - low_60) / (high_60 - low_60)

    # Anna review Fix B: 因果定律——base 深度
    # 当前 phase 之前的 base 持续多少周
    prior_markup_weeks = _count_higher_tf_phase_duration(df_w, "markup", 8.0)
    prior_markdown_weeks = _count_higher_tf_phase_duration(df_w, "markdown", 8.0)
    prior_ranging_weeks = _count_higher_tf_phase_duration(df_w, "ranging", 8.0)
    # base = 当前 phase 之前的整理期长度（粗略）
    if weekly_phase == "markup":
        prior_base_weeks = prior_ranging_weeks  # markup 之前是整理才有蓄势
    elif weekly_phase == "markdown":
        prior_base_weeks = prior_markup_weeks  # markdown 之前的 markup 是 cause
    else:
        prior_base_weeks = 0

    # Anna review Fix D: 相对强度
    own_60d_return = (today["close"] - df.iloc[-60]["close"]) / df.iloc[-60]["close"] * 100 if len(df) >= 60 else 0
    relative_strength = own_60d_return - market_baseline_60d

    # Layer 5: 评分
    best_cand = max(recent_cands, key=lambda c: max(
        c.get("vol_ratio_pct", 0), c.get("abspct_pct", 0), c.get("spread_pct", 0)
    ))
    signal_type, score = _interpret_signal(
        weekly_phase, monthly_phase, best_cand, pos_in_range,
        prior_markdown_weeks=prior_markdown_weeks,
        prior_markup_weeks=prior_markup_weeks,
        prior_base_weeks=prior_base_weeks,
        relative_strength=relative_strength,
    )

    return {
        "code": code,
        "close": float(today["close"]),
        "amount_yi": round(avg_amount_yi, 1),
        "pos_60d": round(pos_in_range, 2),
        "own_60d_return": round(own_60d_return, 1),
        "relative_strength": round(relative_strength, 1),
        "weekly_phase": weekly_phase,
        "weekly_trend": weekly.get("trend_pct", 0),
        "weekly_consist": weekly.get("direction_consistency", 0),
        "monthly_phase": monthly_phase,
        "monthly_trend": monthly.get("trend_pct", 0),
        "monthly_pos": monthly.get("position_in_range", 0.5),
        "prior_markup_weeks": prior_markup_weeks,
        "prior_markdown_weeks": prior_markdown_weeks,
        "prior_base_weeks": prior_base_weeks,
        "candidate_date": best_cand.get("date", ""),
        "candidate_bar_type": best_cand.get("bar_type", ""),
        "candidate_max_pct": round(max(
            best_cand.get("vol_ratio_pct", 0),
            best_cand.get("abspct_pct", 0),
            best_cand.get("spread_pct", 0),
        ), 2),
        "candidate_close_position": round(best_cand.get("close_position", 0.5), 2),
        "candidate_lower_shadow": round(best_cand.get("lower_shadow", 0), 2),
        "candidate_upper_shadow": round(best_cand.get("upper_shadow", 0), 2),
        "candidate_range_x": round(best_cand.get("range_vs_5d_avg", 0), 2),
        "signal_type": signal_type,
        "score": round(score, 1),
    }


# ── 全市场扫描 ────────────────────────────────────────

def _compute_market_baseline_60d(codes: list[str], as_of: str) -> float:
    """先快速扫一遍算市场 60d 中位数收益（基准）。"""
    db_path = REPO / "data" / "market_history.db"
    conn = sqlite3.connect(str(db_path))
    rets = []
    for code in codes:
        rs = conn.execute(
            "SELECT close FROM daily_kline WHERE code=? AND date<=? ORDER BY date DESC LIMIT 60",
            (code, as_of[:10])
        ).fetchall()
        if len(rs) >= 60:
            r = (rs[0][0] - rs[-1][0]) / rs[-1][0] * 100 if rs[-1][0] > 0 else 0
            rets.append(r)
    conn.close()
    if not rets:
        return 0.0
    rets.sort()
    return rets[len(rets) // 2]  # 中位数


def scan_market(
    boards: list[str],
    as_of: str,
    min_amount: float = 1.0,
) -> list[dict]:
    db_path = REPO / "data" / "market_history.db"
    conn = sqlite3.connect(str(db_path))
    where = " OR ".join([f"code LIKE '{b}%'" for b in boards])
    codes = [r[0] for r in conn.execute(
        f"SELECT DISTINCT code FROM daily_kline WHERE {where}"
    ).fetchall()]
    conn.close()
    print(f"扫描 {len(codes)} 只股票（板块 {','.join(boards)}）...", flush=True)

    # Anna review Fix D: 先算市场 60d 基准
    print("第一遍：算市场 60d 中位数基准...", flush=True)
    t_base = time.time()
    market_baseline = _compute_market_baseline_60d(codes, as_of)
    print(f"  市场 60d 中位数收益: {market_baseline:+.1f}% ({time.time()-t_base:.0f}s)", flush=True)

    print("第二遍：评分筛选...", flush=True)
    results = []
    t0 = time.time()
    for i, code in enumerate(codes, 1):
        if i % 200 == 0:
            elapsed = time.time() - t0
            eta = elapsed / i * (len(codes) - i)
            print(f"  {i}/{len(codes)} | 通过 {len(results)} | {elapsed:.0f}s ETA {eta:.0f}s", flush=True)
        try:
            r = screen_one(code, as_of=as_of, min_amount=min_amount, market_baseline_60d=market_baseline)
            if r:
                results.append(r)
        except Exception:
            continue

    return results


# ── 主入口 ────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--as-of", default="2026-04-30", help="截至日期 YYYY-MM-DD")
    p.add_argument("--top", type=int, default=30, help="输出前 N 只")
    p.add_argument("--min-amount", type=float, default=1.0, help="最低日均成交额（亿元）")
    p.add_argument("--boards", default="300,301", help="板块前缀，逗号分隔（300=创业板, 301=创业板增量, 688=科创, 60=主板, 00=深主板）")
    p.add_argument("--out", default=None, help="输出 CSV 路径（默认 data/anna_picks_YYYYMMDD.csv）")
    args = p.parse_args()

    boards = [b.strip() for b in args.boards.split(",") if b.strip()]
    results = scan_market(boards=boards, as_of=args.as_of, min_amount=args.min_amount)

    # 排序：score 降序
    results.sort(key=lambda r: -r["score"])

    # 输出 CSV
    out_path = Path(args.out) if args.out else REPO / "data" / f"anna_picks_{args.as_of.replace('-', '')}.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="") as f:
        if results:
            writer = csv.DictWriter(f, fieldnames=list(results[0].keys()))
            writer.writeheader()
            writer.writerows(results)

    # 打印 top N
    print(f"\n===== Anna 选股结果 ({args.as_of}) — 通过筛 {len(results)} 只 =====\n")
    if not results:
        print("（无候选）")
        return
    print(f"{'rank':<4} {'code':<8} {'close':>8} {'亿':>5} {'weekly':<10} {'monthly':<10} {'pos':>5} {'cand':<11} {'score':>6}  signal")
    print("-" * 120)
    for i, r in enumerate(results[:args.top], 1):
        print(
            f"{i:<4} {r['code']:<8} {r['close']:>8.2f} {r['amount_yi']:>5.1f} "
            f"{r['weekly_phase']:<10} {r['monthly_phase']:<10} "
            f"{r['pos_60d']:>5.2f} {r['candidate_date']:<11} {r['score']:>6.1f}  {r['signal_type']}"
        )

    # 信号类型分布统计
    print(f"\n===== 信号分布 =====")
    from collections import Counter
    by_signal = Counter(r["signal_type"] for r in results)
    for sig, n in by_signal.most_common():
        print(f"  {sig:<30} {n:>4}")

    print(f"\n保存到: {out_path}")


if __name__ == "__main__":
    main()
