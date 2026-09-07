"""可视化 anna v91 backtest 的入场/退出 + warning 信号 vs 实际价格走势.

目的: 验证 LLM warning_phase '派发初期预警' 是否真的预测局部高点.

输出: 每笔 trade 一张图, 标:
- K线 (entry-30d 到 exit+10d 范围)
- 绿三角: BUY signal day
- 红三角: SELL signal day
- 黄星: warning_phase 含 派发/顶/高位/BC 的日子
- 文本标签: warning 内容 + 当日 verdict
"""
import json, sqlite3, sys
from collections import defaultdict
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm

_avail = {f.name for f in fm.fontManager.ttflist}
for cjk in ["PingFang HK", "PingFang SC", "Heiti SC", "STHeiti"]:
    if cjk in _avail:
        plt.rcParams["font.sans-serif"] = [cjk]
        plt.rcParams["font.family"] = "sans-serif"
        break
plt.rcParams["axes.unicode_minus"] = False


# All 15 trades (13 closed + 2 forced-close held)
TRADES = [
    # winners (7)
    ('688106', '2025-09-23', '2025-09-24', '2025-11-17', '2025-11-18', +6.74, 'WIN'),
    ('603358', '2025-09-23', '2025-09-24', '2026-01-20', '2026-01-21', +24.88, 'WIN'),
    ('000547', '2025-11-25', '2025-11-26', '2026-01-20', '2026-01-21', +86.49, 'WIN'),
    ('000751', '2026-01-22', '2026-01-23', '2026-02-26', '2026-02-27', +26.71, 'WIN'),
    ('000669', '2026-02-27', '2026-03-02', '2026-03-12', '2026-03-13', +11.62, 'WIN'),
    ('600207', '2026-04-13', '2026-04-14', '2026-04-24', '2026-04-24', +2.72, 'HELD'),
    ('688146', '2026-04-13', '2026-04-14', '2026-04-24', '2026-04-24', +20.02, 'HELD'),
    # losers (8)
    ('300318', '2025-11-18', '2025-11-19', '2025-11-24', '2025-11-25', -17.60, 'LOSS'),
    ('301283', '2026-01-21', '2026-01-22', '2026-01-26', '2026-01-27', -14.23, 'LOSS'),
    ('002279', '2026-01-27', '2026-01-28', '2026-01-28', '2026-01-29', -4.81, 'LOSS'),
    ('002532', '2026-01-29', '2026-01-30', '2026-02-02', '2026-02-03', -6.35, 'LOSS'),
    ('002602', '2026-02-04', '2026-02-05', '2026-03-12', '2026-03-13', -15.09, 'LOSS'),
    ('002135', '2026-03-13', '2026-03-16', '2026-03-26', '2026-03-27', -10.62, 'LOSS'),
    ('300992', '2026-03-13', '2026-03-16', '2026-04-09', '2026-04-10', -7.12, 'LOSS'),
    ('603898', '2026-03-27', '2026-03-30', '2026-04-01', '2026-04-01', -13.10, 'LOSS'),
]

# Load all warning events from cache
warnings_by_code = defaultdict(list)
verdicts_by_code = defaultdict(dict)  # code → date → (verdict, level, phase, warning)
with open('/tmp/anna_v91_v3_cache.jsonl') as f:
    for line in f:
        obj = json.loads(line)
        res = obj.get('result', {})
        if not res.get('ok', True):
            continue
        code = obj['code']
        date = obj['date']
        warning = str(res.get('llm_warning_phase', '') or '')
        verdict = str(res.get('llm_verdict', '') or '')
        level = res.get('llm_confirmation_level', 0) or 0
        phase = res.get('llm_phase', '') or ''
        verdicts_by_code[code][date] = {
            'verdict': verdict, 'level': level, 'phase': phase, 'warning': warning,
        }
        if any(k in warning for k in ['派发', '顶', '高位', 'BC']):
            warnings_by_code[code].append((date, warning, verdict, level, phase))


conn = sqlite3.connect('/Users/evilkylin/Projects/AlphaAgents/data/market_history.db')
out_dir = Path('/Users/evilkylin/Projects/AlphaAgents/data/charts/anna_v91_trades')
out_dir.mkdir(parents=True, exist_ok=True)

for code, sig_date, entry_date, exit_sig_date, exit_date, net, kind in TRADES:
    # Plot range: entry - 30 days to exit + 10 days
    rows = conn.execute(
        'SELECT date, open, high, low, close, volume FROM daily_kline WHERE code=? '
        'AND date BETWEEN date(?, "-50 days") AND date(?, "+15 days") ORDER BY date',
        (code, entry_date, exit_date)
    ).fetchall()
    if not rows:
        print(f'  no data {code}')
        continue

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(16, 8), sharex=True,
                                    gridspec_kw={'height_ratios': [3, 1]})

    # K-line
    dates = [r[0] for r in rows]
    n = len(rows)
    for i, r in enumerate(rows):
        d, op, hi, lo, cl, vol = r
        color = '#d62728' if cl >= op else '#2ca02c'
        ax1.plot([i, i], [lo, hi], color=color, linewidth=0.5, alpha=0.7)
        ax1.bar(i, abs(cl - op), bottom=min(op, cl), color=color, width=0.7,
                alpha=0.7, edgecolor='black', linewidth=0.2)
        ax2.bar(i, vol, color=color, width=0.7, alpha=0.7)

    # Mark entry (signal day)
    if sig_date in dates:
        i_sig = dates.index(sig_date)
        sig_lo = rows[i_sig][3]
        ax1.scatter(i_sig, sig_lo * 0.97, marker='^', s=300, color='#1f77b4',
                    edgecolors='black', linewidths=1.5, zorder=10, label='BUY signal day')
        sig_data = verdicts_by_code[code].get(sig_date, {})
        ax1.annotate(f'BUY\n{sig_data.get("verdict","")} L{sig_data.get("level","")}\n{sig_data.get("phase","")}',
                     xy=(i_sig, sig_lo * 0.97),
                     xytext=(i_sig, sig_lo * 0.92), fontsize=9, ha='center', va='top',
                     fontweight='bold', color='#1f77b4',
                     bbox=dict(boxstyle='round,pad=0.3', fc='#dceefb', ec='#1f77b4', lw=1))

    # Mark exit (signal day)
    if exit_sig_date in dates:
        i_exit = dates.index(exit_sig_date)
        exit_hi = rows[i_exit][2]
        sell_color = '#2ca02c' if net > 0 else '#d62728'
        ax1.scatter(i_exit, exit_hi * 1.03, marker='v', s=300, color=sell_color,
                    edgecolors='black', linewidths=1.5, zorder=10, label='SELL signal day')
        sig_data = verdicts_by_code[code].get(exit_sig_date, {})
        ax1.annotate(f'SELL ({sig_data.get("phase","")})\nnet {net:+.2f}%',
                     xy=(i_exit, exit_hi * 1.03),
                     xytext=(i_exit, exit_hi * 1.07), fontsize=9, ha='center', va='bottom',
                     fontweight='bold', color=sell_color,
                     bbox=dict(boxstyle='round,pad=0.3', fc='#fff', ec=sell_color, lw=1.5))

    # Mark all warning days (yellow stars)
    warn_count = 0
    for w_date, w_text, w_verdict, w_level, w_phase in warnings_by_code.get(code, []):
        if w_date not in dates:
            continue
        i_w = dates.index(w_date)
        w_hi = rows[i_w][2]
        is_buy_day = w_date == sig_date  # avoid double-marking entry day
        if not is_buy_day:
            ax1.scatter(i_w, w_hi * 1.015, marker='*', s=200, color='#FFD700',
                        edgecolors='#8B4513', linewidths=1, zorder=8, alpha=0.9,
                        label='⚠️ warning' if warn_count == 0 else None)
            warn_count += 1

    title = f'{code} | {kind} {net:+.2f}% | entry sig {sig_date} → exit sig {exit_sig_date} | held {(rows.index([r for r in rows if r[0]==exit_date][0]) - rows.index([r for r in rows if r[0]==entry_date][0])) if entry_date in dates and exit_date in dates else "?"}d'
    ax1.set_title(title, fontsize=12)
    ax1.set_ylabel('Price')
    ax1.legend(loc='upper left', fontsize=10)
    ax1.grid(True, alpha=0.3)

    ax2.set_ylabel('Volume')
    ax2.grid(True, alpha=0.3)
    tick_idx = list(range(0, n, max(1, n // 15)))
    ax2.set_xticks(tick_idx)
    ax2.set_xticklabels([rows[i][0][5:] for i in tick_idx], rotation=45, fontsize=8)
    ax2.set_xlabel('Date')

    plt.tight_layout()
    out = out_dir / f'{kind}_{code}_{sig_date}_{net:+.0f}.png'
    plt.savefig(out, dpi=110, bbox_inches='tight')
    plt.close()
    print(f'saved {out.name}  warnings: {warn_count}')

print(f'\nAll charts in {out_dir}')
