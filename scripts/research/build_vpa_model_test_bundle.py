"""Generate paste-ready VPA test bundles for cross-model evaluation.

For each (code, date) test case, produces a self-contained markdown file:
  - System prompt (ANNA_COULLING_PROMPT)
  - User message (VPA precomputed data via _format_text, as-of cutoff)
  - Hidden ground truth (forward 5d/10d/20d close returns)
  - Scoring rubric tied to Anna Coulling's veto rules

Pure local: 0 LLM calls. Outputs into data/vpa_model_test/.

Usage:
    python scripts/build_vpa_model_test_bundle.py
"""

from __future__ import annotations

import csv
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from alpha_agents.tools.vpa import (
    ANNA_COULLING_PROMPT,
    _load_ohlcv, _compute_derived, _detect_patterns, _format_text,
)

OUT_DIR = REPO / "data/vpa_model_test"
DB = REPO / "data/market_history.db"

# Test cases: (case_id, code, as_of_date, scenario, expected_verdict_set, expected_phase_set, anna_veto_notes)
TEST_CASES = [
    {
        "id": "A",
        "code": "300058",
        "as_of": "2025-10-30",
        "scenario": "拉升中假派发陷阱（否决规则 #1）",
        "expected_phase_allowed": ["拉升", "震荡", "派发初期(仅 cfm=False)"],
        "expected_phase_forbidden": ["派发中期", "派发尾声", "抛售高峰", "下跌", "派发(任何 cfm=True 子阶段)"],
        "expected_verdict": ["看多", "偏多", "中性"],
        "anna_key": "本日仍在创 20 日新高（昨日 6.20 → 今 6.76），按 Anna 否决规则 #1：禁止任何派发子阶段判定。当日长上影 + 巨量是 markup 中的 absorption（吸筹方继续吃单），不是 distribution。BC 五条硬性条件中：① 延伸涨势 < 5 周 ② 收盘在上半区（不是下半区） ③ 无 AR 反向走出。皆不满足。",
    },
    {
        "id": "B",
        "code": "300058",
        "as_of": "2025-11-26",
        "scenario": "真正的顶部 BC/Distribution 形成",
        "expected_phase_allowed": ["派发", "派发初期", "派发中期"],
        "expected_phase_forbidden": ["拉升", "吸筹"],
        "expected_verdict": ["看空", "偏空"],
        "anna_key": "10 月起延伸涨势 ≥ 5 周满足前提；从 6.20 涨到 10.93 = +76%；当日 K 线高位放量长上影 + 收盘在 K 线下半区。**关键**：Anna 不会跳过 PSY/AR 直接喊'派发尾声'——应该是'派发初期'或'PSY 候选'。phase 选'派发尾声/抛售高峰' = 过度自信。",
    },
    {
        "id": "C",
        "code": "300058",
        "as_of": "2025-12-10",
        "scenario": "回调底部真吸筹/Spring 识别",
        "expected_phase_allowed": ["吸筹", "吸筹初期", "吸筹尾声", "卖压衰竭"],
        "expected_phase_forbidden": ["派发", "下跌", "拉升"],
        "expected_verdict": ["看多", "偏多", "中性"],
        "anna_key": "11-26 顶后跌至 8.74 = 从 10.93 回撤 -20%。今日缩量止跌，前几日有 Spring 假跌破特征。Anna 视角：这是 LPS 候选，应识别为 accumulation phase E。**非常容易被误判为'下跌延续'**——若模型喊'派发尾声'或'下跌中期'就明显错。",
    },
    {
        "id": "D",
        "code": "300058",
        "as_of": "2026-01-13",
        "scenario": "真终极顶 SC（Selling Climax 级反转）",
        "expected_phase_allowed": ["派发尾声", "抛售高峰", "下跌初期"],
        "expected_phase_forbidden": ["拉升", "吸筹"],
        "expected_verdict": ["看空", "偏空"],
        "anna_key": "极端情形：当日开盘 22.50 冲到 23.87，巨量长上影，收盘 20.85（下半区，反转-12% 当日）。延伸涨势 ≥ 12 周（从 6.20 → 23.87 = +285%）。BC 五条硬性条件：① 延伸涨势 ✓ ② 极端量（应当日 ≥ 20 日均量 3×）③ 宽幅 K（range 5.21 远超 5 日均）✓ ④ 收盘下半区 ✓ ⑤ AR 待确认。这是教科书级 BC——应高 confidence + cfm=True。",
    },
    {
        "id": "E",
        "code": "300058",
        "as_of": "2026-04-13",
        "scenario": "短期弱势 vs 派发判别（中等难度）",
        "expected_phase_allowed": ["拉升", "震荡", "派发初期(仅 cfm=False)"],
        "expected_phase_forbidden": ["派发中期", "派发尾声", "抛售高峰", "下跌"],
        "expected_verdict": ["中性", "偏多", "偏空"],
        "anna_key": "上涨后小幅回调，没有明确水平区间形成。Anna：这只是 markup 中的正常消化，不是派发。模型若过度自信喊'派发尾声 cfm=True'就是典型陷阱。",
    },
]

MODELS_TO_TEST = [
    "Claude Sonnet 4.5", "Claude Opus 4",
    "GPT-5", "GPT-4o",
    "Gemini 2.5 Pro",
    "DeepSeek V3.1",
    "Qwen Max", "Qwen Plus",
    "LongCat-Flash",
    "Doubao Pro 1.5",
    "Kimi K2",
    "MiniMax M1",
]


def get_forward_returns(code: str, as_of: str) -> dict:
    conn = sqlite3.connect(str(DB))
    rows = conn.execute(
        "SELECT date, open, high, low, close FROM daily_kline "
        "WHERE code=? AND date >= ? ORDER BY date LIMIT 25",
        (code, as_of),
    ).fetchall()
    if not rows:
        return {}
    base_close = float(rows[0][4])
    out = {"base_date": rows[0][0], "base_close": base_close}
    for h, label in [(5, "fwd5d"), (10, "fwd10d"), (20, "fwd20d")]:
        if h < len(rows):
            out[label] = {
                "date": rows[h][0],
                "close": float(rows[h][4]),
                "ret_pct": (float(rows[h][4]) - base_close) / base_close * 100,
            }
    return out


def build_case(case: dict) -> str:
    code = case["code"]
    as_of = case["as_of"]

    df = _load_ohlcv(code, days=60, include_realtime=False, as_of=as_of)
    if df is None:
        raise RuntimeError(f"no data for {code} as_of={as_of}")
    df = _compute_derived(df, window=20)
    patterns = _detect_patterns(df)
    vpa_text = _format_text(code, "", df, patterns, window=20)

    fwd = get_forward_returns(code, as_of)

    md = []
    md.append(f"# VPA Model Test — Case {case['id']}（{code} / {as_of}）")
    md.append(f"\n**测试场景**：{case['scenario']}\n")
    md.append("## 使用方法\n")
    md.append("1. 把下面 **【系统提示词】** 区块整段粘贴到 chat 的 system 角色")
    md.append("   （或如果模型不支持 system，作为首条消息）")
    md.append("2. 等模型确认 OK 后，把 **【用户消息】** 区块整段粘贴")
    md.append("3. 模型给出完整 VPA 报告（应包含 `<!-- VERDICT: {...} -->` JSON 块）")
    md.append("4. 翻到 **【评分参考】** 对照打分（看完前不要先看）\n")

    md.append("---\n")
    md.append("## 【系统提示词】（粘贴这段）\n")
    md.append("````\n" + ANNA_COULLING_PROMPT + "\n````\n")

    md.append("---\n")
    md.append("## 【用户消息】（粘贴这段）\n")
    md.append("````\n以下是 " + code + " 的量价预计算数据，请按 Anna Coulling 理论做完整分析：\n\n" + vpa_text + "\n````\n")

    md.append("---\n")
    md.append("## 【评分参考】（盖住，先看模型答案再翻）\n")
    md.append("<details>\n<summary>点击展开评分参考</summary>\n")
    md.append(f"\n### Anna Coulling 视角的关键判断点\n")
    md.append(case["anna_key"])
    md.append(f"\n\n### 允许的 phase 答案\n")
    for p in case["expected_phase_allowed"]:
        md.append(f"- ✓ {p}")
    md.append(f"\n### 禁止的 phase 答案（出现即扣分）\n")
    for p in case["expected_phase_forbidden"]:
        md.append(f"- ✗ {p}")
    md.append(f"\n### 允许的 verdict（看多/偏多/中性/偏空/看空）\n")
    md.append(f"- {' / '.join(case['expected_verdict'])}")

    md.append(f"\n\n### 实际后续走势（ground truth）\n")
    md.append(f"- 信号日 ({fwd.get('base_date')}) 收盘: {fwd.get('base_close'):.2f}")
    for k in ("fwd5d", "fwd10d", "fwd20d"):
        if k in fwd:
            v = fwd[k]
            md.append(f"- {k} ({v['date']}) 收盘: {v['close']:.2f}  "
                      f"**{v['ret_pct']:+.1f}%**")

    md.append(f"\n### 评分（每项 1 分，总分 4）\n")
    md.append("| 维度 | 评分 | 说明 |")
    md.append("|---|---|---|")
    md.append("| **phase 在允许列表内** | __/1 | 出现禁止的 phase = 0；只在允许的 phase 内 = 1 |")
    md.append("| **verdict 在允许列表内** | __/1 | 方向相反 = 0 |")
    md.append("| **confidence 校准合理** | __/1 | 错的方向却 conf > 0.7 = 0；模糊场景给低 conf = 1 |")
    md.append("| **reason 引用 Anna 关键概念** | __/1 | 提到趋势前提/AR 确认/5 硬条件/否决规则 = 1 |")
    md.append(f"\n### 模型回答记录区\n")
    md.append("```\n[在此粘贴模型给的 VERDICT JSON 块]\n```\n")

    md.append("\n</details>\n")
    return "\n".join(md)


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    for case in TEST_CASES:
        path = OUT_DIR / f"case_{case['id']}_{case['code']}_{case['as_of'].replace('-', '')}.md"
        path.write_text(build_case(case), encoding="utf-8")
        print(f"  ✓ {path.name}  ({path.stat().st_size // 1024} KB)")

    # Build scoring CSV template
    csv_path = OUT_DIR / "scoring_template.csv"
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["model", "case", "code", "as_of", "scenario",
                    "phase_score", "verdict_score", "conf_score", "reason_score",
                    "total", "model_phase", "model_verdict", "model_conf", "model_cfm",
                    "notes"])
        for model in MODELS_TO_TEST:
            for case in TEST_CASES:
                w.writerow([model, case["id"], case["code"], case["as_of"],
                            case["scenario"], "", "", "", "", "", "", "", "", "", ""])
    print(f"\n  ✓ {csv_path.name}  ({len(MODELS_TO_TEST)} 模型 × {len(TEST_CASES)} cases = "
          f"{len(MODELS_TO_TEST) * len(TEST_CASES)} 行待填)")

    readme = OUT_DIR / "README.md"
    readme.write_text(f"""# VPA Model Test Bundle

5 个测试 case 用同一只股票（300058，避免股票特异性干扰），覆盖 5 类典型场景：

| Case | 日期 | 场景 |
|---|---|---|
| A | 2025-10-30 | 拉升中假派发陷阱（否决规则 #1）|
| B | 2025-11-26 | 真正的顶部 BC/Distribution 形成 |
| C | 2025-12-10 | 回调底部真吸筹/Spring 识别 |
| D | 2026-01-13 | 真终极顶 SC（Selling Climax 级反转）|
| E | 2026-04-13 | 短期弱势 vs 派发判别（中等难度）|

## 使用流程

1. 打开任一 `case_*.md`，按文件内说明粘贴到目标模型的 chat
2. 记录模型完整回答（特别是 VERDICT JSON 块）
3. 翻到文件底部的【评分参考】对照打分
4. 把分数填到 `scoring_template.csv` 对应行

## 测试模型清单

预填好的模型行：
{chr(10).join('- ' + m for m in MODELS_TO_TEST)}

可以选你能访问的子集测试。每个 case 总分 4，每模型 5 case 满分 20。

## 输出对比

跑完后用 pivot 看每模型在每 case 的得分，识别：
- 哪个模型最贴近 Anna 的标准
- 哪类场景所有模型都易错（说明 prompt 还需加强）
- 哪个模型最适合作为 VPA 主模型
""", encoding="utf-8")
    print(f"  ✓ {readme.name}")
    print(f"\n输出目录: {OUT_DIR}")


if __name__ == "__main__":
    main()
