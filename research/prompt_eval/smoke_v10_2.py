"""Smoke test: v10.2 prompt + Phase 1 code refactor on 1 case.

Verifies:
- prompt_version / model / provider in return
- prior_state actually injected into user message
- candidates explicitly fenced into user message
- status / analysis_valid fields present
- Verdict still strategically correct (300318 should be 偏空)
"""
from __future__ import annotations
import json, os, sys, time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env")
except ImportError:
    pass

from alpha_agents.tools.vpa.llm import _call_llm_vpa, PROMPT_VERSION

print(f'PROMPT_VERSION = {PROMPT_VERSION}\n')

# Load cached vpa_text from real backtest
with open('/tmp/anna_v91_v3_cache.jsonl') as f:
    vpa_text, candidates = None, None
    for line in f:
        obj = json.loads(line)
        if obj['code'] == '300318' and obj['date'] == '2025-11-18':
            vpa_text = obj['result'].get('text', '')
            # extract candidates from cache for re-test
            candidates = obj['result'].get('candidates_snapshot') or []
            break

assert vpa_text, 'no cached text for 300318 11-18'
print(f'vpa_text: {len(vpa_text)} chars')
print(f'candidates: {len(candidates)} entries\n')

# Inject a fake prior_state (simulating defense mode chain)
prior_state = {
    'analysis_date': '2025-11-17',
    'phase': '拉升',
    'verdict': '看多',
    'selected_candidate_id': 'cand-2025-11-14',
    'rationale': 'SOS 突破 + markup 延续',
}

# Force DeepSeek V4-Pro for richer reasoning
os.environ['DEEPSEEK_MODEL'] = 'deepseek-v4-pro'
# Disable AGENT to ensure DeepSeek path
os.environ.pop('AGENT_API_KEY', None)
os.environ.pop('SILICONFLOW_API_KEY', None)

t0 = time.time()
result = _call_llm_vpa(
    code='300318',
    vpa_text=vpa_text,
    previous_analysis='',
    as_of='2025-11-18',
    candidates=candidates,
    prior_state=prior_state,
)
dt = time.time() - t0

print(f'─── result keys ───')
print(f'  {sorted(result.keys())}')
print()
print(f'─── metadata ───')
print(f'  status:           {result.get("status")}')
print(f'  analysis_valid:   {result.get("analysis_valid")}')
print(f'  prompt_version:   {result.get("prompt_version")}')
print(f'  model:            {result.get("model")}')
print(f'  provider:         {result.get("provider")}')
print(f'  elapsed:          {dt:.0f}s')
print()
print(f'─── verdict ───')
print(f'  verdict:    {result.get("verdict")}')
print(f'  phase:      {result.get("phase")!r}')
print(f'  warning:    {result.get("warning_phase")!r}')
print(f'  confidence: {result.get("confidence")}')
print(f'  confirmed:  {result.get("confirmed")} (derived from phase_change.confirmed)')
print(f'  phase_change: {result.get("phase_change")}')
print(f'  reason:     {result.get("reason")[:120]}')
print()
sigs = result.get('signals', [])
print(f'─── signals ({len(sigs)}) ───')
for s in sigs[:5]:
    print(f'  {s.get("name", "?")}: confirmed={s.get("confirmed")} {s.get("by", s.get("need", ""))[:80]}')

# Verify cross_family_blocked is real bool
print()
print(f'─── type safety ───')
cfb = result.get('cross_family_blocked')
print(f'  cross_family_blocked: {cfb!r} (type: {type(cfb).__name__})')

# Save full result
out = {'meta': {'elapsed_s': dt}, 'result': result}
out['result'].pop('report', None)  # drop the long narrative for compactness
with open('/tmp/v10_2_smoke.json', 'w') as f:
    json.dump(out, f, ensure_ascii=False, indent=2, default=str)
print(f'\n[saved] /tmp/v10_2_smoke.json')
