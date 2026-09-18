#!/usr/bin/env python
"""①C：构造贴阈值窗口——在可重建的历史上扫描单步反事实的翻转带。

## 为什么需要它

2026-09-17 计划的路 1 实测（5 天 × 4 主题）：`w_rel ±0.05` 一个组合都没
翻转，因为**每一个**评分距 admit 阈值 0.0445–0.4592，而权重位移上限只有
0.05。结论"位点惰性"有两种解释，当时无法区分：

1. 位点真的惰性（位移太小，永远够不着阈值）；
2. **窗口选择**使然——那 5 天恰好没有评分贴近阈值的 (日, 主题)。

本脚本回答的是 2：在 `sector_flow_snapshots` 能支持的重建窗口里
（2026-09-08 起，更早没有逐日板块资金流），对**每一个** (日, 主题) 跑
`single_step_counterfactual`，让"有没有评分贴近阈值的日子"从推测变成
计数。翻转判定**不在这里重算**——直接调用
`evolution.causal_trace.single_step_counterfactual`，即路 1 实测所用的
同一个函数，两臂的参数集是它唯一的输入差异。

## 评分口径（继承 `rebuild_theme_scores`，先说清边界）

`confirm` 没有历史源，重建口径取 0。它对两臂**相同**，因此不影响翻转
判定——改变的是两臂共有的那 0.20 权重——但它使重建评分系统性低于生产
评分。所以本脚本的结论是关于**阈值邻近结构**的（有多少评分在位移能及
的范围内），不是关于"那天生产会不会准入"的。

## 重复截面

快照抓取器在非交易日也可能留行（2026-09-12 是周六，09:00–09:09 的行与
09-11 收盘的帧完全一致）。同一帧只计一次证据：按 (scope, sector_name,
net_flow_yi, change_pct) 的帧哈希去重，被跳过的日期在结果里指认它重复了
哪一天——跳过是事实，不是丢弃。

## 用法

    python scripts/threshold_window_counterfactual.py                 # 全窗口全主题
    python scripts/threshold_window_counterfactual.py --json-out /tmp/c.json
    python scripts/threshold_window_counterfactual.py --names 光纤概念,苹果概念

只读两张库，不写任何库。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alpha_agents.data.scoring import DEFAULT_DECISION_PARAMS  # noqa: E402
from alpha_agents.evolution.causal_trace import (  # noqa: E402
    single_step_counterfactual,
)
from rebuild_theme_scores import (  # noqa: E402
    EARLIEST_REBUILDABLE,
    _conn,
    _cross_section,
    _match_board,
)

logger = logging.getLogger(__name__)


def _frame_hash(rows: list[dict]) -> str:
    """One day's board as a comparable fingerprint.

    Two days whose boards agree row-for-row are the same evidence no matter
    what the calendar says; a hash over the sorted rows says that without
    anybody having to eyeball 380 lines.
    """
    items = sorted(
        (r["scope"], r["concept"], round(r["net_flow_yi"], 6),
         round(r["change_pct"], 6))
        for r in rows)
    blob = json.dumps(items, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _arms(gate: dict, delta: float) -> tuple[dict, dict]:
    """Baseline vs ±delta, as two full parameter sets.

    The direction each pair tests is recorded on its result row; the baseline
    is the same gene value version #2 froze, so an up-arm is version #3's
    locus and a down-arm is the candidate's opposite.
    """
    up = {**gate, "w_rel": gate["w_rel"] + delta}
    down = {**gate, "w_rel": gate["w_rel"] - delta}
    return up, down


def scan(conn: sqlite3.Connection, *, names: list[str], start: str,
         end: str, delta: float) -> dict:
    """Run both arms for every (day, theme) in the window; count the flips."""
    days = [r[0] for r in conn.execute(
        "SELECT DISTINCT substr(captured_at,1,10) d "
        "FROM sector_flow_snapshots WHERE d >= ? AND d <= ? ORDER BY d",
        (start, end))]
    if not days:
        raise SystemExit(f"no sector_flow snapshots between {start} and {end}")

    gate = DEFAULT_DECISION_PARAMS["theme_gate"]
    up_arm, down_arm = _arms(gate, delta)
    seen: dict[str, str] = {}
    flips: list[dict] = []
    comparable = 0
    unmatched = 0
    refused: list[dict] = []
    day_report: list[dict] = []

    for day in days:
        rows = _cross_section(conn, day, f"{day} 23:59:59")
        if not rows:
            day_report.append({"day": day, "board": 0, "scored": 0})
            continue
        digest = _frame_hash(rows)
        dup_of = seen.get(digest)
        if dup_of is not None:
            day_report.append({"day": day, "board": len(rows), "scored": 0,
                               "duplicate_of": dup_of})
            logger.info("%s is the same frame as %s — counted once", day, dup_of)
            continue
        seen[digest] = day
        scored = 0
        flows = [r["net_flow_yi"] for r in rows]
        changes = [r["change_pct"] for r in rows]
        for name in names:
            board = _match_board(name, rows)
            if board is None:
                unmatched += 1
                continue
            # The tool refuses a board without its frame -- a percentile over
            # a pre-filtered list is not a percentile -- so the whole frame
            # travels with the row, exactly as rebuild_theme_scores.score_day
            # computes it.
            board_row = {**board, "frame_flows": flows,
                         "frame_changes": changes, "confirm": 0.0}
            for direction, params_b in (("up", up_arm), ("down", down_arm)):
                got = single_step_counterfactual(
                    theme=name, day=day, board_row=board_row,
                    params_a=gate, params_b=params_b)
                if not got.get("comparable"):
                    refused.append({"day": day, "theme": name,
                                    "reason": got.get("reason")})
                    continue
                comparable += 1
                scored += 1
                if got["changed"]:
                    flips.append({
                        "day": day, "theme": name, "direction": direction,
                        "score": got["a"]["score"],
                        "admit_score": got["a"]["admit_score"],
                        "score_b": got["b"]["score"],
                        "flip_margin": got["flip_margin"],
                        "rel_pct": got["a"]["rel_pct"],
                        "flow_pct": got["a"]["flow_pct"],
                    })
        day_report.append({"day": day, "board": len(rows), "scored": scored})

    return {
        "window": {"start": start, "end": end, "days": len(days),
                   "unique_frames": len(seen)},
        "names": len(names),
        "comparable_pairs": comparable,
        "unmatched_pairs": unmatched,
        "refused": refused,
        "flips": flips,
        "days": day_report,
        "scale": {
            "gate": gate, "delta": delta,
            "confirm": 0.0,
            "note": ("rebuild scale: confirm=0 (no historical source), so "
                     "scores sit lower than production's; identical for both "
                     "arms, which is what flip judgements need"),
        },
    }


def _summary(result: dict) -> str:
    flips = result["flips"]
    up = sum(1 for f in flips if f["direction"] == "up")
    down = len(flips) - up
    themes = sorted({f["theme"] for f in flips})
    days = sorted({f["day"] for f in flips})
    margins = sorted(f["flip_margin"] for f in flips)
    lines = [
        f"window {result['window']['start']}..{result['window']['end']}: "
        f"{result['window']['days']} snapshot days, "
        f"{result['window']['unique_frames']} unique frames, "
        f"{result['names']} themes",
        f"comparable pairs: {result['comparable_pairs']} "
        f"(each day-theme tried in both directions)",
        f"flips: {len(flips)} (up {up} / down {down}) "
        f"across {len(days)} days, {len(themes)} themes",
    ]
    if margins:
        lines.append(
            f"flip margins: min {margins[0]:.4f}, median "
            f"{margins[len(margins)//2]:.4f}, max {margins[-1]:.4f}")
        lines.append(f"themes: {', '.join(themes)}")
        lines.append(f"days: {', '.join(days)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--snapshots-db", default="data/market_snapshots.db")
    parser.add_argument("--memory-db", default="data/memory.db")
    parser.add_argument("--names", default=None,
                        help="comma-separated theme names; default = every "
                             "row in theme_lines")
    parser.add_argument("--start", default=EARLIEST_REBUILDABLE)
    parser.add_argument("--end", default="2099-12-31")
    parser.add_argument("--delta", type=float, default=0.05,
                        help="the w_rel step a candidate proposes")
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args(argv)

    if args.start < EARLIEST_REBUILDABLE:
        raise SystemExit(
            f"--start {args.start} is before {EARLIEST_REBUILDABLE}: earlier "
            "days have no sector_flow source, and filling them from later "
            "days would write the future into the past")

    src = _conn(Path(args.snapshots_db), readonly=True)
    mem = _conn(Path(args.memory_db), readonly=True)
    try:
        if args.names:
            names = [n.strip() for n in args.names.split(",") if n.strip()]
        else:
            names = [r[0] for r in mem.execute(
                "SELECT name FROM theme_lines ORDER BY name")]
        result = scan(src, names=names, start=args.start, end=args.end,
                      delta=args.delta)
    finally:
        src.close()
        mem.close()

    print(_summary(result))
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8")
        logger.info("full result written to %s", args.json_out)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    raise SystemExit(main())
