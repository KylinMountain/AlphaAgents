"""Read-only, standard-library probes. No provider, corpus or business DB.

Run from the repository root:
    python docs/reviews/evidence/2026-09-25-trader-lifecycle/reproduce_review_findings.py .

These assertions capture the reviewed defects, not desired future behavior.
Gate probes exercise only the pure evaluator; they never approve or promote.
"""
from pathlib import Path
import json
import runpy
import sqlite3
import sys

root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
gate = runpy.run_path(str(root / 'alpha_agents/evolution/holdout_gate.py'))
review = runpy.run_path(str(root / 'alpha_agents/evolution/trade_review.py'))
day = runpy.run_path(str(root / 'alpha_agents/evolution/day_record.py'))
memory = runpy.run_path(str(root / 'alpha_agents/pipeline/tasks/session_memory.py'))
base = [{'date': '2026-01-06', 'code': f'{i:06d}', 'brier': 0.25} for i in range(20)]
identical = gate['evaluate_candidate'](base, base)
slightly_worse = gate['evaluate_candidate'](base, [dict(r, brier=0.254) for r in base])
assert identical['outcome'] == 'promote'
assert slightly_worse['outcome'] == 'promote'
assert slightly_worse['mean_diff'] > 0

h = sqlite3.connect(':memory:')
h.execute('CREATE TABLE daily_kline (code TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL)')
h.executemany('INSERT INTO daily_kline VALUES (?,?,?,?,?,?)', [
    ('600001', '2026-01-05', 10, 10, 10, 10),
    ('600001', '2026-01-06', 10, 11, 10, 11),
])
stale = day['_bar'](h, '600001', '2026-01-07')
assert stale is not None and abs(stale[1] - 10) < 1e-8
# An open-priced sale cannot earn the high that happens later that day.
h.execute('INSERT INTO daily_kline VALUES (?,?,?,?,?,?)', ('600001', '2026-01-07', 10, 12, 9, 11))
facts = review['facts']({'id':1, 'code':'600001', 'open_date':'2026-01-06',
    'close_date':'2026-01-07', 'open_price':10, 'close_price':10, 'return_pct':0}, h)
assert facts['peak_pct'] == 20 and facts['peak_date'] == '2026-01-07'
h.close()
memory['note_decline']('600001', 10, 'pullback trader declined')
shared = memory['recall_decline']('600001', 10)
assert 'pullback trader declined' in shared

out = {
    'scope': 'synthetic code counterexamples, not historical trading performance',
    'gate_identical_scores': identical,
    'gate_worse_within_tolerance': slightly_worse,
    'gate_distinct_dates_in_input': 1,
    'missing_day_returned_stale_bar': {'requested_day':'2026-01-07', 'latest_stored_day':'2026-01-06',
                                     'returned_close':stale[0], 'returned_change_pct':round(stale[1], 6)},
    'open_exit_still_uses_full_exit_day_high': {'peak_pct':facts['peak_pct'], 'giveback_pp':facts['giveback_pp'],
        'qualification':'declared whole-day upper bound, not an achievable exit; phase is absent from the input contract'},
    'session_memory_has_no_trader_identity': {'key':'600001', 'other_caller_can_read_same_rejection':True},
}
print(json.dumps(out, ensure_ascii=False, indent=2))
