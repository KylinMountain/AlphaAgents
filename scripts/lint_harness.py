#!/usr/bin/env python3
"""Mechanical enforcement of the invariants in docs/GOLDEN_PRINCIPLES.md.

Every message here is written for the agent that will fix it: what broke,
where, and the specific edit to make. A lint that only says "violation on
line 40" costs a round trip to work out the intent; one that states the
remedy gets fixed in the same run.

    uv run python scripts/lint_harness.py [--fix-hint] [paths...]

Exit 1 on any violation.
"""

from __future__ import annotations

import argparse
import ast
import builtins
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGE = "alpha_agents"

# ── Principle 4: layer direction ─────────────────────────────────────
# Index = position in the dependency order. A module may import from its
# own layer or any layer before it, never after.
# Storage precedes sources: a source's job includes persisting what it
# fetched, so sources depend on the store, not the other way round.
# evolution precedes pipeline: the review task drives lesson extraction
# and the holdout gate, not the reverse. Both orderings were corrected
# after the linter flagged 27 "violations" that were really this file
# being wrong about the architecture.
LAYERS = ["data", "sources", "tools", "evolution", "pipeline", "agents", "server"]

# Importable from anywhere — cross-cutting, not layers.
#
# evolution.replay_mode is here rather than in the evolution layer: it is
# a contextvar holding a global "as of" instant, read by every data
# reader so historical replay is honest. Treating it as a layer would
# force 60+ readers to thread a parameter through for a value that is
# ambient by design. The linter surfaced this — the model was wrong, not
# the code.
CROSS_CUTTING = {"config", "http_client", "notify", "model_factory"}
CROSS_CUTTING_MODULES = {"alpha_agents.evolution.replay_mode"}

MAX_FILE_LINES = 1200

# Vendored third-party code and generated files are not ours to shape.
SKIP_DIRS = {"__pycache__", ".venv", "node_modules", "build", "dist"}

# Known violations, grandfathered so the gate can be turned on today.
# New violations fail; these are paid down from
# docs/exec-plans/tech-debt-tracker.md. Deleting a line here is the only
# way the number goes down, which is the point — a baseline that can grow
# is not a baseline.
BASELINE_PATH = REPO / "scripts" / "lint_baseline.txt"


class Violation:
    def __init__(self, path: Path, line: int, rule: str, problem: str,
                 remedy: str):
        self.path = path
        self.line = line
        self.rule = rule
        self.problem = problem
        self.remedy = remedy

    def render(self) -> str:
        rel = self.path.relative_to(REPO)
        return (f"{rel}:{self.line}: [{self.rule}] {self.problem}\n"
                f"    → 修复: {self.remedy}")


def _layer_of(module: str) -> str | None:
    """Layer name for an alpha_agents submodule path, if it is in one."""
    parts = module.split(".")
    if len(parts) < 2 or parts[0] != PACKAGE:
        return None
    return parts[1] if parts[1] in LAYERS else None


def check_layering(path: Path, tree: ast.AST) -> list[Violation]:
    """Principle 4 — imports only go forward through the layers."""
    rel = path.relative_to(REPO)
    own = _layer_of(".".join(rel.with_suffix("").parts))
    if own is None:
        return []
    own_rank = LAYERS.index(own)

    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
        elif isinstance(node, ast.Import):
            module = node.names[0].name if node.names else ""
        else:
            continue

        parts = module.split(".")
        if len(parts) < 2 or parts[0] != PACKAGE or parts[1] in CROSS_CUTTING:
            continue
        if module in CROSS_CUTTING_MODULES:
            continue
        target = _layer_of(module)
        if target is None or target == own:
            continue

        if LAYERS.index(target) > own_rank:
            out.append(Violation(
                path, node.lineno, "layering",
                f"{own}/ 依赖了下游的 {target}/ — 方向是 "
                f"{' → '.join(LAYERS)}，只能向前依赖",
                f"把需要的能力上移到 {own}/ 或更前的层，或让 {target}/ "
                f"通过参数接收它；不要在 {own}/ 里 import {module}。"
                f"跨切面 ({', '.join(sorted(CROSS_CUTTING))}) 不受此限。",
            ))
    return out


def check_file_size(path: Path, source: str) -> list[Violation]:
    """Principle 10 — a file an agent cannot hold in context gets edited blindly."""
    n = source.count("\n") + 1
    if n <= MAX_FILE_LINES:
        return []
    return [Violation(
        path, n, "file-size",
        f"{n} 行 > 上限 {MAX_FILE_LINES}",
        f"按职责拆分：找出文件里彼此不共享状态的函数组，各自成模块。"
        f"不要为了过线而机械对半切——那只会制造两个都难懂的文件。",
    )]


def check_undefined_names(path: Path, tree: ast.AST) -> list[Violation]:
    """A name used but never imported or defined in the module.

    Python only raises NameError when the line actually executes, so a call
    site added without its import survives every test that does not reach
    that branch. This shipped: ``intraday_monitor`` called
    ``build_decision_context`` inside the anomaly path and crashed the 盘中
    追因 task every five minutes in production, silently, for a day.
    """
    # Injected by the import machinery, so absent from both the module's
    # own bindings and the builtins list.
    bound: set[str] = {"__file__", "__name__", "__doc__", "__package__",
                       "__spec__", "__loader__", "__builtins__", "__debug__"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            bound.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)

    out, reported = [], set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)):
            continue
        name = node.id
        if name in bound or name in dir(builtins) or name in reported:
            continue
        reported.add(name)
        out.append(Violation(
            path, node.lineno, "undefined-name",
            f"用到了 {name}，但模块里既没 import 也没定义",
            f"补上 import，或确认拼写。测试跑不到这一行时，NameError 只会在"
            f"生产环境的那条分支上炸，且日志里只有一行 NameError。",
        ))
    return out


def check_logging(path: Path, tree: ast.AST) -> list[Violation]:
    """Principle 9 — lazy formatting, greppable messages."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not isinstance(fn, ast.Attribute):
            continue
        if fn.attr not in {"debug", "info", "warning", "error", "exception",
                           "critical"}:
            continue
        if not (isinstance(fn.value, ast.Name) and
                fn.value.id in {"logger", "log", "logging"}):
            continue
        if node.args and isinstance(node.args[0], ast.JoinedStr):
            out.append(Violation(
                path, node.lineno, "structured-logging",
                "日志用了 f-string",
                ' 改成惰性格式化: logger.info("处理 %s 失败: %s", code, err)。'
                "这样未触发的日志不付格式化开销，且相同事件的消息可被 grep 聚合。",
            ))
    return out


def check_exceptions(path: Path, tree: ast.AST) -> list[Violation]:
    """Principle 8 — failures are loud.

    The PBOC parser silently returned rows whose every headline was the
    string "true" for months. A crash would have been better.
    """
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue

        if node.type is None:
            out.append(Violation(
                path, node.lineno, "bare-except",
                "裸 except: 会吞掉 KeyboardInterrupt 和 SystemExit",
                "改成 except Exception:，并且明确处理——记日志或重新抛出。",
            ))
            continue

        body_is_pass = (len(node.body) == 1 and
                        isinstance(node.body[0], ast.Pass))
        if body_is_pass:
            out.append(Violation(
                path, node.lineno, "silent-except",
                "except ... : pass 静默吞掉异常",
                'logger.debug("...失败: %s", e) 至少留痕。'
                "若这里确实可以忽略，在 pass 上方写一行注释说明为什么——"
                "静默失败是本仓库出过事故的地方。",
            ))
    return out


def check_self_grading(path: Path, tree: ast.AST) -> list[Violation]:
    """Principle 1 — an LLM response must not drive a score or status write.

    Heuristic and deliberately narrow: flags a status/weight/score write
    whose value traces back to a name suggesting an LLM response, inside
    the evolution layer where memory utility is decided.
    """
    rel = str(path.relative_to(REPO))
    if "/evolution/" not in rel:
        return []

    sanctioned = {"principle_scoring.py", "holdout_gate.py"}
    if path.name in sanctioned:
        return []

    llm_ish = ("llm", "response", "completion", "verdict", "judgement",
               "judgment")
    write_ish = ("set_principle_status", "update_playbook_status",
                 "record_playbook_trade", "win_rate")

    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = ""
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        if not any(w in name for w in write_ish):
            continue

        src = ast.dump(node)
        if any(t in src.lower() for t in llm_ish):
            out.append(Violation(
                path, node.lineno, "self-grading",
                f"{name}(...) 的取值疑似来自 LLM 输出",
                "记忆/原则/playbook 的存废只能由市场数据决定 —— 用 "
                "evolution/principle_scoring.py 的 score_principle，"
                "或 data/scoring.py 的 Brier/残差。模型只能提议，不能裁决。"
                "见 docs/GOLDEN_PRINCIPLES.md 第 1 条。",
            ))
    return out


def check_duplication(files: list[tuple[Path, ast.AST]]) -> list[Violation]:
    """Principle 3 — the same helper in three places becomes three helpers."""
    bodies: dict[str, list[tuple[Path, int, str]]] = defaultdict(list)
    for path, tree in files:
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = node.body
            # Strip the docstring before comparing. Two copies of the same
            # helper usually get reworded docstrings, and comparing those
            # made the check miss real duplication — it did, in this repo.
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body = body[1:]
            if len(body) < 4:
                continue          # trivial wrappers are not duplication
            key = ast.dump(ast.Module(body=body, type_ignores=[]))
            bodies[key].append((path, node.lineno, node.name))

    out = []
    for _body, sites in bodies.items():
        if len(sites) < 2:
            continue
        modules = {p for p, _l, _n in sites}
        if len(modules) < 2:
            continue          # same file: overloads and dispatch are fine
        first_path, first_line, first_name = sites[0]
        others = ", ".join(
            f"{p.relative_to(REPO)}:{ln} ({nm})" for p, ln, nm in sites[1:]
        )
        out.append(Violation(
            first_path, first_line, "duplication",
            f"{first_name}() 与另 {len(sites) - 1} 处函数体完全相同: {others}",
            "抽到共享模块，原处改为 import。不变量集中在一处，"
            "否则修一次 bug 要修 N 遍。见 docs/GOLDEN_PRINCIPLES.md 第 3 条。",
        ))
    return out


def iter_python_files(roots: list[Path]) -> list[Path]:
    out = []
    for root in roots:
        if root.is_file() and root.suffix == ".py":
            out.append(root)
            continue
        for p in root.rglob("*.py"):
            if any(part in SKIP_DIRS for part in p.parts):
                continue
            out.append(p)
    return sorted(out)


def load_baseline() -> set[str]:
    if not BASELINE_PATH.exists():
        return set()
    return {
        line.strip() for line in BASELINE_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }


def violation_key(v: Violation) -> str:
    """Stable identity for a violation. Deliberately excludes the line
    number — otherwise every edit above a grandfathered violation would
    resurrect it as new."""
    return f"{v.path.relative_to(REPO)}::{v.rule}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="*", default=None,
                    help="files or dirs to check (default: alpha_agents/)")
    ap.add_argument("--write-baseline", action="store_true",
                    help="record current violations as grandfathered")
    ap.add_argument("--show-baseline", action="store_true",
                    help="also list grandfathered violations")
    args = ap.parse_args()

    roots = ([Path(p).resolve() for p in args.paths]
             if args.paths else [REPO / PACKAGE])

    violations: list[Violation] = []
    parsed: list[tuple[Path, ast.AST]] = []

    for path in iter_python_files(roots):
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (SyntaxError, UnicodeDecodeError) as e:
            violations.append(Violation(
                path, getattr(e, "lineno", 1) or 1, "parse",
                f"无法解析: {e}", "修复语法错误后重跑。",
            ))
            continue

        parsed.append((path, tree))
        violations += check_file_size(path, source)
        violations += check_layering(path, tree)
        violations += check_logging(path, tree)
        violations += check_undefined_names(path, tree)
        violations += check_exceptions(path, tree)
        violations += check_self_grading(path, tree)

    violations += check_duplication(parsed)

    if args.write_baseline:
        keys = sorted({violation_key(v) for v in violations})
        BASELINE_PATH.write_text(
            "# Grandfathered violations — see docs/exec-plans/tech-debt-tracker.md\n"
            "# New violations fail CI. Remove a line only by fixing the code.\n"
            + "\n".join(keys) + "\n", encoding="utf-8")
        print(f"已写入 baseline: {len(keys)} 条 → {BASELINE_PATH.name}")
        return 0

    baseline = load_baseline()
    grandfathered = [v for v in violations if violation_key(v) in baseline]
    fresh = [v for v in violations if violation_key(v) not in baseline]

    if args.show_baseline and grandfathered:
        print(f"— 存量({len(grandfathered)}，已记入技术债) —")
        for v in sorted(grandfathered, key=lambda v: (str(v.path), v.line)):
            print("  " + v.render().replace("\n", "\n  "))
        print()

    violations = fresh

    if not violations:
        msg = f"✅ harness lint 通过 ({len(parsed)} 个文件)"
        if grandfathered:
            msg += f"，存量 {len(grandfathered)} 条待偿还"
        print(msg)
        return 0

    by_rule: dict[str, int] = defaultdict(int)
    for v in violations:
        by_rule[v.rule] += 1

    for v in sorted(violations, key=lambda v: (str(v.path), v.line)):
        print(v.render())

    summary = ", ".join(f"{k}={n}" for k, n in sorted(by_rule.items()))
    print(f"\n❌ {len(violations)} 处新增违规 ({summary})")
    if grandfathered:
        print(f"   (另有 {len(grandfathered)} 条存量已豁免，见 lint_baseline.txt)")
    print("规则依据: docs/GOLDEN_PRINCIPLES.md")
    return 1


if __name__ == "__main__":
    sys.exit(main())
