# Agent Evolution Phase 4 — Meta-Metrics + A/B Validation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Phase 4 is self-measurement — "is the evolution system actually improving decisions?" Runs daily but has real signal only after Phase 3 has been live ≥2 weeks.

**Goal:** Answer the question *"is the evolution system actually improving recommendations vs the pre-evolution baseline?"* via daily metric snapshots plus observational A/B (matched-vs-unmatched candidates). If metrics stay flat or regress, Phase 1-3 architecture itself is questionable and this visibility triggers redesign — not blind faith.

**Architecture:** New `evolution_metrics` table stores a daily snapshot computed from `predictions` + `trading_principles` + `playbooks` aggregates. Phase 4 adds no new LLM calls — all metrics are SQL. `build_morning_context` gains a `mode="baseline"` parameter that omits principles/playbooks/lessons for comparison prompts. New `/evolution` chat command renders the trend. Weekly report surfaces the delta.

**Tech Stack:** same — SQLite + pytest, no new deps.

**Spec:** `docs/superpowers/specs/2026-04-16-agent-evolution-design.md` §Design Constraints #9 ("Evolution observability").
**Branch:** `feat/evolution-phase1` (continue).

---

## File Map

### Created
- `alpha_agents/evolution/metrics.py` — `compute_evolution_metrics(today)`, `get_metrics_trend(days)`, `format_metrics_trend(rows)`
- `tests/test_evolution_metrics.py` — unit tests (mocked DB rows)

### Modified
- `alpha_agents/data/memory_store.py` — add `evolution_metrics` table + CRUD
- `alpha_agents/evolution/lessons.py` — `post_review` also writes daily metrics snapshot
- `alpha_agents/evolution/context_builder.py` — `build_morning_context(mode="baseline")` skips principles/playbooks/lessons
- `alpha_agents/evolution/__init__.py` — export metrics helpers
- `alpha_agents/agents/chat_commands/handlers.py` — `/evolution` command
- `alpha_agents/pipeline/tasks/weekly_report.py` — surface 7-day metrics delta

---

## Task 1: `evolution_metrics` table + CRUD

**Files:**
- Modify: `alpha_agents/data/memory_store.py`
- Modify: `tests/test_evolution_schema.py` (append)

- [ ] **Step 1: Failing tests**

```python
def test_evolution_metrics_table_exists(tmp_path, monkeypatch):
    import alpha_agents.data.memory_store as ms
    db = tmp_path / "memory.db"
    monkeypatch.setattr(ms, "MEMORY_DB_PATH", db)
    if hasattr(ms._local, "conn"):
        monkeypatch.delattr(ms._local, "conn", raising=False)
    ms._get_conn()
    import sqlite3
    conn = sqlite3.connect(str(db))
    cols = {row[1] for row in conn.execute("PRAGMA table_info(evolution_metrics)")}
    conn.close()
    expected = {"date", "intraday_hit_rate_7d", "intraday_count_7d",
                "matched_hit_rate_7d", "matched_count_7d",
                "unmatched_hit_rate_7d", "unmatched_count_7d",
                "active_principles", "weakened_principles",
                "active_playbooks", "degraded_playbooks",
                "lessons_count_7d"}
    assert expected <= cols


def test_upsert_evolution_metrics_and_trend(tmp_path, monkeypatch):
    import alpha_agents.data.memory_store as ms
    db = tmp_path / "memory.db"
    monkeypatch.setattr(ms, "MEMORY_DB_PATH", db)
    if hasattr(ms._local, "conn"):
        monkeypatch.delattr(ms._local, "conn", raising=False)

    ms.upsert_evolution_metrics("2026-04-17", {
        "intraday_hit_rate_7d": 0.62, "intraday_count_7d": 200,
        "matched_hit_rate_7d": 0.75, "matched_count_7d": 40,
        "unmatched_hit_rate_7d": 0.55, "unmatched_count_7d": 160,
        "active_principles": 5, "weakened_principles": 1,
        "active_playbooks": 3, "degraded_playbooks": 0,
        "lessons_count_7d": 18,
    })
    # Upserts re-insert same date:
    ms.upsert_evolution_metrics("2026-04-17", {
        "intraday_hit_rate_7d": 0.65, "intraday_count_7d": 201,
        "matched_hit_rate_7d": 0.75, "matched_count_7d": 40,
        "unmatched_hit_rate_7d": 0.55, "unmatched_count_7d": 160,
        "active_principles": 5, "weakened_principles": 1,
        "active_playbooks": 3, "degraded_playbooks": 0,
        "lessons_count_7d": 18,
    })
    trend = ms.get_evolution_metrics_trend(days=7)
    assert len(trend) == 1  # one row, updated not duplicated
    assert abs(trend[0]["intraday_hit_rate_7d"] - 0.65) < 1e-9
```

- [ ] **Step 2: Add schema to `_SCHEMA`**

```sql
CREATE TABLE IF NOT EXISTS evolution_metrics (
    date TEXT PRIMARY KEY,
    intraday_hit_rate_7d REAL,
    intraday_count_7d INTEGER,
    matched_hit_rate_7d REAL,
    matched_count_7d INTEGER,
    unmatched_hit_rate_7d REAL,
    unmatched_count_7d INTEGER,
    active_principles INTEGER DEFAULT 0,
    weakened_principles INTEGER DEFAULT 0,
    active_playbooks INTEGER DEFAULT 0,
    degraded_playbooks INTEGER DEFAULT 0,
    lessons_count_7d INTEGER DEFAULT 0
);
```

- [ ] **Step 3: Add CRUD helpers**

```python
# ── Phase 4: evolution metrics ────────────────────────────────
def upsert_evolution_metrics(date: str, fields: dict) -> None:
    """Insert or replace a daily metrics row. `fields` maps column names to values."""
    # Column order must match schema
    cols = [
        "intraday_hit_rate_7d", "intraday_count_7d",
        "matched_hit_rate_7d", "matched_count_7d",
        "unmatched_hit_rate_7d", "unmatched_count_7d",
        "active_principles", "weakened_principles",
        "active_playbooks", "degraded_playbooks",
        "lessons_count_7d",
    ]
    values = [fields.get(c) for c in cols]
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO evolution_metrics "
            f"(date, {', '.join(cols)}) VALUES (?, {', '.join('?' * len(cols))})",
            (date, *values),
        )
        conn.commit()


def get_evolution_metrics_trend(days: int = 30) -> list[dict]:
    from datetime import datetime, timedelta
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = _get_conn().execute(
        "SELECT * FROM evolution_metrics WHERE date >= ? ORDER BY date",
        (cutoff,),
    ).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 4: Run all schema tests — 16 passing (14 + 2 new).

- [ ] **Step 5: Commit**
```bash
git add alpha_agents/data/memory_store.py tests/test_evolution_schema.py
git commit -m "feat(evolution): evolution_metrics table + CRUD (Phase 4)"
```

---

## Task 2: `compute_evolution_metrics` — SQL aggregates into one row

**Files:**
- Create: `alpha_agents/evolution/metrics.py`
- Create: `tests/test_evolution_metrics.py`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for alpha_agents.evolution.metrics."""
from unittest.mock import patch, MagicMock


def test_compute_metrics_with_empty_db_returns_zeros():
    with patch("alpha_agents.evolution.metrics._query_intraday_buckets",
               return_value={
                   "intraday_hit_rate_7d": 0.0, "intraday_count_7d": 0,
                   "matched_hit_rate_7d": 0.0, "matched_count_7d": 0,
                   "unmatched_hit_rate_7d": 0.0, "unmatched_count_7d": 0,
               }), \
         patch("alpha_agents.evolution.metrics.get_active_principles",
               return_value=[]), \
         patch("alpha_agents.evolution.metrics.get_all_principles_including_weakened",
               return_value=[]), \
         patch("alpha_agents.evolution.metrics.get_all_playbooks",
               return_value=[]), \
         patch("alpha_agents.evolution.metrics.get_recent_daily_lessons",
               return_value=[]):
        from alpha_agents.evolution.metrics import compute_evolution_metrics
        m = compute_evolution_metrics("2026-04-17")
    assert m["intraday_hit_rate_7d"] == 0.0
    assert m["active_principles"] == 0
    assert m["active_playbooks"] == 0
    assert m["lessons_count_7d"] == 0


def test_compute_metrics_tallies_populated_state():
    buckets = {
        "intraday_hit_rate_7d": 0.60, "intraday_count_7d": 200,
        "matched_hit_rate_7d": 0.75, "matched_count_7d": 40,
        "unmatched_hit_rate_7d": 0.56, "unmatched_count_7d": 160,
    }
    # 3 active principles + 1 weakened
    act_princ = [{"status": "active"}] * 3
    all_princ_inc_weak = act_princ + [{"status": "weakened"}]
    # 2 active playbooks + 1 degraded
    pbs = [{"status": "active"}, {"status": "active"},
            {"status": "degraded"}, {"status": "deprecated"}]
    # 10 lessons last 7d
    lessons = [{"date": "2026-04-16"}] * 10
    with patch("alpha_agents.evolution.metrics._query_intraday_buckets",
               return_value=buckets), \
         patch("alpha_agents.evolution.metrics.get_active_principles",
               return_value=act_princ), \
         patch("alpha_agents.evolution.metrics.get_all_principles_including_weakened",
               return_value=all_princ_inc_weak), \
         patch("alpha_agents.evolution.metrics.get_all_playbooks",
               return_value=pbs), \
         patch("alpha_agents.evolution.metrics.get_recent_daily_lessons",
               return_value=lessons):
        from alpha_agents.evolution.metrics import compute_evolution_metrics
        m = compute_evolution_metrics("2026-04-17")
    assert m["intraday_hit_rate_7d"] == 0.60
    assert m["active_principles"] == 3
    assert m["weakened_principles"] == 1
    assert m["active_playbooks"] == 2
    assert m["degraded_playbooks"] == 1
    assert m["lessons_count_7d"] == 10


def test_compute_metrics_persists_via_upsert():
    """compute_evolution_metrics writes to DB via upsert, returns the dict."""
    with patch("alpha_agents.evolution.metrics._query_intraday_buckets",
               return_value={
                   "intraday_hit_rate_7d": 0.5, "intraday_count_7d": 10,
                   "matched_hit_rate_7d": 0.0, "matched_count_7d": 0,
                   "unmatched_hit_rate_7d": 0.5, "unmatched_count_7d": 10,
               }), \
         patch("alpha_agents.evolution.metrics.get_active_principles",
               return_value=[]), \
         patch("alpha_agents.evolution.metrics.get_all_principles_including_weakened",
               return_value=[]), \
         patch("alpha_agents.evolution.metrics.get_all_playbooks",
               return_value=[]), \
         patch("alpha_agents.evolution.metrics.get_recent_daily_lessons",
               return_value=[]), \
         patch("alpha_agents.evolution.metrics.upsert_evolution_metrics") as m_upsert:
        from alpha_agents.evolution.metrics import compute_evolution_metrics
        result = compute_evolution_metrics("2026-04-17")
    m_upsert.assert_called_once()
    assert m_upsert.call_args.args[0] == "2026-04-17"
    assert m_upsert.call_args.args[1] == result


def test_format_metrics_trend_produces_readable_summary():
    from alpha_agents.evolution.metrics import format_metrics_trend
    rows = [
        {"date": "2026-04-10", "intraday_hit_rate_7d": 0.55,
         "matched_hit_rate_7d": 0.55, "matched_count_7d": 0,
         "unmatched_hit_rate_7d": 0.55, "unmatched_count_7d": 40,
         "active_principles": 0, "active_playbooks": 0, "lessons_count_7d": 0},
        {"date": "2026-04-17", "intraday_hit_rate_7d": 0.65,
         "matched_hit_rate_7d": 0.75, "matched_count_7d": 40,
         "unmatched_hit_rate_7d": 0.56, "unmatched_count_7d": 160,
         "active_principles": 5, "active_playbooks": 3, "lessons_count_7d": 18},
    ]
    text = format_metrics_trend(rows)
    assert "55" in text and "65" in text
    assert "matched" in text.lower() or "匹配" in text
```

- [ ] **Step 2: Implement `alpha_agents/evolution/metrics.py`**

```python
"""Phase 4 — Evolution meta-metrics. Is the evolution system actually helping?

Pure SQL aggregates + Python counts, no LLM. Computed daily at review end.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from alpha_agents.data.memory_store import (
    upsert_evolution_metrics,
    get_active_principles,
    get_all_principles_including_weakened,
    get_all_playbooks,
    get_recent_daily_lessons,
)

logger = logging.getLogger(__name__)


def _query_intraday_buckets(days: int = 7) -> dict:
    """SQL: verified intraday predictions bucketed by whether they matched a playbook.

    A prediction is 'matched' if its features_json matches any active playbook.
    Since matching is a runtime decision not persisted, we re-run it here against
    today's active playbook set — conservative but honest.
    """
    from alpha_agents.data.memory_store import _get_conn
    from alpha_agents.evolution.playbook import match_playbook
    import json

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = _get_conn().execute(
        "SELECT features_json, hit FROM predictions "
        "WHERE report_type = 'intraday' AND hit IS NOT NULL AND date >= ?",
        (cutoff,),
    ).fetchall()

    total = hits = m_total = m_hits = u_total = u_hits = 0
    for r in rows:
        total += 1
        if r["hit"]:
            hits += 1
        try:
            features = json.loads(r["features_json"] or "{}")
        except json.JSONDecodeError:
            features = {}
        matched = bool(features and match_playbook(features))
        if matched:
            m_total += 1
            if r["hit"]:
                m_hits += 1
        else:
            u_total += 1
            if r["hit"]:
                u_hits += 1
    return {
        "intraday_hit_rate_7d": (hits / total) if total else 0.0,
        "intraday_count_7d": total,
        "matched_hit_rate_7d": (m_hits / m_total) if m_total else 0.0,
        "matched_count_7d": m_total,
        "unmatched_hit_rate_7d": (u_hits / u_total) if u_total else 0.0,
        "unmatched_count_7d": u_total,
    }


def compute_evolution_metrics(today: str) -> dict:
    """Aggregate a daily snapshot of evolution-system health and persist it.
    Returns the computed dict."""
    buckets = _query_intraday_buckets(days=7)

    active_pr = len(get_active_principles())
    all_pr = get_all_principles_including_weakened()
    weakened_pr = sum(1 for p in all_pr if p.get("status") == "weakened")

    playbooks = get_all_playbooks()
    active_pb = sum(1 for p in playbooks if p.get("status") == "active")
    degraded_pb = sum(1 for p in playbooks if p.get("status") == "degraded")

    lessons_7d = len(get_recent_daily_lessons(days=7))

    metrics = {
        **buckets,
        "active_principles": active_pr,
        "weakened_principles": weakened_pr,
        "active_playbooks": active_pb,
        "degraded_playbooks": degraded_pb,
        "lessons_count_7d": lessons_7d,
    }
    try:
        upsert_evolution_metrics(today, metrics)
    except Exception as e:
        logger.warning("Failed to persist evolution metrics: %s", e)
    return metrics


def format_metrics_trend(rows: list[dict]) -> str:
    """Pretty-print a trend for display in reports / chat."""
    if not rows:
        return "（尚无 evolution_metrics 数据）"
    lines = ["【进化系统自评】"]
    lines.append(
        f"{'date':<12} {'7d胜率':>8} {'匹配胜率':>10} {'未匹配':>8}"
        f" {'原则':>5} {'Playbook':>10} {'lessons':>8}"
    )
    for r in rows:
        matched = (f"{r['matched_hit_rate_7d']*100:.0f}%"
                   f"/{r['matched_count_7d']}")
        unmatched = (f"{r['unmatched_hit_rate_7d']*100:.0f}%"
                     f"/{r['unmatched_count_7d']}")
        lines.append(
            f"{r['date']:<12} "
            f"{r['intraday_hit_rate_7d']*100:>6.0f}%  "
            f"{matched:>10} "
            f"{unmatched:>10} "
            f"{r['active_principles']:>5d} "
            f"{r['active_playbooks']:>10d} "
            f"{r.get('lessons_count_7d', 0):>8d}"
        )
    if len(rows) >= 2:
        first, last = rows[0], rows[-1]
        h_delta = (last["intraday_hit_rate_7d"] - first["intraday_hit_rate_7d"]) * 100
        lines.append(f"\n7d胜率变化：{h_delta:+.1f}pp （{first['date']} → {last['date']}）")
    return "\n".join(lines)
```

- [ ] **Step 3: Run tests — 4 pass.

- [ ] **Step 4: Commit**
```bash
git add alpha_agents/evolution/metrics.py tests/test_evolution_metrics.py
git commit -m "feat(evolution): compute_evolution_metrics — daily self-measurement snapshot"
```

---

## Task 3: Wire `compute_evolution_metrics` into `post_review`

**Files:**
- Modify: `alpha_agents/evolution/lessons.py` (extend `post_review`)
- Modify: `alpha_agents/evolution/__init__.py` (export)

- [ ] **Step 1: Extend `post_review`**

At the end of `post_review` (after the Phase 3 playbook stats block), BEFORE the final `return "\n".join(lines)`, add:

```python
    # Phase 4: daily metrics snapshot (self-measurement)
    try:
        from alpha_agents.evolution.metrics import compute_evolution_metrics
        metrics = await asyncio.to_thread(compute_evolution_metrics, today)
        # Only surface one-line delta vs yesterday; full trend in weekly_report
        lines.append(
            f"【进化自评】7d胜率{metrics['intraday_hit_rate_7d']*100:.0f}% | "
            f"Playbook匹配{metrics['matched_count_7d']}笔胜率{metrics['matched_hit_rate_7d']*100:.0f}% | "
            f"原则{metrics['active_principles']}活+{metrics['weakened_principles']}弱 | "
            f"Playbook{metrics['active_playbooks']}活+{metrics['degraded_playbooks']}弱"
        )
    except Exception as e:
        logger.warning("Evolution metrics computation failed: %s", e)
```

Also update the `nothing` check — metrics computation shouldn't count as "nothing happened" by itself, but the lessons/principles/playbooks section being empty still should skip the whole block. Simplest: if the metrics-append succeeds, the report is no longer empty. Leave the existing `nothing` logic but metrics lines are only added when we ARE producing output. Move the metrics line AFTER the `nothing` check so a completely quiet day still returns "":

```python
    nothing = (lesson_count == 0 and sum(counts.values()) == 0
               and not playbook_ops and not created_ids)

    # Always compute metrics (they run every day), but surface them only when
    # there's already something to report. This keeps the behavior of "silent
    # day → empty string" intact.
    try:
        from alpha_agents.evolution.metrics import compute_evolution_metrics
        metrics = await asyncio.to_thread(compute_evolution_metrics, today)
    except Exception as e:
        logger.warning("Evolution metrics computation failed: %s", e)
        metrics = None

    if nothing:
        return ""

    lines = ["【经验沉淀】"]
    # ... existing lines construction ...

    if metrics:
        lines.append(
            f"【进化自评】7d胜率{metrics['intraday_hit_rate_7d']*100:.0f}% | "
            f"Playbook匹配{metrics['matched_count_7d']}笔胜率{metrics['matched_hit_rate_7d']*100:.0f}% | "
            f"原则{metrics['active_principles']}活+{metrics['weakened_principles']}弱 | "
            f"Playbook{metrics['active_playbooks']}活+{metrics['degraded_playbooks']}弱"
        )

    return "\n".join(lines)
```

(Adjust flow as needed to match the existing `post_review` shape.)

- [ ] **Step 2: Export from `__init__.py`**

Add `from alpha_agents.evolution.metrics import compute_evolution_metrics, get_evolution_metrics_trend, format_metrics_trend` and list in `__all__`.

- [ ] **Step 3: Syntax + smoke**

```bash
uv run python -m py_compile alpha_agents/evolution/lessons.py alpha_agents/evolution/__init__.py
uv run python -c "from alpha_agents.evolution import compute_evolution_metrics, format_metrics_trend; print('OK')"
```

- [ ] **Step 4: Full evolution test suite passes** — 63 (59 + 4 new).

- [ ] **Step 5: Commit**
```bash
git add alpha_agents/evolution/lessons.py alpha_agents/evolution/__init__.py
git commit -m "feat(evolution): post_review writes daily evolution_metrics snapshot"
```

---

## Task 4: A/B mode in `build_morning_context`

**Files:**
- Modify: `alpha_agents/evolution/context_builder.py`
- Modify: `tests/test_evolution_context_builder.py`

- [ ] **Step 1: Failing tests**

```python
def test_build_morning_context_baseline_mode_strips_evolution_sections():
    with patch("alpha_agents.evolution.context_builder.inject_sentiment",
               return_value="【情绪周期】升温"), \
         patch("alpha_agents.evolution.context_builder.inject_cognition",
               return_value="【市场认知】\n• CPO: 高位"), \
         patch("alpha_agents.evolution.context_builder.inject_principles",
               return_value="【交易经验手册】\n• 这条不应该出现"), \
         patch("alpha_agents.evolution.context_builder.inject_recent_lessons",
               return_value="【近期教训】\n• 这条不应该出现"), \
         patch("alpha_agents.evolution.context_builder.inject_playbooks",
               return_value="【活跃 Playbook】\n• 这条不应该出现"):
        from alpha_agents.evolution.context_builder import build_morning_context
        baseline = build_morning_context(themes=[], stats="命中率62%", mode="baseline")
        full = build_morning_context(themes=[], stats="命中率62%", mode="full")
    # Baseline keeps sentiment + cognition + stats (pre-evolution world)
    assert "【情绪周期】" in baseline
    assert "【市场认知】" in baseline
    assert "命中率62%" in baseline
    # Baseline strips Phase 2 and Phase 3 sections
    assert "【交易经验手册】" not in baseline
    assert "【近期教训】" not in baseline
    assert "【活跃 Playbook】" not in baseline
    # Full mode has everything
    assert "【交易经验手册】" in full
    assert "【近期教训】" in full
    assert "【活跃 Playbook】" in full


def test_build_morning_context_default_mode_is_full():
    with patch("alpha_agents.evolution.context_builder.inject_sentiment", return_value=""), \
         patch("alpha_agents.evolution.context_builder.inject_cognition", return_value=""), \
         patch("alpha_agents.evolution.context_builder.inject_principles",
               return_value="【交易经验手册】\n• 必须出现"), \
         patch("alpha_agents.evolution.context_builder.inject_recent_lessons", return_value=""), \
         patch("alpha_agents.evolution.context_builder.inject_playbooks", return_value=""):
        from alpha_agents.evolution.context_builder import build_morning_context
        result = build_morning_context(themes=[], stats="")  # no mode → full
    assert "【交易经验手册】" in result
```

- [ ] **Step 2: Implement**

Update `build_morning_context` in `context_builder.py`:

```python
def build_morning_context(themes: list[dict], stats: str,
                          mode: str = "full") -> str:
    """Build the enriched context injected into the morning agent.

    Args:
        mode: "full" (default) includes all Phase 1/2/3 sections.
              "baseline" includes only Phase 1 (sentiment + cognition + stats) —
              used by Phase 4 A/B validation to compare against pre-evolution prompts.
    """
    sections = []
    # Phase 1 atoms — always included
    for part in (inject_sentiment(), inject_cognition()):
        if part:
            sections.append(part)
    # Phase 2/3 atoms — stripped in baseline mode
    if mode != "baseline":
        for part in (inject_principles(), inject_recent_lessons(),
                     inject_playbooks()):
            if part:
                sections.append(part)
    if stats:
        sections.append(stats)
    return "\n\n".join(sections)
```

Also update the 4 existing morning-context tests in `tests/test_evolution_context_builder.py` — they default to `mode="full"` which is the current behavior, so no changes needed unless they were passing `mode=` positionally. Verify they still pass unchanged.

- [ ] **Step 3: Run all tests — 65 pass (63 + 2 new).

- [ ] **Step 4: Commit**
```bash
git add alpha_agents/evolution/context_builder.py tests/test_evolution_context_builder.py
git commit -m "feat(evolution): build_morning_context mode='baseline' for A/B validation"
```

---

## Task 5: `/evolution` chat command

**Files:**
- Modify: `alpha_agents/agents/chat_commands/handlers.py`

- [ ] **Step 1: Add handler**

Following the existing registry pattern (same shape as `/playbook` from Phase 3):

```python
async def handle_evolution(arg: str, ctx: ChatContext) -> None:
    """Show evolution system health — recent metric trend."""
    from alpha_agents.evolution import get_evolution_metrics_trend, format_metrics_trend
    rows = get_evolution_metrics_trend(days=30)
    text = format_metrics_trend(rows)
    from rich.panel import Panel
    ctx.console.print(Panel(text, title="进化系统自评（30天）", border_style="cyan"))
```

Register as `Command("/evolution", "数据", "查看进化系统自评指标", handle_evolution)`.

- [ ] **Step 2: Syntax + smoke**

```bash
uv run python -m py_compile alpha_agents/agents/chat_commands/handlers.py
uv run python -c "from alpha_agents.agents.chat_commands.handlers import REGISTRY; print([c.name for c in REGISTRY if 'evolution' in c.name])"
```
Expected: `['/evolution']`.

- [ ] **Step 3: Commit**
```bash
git add alpha_agents/agents/chat_commands/handlers.py
git commit -m "feat(evolution): /evolution chat command shows 30-day metric trend"
```

---

## Task 6: Surface metrics in weekly_report

**Files:**
- Modify: `alpha_agents/pipeline/tasks/weekly_report.py`

- [ ] **Step 1: Locate where report text is assembled**

Open `weekly_report.py`. It likely assembles a report via an LLM call + some deterministic sections. Find where to inject the metrics block.

- [ ] **Step 2: Append the metrics block**

Just before the final notify/return, prepend a deterministic metrics section to the report:

```python
# Phase 4: append evolution self-assessment
try:
    from alpha_agents.evolution import get_evolution_metrics_trend, format_metrics_trend
    rows = get_evolution_metrics_trend(days=7)
    if rows:
        report = (report or "") + "\n\n" + format_metrics_trend(rows)
except Exception as e:
    logger.warning("Weekly evolution metrics block failed: %s", e)
```

Place this AFTER the LLM-generated weekly content is fully assembled, so the metrics aren't part of the LLM's context (they're a pure append).

- [ ] **Step 3: Syntax check + commit**

```bash
uv run python -m py_compile alpha_agents/pipeline/tasks/weekly_report.py
git add alpha_agents/pipeline/tasks/weekly_report.py
git commit -m "feat(evolution): weekly report surfaces evolution metrics trend"
```

---

## Task 7: Smoke + mark shipped

- [ ] **Step 1: Smoke run compute + fetch trend**

```bash
set -a; source .env; set +a
uv run python -c "
from alpha_agents.evolution import compute_evolution_metrics, get_evolution_metrics_trend, format_metrics_trend
m = compute_evolution_metrics('2026-04-17')
print('today metrics:', m)
print()
print(format_metrics_trend(get_evolution_metrics_trend(days=30)))
"
```
Expected: metrics dict with today's numbers; trend table showing 1 row (today only, since Phase 4 just started).

- [ ] **Step 2: Append Phase 4 shipped note to spec**

Append to `docs/superpowers/specs/2026-04-16-agent-evolution-design.md` (parallel to Phase 1/2/3 sections):

```markdown

---

## Phase 4 Shipped — 2026-MM-DD

Evolution meta-metrics + A/B validation landed.

**New table:** `evolution_metrics` (date-keyed upsert) tracks 12 daily signals:
7d hit rate (global/matched/unmatched), matched-vs-unmatched deltas,
active/weakened principle counts, active/degraded playbook counts, recent
lessons count.

**New module** `alpha_agents/evolution/metrics.py`:
- `compute_evolution_metrics(today)` — pure SQL + Python aggregation (no LLM)
- `get_evolution_metrics_trend(days)` — fetch last N days
- `format_metrics_trend(rows)` — pretty-print with WoW delta

**Integration:**
- `post_review()` now writes a daily snapshot and appends a one-line summary
- `weekly_report.py` appends the 7-day metrics trend block
- `build_morning_context(mode="baseline")` strips Phase 2/3 sections for A/B comparison
- `/evolution` chat command renders the 30-day trend

**Why this matters:** Phase 1-3 assume "more context = better decisions." Phase 4
tests that assumption with real data. If matched hit rate consistently beats
unmatched, playbooks are earning their keep. If not — the architecture itself
needs revision, and Phase 4 makes that signal visible.

**Phase 4 commits:** (fill in after completion)
```

- [ ] **Step 3: Commit**
```bash
git add docs/superpowers/specs/2026-04-16-agent-evolution-design.md
git commit -m "docs(evolution): mark Phase 4 shipped"
```

---

## Done Criteria (Phase 4)

- [ ] `evolution_metrics` table + upsert/trend helpers
- [ ] `compute_evolution_metrics` aggregates 12 fields from 3 tables + 1 SQL query
- [ ] `format_metrics_trend` produces a readable trend table with WoW delta
- [ ] `post_review` writes a daily snapshot and surfaces one-line summary
- [ ] `build_morning_context(mode="baseline")` strips Phase 2/3 sections
- [ ] `/evolution` chat command works
- [ ] `weekly_report` appends 7-day trend
- [ ] All previous tests (59) still pass; new Phase 4 tests: 2 schema + 4 metrics + 2 context_builder = 8 new
- [ ] Total: 67 tests passing
