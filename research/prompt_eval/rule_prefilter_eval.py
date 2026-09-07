"""评估 rule-based pre-filter 能省多少 LLM 调用, 又会错过多少 warning.

思路:
  对 cache 里每个 (code, date), 用 30 日 K 线算 VPA 特征,
  rule 决定 "这个 bar 值不值得 LLM 看".

  Skip 规则 (rule 说 "无 climax 嫌疑, 跳过"):
    - 当日 vol_p < 0.85 AND range_x < 1.2  (无放量也无宽幅)
    - AND 当日 lower_wick_ratio < 0.4 AND upper_wick_ratio < 0.4 (无明显上下影)
    - AND 过去 5 bar 无 strict-BC 也无 strict-SC

  Keep 规则 (rule 说 "有嫌疑, 喂 LLM"):
    - 任一上述条件不满足 → 不跳过

评估指标:
  • skip_rate: rule 跳过的占比
  • miss_rate: 跳过的里面有多少 LLM 实际给了 warning (我们错过了)
  • wasted_calls: 没跳但 LLM 也没 warning (本可以跳)
  • effective_savings: 跳过且 LLM 也无 warning 的比例
"""
from __future__ import annotations
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

DB = '/Users/evilkylin/Projects/AlphaAgents/data/market_history.db'
CACHE = '/tmp/anna_v91_v3_cache.jsonl'

WARNING_KEYS = ('派发', '顶', '高位', 'BC', '吸筹尾声', 'SC', 'SOW', 'UTAD')


def safe_div(a, b, default=0.0):
    if b is None or b == 0 or pd.isna(b):
        return default
    return a / b


def compute_features(rows):
    df = pd.DataFrame(rows, columns=['date', 'open', 'high', 'low', 'close', 'volume'])
    df['range'] = df['high'] - df['low']
    df['range_avg30'] = df['range'].rolling(30, min_periods=10).mean()
    df['range_x'] = df.apply(lambda r: safe_div(r['range'], r['range_avg30']), axis=1)

    def vol_pct(i):
        lo = max(0, i - 29)
        window = df['volume'].iloc[lo:i + 1].values
        if len(window) < 10:
            return None
        return float((window <= df['volume'].iloc[i]).mean())
    df['vol_p_in_30'] = [vol_pct(i) for i in range(len(df))]

    df['upper_wick'] = df['high'] - df[['open', 'close']].max(axis=1)
    df['lower_wick'] = df[['open', 'close']].min(axis=1) - df['low']
    df['upper_wick_ratio'] = df.apply(lambda r: safe_div(r['upper_wick'], r['range']), axis=1)
    df['lower_wick_ratio'] = df.apply(lambda r: safe_div(r['lower_wick'], r['range']), axis=1)
    df['close_pos'] = df.apply(lambda r: safe_div(r['close'] - r['low'], r['range'], 0.5), axis=1)
    return df


def is_strict_bc(r):
    vp = r.get('vol_p_in_30')
    return (vp is not None and vp >= 0.95
            and r['range_x'] >= 1.5
            and r['upper_wick_ratio'] >= 0.4
            and r['close_pos'] < 0.4)


def is_strict_sc(r):
    vp = r.get('vol_p_in_30')
    return (vp is not None and vp >= 0.95
            and r['range_x'] >= 1.5
            and r['lower_wick_ratio'] >= 0.4
            and r['close_pos'] > 0.6)


def rule_should_skip(df, target_date):
    """Returns (skip: bool, reason: str)."""
    if target_date not in df['date'].values:
        return False, 'no kline'
    i = df[df['date'] == target_date].index[0]
    row = df.iloc[i]

    vp = row.get('vol_p_in_30')
    if vp is None:
        return False, 'insufficient history'

    # --- KEEP triggers (any one keeps the bar) ---
    if vp >= 0.85 and row['range_x'] >= 1.2:
        return False, 'high-vol+wide-range'
    if vp >= 0.95:
        return False, 'extreme-vol'
    if row['range_x'] >= 1.5:
        return False, 'wide-range alone'
    if row['upper_wick_ratio'] >= 0.4 or row['lower_wick_ratio'] >= 0.4:
        return False, 'long-wick'

    # Past 5 bars contain a strict climax bar?
    lo = max(0, i - 5)
    for _, r in df.iloc[lo:i].iterrows():
        if is_strict_bc(r) or is_strict_sc(r):
            return False, 'recent-climax'

    return True, 'quiet'


def has_warning(result):
    w = (result.get('llm_warning_phase') or '').strip()
    return any(k in w for k in WARNING_KEYS)


def is_actionable_warning(result):
    """Strategy 实际会基于哪些 warning 改动作?

    1. verdict 含 '空' (看空/偏空/中性偏空) → 触发 SELL
    2. phase 含 '派发末期'/'派发确认' → 强烈 SELL signal
    3. confirmation_level >= 2 AND warning 存在 → 高置信度 warning

    其他 (派发初期预警 + 偏多) 是 soft 提示, 在 strategy 里不强制改动作.
    """
    if not has_warning(result):
        return False
    verdict = (result.get('llm_verdict') or '')
    phase = (result.get('llm_phase') or '')
    level = result.get('llm_confirmation_level') or 0

    if '空' in verdict:
        return True
    if '派发末期' in phase or '派发确认' in phase or 'SOW' in phase:
        return True
    if level >= 2 and ('派发' in (result.get('llm_warning_phase') or '')):
        return True
    return False


def main():
    # 1) Load cache, group by code
    by_code = defaultdict(list)  # code → list of (date, result)
    with open(CACHE) as f:
        for line in f:
            obj = json.loads(line)
            res = obj.get('result', {})
            if not res.get('ok', True):
                continue
            by_code[obj['code']].append((obj['date'], res))

    print(f'[load] {sum(len(v) for v in by_code.values())} cached LLM obs across {len(by_code)} codes\n')

    # 2) For each code, fetch 70 days back from earliest cached date, compute features
    conn = sqlite3.connect(DB)

    stats = {
        'total': 0,
        'skip_quiet': 0,         # rule says skip
        'skip_quiet_was_warning': 0,  # MISSED any warnings
        'skip_quiet_was_actionable': 0,  # MISSED actionable warnings (verdict=空 or phase=派发末期)
        'keep_was_warning': 0,   # rule kept and LLM warned
        'keep_was_actionable': 0,
        'keep_no_warning': 0,    # rule kept but LLM said no warning
        'no_kline': 0,
    }
    miss_examples = []
    skip_examples = []
    keep_no_warning_examples = []

    for code, entries in by_code.items():
        entries.sort()
        earliest = entries[0][0]
        # Fetch enough K-line: 70 days before earliest, to latest+5
        latest = entries[-1][0]
        rows = conn.execute(
            'SELECT date, open, high, low, close, volume FROM daily_kline '
            'WHERE code=? AND date BETWEEN date(?, "-70 days") AND date(?, "+5 days") '
            'ORDER BY date', (code, earliest, latest)
        ).fetchall()
        if not rows or len(rows) < 30:
            stats['total'] += len(entries)
            stats['no_kline'] += len(entries)
            continue

        df = compute_features(rows)

        for date, result in entries:
            stats['total'] += 1
            warn = has_warning(result)
            actionable = is_actionable_warning(result)
            skip, reason = rule_should_skip(df, date)

            if skip:
                stats['skip_quiet'] += 1
                if warn:
                    stats['skip_quiet_was_warning'] += 1
                if actionable:
                    stats['skip_quiet_was_actionable'] += 1
                    if len(miss_examples) < 15:
                        miss_examples.append((code, date, result.get('llm_warning_phase'), result.get('llm_verdict'), result.get('llm_phase'), result.get('llm_confirmation_level')))
                else:
                    if len(skip_examples) < 5:
                        skip_examples.append((code, date, result.get('llm_phase'), result.get('llm_verdict')))
            else:
                if warn:
                    stats['keep_was_warning'] += 1
                if actionable:
                    stats['keep_was_actionable'] += 1
                if not warn:
                    stats['keep_no_warning'] += 1
                    if len(keep_no_warning_examples) < 5:
                        keep_no_warning_examples.append((code, date, reason, result.get('llm_phase'), result.get('llm_verdict')))

    # 3) Print summary
    print('═══ Pre-filter 评估结果 ═══\n')
    n = stats['total']
    no_data = stats['no_kline']
    valid = n - no_data
    skip = stats['skip_quiet']
    miss = stats['skip_quiet_was_warning']
    keep_warn = stats['keep_was_warning']
    keep_nowarn = stats['keep_no_warning']

    total_warnings = miss + keep_warn
    miss_action = stats['skip_quiet_was_actionable']
    keep_action = stats['keep_was_actionable']
    total_actionable = miss_action + keep_action

    print(f'  总观察:      {n}')
    print(f'  无 K 线数据:  {no_data}')
    print(f'  有效观察:    {valid}\n')

    print(f'  规则 SKIP:   {skip} ({skip/valid*100:.1f}% of valid)')
    print(f'  规则 KEEP:   {valid - skip} ({(valid-skip)/valid*100:.1f}% of valid)')
    print(f'    └ LLM 喊 warning:    {keep_warn}  (rule 保留 LLM 也警, 正确)')
    print(f'    └ LLM 没 warning:    {keep_nowarn}  (rule 保留但 LLM 不警, 浪费)')
    print()
    print('  ── 所有 warning (含 soft 派发初期预警+偏多) ──')
    print(f'    Total:               {total_warnings} ({total_warnings/valid*100:.1f}% of valid)')
    print(f'    漏 (skip+有 warn):   {miss} / {total_warnings} = {miss/max(1,total_warnings)*100:.1f}%')
    print()
    print('  ── 仅 actionable warning (verdict=空 OR phase=派发末期 OR L≥2+派发) ──')
    print(f'    Total:               {total_actionable} ({total_actionable/valid*100:.1f}% of valid)')
    if total_actionable:
        print(f'    漏 (skip+actionable): {miss_action} / {total_actionable} = {miss_action/total_actionable*100:.1f}%')
    print()
    print(f'  💰 LLM 调用节省率:        {skip/valid*100:.1f}%')
    print(f'  ⚠️  All-warning 漏报率:   {miss/max(1,total_warnings)*100:.1f}%')
    print(f'  ⚠️  Actionable 漏报率:   {miss_action/max(1,total_actionable)*100:.1f}% ← 真正关心这个')
    print()

    if miss_examples:
        print('  漏报 actionable examples (rule skipped, but LLM gave actionable warning):')
        for code, date, w, v, p, lvl in miss_examples:
            print(f'    {code} @ {date}: warn={w!r} verdict={v} phase={p} L{lvl}')
        print()

    if skip_examples:
        print('  正确跳过 examples (rule skipped, LLM no warning):')
        for code, date, p, v in skip_examples:
            print(f'    {code} @ {date}: phase={p} verdict={v}')
        print()

    if keep_no_warning_examples:
        print('  浪费 examples (rule kept but LLM no warning):')
        for code, date, reason, p, v in keep_no_warning_examples:
            print(f'    {code} @ {date}: kept={reason!r} phase={p} verdict={v}')


if __name__ == '__main__':
    main()
