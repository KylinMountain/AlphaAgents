"""v10.3 + Phase 3 hardening 完整 pipeline 测试 — 跑 4 bad case + 3 干扰.

测的是新版 _call_llm_vpa 的全流程:
- v10.3 prompt
- prior_state + candidates 显式注入 (P0.1+P0.2)
- _validate_llm_verdict_schema (P0.2)
- _validate_selected_candidate (P1.7)
- guard + validator + footer
- _error_result envelope (P1.6)
- direction + usage + as_of + schema_warnings 字段

验证:
- 7 个 case 都能跑通 (没 envelope 异常)
- Verdict direction 与真实结果一致
- 新 envelope 字段全部 populated
- absorbed_test override / footer 在该出现的地方出现
"""
from __future__ import annotations
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env")
except ImportError:
    pass

# Force V4-Pro for richer reasoning (more reliable on edge cases)
os.environ["DEEPSEEK_MODEL"] = "deepseek-v4-pro"
# Force deepseek (not AGENT/SiliconFlow)
os.environ["VPA_LLM_PROVIDER"] = "deepseek"
os.environ.pop("AGENT_API_KEY", None)
os.environ.pop("SILICONFLOW_API_KEY", None)

from alpha_agents.tools.vpa.llm import _call_llm_vpa, PROMPT_VERSION

print(f'═══ v10.3 + Phase 3 envelope 全流程测试 ═══')
print(f'PROMPT_VERSION: {PROMPT_VERSION}\n')


CASES = [
    # (label, code, date, expected_direction_signs, note)
    ('A1-真跌', '300318', '2025-11-18', ['偏空', '看空'], 'BC+AR 已现, 应判派发'),
    ('A2-真跌', '002135', '2026-03-19', ['偏空', '看空'], '应升级派发, 不应是 absorbed test'),
    ('B1-错警', '000547', '2025-11-25', ['看多', '偏多'], '11-20 absorbed test override'),
    ('B2-错警', '603358', '2025-11-07', ['看多', '偏多'], '不应加 warning'),
    ('干扰-1',  '688106', '2025-11-07', ['看多', '偏多'], 'clean markup'),
    ('干扰-2',  '603358', '2025-12-25', ['看多', '偏多'], 'clean markup'),
    ('干扰-3',  '600382', '2026-01-21', ['看多', '偏多'], 'clean markup'),
]


def get_cached(code: str, date: str) -> tuple[str, list[dict]] | tuple[None, None]:
    with open('/tmp/anna_v91_v3_cache.jsonl') as f:
        for line in f:
            obj = json.loads(line)
            if obj['code'] == code and obj['date'] == date:
                res = obj['result']
                return res.get('text', ''), res.get('candidates_snapshot') or []
    return None, None


results = []
for label, code, date, expected_dirs, note in CASES:
    print(f'━━━━━━━━━━ {label}: {code} @ {date} ━━━━━━━━━━')
    print(f'  期望 direction ∈ {expected_dirs}: {note}')

    text, cands = get_cached(code, date)
    if not text:
        print(f'  ✗ no cached text\n')
        continue

    # Inject prior_state to exercise that path
    prior_state = {
        'analysis_date': '2025-11-17' if '11' in date else '2026-03-18',
        'phase': '拉升',
        'verdict': '看多',
        'selected_candidate_id': None,
        'rationale': 'SOS confirmed, markup ongoing',
    }

    print(f'  调用中... (vpa_text {len(text)} chars, {len(cands)} candidates)', flush=True)
    t0 = time.time()
    r = _call_llm_vpa(
        code=code,
        vpa_text=text,
        previous_analysis='',
        as_of=date,
        candidates=cands,
        prior_state=prior_state,
    )
    dt = time.time() - t0

    # Verify envelope structure
    envelope_ok = all(k in r for k in [
        'status', 'analysis_valid', 'prompt_version', 'model', 'provider',
        'as_of', 'usage', 'verdict', 'direction', 'phase',
        'phase_change', 'signals', 'selected_climax', 'schema_warnings',
    ])

    direction = r.get('direction')
    direction_ok = direction in expected_dirs

    print(f'  [{dt:.0f}s] status={r.get("status")} valid={r.get("analysis_valid")} '
          f'envelope_ok={envelope_ok} direction_ok={direction_ok}')
    print(f'  direction: {direction} | phase: {r.get("phase")!r} | '
          f'warning: {(r.get("warning_phase") or "─")!r}')
    print(f'  pc: from={r.get("phase_change", {}).get("from")} '
          f'to={r.get("phase_change", {}).get("to")} '
          f'confirmed={r.get("phase_change", {}).get("confirmed")}')

    sigs = r.get('signals', []) or []
    sig_summary = [f"{s.get('name', '?')[:18]} c={s.get('confirmed')}" for s in sigs[:3]]
    print(f'  signals[{len(sigs)}]: {sig_summary}')

    sel = r.get('selected_climax', {}) or {}
    print(f'  selected_climax: id={sel.get("candidate_id")} type={sel.get("climax_type")} '
          f'rationale={(sel.get("rationale") or "")[:60]}')

    sw = r.get('schema_warnings', [])
    if sw:
        print(f'  schema_warnings: {sw}')
    failures = r.get('vph_conflict_failures', [])
    if failures:
        print(f'  vph_conflict_failures: {failures[:2]}')

    usage = r.get('usage')
    if usage:
        print(f'  usage: {usage}')
    print(f'  reason: {(r.get("reason") or "")[:120]}')

    # Look for footer in report
    report = r.get('report', '')
    has_footer = '【校验器修正】' in report
    print(f'  footer present: {has_footer}')

    results.append({
        'label': label, 'code': code, 'date': date,
        'expected': expected_dirs, 'direction': direction,
        'direction_ok': direction_ok, 'envelope_ok': envelope_ok,
        'phase': r.get('phase'), 'warning': r.get('warning_phase'),
        'signals': sig_summary, 'has_footer': has_footer,
        'schema_warnings': sw, 'usage': usage,
        'elapsed_s': dt,
    })

    with open('/tmp/v10_3_bad_cases.json', 'w') as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    print()


print('\n═══ 总结 ═══')
n = len(results)
direction_correct = sum(1 for r in results if r['direction_ok'])
envelope_correct = sum(1 for r in results if r['envelope_ok'])
total_time = sum(r['elapsed_s'] for r in results)
print(f'Cases: {n}')
print(f'Direction 正确: {direction_correct}/{n} ({direction_correct/n*100:.0f}%)')
print(f'Envelope 完整: {envelope_correct}/{n}')
print(f'总耗时: {total_time:.0f}s ({total_time/60:.1f}分钟)')
print()
print(f'{"Case":12} {"direction":10} {"phase":12} {"warning":18} {"footer":6}')
print('─' * 70)
for r in results:
    fok = '✓' if r['direction_ok'] else '✗'
    print(f"{r['label']:12} {(r['direction'] or '?'):10} {r['phase']:12} "
          f"{(r['warning'] or '─'):18} {'Y' if r['has_footer'] else '─'} {fok}")

print(f'\n[saved] /tmp/v10_3_bad_cases.json')
