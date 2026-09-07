"""Answer first, then judge.

In self-play experiments a judge's pass rate climbed 0.72 → 0.94 while
true accuracy sat flat at 0.20 — a 0.74 gap between what the judge
accepted and what was correct. Ensembling three judges still accepted 55%
of cheating answers. The one intervention that worked was making the
judge commit to its *own* answer before seeing the candidate: false
positives fell from 0.72 to 0.012.

The mechanism is simple. A judge shown only a candidate evaluates
plausibility, and a fluent wrong answer is plausible. A judge that has
already written down what it thinks the answer is has something concrete
to disagree with, so a mismatch becomes visible instead of being smoothed
over.

This matters most where the model reviews its own prior output — the
review task's 今日做对/做错 summary, whose lessons become principles. See
docs/self_improvement_roadmap.md G5.
"""

import logging

logger = logging.getLogger(__name__)

# Prepended to any prompt where a model assesses a candidate answer,
# including one of its own.
INDEPENDENT_FIRST_INSTRUCTION = """在评价任何既有结论之前，你必须先独立作答。

第一步 — 独立判断（不要看、不要引用下方已有的结论）：
仅根据给出的**客观数据**（行情、资金流、成交记录、评分结果），写出你自己
的判断。这一步不允许出现"我之前认为""原推荐"之类的字样。

第二步 — 对照：
把你第一步的独立判断与既有结论逐条对比，明确指出**分歧在哪里**。

第三步 — 结论：
只有在第一、二步完成后，才能给出评价。若你的独立判断与既有结论一致，说明
理由；若不一致，以**数据**为准，不要为既有结论找补。

严禁跳过第一步直接评价。"""


def wrap_judge_prompt(base_prompt: str) -> str:
    """Prefix a judging prompt with the answer-first requirement."""
    return f"{INDEPENDENT_FIRST_INSTRUCTION}\n\n---\n\n{base_prompt}"


def split_judgement(response: str) -> dict:
    """Separate the independent answer from the assessment that follows.

    Returns {"independent", "comparison", "verdict", "complied"}.
    ``complied`` is False when the model skipped straight to a verdict,
    which is exactly the failure this guard exists to catch — callers
    should treat a non-compliant response as unreliable rather than
    quietly using it.
    """
    if not response:
        return {"independent": "", "comparison": "", "verdict": "",
                "complied": False}

    markers = [
        ("independent", ("第一步", "独立判断")),
        ("comparison", ("第二步", "对照")),
        ("verdict", ("第三步", "结论")),
    ]

    positions: list[tuple[int, str]] = []
    for key, needles in markers:
        idx = min((response.find(n) for n in needles if response.find(n) >= 0),
                  default=-1)
        if idx >= 0:
            positions.append((idx, key))
    positions.sort()

    if not positions:
        # No structure at all — the model ignored the instruction.
        return {"independent": "", "comparison": "", "verdict": response,
                "complied": False}

    out = {"independent": "", "comparison": "", "verdict": ""}
    for i, (start, key) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(response)
        out[key] = response[start:end].strip()

    complied = bool(out["independent"].strip())
    if not complied:
        logger.warning(
            "Judge response skipped the independent step — treating as "
            "unreliable (first 120 chars: %s)", response[:120],
        )
    out["complied"] = complied
    return out
