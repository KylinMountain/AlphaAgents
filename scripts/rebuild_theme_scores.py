#!/usr/bin/env python
"""按日重建历史主题评分，写进回放库的 `theme_lines.trend_score`。

## 为什么需要它

回放的买入路径会过主题门（`data/theme_gate.theme_admits`），门的判据是
`theme_lines.trend_score`。回放用的是合成主题 `WALK-PLACEHOLDER`，它的
`trend_score` 是 NULL——而 NULL 是门**刻意放行**的分支（"量不出来"不等于
"这条线弱"）。

后果不是回放跑不了，是**回放里主题门永远不生效**：policy variant 目前唯一
能改的基因位点（`theme_gate.w_rel ∓0.05`）在回放中不参与任何决策，
于是 `counterfactual_change_rate` 必然是 0——不是因为学习没用，而是因为
唯一能变的参数没有被读到。这就是"RSI golden path 跑不通"的真正原因。

## 数据源与它的边界（先说清楚）

生产 `theme_manager.theme_score()` 读的是**当下**的同花顺板块快照
（`get_concept_fund_flow` / `get_industry_fund_flow`），那是实时接口，
没有历史。能重建历史的源只有 `market_snapshots.db`：

| 表 | 覆盖 | 用途 |
|---|---|---|
| `sector_flow_snapshots` | **2026-09-08 起** | ✅ 唯一的逐日板块资金流历史 |
| `kpl_concept_daily` | **0 行** | ❌ 本该最合适（概念日线 + `days`/`up_nums`） |
| `hm_daily` | **0 行** | ❌ |

**所以本脚本能重建的窗口从 2026-09-08 开始，不能更早。** 早于它的日期
一律不写——拿 9 月的评分回填 8 月，是把未来数据写进历史，比不写更糟。

## 评分口径：与生产一致，但确认项无源

生产：

```
score = w_flow*flow_pct + w_rel*rel_pct + w_confirm*confirm
```

- `flow_pct` / `rel_pct`：该板块的 `net_flow_yi` / `change_pct` 在**全板**
  （concept + industry 合并）里的百分位。本脚本按同一口径算。
- `confirm`：生产用主题的 `strength / 10`。`strength` 是"连续确认了几天"，
  **不在快照里**，无法按 as-of 重建。

对 `confirm` 本脚本**不猜**：默认取 0（把权重的 0.20 让给前两项的百分位），
并把这一事实写进每行的 `notes` 与运行报告的 `confirm` 字段。这会让重建
评分**系统性地低于**生产评分，因此：

- 重建的评分只用于**回放**（那里原本是 NULL，门恒放行），不写生产库；
- 门的行为在回放里因此是"更容易被拒"，而不是"更容易放行"——
  偏向保守，不会凭空制造通过的订单。

## 用法

    python scripts/rebuild_theme_scores.py --data-dir /tmp/walktools \
        --start 2026-09-08 --end 2026-09-14

    # 先看会写什么，不落库
    python scripts/rebuild_theme_scores.py --data-dir /tmp/walktools --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from bisect import bisect_left
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alpha_agents.data import scoring  # noqa: E402

logger = logging.getLogger(__name__)

#: 重建窗口的最早日期。这是数据的边界，不是偏好，写死在这里让越界请求
#: 立刻失败而不是静默返回空。
EARLIEST_REBUILDABLE = "2026-09-08"


def _conn(path: Path, *, readonly: bool) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    else:
        conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _percentile(values: list[float], value: float) -> float:
    """Where ``value`` sits in ``values``, as 0–1. Mid-rank for ties.

    Byte-for-byte the same rule as ``theme_manager._percentile``, including
    the mid-rank. Two definitions of a percentile would put the replay's gate
    on a different scale from production's, and the difference would look like
    a behaviour change.
    """
    if not values:
        return 0.5
    below = sum(1 for v in values if v < value)
    equal = sum(1 for v in values if v == value)
    return (below + equal / 2) / len(values)


def _board_keys(name: str) -> list[str]:
    """Spellings worth comparing a theme name against a board name.

    Copied in behaviour from ``sector_ranking._board_keys``: a theme and a
    board disagree about spelling far more often than about anything else
    (`小金属概念` is board `小金属`). Not imported, because that helper is
    private to a tool module and importing across tool modules would couple
    two things that only share a convention.
    """
    import re
    stripped = re.sub(r"[（(][^）)]*[）)]", "", name).strip()
    keys = {name, stripped}
    for suffix in ("概念", "板块", "指数", "行业", "主题"):
        if stripped.endswith(suffix):
            keys.add(stripped[: -len(suffix)])
        else:
            keys.add(stripped + suffix)
    return sorted(k for k in keys if k)


def _cross_section(conn: sqlite3.Connection, day: str,
                   cut: str) -> list[dict]:
    """The whole board as of ``day``'s close, in production's shape.

    One row per board, taken at the **last** snapshot at or before ``cut`` on
    that day. ``scope`` is carried because the production cross-section merges
    concepts and industries into one frame (a theme may live in either), and
    the percentile must be taken over that merged frame.
    """
    rows = conn.execute(
        """
        SELECT sector_name, scope, net_flow_yi, change_pct, company_count,
               MAX(captured_at) AS last_seen
        FROM sector_flow_snapshots
        WHERE captured_at <= ? AND substr(captured_at,1,10) = ?
        GROUP BY sector_name, scope
        """, (cut, day)).fetchall()
    return [{
        "concept": r["sector_name"],
        "scope": r["scope"],
        "net_flow_yi": float(r["net_flow_yi"] or 0.0),
        "change_pct": float(r["change_pct"] or 0.0),
        "company_count": r["company_count"],
        "last_seen": r["last_seen"],
    } for r in rows]


def _match_board(name: str, rows: list[dict]) -> dict | None:
    """The board row a theme name refers to, or None.

    Exact key match first, then a unique case/punctuation-insensitive one.
    Ambiguity resolves to ``None`` — the same choice production makes, and for
    the same reason: guessing between two boards is how a score gets attached
    to the wrong line.
    """
    lowered = {str(r["concept"]).strip().lower(): r for r in rows}
    keys = _board_keys(name)
    for key in keys:
        hit = lowered.get(key.lower())
        if hit is not None:
            return hit
    # Substring, only when exactly one board matches.
    hits = [r for r in rows
            if any(k.lower() in str(r["concept"]).lower() for k in keys)]
    return hits[0] if len(hits) == 1 else None


def score_day(conn: sqlite3.Connection, day: str, cut: str,
              names: list[str]) -> dict[str, dict]:
    """Recompute one day's scores for ``names``, in production's formula.

    Returns ``{name: {...}}`` and **omits** a name whose board is not in the
    frame: production returns ``None`` there ("cannot see it"), and the caller
    records a decay rather than a zero. Writing a zero would be a claim that
    the line had no money, which is a different fact from "the board does not
    name it".
    """
    rows = _cross_section(conn, day, cut)
    if not rows:
        return {}
    flows = [r["net_flow_yi"] for r in rows]
    changes = [r["change_pct"] for r in rows]
    gate = scoring.DEFAULT_DECISION_PARAMS["theme_gate"]

    out: dict[str, dict] = {}
    for name in names:
        board = _match_board(name, rows)
        if board is None:
            continue
        flow_pct = _percentile(flows, board["net_flow_yi"])
        rel_pct = _percentile(changes, board["change_pct"])
        # `confirm` has no historical source; see the module docstring. Zero
        # rather than a guess, recorded as such on every row it writes.
        confirm = 0.0
        score = (gate["w_flow"] * flow_pct + gate["w_rel"] * rel_pct
                 + gate["w_confirm"] * confirm)
        out[name] = {
            "score": round(score, 4),
            "flow_pct": round(flow_pct, 4),
            "rel_pct": round(rel_pct, 4),
            "confirm": confirm,
            "board": board["concept"],
            "board_scope": board["scope"],
            "board_rank": 1 + sum(1 for r in rows
                                  if r["net_flow_yi"] > board["net_flow_yi"]),
            "board_of": len(rows),
            "as_of": cut,
        }
    return out


def _cut_for(day: str) -> str:
    """The instant a day's score may be known: after its close.

    15:30 rather than 15:00 — the snapshots in this store are written by a
    scheduler that runs on the half hour, and 2026-09-10's last row is 20:16
    while 09-14's is 15:34. Using end-of-day means a 09:00 decision tomorrow
    sees it and a 09:00 decision today cannot.
    """
    return f"{day} 23:59:59"


def rebuild(data_dir: Path, *, start: str, end: str, names: list[str],
            dry_run: bool = False) -> dict:
    """Write reconstructed scores into ``data_dir``'s score history.

    Reads ``market_snapshots.db`` (the corpus, shared read-only) and writes
    only ``memory.db``'s ``theme_score_history`` — the replay's own book. It
    never touches the production directory: a reconstructed score belongs to
    the replay that asked for it.

    **Dated rows, not an overwritten column.** ``theme_lines.trend_score``
    holds today's answer and is rewritten every cycle; writing history into it
    would make every replayed day read the last day rebuilt. Each day lands as
    its own row here, and the gate reads the row whose ``as_of`` is at or
    before the moment being replayed.
    """
    if start < EARLIEST_REBUILDABLE:
        raise SystemExit(
            f"--start {start} is before {EARLIEST_REBUILDABLE}, which is the "
            "first day `sector_flow_snapshots` holds. Earlier days have no "
            "source; filling them with a later day's score would write the "
            "future into the past.")

    snapshots = data_dir / "market_snapshots.db"
    memory = data_dir / "memory.db"
    if not snapshots.exists():
        raise SystemExit(f"no market_snapshots.db in {data_dir}")
    if not memory.exists():
        raise SystemExit(f"no memory.db in {data_dir}")

    src = _conn(snapshots, readonly=True)
    try:
        days = [r[0] for r in src.execute(
            "SELECT DISTINCT substr(captured_at,1,10) d "
            "FROM sector_flow_snapshots WHERE d >= ? AND d <= ? ORDER BY d",
            (start, end))]
        if not days:
            raise SystemExit(
                f"no sector_flow snapshots between {start} and {end}")

        per_day: dict[str, dict] = {}
        for day in days:
            scored = score_day(src, day, _cut_for(day), names)
            if scored:
                per_day[day] = scored
    finally:
        src.close()

    rows = [(name, day, s) for day, scored in per_day.items()
            for name, s in scored.items()]
    if not dry_run:
        dst = _conn(memory, readonly=False)
        try:
            dst.execute(
                "CREATE TABLE IF NOT EXISTS theme_score_history ("
                " id INTEGER PRIMARY KEY, theme TEXT NOT NULL,"
                " as_of TEXT NOT NULL, score REAL NOT NULL,"
                " flow_pct REAL, rel_pct REAL, confirm REAL,"
                " board TEXT, board_scope TEXT, board_rank INTEGER,"
                " board_of INTEGER, source TEXT, UNIQUE(theme, as_of))")
            for name, day, s in rows:
                dst.execute(
                    "INSERT INTO theme_score_history "
                    "(theme, as_of, score, flow_pct, rel_pct, confirm, board,"
                    " board_scope, board_rank, board_of, source) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(theme, as_of) DO UPDATE SET "
                    " score=excluded.score, flow_pct=excluded.flow_pct,"
                    " rel_pct=excluded.rel_pct, confirm=excluded.confirm,"
                    " board=excluded.board, board_scope=excluded.board_scope,"
                    " board_rank=excluded.board_rank, board_of=excluded.board_of",
                    (name, s["as_of"], s["score"], s["flow_pct"], s["rel_pct"],
                     s["confirm"], s["board"], s["board_scope"],
                     s["board_rank"], s["board_of"],
                     "sector_flow_snapshots"))
            dst.commit()
        finally:
            dst.close()

    missing = {n: sum(1 for day in per_day if n not in per_day[day])
               for n in names}
    return {
        "days": days,
        "scored_days": len(per_day),
        "rows_written": len(rows),
        "dry_run": dry_run,
        "unmatched_days": missing,
        "sample": next(iter(per_day.values()), {}),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path, required=True,
                    help="回放的数据目录（含 market_snapshots.db 与 memory.db）")
    ap.add_argument("--start", default=EARLIEST_REBUILDABLE,
                    help=f"首日（不早于 {EARLIEST_REBUILDABLE}）")
    ap.add_argument("--end", default=None, help="末日（默认取库里最后一天）")
    ap.add_argument("--theme", action="append", default=None,
                    help="要重建的主题名，可重复；默认重建库里所有主题")
    ap.add_argument("--dry-run", action="store_true",
                    help="只报告会写什么，不落库")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")

    end = args.end
    if end is None:
        conn = _conn(args.data_dir / "market_snapshots.db", readonly=True)
        try:
            end = conn.execute(
                "SELECT MAX(substr(captured_at,1,10)) FROM sector_flow_snapshots"
            ).fetchone()[0]
        finally:
            conn.close()
        if end is None:
            raise SystemExit("sector_flow_snapshots is empty")

    names = args.theme
    if not names:
        conn = _conn(args.data_dir / "memory.db", readonly=True)
        try:
            names = [r[0] for r in conn.execute(
                "SELECT name FROM theme_lines ORDER BY name")]
        finally:
            conn.close()
        if not names:
            raise SystemExit(
                "no themes in theme_lines — seed the replay's theme first "
                "(walk_forward does this at run start)")

    result = rebuild(args.data_dir, start=args.start, end=end, names=names,
                     dry_run=args.dry_run)
    print(json.dumps({**result, "themes": names}, ensure_ascii=False, indent=2))
    if not result["scored_days"]:
        print("⚠️ 没有任何一天被评分：主题名与板块名对不上，或该窗口无数据")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
