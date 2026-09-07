#!/usr/bin/env python3
"""Keep the knowledge base honest.

A monolithic instruction file rots into a graveyard of stale rules that
agents cannot tell apart from live ones — an attractive nuisance. The
defence is that the knowledge base is itself mechanically checked:
coverage, freshness, ownership, cross-links.

    uv run python scripts/lint_docs.py

Exit 1 on any violation. Messages state the remedy, same as
scripts/lint_harness.py.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"

# AGENTS.md is a map, not an encyclopedia. Past this it stops being one.
MAX_AGENTS_LINES = 140

# A doc describing code that has moved on is worse than no doc.
STALE_AFTER_DAYS = 120

# Files the map must point at, or a reader has no route to them.
REQUIRED_DOCS = [
    "ARCHITECTURE.md",
    "docs/GOLDEN_PRINCIPLES.md",
    "docs/QUALITY_SCORE.md",
    "docs/exec-plans/tech-debt-tracker.md",
    "deploy/README.md",
]

# Research claims need their sample size stated — n<50 does not ship, and
# a doc that omits n cannot be checked against that rule.
RESEARCH_DIR_MARKERS = ("evaluation", "research", "study")


class Issue:
    def __init__(self, path: str, problem: str, remedy: str):
        self.path = path
        self.problem = problem
        self.remedy = remedy

    def render(self) -> str:
        return f"{self.path}: {self.problem}\n    → 修复: {self.remedy}"


def _git_last_modified(path: Path) -> datetime | None:
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%cI", "--", str(path)],
            cwd=REPO, capture_output=True, text=True, timeout=10,
        )
        stamp = out.stdout.strip()
        if not stamp:
            return None
        return datetime.fromisoformat(stamp).replace(tzinfo=None)
    except Exception:
        return None


def check_agents_map() -> list[Issue]:
    """AGENTS.md stays a table of contents."""
    path = REPO / "AGENTS.md"
    if not path.exists():
        return [Issue("AGENTS.md", "缺失",
                      "创建 AGENTS.md 作为目录/地图，指向 docs/ 下的真相来源。")]

    text = path.read_text(encoding="utf-8")
    n = text.count("\n") + 1
    issues = []
    if n > MAX_AGENTS_LINES:
        issues.append(Issue(
            "AGENTS.md", f"{n} 行 > 上限 {MAX_AGENTS_LINES}",
            "它是目录不是百科：把细节移进 docs/ 下的专题文件，这里只留指针。"
            "过长的指令文件会挤占任务本身的上下文，且'什么都重要'等于'什么都不重要'。",
        ))
    return issues


def check_required_docs() -> list[Issue]:
    """Coverage — the map's destinations must exist."""
    out = []
    for rel in REQUIRED_DOCS:
        if not (REPO / rel).exists():
            out.append(Issue(
                rel, "AGENTS.md 指向的文件不存在",
                f"创建 {rel}，或从 AGENTS.md 的索引表里删掉这一行。"
                "指向空气的地图比没有地图更糟。",
            ))
    return out


def check_cross_links() -> list[Issue]:
    """Every relative markdown link resolves."""
    out = []
    link_re = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
    for md in [REPO / "AGENTS.md", *sorted(DOCS.rglob("*.md"))]:
        if not md.exists():
            continue
        # Archived plans are a historical record. Their links described
        # the world at the time and are not maintained; requiring them to
        # resolve would only pressure someone into editing history.
        if "completed" in md.parts:
            continue
        try:
            text = md.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for label, target in link_re.findall(text):
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            resolved = (md.parent / target.split("#")[0]).resolve()
            if not resolved.exists():
                out.append(Issue(
                    str(md.relative_to(REPO)),
                    f"链接失效: [{label}]({target})",
                    f"修正路径或删除该链接。目标 {target} 不存在。",
                ))
    return out


def check_freshness() -> list[Issue]:
    """A doc naming a module that no longer exists is stale."""
    out = []
    cutoff = datetime.now() - timedelta(days=STALE_AFTER_DAYS)
    code_ref = re.compile(r"`(alpha_agents/[\w/]+\.py)`")

    for md in sorted(DOCS.rglob("*.md")):
        if "completed" in md.parts:
            continue          # historical record; staleness is expected
        try:
            text = md.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        for ref in set(code_ref.findall(text)):
            if not (REPO / ref).exists():
                out.append(Issue(
                    str(md.relative_to(REPO)),
                    f"引用了不存在的模块 {ref}",
                    f"更新为实际路径，或删除该段——文档描述的代码已经不在了。",
                ))

        modified = _git_last_modified(md)
        if modified and modified < cutoff and "roadmap" not in md.name:
            age = (datetime.now() - modified).days
            out.append(Issue(
                str(md.relative_to(REPO)),
                f"{age} 天未更新 (阈值 {STALE_AFTER_DAYS})",
                "确认内容仍反映当前代码行为；确认后随任意修改一并提交以刷新时间戳，"
                "或移入 docs/exec-plans/completed/ 作为历史存档。",
            ))
    return out


def check_research_states_n() -> list[Issue]:
    """Small samples do not ship; a claim without n cannot be checked."""
    out = []
    n_re = re.compile(r"\bn\s*=\s*\d+|\bn=\d+|样本\s*\d+|\d+\s*个窗口")
    for md in sorted(DOCS.rglob("*.md")):
        if not any(m in md.name.lower() for m in RESEARCH_DIR_MARKERS):
            continue
        text = md.read_text(encoding="utf-8")
        if not n_re.search(text):
            out.append(Issue(
                str(md.relative_to(REPO)),
                "研究结论未标注样本量",
                "在每个结论旁写出 n 和独立窗口数。本仓库有过 n=7~17 得出"
                "与大样本完全相反排序的先例。见 docs/GOLDEN_PRINCIPLES.md 第 7 条。",
            ))
    return out


def check_active_plans_have_criteria() -> list[Issue]:
    """An in-flight plan needs criteria a machine can check."""
    out = []
    active = DOCS / "exec-plans" / "active"
    if not active.exists():
        return out
    for md in sorted(active.glob("*.md")):
        if md.name == "README.md":
            continue
        text = md.read_text(encoding="utf-8")
        if "验收" not in text and "acceptance" not in text.lower():
            out.append(Issue(
                str(md.relative_to(REPO)),
                "缺少验收标准",
                "写一节「验收」，用机器可检查的说法：'跑 X 输出 Y' 而不是"
                "'改进 Z'。不可检查的标准等于没有标准。",
            ))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--skip-freshness", action="store_true",
                    help="skip git-timestamp checks (useful in shallow clones)")
    args = ap.parse_args()

    issues: list[Issue] = []
    issues += check_agents_map()
    issues += check_required_docs()
    issues += check_cross_links()
    issues += check_research_states_n()
    issues += check_active_plans_have_criteria()
    if not args.skip_freshness:
        issues += check_freshness()

    if not issues:
        print("✅ 知识库校验通过")
        return 0

    for i in issues:
        print(i.render())
    print(f"\n❌ {len(issues)} 处问题")
    print("规则依据: AGENTS.md / docs/GOLDEN_PRINCIPLES.md")
    return 1


if __name__ == "__main__":
    sys.exit(main())
