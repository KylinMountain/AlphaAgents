"""Evaluate the active plans' acceptance criteria against a finished replay.

AGENTS.md asks for criteria "a machine can check". The three plans written
2026-09-22 state theirs as numbers, and this is the machine. It reads a
replay's own ``memory.db`` and its log, and prints one line per criterion
with the number behind it, so a verdict is a reading rather than a memory.

    uv run python scripts/check_replay_acceptance.py <replay-dir> <log>

Exit status is 1 when any criterion fails, so it can gate a run.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path


def _rows(conn, sql, *params):
    return conn.execute(sql, params).fetchall()


def _count(conn, sql, *params) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def check(replay_dir: Path, log_path: Path) -> list[tuple[bool, str]]:
    conn = sqlite3.connect(f"file:{replay_dir / 'memory.db'}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    log = log_path.read_text(errors="replace") if log_path.exists() else ""
    out: list[tuple[bool, str]] = []

    # --- invalidation-wakes-the-agent.md ---
    # 1. No thesis closed by a condition without the agent having spoken.
    silent = _count(
        conn,
        "SELECT COUNT(*) FROM theses t JOIN virtual_portfolio p "
        "ON p.thesis_id = t.id "
        "WHERE t.close_kind != '' AND t.close_kind IS NOT NULL "
        "  AND (p.close_reason IS NULL OR p.close_reason NOT LIKE '%agent%')")
    out.append((silent == 0,
                f"条件平仓但 agent 无发言: {silent} 笔（应为 0）"))

    # 2. At least one override: triggered, then still held that day.
    overrides = 0
    for row in _rows(conn, "SELECT checkpoints FROM theses"):
        points = json.loads(row["checkpoints"] or "[]")
        fired = [p for p in points if p.get("verdict") == "triggered"]
        if len(fired) > 1:
            overrides += 1
    out.append((overrides > 0,
                f"触发后仍持有（同一条件被记录多次）: {overrides} 条（应 > 0）"))

    # 3. The signal line carried real content.
    woke = log.count("theses triggered, agent asked")
    out.append((woke > 0, f"agent 被唤醒的交易日: {woke} 天（应 > 0）"))

    # --- theme-conditions-measure-state-not-noise.md ---
    # 1. theme_rank_worse_than stops dominating.
    kinds: dict[str, int] = {}
    for row in _rows(conn,
                     "SELECT close_kind FROM theses "
                     "WHERE close_kind != '' AND close_kind IS NOT NULL"):
        kinds[row["close_kind"]] = kinds.get(row["close_kind"], 0) + 1
    rank_share = kinds.get("theme_rank_worse_than", 0)
    out.append((rank_share == 0,
                f"theme_rank_worse_than 平仓: {rank_share} 笔"
                f"（对照组 5/5 全是它）｜全部: {kinds or '无'}"))

    # --- unfilled-claims-are-evidence-too.md ---
    orphan = _count(conn, "SELECT COUNT(*) FROM theses "
                          "WHERE status = 'active' AND position_id IS NULL")
    out.append((orphan == 0,
                f"未成交却仍 active 的 thesis: {orphan} 条（应为 0）"))

    leaked = _count(conn,
                    "SELECT COUNT(*) FROM theses t JOIN virtual_portfolio p "
                    "ON p.thesis_id = t.id "
                    "WHERE t.status = 'active' AND p.status NOT IN "
                    "('open', 'pending')")
    out.append((leaked == 0,
                f"仓位已了结却仍 active 的 thesis: {leaked} 条（应为 0）"))

    # --- replay hygiene, from the clock fix ---
    stale = _count(conn, "SELECT COUNT(*) FROM virtual_portfolio "
                         "WHERE close_date IS NOT NULL AND close_date > ?",
                   "2026-06-30")
    out.append((stale == 0,
                f"平仓日期落在回放窗口之外: {stale} 笔（墙钟污染，应为 0）"))

    errors = sum(log.count(k) for k in
                 ("Traceback", "decider failed", "empty content"))
    out.append((errors == 0, f"运行期错误: {errors}（应为 0）"))

    conn.close()
    return out


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    results = check(Path(sys.argv[1]), Path(sys.argv[2]))
    print("验收标准（2026-09-22 三个 active 计划）")
    print("=" * 58)
    for ok, line in results:
        print(f"  {'✅' if ok else '❌'} {line}")
    failed = [line for ok, line in results if not ok]
    print("=" * 58)
    print(f"{len(results) - len(failed)}/{len(results)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
