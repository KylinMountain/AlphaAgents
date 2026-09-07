"""v9.3 prompt vs v9.1 baseline (v4-pro 模型).

v9.3 对 v9.2 的两处修正:
  Edit 1 修订: 删掉"仅单根 PSY/放量上影 + 强 bullish vph + BC 不全 → 保留拉升+派发预警"
              → 这条让 healthy markup (B2 603358 11-07) 被错加 warning, 删掉
  Edit 3 修订: 把 absorbed_test 上下影口诀提到顶层"重要原则"区
              + 加 absorbed_test 后 5 bar override 规则 (强化 B1 修复)

测试集同 v9.2 (4 核 + 3 干扰).
"""
from __future__ import annotations
import json
import os
import re
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

from openai import OpenAI

from alpha_agents.tools.vpa.llm import ANNA_COULLING_PROMPT

API_KEY = os.getenv('DEEPSEEK_API_KEY')
BASE_URL = os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com/v1')
MODEL = 'deepseek-v4-pro'

CASES = [
    ('A1-真跌', '300318', '2025-11-18',
     'v9.1+pro 已对: 偏空 派发初期 confirmed=true',
     'v9.3 不应回退'),
    ('A2-真跌', '002135', '2026-03-19',
     'v9.1: 偏多+派发预警 (markup-bias 锁死)',
     'v9.3: 派发初期 confirmed=false (升级但不到 SOW)'),
    ('B1-错警', '000547', '2025-11-25',
     'v9.1: 偏多+派发预警',
     'v9.3: 拉升 (不警, 因 11-20 absorbed test override)'),
    ('B2-错警', '603358', '2025-11-07',
     'v9.1: 偏多 拉升 (本来就对)',
     'v9.3: 看多/偏多 拉升 (不应加 warning)'),
    ('干扰-1', '688106', '2025-11-07',
     'v9.1+pro 大错: 偏空 下跌',
     'v9.3: 看多 拉升 (这次也修?)'),
    ('干扰-2', '603358', '2025-12-25', 'v9.1: 偏多 拉升 ✓', '看多 拉升'),
    ('干扰-3', '600382', '2026-01-21', 'v9.1: 看多 拉升 ✓', '看多 拉升'),
]


def make_v93_prompt() -> str:
    p = ANNA_COULLING_PROMPT

    # ---- Edit 1 (v9.3): 只保留 "vph 不否决" 核心, 删掉副作用 bullet ----
    edit1_old = "- **量价配合仍是 bullish（涨日放量、跌日缩量）时标派发**——这违反第一层判据"
    edit1_new = (
        "- **vph 不否决派发判定 (markup-bias 修正, v9.3)**: 5 日量价配合仍 bullish 不能用来否决派发. \n"
        "  • BC 5 条全满足 + AR 已现 (≥ 2 根 K 线反向走出 ≥ 该股近期 ATR) → `phase=派发初期 confirmed=false`, vph 不强制 bearish\n"
        "  • vph 转 bearish 是派发**中期/尾声**的进阶证据, 不是**初期**的门槛\n"
        "  • healthy markup 中孤立 PSY (无 BC, 无 AR) 应保持 `phase=拉升 warning_phase=''`, 不要因为有 PSY 就加预警"
    )
    if edit1_old not in p:
        raise RuntimeError("Edit 1: anchor not found")
    p = p.replace(edit1_old, edit1_new)

    # ---- Edit 2 (v9.3): 加保险 — 单根 K 线不能跳两级 ----
    edit2_old = "- `拉升 → 派发`：必须有已延伸涨势 + 买入高潮顶部（BC）并出现后续自动回落，或多个初步供应（PSY）+ bearish 量价配合；创新高本身不否定派发，单日放量上影只能是初步供应/卖压预警。"
    edit2_new = (
        "- `拉升 → 派发` (v9.3 递进, 严禁跳级):\n"
        "  • `派发初期 confirmed=false`: 已延伸涨势 + BC 5 条满足 + AR 已现. vph 状态不限制.\n"
        "  • `派发初期 confirmed=true`: 上述 + 5 日 vph 已转 bearish (涨日缩量/跌日放量).\n"
        "  • `派发中期`: 上述 + ST 出现 + trading range 触及 ≥ 2 次.\n"
        "  • `派发尾声/SOW confirmed=true`: 上述 + 放量阴线**跌破 AR 低点 ≥ 3%**.\n"
        "  **严禁单根放量阴线直接判 SOW** — 必须先经派发初期/中期累积. 创新高本身不否定派发, 单日放量上影只能是初步供应预警."
    )
    if edit2_old not in p:
        raise RuntimeError("Edit 2: anchor not found")
    p = p.replace(edit2_old, edit2_new)

    # ---- Edit 3 (v9.3): 顶层"重要原则" + absorbed_test override ----
    # 找早期 "重要原则" 段(关键 K 线信号 -> 重要原则)
    edit3_marker = ("### 重要原则\n"
                    "**任何单根 K 线形态都只是\"候选信号\"，必须由后续 1-3 根 K 线的价格结构验证（破/守支撑、更高高点/更高低点序列形成）才能升级为可执行信号。**")
    edit3_addition = (
        "\n\n**v9.3 新增 — 上下影 + 收盘方向决定本质 (BC vs absorbed test)**:\n"
        "\n"
        "巨量宽幅 K 线的"
        "**上影 vs 下影 + 收盘位置**直接决定 Anna 解读, 不可混淆:\n"
        "\n"
        "| 形态 | 上影 vs 下影 | 收盘位置 | Anna 解读 | 阶段处理 |\n"
        "|---|---|---|---|---|\n"
        "| BC 候选 | 长**上**影 ≥ 实体 | 收盘在**下**半区 (close_pos < 0.4) | 卖方在高位倾倒, 主力派发 | `phase=派发初期 confirmed=false` (待 AR) |\n"
        "| Successful Supply Test | 长**下**影 ≥ 实体 | 收盘在**上**半区 (close_pos > 0.6) | 盘中卖压被买方吸纳, markup 强化 | `phase=拉升`, signals 加 `absorbed_supply_test=true`, **不要加 warning_phase** |\n"
        "\n"
        "**override 规则**: 若近 5 根 K 线内出现 confirmed `absorbed_supply_test`, 后续 5 根 K 线的 BC 候选门槛**升级**: \n"
        "- 单纯 PSY (放量上影) 不足以触发 `warning_phase=派发预警`\n"
        "- 必须见到完整 BC (5 条全满足) + 后续 1-2 根 K 线 AR 反向走出, 才允许 warning\n"
        "- 这是 Anna 的核心原则: 一次成功的供应测试已证明买方控盘, 后续单根可疑 K 线应保持 markup 解读\n"
        "\n"
        "**典型误判**: 巨量长**下**影 + 收**上**半区 错标 BC 候选 (case: 000547 2025-11-20). 收盘方向决定方向, 不要混淆."
    )
    if edit3_marker not in p:
        raise RuntimeError("Edit 3: anchor not found")
    p = p.replace(edit3_marker, edit3_marker + edit3_addition)

    return p


PROMPT_V91 = ANNA_COULLING_PROMPT
PROMPT_V93 = make_v93_prompt()


def get_cached_text(code, date):
    with open('/tmp/anna_v91_v3_cache.jsonl') as f:
        for line in f:
            obj = json.loads(line)
            if obj['code'] == code and obj['date'] == date:
                return obj['result'].get('text', '')
    return None


def call_v4_pro(system_prompt, user_text):
    client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
    t0 = time.time()
    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_text},
            ],
            max_tokens=12000, timeout=180, extra_body={'enable_thinking': True},
        )
        dt = time.time() - t0
        return resp.choices[0].message.content, {'time': dt, 'tokens': resp.usage.total_tokens if resp.usage else 0}
    except Exception as e:
        return f'ERROR: {type(e).__name__}: {str(e)[:200]}', {'time': time.time() - t0}


def extract_verdict(report):
    if not report: return {}
    m = re.search(r'<!--\s*VERDICT:\s*(\{.*?\})\s*-->', report, re.DOTALL)
    if not m: return {}
    try: return json.loads(m.group(1))
    except: return {}


def fmt_verdict(v):
    if not v: return '(no VERDICT)'
    pc = v.get('phase_change', {}) or {}
    pc_str = f" pc={pc.get('from','')}→{pc.get('to','')} c={pc.get('confirmed')}" if pc.get('confirmed') is not None else ''
    sigs = [s.get('name', '')[:15] for s in (v.get('signals', []) or [])][:3]
    return f"{v.get('direction','?')} phase={v.get('phase','?')!r} warn={(v.get('warning_phase','') or '─')!r}{pc_str} sigs={sigs}"


def main():
    print(f'═══ v9.1 vs v9.3 prompt 对比 (model={MODEL}) ═══\n')
    print(f'v9.1 prompt: {len(PROMPT_V91)} chars')
    print(f'v9.3 prompt: {len(PROMPT_V93)} chars (+{len(PROMPT_V93) - len(PROMPT_V91)})\n')

    # Load v9.2 results to compare side-by-side
    v92_results = {}
    try:
        with open('/tmp/v92_prompt_compare.json') as f:
            for r in json.load(f):
                v92_results[(r['code'], r['date'])] = r['v92_verdict']
    except FileNotFoundError:
        pass

    results = []
    for label, code, date, exp91, exp93 in CASES:
        print(f'━━━━━━━━━━ {label}: {code} @ {date} ━━━━━━━━━━')
        print(f'  v9.1 期望: {exp91}')
        print(f'  v9.3 期望: {exp93}')

        text = get_cached_text(code, date)
        if not text:
            print('  ✗ no cached text\n'); continue

        # v9.1
        print(f'  [v9.1] 调用中...', flush=True)
        rep1, m1 = call_v4_pro(PROMPT_V91, text)
        v1 = extract_verdict(rep1) if 'ERROR' not in rep1[:20] else {}
        if 'ERROR' in rep1[:20]:
            print(f'    ✗ {rep1[:200]}')
        else:
            print(f'    [{m1["time"]:.0f}s] {fmt_verdict(v1)}')
            print(f'    reason: {v1.get("reason","")[:120]}')

        # v9.3
        print(f'\n  [v9.3] 调用中...', flush=True)
        rep3, m3 = call_v4_pro(PROMPT_V93, text)
        v3 = extract_verdict(rep3) if 'ERROR' not in rep3[:20] else {}
        if 'ERROR' in rep3[:20]:
            print(f'    ✗ {rep3[:200]}')
        else:
            print(f'    [{m3["time"]:.0f}s] {fmt_verdict(v3)}')
            print(f'    reason: {v3.get("reason","")[:120]}')

        # v9.2 reference
        v2 = v92_results.get((code, date), {})
        if v2:
            print(f'\n  [v9.2 参考] {fmt_verdict(v2)}')

        results.append({'label': label, 'code': code, 'date': date,
                        'v91': v1, 'v93': v3, 'v92': v2,
                        'v91_meta': m1, 'v93_meta': m3,
                        'v91_report': rep1[:6000], 'v93_report': rep3[:6000]})

        with open('/tmp/v93_prompt_compare.json', 'w') as f:
            json.dump(results, f, ensure_ascii=False, indent=2, default=str)

        print()

    # Summary
    print('\n═══ 总结 (v9.1 vs v9.2 vs v9.3) ═══')
    print(f'{"Case":12} {"v9.1":34} {"v9.2":34} {"v9.3":34}')
    print('─' * 116)
    for r in results:
        def short(v):
            d = v.get('direction', '?')
            p = v.get('phase', '?')
            w = v.get('warning_phase', '') or '─'
            return f'{d} {p} w={w}'[:34]
        print(f'{r["label"]:12} {short(r["v91"]):34} {short(r.get("v92", {})):34} {short(r["v93"]):34}')

    print(f'\n[saved] /tmp/v93_prompt_compare.json')


if __name__ == '__main__':
    main()
