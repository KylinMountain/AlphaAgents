"""对 4 个关键 case 用 v4-pro 重新分析, 看是模型问题还是 prompt 问题.

Type A (warning 准, 后面真跌): 300318 11-18, 002135 03-19
Type B (warning 错, 后面大涨): 000547 11-25, 603358 11-08

每个 case 用相同 prompt 同时跑 v4-flash + v4-pro, 对比 verdict.
"""
import os, sys, time, json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env")
except ImportError:
    pass

from openai import OpenAI

API_KEY = os.getenv('DEEPSEEK_API_KEY')
BASE_URL = os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com/v1')

# 4 test cases
CASES = [
    ('A', '300318', '2025-11-18', 'entry day, 4 天后 -17.6% (warning 准)'),
    ('A', '002135', '2026-03-19', 'peak day, 4 天后 -18% (warning 准)'),
    ('B', '000547', '2025-11-25', 'entry day, 后续 +185% (warning 错)'),
    ('B', '603358', '2025-11-08', 'peak day, 后续短跌再涨 (warning 错)'),
]


def get_cached_text(code, date):
    """Find cached LLM input text from v3 cache."""
    with open('/tmp/anna_v91_v3_cache.jsonl') as f:
        for line in f:
            obj = json.loads(line)
            if obj['code'] == code and obj['date'] == date:
                return obj['result'].get('text', '')
    return None


def call_llm(model, system_prompt, user_text):
    client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
    extra_body = {}
    if 'v4' in model:
        extra_body = {'enable_thinking': False} if 'pro' not in model else {'enable_thinking': True}
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_text},
            ],
            max_tokens=12000,
            timeout=180,
            extra_body=extra_body if extra_body else None,
        )
        return resp.choices[0].message.content, resp.usage
    except Exception as e:
        return f'ERROR: {type(e).__name__}: {str(e)[:200]}', None


def extract_verdict(report):
    """Find <!-- VERDICT: {...} --> tag."""
    import re
    m = re.search(r'<!--\s*VERDICT:\s*(\{.*?\})\s*-->', report, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except: pass
    return {}


# Get the v9.1 system prompt
from alpha_agents.tools.vpa.llm import ANNA_COULLING_PROMPT


print('═══ v4-flash vs v4-pro 对比 ═══\n')

results = []
for type_, code, date, descr in CASES:
    print(f'━━━ Type {type_}: {code} @ {date}')
    print(f'    {descr}')
    user_text = get_cached_text(code, date)
    if not user_text:
        print(f'    ✗ no cached text\n')
        continue

    # Find cached v4-flash result
    flash_verdict = None
    with open('/tmp/anna_v91_v3_cache.jsonl') as f:
        for line in f:
            obj = json.loads(line)
            if obj['code'] == code and obj['date'] == date:
                res = obj['result']
                flash_verdict = {
                    'verdict': res.get('llm_verdict'),
                    'level': res.get('llm_confirmation_level'),
                    'phase': res.get('llm_phase'),
                    'warning': res.get('llm_warning_phase'),
                    'reason': res.get('llm_reason'),
                }
                break

    print(f'    [v4-flash]:')
    if flash_verdict:
        print(f'      verdict: {flash_verdict["verdict"]} L{flash_verdict["level"]} phase={flash_verdict["phase"]}')
        print(f'      warning: {flash_verdict["warning"]!r}')
        print(f'      reason: {flash_verdict["reason"][:100] if flash_verdict["reason"] else ""}')

    print(f'    [v4-pro]: 调用中...', flush=True)
    t0 = time.time()
    report, usage = call_llm('deepseek-v4-pro', ANNA_COULLING_PROMPT, user_text)
    dt = time.time() - t0

    if 'ERROR' in report[:20]:
        print(f'      ✗ {report[:150]}')
    else:
        v = extract_verdict(report)
        print(f'      time: {dt:.0f}s, tokens: {usage.total_tokens if usage else "?"}')
        print(f'      verdict: {v.get("direction")} L{v.get("confirmation_level")} phase={v.get("phase")}')
        print(f'      warning: {v.get("warning_phase","")!r}')
        print(f'      reason: {v.get("reason","")[:100]}')

    results.append({
        'type': type_, 'code': code, 'date': date,
        'flash': flash_verdict,
        'pro_report': report[:5000],
        'pro_verdict': v if 'ERROR' not in report[:20] else None,
    })
    print()

# Save
with open('/tmp/v4_flash_vs_pro_compare.json', 'w') as f:
    json.dump(results, f, ensure_ascii=False, indent=2, default=str)
print(f'\n[saved] /tmp/v4_flash_vs_pro_compare.json')
