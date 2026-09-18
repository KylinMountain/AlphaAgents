#!/usr/bin/env python
"""Policy contamination: judgement wearing the clothes of fact.

The repository has exactly one door through which a claim about markets may
become behaviour: ``candidate → evidence → holdout → human approval →
policy version``. Everything else is an assertion someone typed.

That door is bypassed when a tool's docstring says ``ROE > 15% = 优质企业``
or a prompt says ``价格是结果，资金是原因``. The agent reads those as facts
about the world, and no evidence ever evaluated them. They are hypotheses
that acquired the status of truth by being written down, which is precisely
the failure mode ``docs/GOLDEN_PRINCIPLES.md`` exists to prevent.

Two kinds of text are checked:

``prompts/``
    The instructions the agent actually reads. A policy statement here
    reaches every decision.

``tools/`` docstrings and returned advice
    A tool that answers ``recommendation: 可介入`` has made the decision
    the trader was supposed to make — and made it with a rule nobody
    versioned.

**What this is not.** It is not a ban on prose, and not a ban on those
specific words. ``morning_scan.md`` legitimately asks for an ``action``
column — that is an *output format* the model fills in, not a rule the
system asserts. The distinction the checker draws is: does this text tell
the agent what is true about markets (contaminated), or does it tell the
agent what shape to answer in (fine)? The pattern list below is the
mechanical approximation, and it is deliberately blunt — a false positive
is a line added to the baseline with a reason, which is cheaper than a
policy that quietly became a law.

Usage::

    python scripts/lint_policy.py            # check, fail on anything new
    python scripts/lint_policy.py --show     # list every finding
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BASELINE_PATH = REPO / "scripts" / "lint_policy_baseline.txt"

#: Where policy statements may hide. Prompts are read by the agent verbatim;
#: tool docstrings are read by the model when it decides whether to call.
SCAN_ROOTS = (
    REPO / "alpha_agents" / "prompts",
    REPO / "alpha_agents" / "tools",
)

#: Phrases that assert something about markets rather than report a
#: measurement. Each entry is (pattern, why it is a claim).
PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"=\s*(优质|高风险|乐观|悲观|利好|利空)"),
     "equates a numeric threshold with a verdict"),
    (re.compile(r"适合看多|适合做多|适合抄底"),
     "tells the agent which side to take"),
    (re.compile(r"操作建议|actionable recommendation|recommendation[\"'：:\s]"),
     "hands the agent a conclusion instead of the inputs"),
    (re.compile(r"可介入|轻仓试探|建议回避|建议观望|等回调|逢低|逢高"),
     "names an action rather than a fact"),
    (re.compile(r"是原因|是结果|本质上|归根结底"),
     "asserts a causal theory of the market"),
    (re.compile(r"最重要的(情绪)?指标|最核心的|唯一有效的"),
     "ranks evidence by an unstated rule"),
    (re.compile(r"是[^，。；]{0,8}驱动的|由[^，。；]{0,8}驱动"),
     "states a market mechanism as settled"),
    (re.compile(r"大资金(看好|撤退)|主力(看好|撤退)"),
     "attributes intent to an aggregate"),
)

#: Files whose policy-shaped text is an **output format** the model must
#: produce, not an assertion the system makes. Excluded by path with the
#: reason recorded, because a blanket exemption is how a checker rots.
FORMAT_ONLY = {
    "alpha_agents/prompts/morning_scan.md": (
        "the action/操作建议 column is a field the model fills in — the "
        "shape of the answer, not a rule the system asserts"),
}

#: Individual lines that match a pattern but are **not** market claims.
#: Keyed on file + a distinctive fragment so a rewritten line stops being
#: exempt rather than silently keeping the pass.
LINE_EXEMPT = {
    ("alpha_agents/tools/price_levels.py",
     "论点决定：等回调就挂在支撑上方一点"):
        "explains how the band is computed and says explicitly 不含任何建议 — "
        "a calculation note, not a market assertion",
}


#: A policy-shaped sentence is allowed **if it declares where it came from**.
#: The point is not to ban priors — a trader may legitimately hold them — but
#: to stop a prior from silently wearing the clothes of a measured fact. A
#: labelled hypothesis is falsifiable and can be retired; an unlabelled one is
#: a law nobody voted on.
#:
#: ``invariant`` is for statements the repository has decided to treat as
#: binding (they live in GOLDEN_PRINCIPLES and are enforced elsewhere);
#: ``approved`` for a claim that passed the evidence door; ``先验``/``hypothesis``
#: for one that has not been tested yet. All three are honest; silence is not.
PROVENANCE = re.compile(
    r"〔[^〕]*(先验|假设|hypothesis|invariant|approved|未验证|未经)[^〕]*〕"
    r"|<!--\s*(先验|假设|hypothesis|invariant|approved)[^>]*-->")


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    text: str

    def key(self) -> str:
        """Stable identity. Line numbers move; the claim does not."""
        return f"{self.path}::{self.rule}::{self.text.strip()[:60]}"


def load_baseline() -> set[str]:
    if not BASELINE_PATH.exists():
        return set()
    return {
        line.strip() for line in
        BASELINE_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }


def _rel(path: Path) -> str:
    return str(path.relative_to(REPO))


def scan_file(path: Path) -> list[Finding]:
    rel = _rel(path)
    if rel in FORMAT_ONLY:
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    lines = text.splitlines()
    out: list[Finding] = []
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        for pattern, why in PATTERNS:
            if not pattern.search(line):
                continue
            if any(rel == p and frag in stripped
                   for (p, frag) in LINE_EXEMPT):
                break
            # A declared source clears the finding — including one declared on
            # the line above, because a multi-line paragraph labels itself in
            # its heading.
            window = "\n".join(lines[max(0, i - 3):i])
            if PROVENANCE.search(line) or PROVENANCE.search(window):
                break
            out.append(Finding(rel, i, why, stripped))
            break
    return out


def scan() -> list[Finding]:
    out: list[Finding] = []
    for root in SCAN_ROOTS:
        if root.is_dir():
            files = sorted(p for p in root.rglob("*")
                           if p.suffix in (".py", ".md"))
        else:
            files = [root] if root.exists() else []
        for path in files:
            out.extend(scan_file(path))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--show", action="store_true",
                    help="list every finding, including grandfathered ones")
    ap.add_argument("--write-baseline", action="store_true",
                    help="record today's findings as the debt baseline")
    args = ap.parse_args(argv)

    findings = scan()
    baseline = load_baseline()
    grandfathered = [f for f in findings if f.key() in baseline]
    fresh = [f for f in findings if f.key() not in baseline]

    if args.write_baseline:
        BASELINE_PATH.write_text(
            "# Grandfathered policy statements — judgement that reached the\n"
            "# agent without passing the evidence door. New ones fail CI.\n"
            "# Remove a line only by moving the claim to versioned knowledge\n"
            "# or by deleting it. See docs/exec-plans/active/\n"
            "# 2026-09-17-from-architecture-to-a-loop-that-turns.md\n"
            + "\n".join(sorted(f.key() for f in findings)) + "\n",
            encoding="utf-8")
        print(f"已写入 baseline: {len(findings)} 条 → {BASELINE_PATH.name}")
        return 0

    if args.show:
        for f in grandfathered:
            print(f"  存量 {f.path}:{f.line} [{f.rule}] {f.text[:70]}")
        print()

    if fresh:
        print(f"❌ 新增策略污染 {len(fresh)} 处——"
              f"这些判断没有走 candidate→evidence→审批 这道门：")
        for f in fresh:
            print(f"   {f.path}:{f.line} [{f.rule}]")
            print(f"      {f.text[:100]}")
        print()
        print("修复方向（二选一）：")
        print("  · 只输出事实：把结论换成数字（broken_rate=29.3%，不是'承接弱'）")
        print("  · 或者移入 versioned knowledge，让它能被 evidence 替换")
        return 1

    if grandfathered:
        print(f"✅ 策略污染检查通过，存量 {len(grandfathered)} 条待偿还")
    else:
        print("✅ 策略污染检查通过，无存量")
    return 0


if __name__ == "__main__":
    sys.exit(main())
