"""按 Anna 严格 VPA 标准, 评判 v9.1 4 个 case 的 warning 当日到底是不是真的派发/BC/SC.

用法: 不靠"后来跌没跌"的先知视角, 只看当日 K 线 + 前 N 日的 vol/range/wick/close-pos 这些
直接可见的 VPA 特征, 套 Anna Coulling 严格 4 条件:

  Strict BC (Buying Climax 买入高潮):
    1. volume in top 5% of last 30 bars (panic vol)
    2. range >= 1.5 × avg30 (super wide spread)
    3. upper_wick_ratio >= 0.4 (sellers absorbing demand)
    4. close in lower 40% of bar (weakness)
    All 4 must hold simultaneously.

  Strict SC候选 (Distribution post-BC):
    - vol >= p80, prior 5 bars include a strict-BC day, today is down/doji
    - OR: wide range down bar w/ vol > avg

每个 case 我们打印:
  - 前 15 个 bar 的 OHLCV + 计算特征
  - 当日是否过 strict BC 4 条
  - 是否符合 Anna 派发模式 (含 prior-bar 上下文)
  - 与 LLM warning 的吻合度
"""
from __future__ import annotations
import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd

DB = '/Users/evilkylin/Projects/AlphaAgents/data/market_history.db'
CACHE = '/tmp/anna_v91_v3_cache.jsonl'

CASES = [
    # type, code, target_date, descr, warning_chain (warning days from cache)
    ('A-真跌', '300318', '2025-11-18', 'entry day, 4d 后 -17.6%'),
    ('A-真跌', '002135', '2026-03-19', 'peak day, 4d 后 -18%'),
    ('B-错警', '000547', '2025-11-25', 'entry day, 后续 +185%'),
    ('B-错警', '603358', '2025-11-07', 'peak day, 短跌再涨 (FRI before 2025-11-08)'),
]

# Helper: load the LLM warning content for a given (code, date)
def load_llm_signals(code: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    with open(CACHE) as f:
        for line in f:
            obj = json.loads(line)
            if obj['code'] != code:
                continue
            res = obj.get('result', {})
            if not res.get('ok', True):
                continue
            out[obj['date']] = {
                'verdict': res.get('llm_verdict'),
                'level': res.get('llm_confirmation_level'),
                'phase': res.get('llm_phase'),
                'warning': res.get('llm_warning_phase'),
                'reason': (res.get('llm_reason') or '')[:200],
            }
    return out


def safe_div(a: float, b: float, default: float = 0.0) -> float:
    """Return a/b, or default if b is 0 (avoid ZeroDivisionError on doji bars)."""
    if b is None or b == 0 or pd.isna(b):
        return default
    return a / b


def compute_features(rows: list[tuple]) -> pd.DataFrame:
    """rows: [(date, open, high, low, close, volume), ...] sorted ascending.

    Adds VPA features: range, range_avg30, range_x, vol_p_in_30,
    upper_wick_ratio, lower_wick_ratio, close_pos.
    All ratios use safe_div to handle doji (high == low).
    """
    df = pd.DataFrame(rows, columns=['date', 'open', 'high', 'low', 'close', 'volume'])
    df['range'] = df['high'] - df['low']
    df['body'] = (df['close'] - df['open']).abs()

    # Rolling averages (use 30-bar window, min_periods=10 so early rows still have a value)
    df['range_avg30'] = df['range'].rolling(30, min_periods=10).mean()
    df['vol_avg30'] = df['volume'].rolling(30, min_periods=10).mean()

    # Range ratio (today vs avg)
    df['range_x'] = df.apply(lambda r: safe_div(r['range'], r['range_avg30'], 0.0), axis=1)

    # Volume percentile within last 30 bars
    def vol_pct(i):
        lo = max(0, i - 29)
        window = df['volume'].iloc[lo:i + 1].values
        if len(window) < 10:
            return None
        return float((window <= df['volume'].iloc[i]).mean())  # 0..1
    df['vol_p_in_30'] = [vol_pct(i) for i in range(len(df))]

    # Upper wick / lower wick / close position within bar
    # Use safe_div on `range` (high-low). When range is 0 (doji-flat bar), return 0.
    df['upper_wick'] = df['high'] - df[['open', 'close']].max(axis=1)
    df['lower_wick'] = df[['open', 'close']].min(axis=1) - df['low']
    df['upper_wick_ratio'] = df.apply(lambda r: safe_div(r['upper_wick'], r['range'], 0.0), axis=1)
    df['lower_wick_ratio'] = df.apply(lambda r: safe_div(r['lower_wick'], r['range'], 0.0), axis=1)
    df['close_pos'] = df.apply(lambda r: safe_div(r['close'] - r['low'], r['range'], 0.5), axis=1)
    df['is_up'] = df['close'] > df['open']

    return df


def test_strict_bc(row: pd.Series) -> tuple[bool, list[str]]:
    """Anna Strict BC 4 conditions. Returns (pass, list of conditions and pass/fail)."""
    conds = []
    vp = row['vol_p_in_30']
    vp_str = f"{vp:.2f}" if vp is not None else 'NA'
    c1 = (vp is not None) and (vp >= 0.95)
    conds.append(f"vol_p≥0.95: {vp_str} → {'✓' if c1 else '✗'}")
    c2 = row['range_x'] >= 1.5
    conds.append(f"range_x≥1.5: {row['range_x']:.2f} → {'✓' if c2 else '✗'}")
    c3 = row['upper_wick_ratio'] >= 0.4
    conds.append(f"upper_wick≥0.4: {row['upper_wick_ratio']:.2f} → {'✓' if c3 else '✗'}")
    c4 = row['close_pos'] < 0.4
    conds.append(f"close_pos<0.4: {row['close_pos']:.2f} → {'✓' if c4 else '✗'}")
    return c1 and c2 and c3 and c4, conds


def classify_bar(row: pd.Series) -> str:
    """Anna-flavored bar classification using only same-bar features."""
    if row['vol_p_in_30'] is None:
        return '(insufficient history)'
    tags = []
    # Buying climax: high vol, wide, upper wick, close low → real distribution
    if row['vol_p_in_30'] >= 0.95 and row['range_x'] >= 1.5 \
       and row['upper_wick_ratio'] >= 0.4 and row['close_pos'] < 0.4:
        tags.append('🔴 STRICT-BC')
    # BC候选 (close pos 限制放宽)
    elif row['vol_p_in_30'] >= 0.90 and row['range_x'] >= 1.3 \
         and row['upper_wick_ratio'] >= 0.30:
        tags.append('🟡 BC候选')
    # Selling climax (lower wick, close high, after a fall)
    if row['vol_p_in_30'] >= 0.95 and row['range_x'] >= 1.5 \
       and row['lower_wick_ratio'] >= 0.4 and row['close_pos'] > 0.6:
        tags.append('🟢 STRICT-SC')
    # 巨量阳: high vol up bar (ambiguous — could be markup or pre-BC)
    if row['vol_p_in_30'] >= 0.90 and row['is_up'] and row['close_pos'] > 0.6:
        tags.append('🔵 巨量阳')
    # No-supply: low vol, narrow
    if row['vol_p_in_30'] is not None and row['vol_p_in_30'] <= 0.20 and row['range_x'] < 0.7:
        tags.append('⚪ no-supply')
    return ', '.join(tags) if tags else '─'


# ==================== main ====================

def main():
    conn = sqlite3.connect(DB)
    print('═══ Strict Anna VPA 评判 — 4 个 v9.1 warning case ═══')
    print('（仅看当日及之前的 K 线特征, 不用后续走势）\n')

    for type_, code, target_date, descr in CASES:
        print(f'━━━━━━━━━━ {type_}: {code} @ {target_date} ━━━━━━━━━━')
        print(f'  描述: {descr}')

        # Pull 60 bars ending at target+0 to give enough rolling history
        rows = conn.execute(
            'SELECT date, open, high, low, close, volume FROM daily_kline '
            'WHERE code = ? AND date <= ? ORDER BY date DESC LIMIT 60',
            (code, target_date),
        ).fetchall()
        rows = list(reversed(rows))
        if not rows:
            print(f'  ✗ no data\n')
            continue
        df = compute_features(rows)

        # LLM signals
        llm_sigs = load_llm_signals(code)

        # Find target day index
        if target_date not in df['date'].values:
            print(f'  ✗ target date {target_date} not in K-line\n')
            continue
        i_target = df[df['date'] == target_date].index[0]

        # Print last 15 bars before & including target
        lo = max(0, i_target - 14)
        sub = df.iloc[lo:i_target + 1]

        print(f'\n  最近 15 bars (含当日):')
        print(f'  {"date":10}  {"O":>6} {"H":>6} {"L":>6} {"C":>6}  {"vol_p":>5} {"rng_x":>5} {"u_w":>4} {"l_w":>4} {"cls_p":>5}  Anna-tag')

        for _, r in sub.iterrows():
            d = r['date']
            tag = classify_bar(r)
            vp = f"{r['vol_p_in_30']:.2f}" if r['vol_p_in_30'] is not None else ' NA'
            marker = '◀──' if d == target_date else ''
            print(f'  {d}  {r["open"]:>6.2f} {r["high"]:>6.2f} {r["low"]:>6.2f} {r["close"]:>6.2f}  '
                  f'{vp:>5} {r["range_x"]:>5.2f} {r["upper_wick_ratio"]:>4.2f} '
                  f'{r["lower_wick_ratio"]:>4.2f} {r["close_pos"]:>5.2f}  {tag} {marker}')
            # Print LLM warning on its own day
            if d in llm_sigs and llm_sigs[d].get('warning'):
                print(f'             [LLM warning]: {llm_sigs[d]["warning"]!r}')

        # ------ verdict on target day ------
        target_row = df.iloc[i_target]
        passed, conds = test_strict_bc(target_row)
        print(f'\n  当日 ({target_date}) 严格 BC 4 条件检测:')
        for c in conds:
            print(f'    • {c}')
        print(f'  ▶ Strict-BC verdict: {"✓ MEETS Anna strict BC" if passed else "✗ does NOT meet strict BC"}')

        # ------ also: was there a strict BC in the prior 5 bars? (for SC follow-through) ------
        prior_lo = max(0, i_target - 5)
        prior_sub = df.iloc[prior_lo:i_target]  # exclusive of target
        prior_bcs = []
        for _, r in prior_sub.iterrows():
            ok, _ = test_strict_bc(r)
            if ok:
                prior_bcs.append(r['date'])
        if prior_bcs:
            print(f'  ▶ 前 5 bar 内有 strict BC: {prior_bcs}')
        else:
            print(f'  ▶ 前 5 bar 内 NO strict BC')

        # ------ what does the LLM say? ------
        target_llm = llm_sigs.get(target_date, {})
        if target_llm:
            print(f'\n  LLM 当日:')
            print(f'    verdict: {target_llm.get("verdict")} L{target_llm.get("level")} phase={target_llm.get("phase")}')
            print(f'    warning: {target_llm.get("warning")!r}')
            print(f'    reason : {target_llm.get("reason")!r}')

        print()

    conn.close()


if __name__ == '__main__':
    main()
