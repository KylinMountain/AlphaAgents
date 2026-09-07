"""v10 (current code) vs v9.1 (saved .md baseline) — v4-pro 对比.

v10 改动 (10 处):
  1. markup-bias 硬否决删除
  2. ST 主签名 = 量小 (Anna 原书)
  3. SOW 阶段化 (range 内 vs 跌破 AR 低)
  4. Successful Supply Test 新章节 + override
  5. no_demand_bar / no_supply_bar 提为 primary
  6. effort/result 正式化
  7. 阈值改用 user 数据 (vol_ratio_pct 等), 不写死
  8. AR 不强制放量
  9. confidence 字段语义清理
  10. 重要规则上提 + 整体压缩 46%

测试集: 4 核 + 3 干扰.
"""
from __future__ import annotations
import json, os, re, sys, time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

try:
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env")
except ImportError:
    pass

from openai import OpenAI

# v10 = current production prompt
from alpha_agents.tools.vpa.llm import ANNA_COULLING_PROMPT as PROMPT_V10

# v9.1 = baseline from saved .md (skip HTML header + title)
def load_v91() -> str:
    md = (REPO / 'docs/prompts/anna-coulling-vpa-v9.1.md').read_text()
    return md[md.index('你是量价分析师'):].rstrip()

PROMPT_V91 = load_v91()

API_KEY = os.getenv('DEEPSEEK_API_KEY')
BASE_URL = os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com/v1')
MODEL = 'deepseek-v4-pro'

CASES = [
    ('A1-真跌', '300318', '2025-11-18', '应升级派发初期 (BC+AR 已现)'),
    ('A2-真跌', '002135', '2026-03-19', '应升级派发初期 (但不到 SOW)'),
    ('B1-错警', '000547', '2025-11-25', '应保持拉升 (11-20 absorbed test override)'),
    ('B2-错警', '603358', '2025-11-07', '应保持看多, 不加 warning'),
    ('干扰-1', '688106', '2025-11-07', '应看多 (v9.1+pro 错判下跌)'),
    ('干扰-2', '603358', '2025-12-25', '应看多/偏多 拉升'),
    ('干扰-3', '600382', '2026-01-21', '应看多 拉升'),
]


def get_text(code, date):
    with open('/tmp/anna_v91_v3_cache.jsonl') as f:
        for line in f:
            obj = json.loads(line)
            if obj['code'] == code and obj['date'] == date:
                return obj['result'].get('text', '')
    return None


def call_pro(sys_p, user_t):
    c = OpenAI(api_key=API_KEY, base_url=BASE_URL)
    t0 = time.time()
    try:
        r = c.chat.completions.create(
            model=MODEL,
            messages=[{'role':'system','content':sys_p},{'role':'user','content':user_t}],
            max_tokens=12000, timeout=180, extra_body={'enable_thinking':True},
        )
        return r.choices[0].message.content, {'time': time.time()-t0, 'tokens': r.usage.total_tokens if r.usage else 0}
    except Exception as e:
        return f'ERROR: {type(e).__name__}: {str(e)[:200]}', {'time': time.time()-t0}


def extract(rep):
    if not rep: return {}
    m = re.search(r'<!--\s*VERDICT:\s*(\{.*?\})\s*-->', rep, re.DOTALL)
    if not m: return {}
    try: return json.loads(m.group(1))
    except: return {}


def fmt(v):
    if not v: return '(no VERDICT)'
    pc = v.get('phase_change', {}) or {}
    pc_str = f' pc={pc.get("from","")}→{pc.get("to","")} c={pc.get("confirmed")}' if pc.get('confirmed') is not None else ''
    sigs = [s.get('name','')[:18] for s in (v.get('signals',[]) or [])][:3]
    return f"{v.get('direction','?')} phase={v.get('phase','?')!r} warn={(v.get('warning_phase','') or '─')!r}{pc_str} sigs={sigs}"


def main():
    print(f'═══ v9.1 vs v10 prompt 对比 (model={MODEL}) ═══\n')
    print(f'v9.1 prompt: {len(PROMPT_V91)} chars')
    print(f'v10  prompt: {len(PROMPT_V10)} chars ({(1-len(PROMPT_V10)/len(PROMPT_V91))*100:.1f}% 压缩)\n')

    results = []
    for label, code, date, expect in CASES:
        print(f'━━━━━━━━━━ {label}: {code} @ {date} ━━━━━━━━━━')
        print(f'  期望: {expect}')
        text = get_text(code, date)
        if not text:
            print('  ✗ no cached text\n'); continue

        print(f'  [v9.1] 调用中...', flush=True)
        rep1, m1 = call_pro(PROMPT_V91, text)
        v1 = extract(rep1) if 'ERROR' not in rep1[:20] else {}
        if 'ERROR' in rep1[:20]:
            print(f'    ✗ {rep1[:200]}')
        else:
            print(f'    [{m1["time"]:.0f}s] {fmt(v1)}')
            print(f'    reason: {v1.get("reason","")[:120]}')

        print(f'\n  [v10] 调用中...', flush=True)
        rep2, m2 = call_pro(PROMPT_V10, text)
        v2 = extract(rep2) if 'ERROR' not in rep2[:20] else {}
        if 'ERROR' in rep2[:20]:
            print(f'    ✗ {rep2[:200]}')
        else:
            print(f'    [{m2["time"]:.0f}s] {fmt(v2)}')
            print(f'    reason: {v2.get("reason","")[:120]}')

        results.append({'label':label,'code':code,'date':date,
                        'v91':v1,'v10':v2,'v91_meta':m1,'v10_meta':m2,
                        'v91_report':rep1[:6000],'v10_report':rep2[:6000]})
        with open('/tmp/v10_vs_v91_compare.json','w') as f:
            json.dump(results, f, ensure_ascii=False, indent=2, default=str)
        print()

    print('\n═══ 总结 ═══')
    print(f'{"Case":12} {"v9.1":40} {"v10":40}')
    print('─' * 96)
    for r in results:
        def s(v):
            return f"{v.get('direction','?')} {v.get('phase','?')} w={v.get('warning_phase','') or '─'}"[:40]
        print(f'{r["label"]:12} {s(r["v91"]):40} {s(r["v10"]):40}')
    print(f'\n[saved] /tmp/v10_vs_v91_compare.json')


if __name__ == '__main__':
    main()
