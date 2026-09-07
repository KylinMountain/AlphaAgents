"""v9.1 prompt vs v9.2 prompt 对比 (v4-pro 模型).

v9.2 修订 3 处:
  1. 罪证 1 (vph 硬否决): 量价仍 bullish 不再否决派发判定
  2. 罪证 2 (phase 切换硬门槛): 改为递进 confirmation
  3. 新增 successful supply test 章节 (markup 中的 "absorbed test")

测试集:
  - 4 个核心 case (300318, 002135, 000547, 603358)
  - 3 个干扰 case (clean markup, 应保持 看多, v9.2 不应误升派发)

每个 case 用相同 user_text, 同时跑两个 prompt, 对比 verdict/phase/warning.
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

# Test cases — 4 core + 3 interference
CASES = [
    # (label, code, date, expectation_v91, expectation_v92)
    ('A1-真跌', '300318', '2025-11-18',
     '偏多+派发预警 (markup-bias 锁死)',
     '派发初期 confirmed=false (BC 14 已现, AR 15-17 已现)'),
    ('A2-真跌', '002135', '2026-03-19',
     '中性+派发预警 (BC候选+SOW)',
     '派发初期 confirmed=false (升级)'),
    ('B1-错警', '000547', '2025-11-25',
     '偏多+派发预警 (3/4 BC 误警)',
     '拉升 + absorbed_supply_test (11-20 是吸纳测试)'),
    ('B2-错警', '603358', '2025-11-07',
     '看多 L3 (本来就对)',
     '看多 L3 (不应回归)'),
    # Interference: clean markup, should stay bullish
    ('干扰-1', '688106', '2025-11-07',
     '看多 L3 拉升 (clean)',
     '看多 L3 拉升 (不应误升派发)'),
    ('干扰-2', '603358', '2025-12-25',
     '看多 L3 拉升 (clean)',
     '看多 L3 拉升 (不应误升派发)'),
    ('干扰-3', '600382', '2026-01-21',
     '看多 L3 拉升 (clean)',
     '看多 L3 拉升 (不应误升派发)'),
]


# ===================== v9.2 prompt =====================

def make_v92_prompt() -> str:
    """Apply 3 edits to v9.1 prompt."""
    p = ANNA_COULLING_PROMPT

    # ---- Edit 1: 放松 vph 硬否决 ----
    edit1_old = "- **量价配合仍是 bullish（涨日放量、跌日缩量）时标派发**——这违反第一层判据"
    edit1_new = (
        "- **v9.2 修订 (markup-bias 修正)**：仅凭 5 日 vph 仍是 bullish 不否决派发判定:\n"
        "  • BC 5 条全满足 + AR 已现 (≥ 2 根 K 线反向走出 ≥ 该股近期 ATR) → `phase=派发初期 confirmed=false` 即可切换, vph 不强制 bearish\n"
        "  • vph 转 bearish 是派发**中期/尾声**的进阶证据, 不是**初期**的门槛\n"
        "  • 仅单根 PSY/放量上影 + 强 bullish vph + BC 形态不全 → 保留 `拉升 + warning_phase=派发预警`, 不切 phase"
    )
    if edit1_old not in p:
        raise RuntimeError("Edit 1: anchor text not found in v9.1 prompt")
    p = p.replace(edit1_old, edit1_new)

    # ---- Edit 2: 递进式 phase 切换 ----
    edit2_old = "- `拉升 → 派发`：必须有已延伸涨势 + 买入高潮顶部（BC）并出现后续自动回落，或多个初步供应（PSY）+ bearish 量价配合；创新高本身不否定派发，单日放量上影只能是初步供应/卖压预警。"
    edit2_new = (
        "- `拉升 → 派发` (v9.2 递进 confirmation):\n"
        "  • `派发初期 confirmed=false`: 已延伸涨势 + BC 5 条满足 + AR 已现. **vph 状态不限制**\n"
        "  • `派发初期 confirmed=true`: 上述 + 5 日 vph 转 bearish (涨日缩量/跌日放量)\n"
        "  • `派发中期`: + ST 出现 + trading range 触及 ≥ 2 次\n"
        "  创新高本身不否定派发, 单日放量上影只能是初步供应/卖压预警."
    )
    if edit2_old not in p:
        raise RuntimeError("Edit 2: anchor text not found in v9.1 prompt")
    p = p.replace(edit2_old, edit2_new)

    # ---- Edit 3: 新增 successful supply test 章节 ----
    # 插入到 markup section 中段消化表后
    edit3_marker = "**关键**：仅看价格是否创新高**不能**区分两者——markup 中段消化往往最终也会创新高，而 BC 那天本身就在创新高。要看**量价配合方向 + climactic 形态**。"
    edit3_addition = (
        "\n\n#### B.1 markup 中段的成功测试 (Successful Supply Test) — v9.2 新增\n"
        "\n"
        "**形态**: 巨量 + 宽幅 + 长**下**影 ≥ 实体 + **收盘在 K 线上半区** (与 BC 上下影对称, 但收盘方向相反)\n"
        "\n"
        "**Anna 解读**: 盘中卖压被买盘吸纳 (\"absorbed supply test\"), markup 强化信号. 严格区分:\n"
        "- 上影 + 收**下**半区 = 卖方占优 → BC 候选\n"
        "- 下影 + 收**上**半区 = 买方吸收 → markup 继续, **不是 BC**\n"
        "\n"
        "**处理**: \n"
        "- `phase=拉升`, signals 加 `absorbed_supply_test=true`\n"
        "- `warning_phase` 留空, 不要标 派发预警\n"
        "- 后续 1-2 根 K 线大概率延续上行\n"
        "\n"
        "**严禁误判**: 收上半区的巨量长下影**不是 BC 候选**. 收盘方向决定本质. 这是 v9.2 修正的常见错误模式 (000547 11-20 误警案例)."
    )
    if edit3_marker not in p:
        raise RuntimeError("Edit 3: anchor text not found in v9.1 prompt")
    p = p.replace(edit3_marker, edit3_marker + edit3_addition)

    return p


PROMPT_V91 = ANNA_COULLING_PROMPT
PROMPT_V92 = make_v92_prompt()


# ===================== helpers =====================

def get_cached_text(code: str, date: str) -> str | None:
    with open('/tmp/anna_v91_v3_cache.jsonl') as f:
        for line in f:
            obj = json.loads(line)
            if obj['code'] == code and obj['date'] == date:
                return obj['result'].get('text', '')
    return None


def call_v4_pro(system_prompt: str, user_text: str) -> tuple[str, dict]:
    client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
    t0 = time.time()
    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=[
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_text},
            ],
            max_tokens=12000,
            timeout=180,
            extra_body={'enable_thinking': True},
        )
        dt = time.time() - t0
        return resp.choices[0].message.content, {
            'time': dt,
            'tokens': resp.usage.total_tokens if resp.usage else 0,
        }
    except Exception as e:
        return f'ERROR: {type(e).__name__}: {str(e)[:200]}', {'time': time.time() - t0}


def extract_verdict(report: str) -> dict:
    if not report:
        return {}
    m = re.search(r'<!--\s*VERDICT:\s*(\{.*?\})\s*-->', report, re.DOTALL)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except Exception:
        return {}


def fmt_verdict(v: dict) -> str:
    if not v:
        return '(no VERDICT)'
    direction = v.get('direction', '?')
    phase = v.get('phase', '?')
    warn = v.get('warning_phase', '') or ''
    pc = v.get('phase_change', {}) or {}
    pc_str = ''
    if pc.get('confirmed') is not None:
        pc_str = f" pc={pc.get('from','')}→{pc.get('to','')} confirmed={pc.get('confirmed')}"
    sig_names = [s.get('name', '') for s in (v.get('signals', []) or [])]
    sig_str = f' signals={sig_names[:3]}' if sig_names else ''
    return f'{direction} phase={phase!r} warn={warn!r}{pc_str}{sig_str}'


# ===================== main =====================

def main():
    print(f'═══ v9.1 vs v9.2 prompt 对比 (model={MODEL}) ═══\n')
    print(f'v9.1 prompt: {len(PROMPT_V91)} chars')
    print(f'v9.2 prompt: {len(PROMPT_V92)} chars (+{len(PROMPT_V92) - len(PROMPT_V91)} chars)\n')

    results = []
    for label, code, date, exp91, exp92 in CASES:
        print(f'━━━━━━━━━━ {label}: {code} @ {date} ━━━━━━━━━━')
        print(f'  期望 v9.1: {exp91}')
        print(f'  期望 v9.2: {exp92}')

        text = get_cached_text(code, date)
        if not text:
            print(f'  ✗ no cached text\n')
            continue
        print(f'  user_text: {len(text)} chars\n')

        # v9.1 prompt
        print(f'  [v9.1 prompt] 调用中...', flush=True)
        rep1, meta1 = call_v4_pro(PROMPT_V91, text)
        v1 = extract_verdict(rep1) if 'ERROR' not in rep1[:20] else {}
        if 'ERROR' in rep1[:20]:
            print(f'    ✗ {rep1[:200]}')
        else:
            print(f'    [{meta1["time"]:.0f}s, {meta1.get("tokens","?")}t]')
            print(f'    verdict: {fmt_verdict(v1)}')
            print(f'    reason : {v1.get("reason","")[:120]}')

        # v9.2 prompt
        print(f'\n  [v9.2 prompt] 调用中...', flush=True)
        rep2, meta2 = call_v4_pro(PROMPT_V92, text)
        v2 = extract_verdict(rep2) if 'ERROR' not in rep2[:20] else {}
        if 'ERROR' in rep2[:20]:
            print(f'    ✗ {rep2[:200]}')
        else:
            print(f'    [{meta2["time"]:.0f}s, {meta2.get("tokens","?")}t]')
            print(f'    verdict: {fmt_verdict(v2)}')
            print(f'    reason : {v2.get("reason","")[:120]}')

        results.append({
            'label': label, 'code': code, 'date': date,
            'expected_v91': exp91, 'expected_v92': exp92,
            'v91_verdict': v1, 'v91_meta': meta1, 'v91_report': rep1[:6000],
            'v92_verdict': v2, 'v92_meta': meta2, 'v92_report': rep2[:6000],
        })

        # Save incrementally
        with open('/tmp/v92_prompt_compare.json', 'w') as f:
            json.dump(results, f, ensure_ascii=False, indent=2, default=str)

        print()

    # Final summary
    print('\n═══ 总结 ═══')
    print(f'{"Case":12} {"v9.1":40} {"v9.2":40}')
    print('─' * 95)
    for r in results:
        v1 = r['v91_verdict']
        v2 = r['v92_verdict']
        v1_summary = f'{v1.get("direction","?")} {v1.get("phase","?")} w={v1.get("warning_phase","") or "─"}'
        v2_summary = f'{v2.get("direction","?")} {v2.get("phase","?")} w={v2.get("warning_phase","") or "─"}'
        print(f'{r["label"]:12} {v1_summary[:40]:40} {v2_summary[:40]:40}')

    print(f'\n[saved] /tmp/v92_prompt_compare.json')


if __name__ == '__main__':
    main()
