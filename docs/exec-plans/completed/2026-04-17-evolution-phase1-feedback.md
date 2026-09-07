# Agent Evolution Phase 1 — L1 Feedback + Schema Extension

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the data loops that already exist (cognition, sentiment, VPA signal confirmations are written but never read by any agent), and extend the `predictions` table with `features_json` so downstream phases (Playbooks) can cluster by decision features.

**Architecture:** Create a new module `alpha_agents/evolution/` with `feedback.py` (inject helpers that query existing tables and return formatted strings) and `context_builder.py` (compose inject helpers into unified contexts for morning/chat/VPA agents). Wire three call sites (`morning_scan.py`, `vpa.py`, `chat.py`) to use the builders instead of their current ad-hoc context assembly. Add `features_json` column to `predictions` via idempotent ALTER TABLE, extend `save_prediction()` signature, and populate it from `intraday_monitor.py`.

**Tech Stack:** Python 3.11 + SQLite (via `alpha_agents.data.memory_store`) + pytest for unit tests. No new external dependencies.

**Spec:** `docs/superpowers/specs/2026-04-16-agent-evolution-design.md`

---

## File Map

### Created
- `alpha_agents/evolution/__init__.py` — public API (re-exports from feedback and context_builder)
- `alpha_agents/evolution/feedback.py` — `inject_sentiment()`, `inject_cognition()`, `inject_vpa_signal_history(code)`
- `alpha_agents/evolution/context_builder.py` — `build_morning_context()`, `build_chat_context()`, `build_vpa_context()` (Phase 1 sections only; leaves L2/L3 hooks as documented no-ops)
- `tests/test_evolution_feedback.py` — unit tests for inject helpers
- `tests/test_evolution_context_builder.py` — unit tests for context composition

### Modified
- `alpha_agents/data/memory_store.py` — (1) `_ensure_schema()` runs idempotent `ALTER TABLE predictions ADD COLUMN features_json` (2) `save_prediction()` accepts optional `features: dict | None = None`
- `alpha_agents/pipeline/tasks/intraday_monitor.py` — populate features dict and pass to `save_prediction()`
- `alpha_agents/tools/vpa.py` — `compute_vpa_with_llm()` calls `build_vpa_context(code)` and prepends result to LLM user_content
- `alpha_agents/pipeline/tasks/morning_scan.py` — replace existing context assembly with `build_morning_context(themes, stats)`; stop dropping `cognition` on the floor
- `alpha_agents/agents/chat.py` — `_build_context()` delegates to `build_chat_context()` and adds sentiment section

### Untouched (Phase 1 scope)
- `alpha_agents/agents/morning.py`, `review_agent.py`, `chat.py` prompt templates — no prompt changes, richer context arrives via user message
- `alpha_agents/tools/vpa.py` `ANNA_COULLING_PROMPT` — unchanged
- `alpha_agents/pipeline/tasks/review.py` — untouched in Phase 1 (post_review integration lands in Phase 2)

---

## Task 1: Add `features_json` column to predictions (idempotent migration)

**Files:**
- Modify: `alpha_agents/data/memory_store.py` (inside `_ensure_schema()`)
- Test: `tests/test_evolution_schema.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/test_evolution_schema.py`:
```python
"""Tests for Phase 1 schema extensions."""
import sqlite3
from pathlib import Path


def _fresh_db(path: Path) -> None:
    """Initialize a memory.db at path using the real schema."""
    import alpha_agents.data.memory_store as ms
    original = ms.MEMORY_DB_PATH
    ms.MEMORY_DB_PATH = path
    # Force fresh connection bound to this path
    if hasattr(ms._local, "conn"):
        del ms._local.conn
    ms._get_conn()  # triggers _ensure_schema
    ms.MEMORY_DB_PATH = original
    if hasattr(ms._local, "conn"):
        del ms._local.conn


def test_predictions_has_features_json_column(tmp_path):
    db = tmp_path / "memory.db"
    _fresh_db(db)
    conn = sqlite3.connect(str(db))
    cols = {row[1] for row in conn.execute("PRAGMA table_info(predictions)")}
    conn.close()
    assert "features_json" in cols


def test_alter_is_idempotent_on_existing_db(tmp_path):
    """Running _ensure_schema twice must not error (simulates app restart)."""
    db = tmp_path / "memory.db"
    _fresh_db(db)
    # Second init: should be no-op, not raise
    _fresh_db(db)
    conn = sqlite3.connect(str(db))
    cols = {row[1] for row in conn.execute("PRAGMA table_info(predictions)")}
    conn.close()
    assert "features_json" in cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_evolution_schema.py -v`
Expected: FAIL — `features_json` not in columns.

- [ ] **Step 3: Find `_ensure_schema` in memory_store.py**

Open `alpha_agents/data/memory_store.py`, locate the `_ensure_schema()` function (or equivalent — the one that runs `conn.executescript(_SCHEMA)` after connection creation). Identify where it runs after schema execution.

- [ ] **Step 4: Add idempotent ALTER TABLE**

After `conn.executescript(_SCHEMA)` in `_ensure_schema()`, add:

```python
# Phase 1 migration: add features_json to predictions (idempotent).
# Used by Playbook clustering (Phase 3) to group predictions by decision features
# (vpa_verdict, theme_strength, institutional, score, etc.).
try:
    conn.execute(
        "ALTER TABLE predictions ADD COLUMN features_json TEXT DEFAULT '{}'"
    )
    conn.commit()
except sqlite3.OperationalError as e:
    # Column already exists — expected on every restart after first migration.
    if "duplicate column name" not in str(e).lower():
        raise
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_evolution_schema.py -v`
Expected: PASS (both tests).

- [ ] **Step 6: Commit**

```bash
git add alpha_agents/data/memory_store.py tests/test_evolution_schema.py
git commit -m "feat(evolution): add features_json column to predictions (Phase 1 migration)"
```

---

## Task 2: Extend `save_prediction()` to accept optional `features` dict

**Files:**
- Modify: `alpha_agents/data/memory_store.py` (function `save_prediction` ~line 453)
- Test: `tests/test_evolution_schema.py` (append)

- [ ] **Step 1: Append failing test**

Append to `tests/test_evolution_schema.py`:
```python
import json


def test_save_prediction_writes_features_json(tmp_path, monkeypatch):
    import alpha_agents.data.memory_store as ms
    db = tmp_path / "memory.db"
    monkeypatch.setattr(ms, "MEMORY_DB_PATH", db)
    if hasattr(ms._local, "conn"):
        monkeypatch.delattr(ms._local, "conn", raising=False)

    features = {"vpa_verdict": "bullish", "theme_strength": 9, "score": 62}
    pred_id = ms.save_prediction(
        date="2026-04-17",
        report_type="intraday",
        code="300857",
        name="协创数据",
        direction="bullish",
        confidence="medium",
        theme_line="CPO",
        entry_price=303.96,
        reason="测试",
        features=features,
    )
    assert pred_id > 0
    row = ms._get_conn().execute(
        "SELECT features_json FROM predictions WHERE id = ?", (pred_id,)
    ).fetchone()
    assert json.loads(row["features_json"]) == features


def test_save_prediction_defaults_features_to_empty(tmp_path, monkeypatch):
    """Legacy callers that don't pass features must still work."""
    import alpha_agents.data.memory_store as ms
    db = tmp_path / "memory.db"
    monkeypatch.setattr(ms, "MEMORY_DB_PATH", db)
    if hasattr(ms._local, "conn"):
        monkeypatch.delattr(ms._local, "conn", raising=False)

    pred_id = ms.save_prediction(
        date="2026-04-17", report_type="morning", code="000001", name="平安银行",
        direction="bullish", confidence="low", theme_line="", entry_price=None,
        reason="legacy test",
    )
    row = ms._get_conn().execute(
        "SELECT features_json FROM predictions WHERE id = ?", (pred_id,)
    ).fetchone()
    # Either "{}" or empty string is acceptable as "no features"
    assert row["features_json"] in ("{}", "", None)
```

- [ ] **Step 2: Run test to verify failure**

Run: `uv run pytest tests/test_evolution_schema.py::test_save_prediction_writes_features_json -v`
Expected: FAIL — `save_prediction()` rejects `features` kwarg.

- [ ] **Step 3: Modify `save_prediction()` signature**

Replace the existing `save_prediction` function body:
```python
def save_prediction(
    date: str,
    report_type: str,
    code: str,
    name: str,
    direction: str,
    confidence: str,
    theme_line: str,
    entry_price: float | None,
    reason: str,
    features: dict | None = None,
) -> int:
    """Record a stock recommendation.

    ``features`` (Phase 1): optional dict of decision-time features used by the
    Playbook clustering in Phase 3. Serialized to ``features_json`` column.
    Pass None for legacy callers (stored as empty '{}').
    """
    features_json = json.dumps(features or {}, ensure_ascii=False)
    with _write_lock:
        conn = _get_conn()
        cur = conn.execute(
            "INSERT INTO predictions (date, report_type, code, name, direction, "
            "confidence, theme_line, entry_price, reason, features_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (date, report_type, code, name, direction, confidence, theme_line,
             entry_price, reason, features_json),
        )
        conn.commit()
        return cur.lastrowid
```

Ensure `import json` exists at the top of the file (it should already).

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_evolution_schema.py -v`
Expected: PASS (all 4 tests including the 2 from Task 1).

- [ ] **Step 5: Commit**

```bash
git add alpha_agents/data/memory_store.py tests/test_evolution_schema.py
git commit -m "feat(evolution): save_prediction accepts optional features dict"
```

---

## Task 3: Wire `features_json` from intraday_monitor

**Files:**
- Modify: `alpha_agents/pipeline/tasks/intraday_monitor.py` (the `save_prediction` call near line 1087)

- [ ] **Step 1: Locate the save_prediction call**

Open `alpha_agents/pipeline/tasks/intraday_monitor.py`, find the `save_prediction(` call (there's one around line 1087 inside the loop that records actionable/signal recommendations).

- [ ] **Step 2: Build the features dict before the call**

Immediately before `save_prediction(...)`, add:
```python
# Phase 1: capture decision-time features for Playbook clustering (Phase 3).
# Fields match the available_fields list in the evolution spec.
features = {
    "vpa_verdict": r.get("vpa_verdict", "unknown"),
    "vpa_phase": (r.get("vpa_result") or {}).get("llm_phase", ""),
    "theme": r.get("theme", ""),
    "score": r.get("score", 0),
    "change_pct": r.get("change_pct", 0),
    "institutional": r.get("institutional", ""),
    "rec_type": rec_type,  # 'signal' vs 'actionable'
}
```

- [ ] **Step 3: Pass features to save_prediction**

Update the `save_prediction(...)` call to pass `features=features` as the last kwarg:
```python
save_prediction(
    date=today,
    report_type=report_type,
    code=code,
    name=r.get("name", ""),
    direction="bullish",
    confidence=confidence,
    theme_line=r.get("theme", ""),
    entry_price=entry_price,
    reason=r.get("reason", "")[:100],
    features=features,
)
```

- [ ] **Step 4: Syntax check**

Run: `uv run python -m py_compile alpha_agents/pipeline/tasks/intraday_monitor.py`
Expected: exit 0, no output.

- [ ] **Step 5: Smoke test — run intraday once**

In chat mode, run `/intraday` and check that the task completes without errors. Then verify a recent prediction row has non-empty features_json:
```bash
sqlite3 data/memory.db "SELECT code, name, features_json FROM predictions WHERE date = date('now') AND report_type = 'intraday' ORDER BY id DESC LIMIT 3;"
```
Expected: `features_json` column shows JSON like `{"vpa_verdict":"bullish","vpa_phase":"...","score":62,...}`.

- [ ] **Step 6: Commit**

```bash
git add alpha_agents/pipeline/tasks/intraday_monitor.py
git commit -m "feat(evolution): populate features_json in intraday predictions"
```

---

## Task 4: Create `alpha_agents/evolution/` module skeleton

**Files:**
- Create: `alpha_agents/evolution/__init__.py`
- Create: `alpha_agents/evolution/feedback.py` (empty stub)
- Create: `alpha_agents/evolution/context_builder.py` (empty stub)

- [ ] **Step 1: Create the module directory and files**

```bash
mkdir -p alpha_agents/evolution
```

Create `alpha_agents/evolution/__init__.py`:
```python
"""Agent evolution layer — closes the learning loop.

Phase 1 (this module): L1 Feedback — inject data that's already written but
not read by any agent (cognition, sentiment, VPA signal confirmations).

Phase 2+ (not yet implemented): L2 Lessons/Principles, L3 Playbooks.
See docs/superpowers/specs/2026-04-16-agent-evolution-design.md.

Public API:
    build_morning_context, build_chat_context, build_vpa_context  — compose
    inject_sentiment, inject_cognition, inject_vpa_signal_history  — atoms
"""

from alpha_agents.evolution.feedback import (
    inject_cognition,
    inject_sentiment,
    inject_vpa_signal_history,
)
from alpha_agents.evolution.context_builder import (
    build_chat_context,
    build_morning_context,
    build_vpa_context,
)

__all__ = [
    "build_chat_context",
    "build_morning_context",
    "build_vpa_context",
    "inject_cognition",
    "inject_sentiment",
    "inject_vpa_signal_history",
]
```

Create `alpha_agents/evolution/feedback.py`:
```python
"""L1 Feedback — query-and-format helpers for data that exists but wasn't injected."""

from __future__ import annotations


def inject_sentiment() -> str:
    """Stub — filled in Task 5."""
    return ""


def inject_cognition() -> str:
    """Stub — filled in Task 7."""
    return ""


def inject_vpa_signal_history(code: str) -> str:
    """Stub — filled in Task 9."""
    return ""
```

Create `alpha_agents/evolution/context_builder.py`:
```python
"""Compose L1 feedback atoms into unified agent contexts."""

from __future__ import annotations

from alpha_agents.evolution.feedback import (
    inject_cognition,
    inject_sentiment,
    inject_vpa_signal_history,
)


def build_morning_context(themes: list[dict], stats: str) -> str:
    """Stub — filled in Task 14."""
    return ""


def build_chat_context(portfolio_summary: str, themes_summary: str, stats_summary: str) -> str:
    """Stub — filled in Task 16."""
    return ""


def build_vpa_context(code: str) -> str:
    """Stub — filled in Task 12."""
    return ""
```

- [ ] **Step 2: Syntax check + import**

Run:
```bash
uv run python -c "from alpha_agents.evolution import build_morning_context, build_chat_context, build_vpa_context, inject_cognition, inject_sentiment, inject_vpa_signal_history; print('OK')"
```
Expected: `OK`.

- [ ] **Step 3: Commit**

```bash
git add alpha_agents/evolution/
git commit -m "feat(evolution): scaffold alpha_agents/evolution module"
```

---

## Task 5: Write failing test for `inject_sentiment`

**Files:**
- Create: `tests/test_evolution_feedback.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_evolution_feedback.py`:
```python
"""Tests for alpha_agents.evolution.feedback."""
from unittest.mock import patch


def test_inject_sentiment_returns_formatted_block():
    fake_cycle = {"phase": "升温", "confidence": 0.8,
                  "strategy": "可追强势，高beta优先"}
    with patch("alpha_agents.evolution.feedback.get_sentiment_cycle",
               return_value=fake_cycle):
        from alpha_agents.evolution.feedback import inject_sentiment
        result = inject_sentiment()
    assert "【情绪周期】" in result
    assert "升温" in result
    assert "可追强势" in result


def test_inject_sentiment_returns_empty_when_db_empty():
    with patch("alpha_agents.evolution.feedback.get_sentiment_cycle",
               return_value=None):
        from alpha_agents.evolution.feedback import inject_sentiment
        result = inject_sentiment()
    assert result == ""


def test_inject_sentiment_fits_budget():
    """Sentiment section budget is 50 chars per spec."""
    fake_cycle = {"phase": "升温", "confidence": 0.8,
                  "strategy": "可追强势，高beta优先，" + "额外说明" * 30}
    with patch("alpha_agents.evolution.feedback.get_sentiment_cycle",
               return_value=fake_cycle):
        from alpha_agents.evolution.feedback import inject_sentiment
        result = inject_sentiment()
    assert len(result) <= 80  # 50 char budget + small header overhead
```

- [ ] **Step 2: Run test to verify failure**

Run: `uv run pytest tests/test_evolution_feedback.py::test_inject_sentiment_returns_formatted_block -v`
Expected: FAIL — current stub returns `""`.

---

## Task 6: Implement `inject_sentiment`

**Files:**
- Modify: `alpha_agents/evolution/feedback.py`

- [ ] **Step 1: Replace the stub**

In `alpha_agents/evolution/feedback.py`, replace `inject_sentiment`:
```python
def inject_sentiment() -> str:
    """Format today's sentiment cycle phase for agent context.

    Returns empty string if no cycle is saved yet (first-run edge case).
    """
    from alpha_agents.data.sentiment_cycle import get_sentiment_cycle
    cycle = get_sentiment_cycle()
    if not cycle:
        return ""
    phase = cycle.get("phase", "")
    strategy = cycle.get("strategy", "")
    if not phase:
        return ""
    # Truncate strategy to stay within ~50 char budget
    if len(strategy) > 40:
        strategy = strategy[:40].rstrip() + "…"
    return f"【情绪周期】{phase}" + (f" — {strategy}" if strategy else "")
```

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/test_evolution_feedback.py -v`
Expected: the 3 sentiment tests PASS.

- [ ] **Step 3: Commit**

```bash
git add alpha_agents/evolution/feedback.py tests/test_evolution_feedback.py
git commit -m "feat(evolution): inject_sentiment — reads sentiment_cycle into agent context"
```

---

## Task 7: Write failing test for `inject_cognition`

**Files:**
- Modify: `tests/test_evolution_feedback.py`

- [ ] **Step 1: Append tests**

Append to `tests/test_evolution_feedback.py`:
```python
def test_inject_cognition_formats_sectors():
    fake_rows = [
        {"sector": "CPO", "position": "high", "fund_trend": "inflow",
         "assessment": "强势延续"},
        {"sector": "数据中心", "position": "mid", "fund_trend": "inflow",
         "assessment": "资金驱动但缺龙头"},
    ]
    with patch("alpha_agents.evolution.feedback.get_all_cognition_latest",
               return_value=fake_rows):
        from alpha_agents.evolution.feedback import inject_cognition
        result = inject_cognition()
    assert "【市场认知】" in result
    assert "CPO" in result and "高位" in result
    assert "数据中心" in result and "中位" in result
    assert "强势延续" in result


def test_inject_cognition_empty_when_no_data():
    with patch("alpha_agents.evolution.feedback.get_all_cognition_latest",
               return_value=[]):
        from alpha_agents.evolution.feedback import inject_cognition
        assert inject_cognition() == ""


def test_inject_cognition_respects_budget():
    """Cognition section budget is 300 chars per spec. Older/lower-priority
    entries should be truncated when exceeding."""
    fake_rows = [
        {"sector": f"Sector{i}", "position": "high", "fund_trend": "inflow",
         "assessment": "评估内容" * 10}
        for i in range(20)
    ]
    with patch("alpha_agents.evolution.feedback.get_all_cognition_latest",
               return_value=fake_rows):
        from alpha_agents.evolution.feedback import inject_cognition
        result = inject_cognition()
    assert len(result) <= 400  # 300 budget + header overhead
```

- [ ] **Step 2: Run test**

Run: `uv run pytest tests/test_evolution_feedback.py -v -k inject_cognition`
Expected: FAIL (stub still returns "").

---

## Task 8: Implement `inject_cognition`

**Files:**
- Modify: `alpha_agents/evolution/feedback.py`

- [ ] **Step 1: Replace the stub**

```python
_POSITION_MAP = {"high": "高位", "mid": "中位", "low": "低位"}
_TREND_MAP = {"inflow": "资金流入", "outflow": "资金流出", "neutral": "资金中性"}
_COGNITION_BUDGET = 300


def inject_cognition() -> str:
    """Format latest market cognition (per sector) for agent context.

    Reads the `market_cognition` table via get_all_cognition_latest. The
    review task writes one row per active theme every day; we surface the
    latest. Truncates to stay within budget, dropping lowest-priority
    (alphabetical last) sectors first.
    """
    from alpha_agents.data.memory_store import get_all_cognition_latest
    rows = get_all_cognition_latest()
    if not rows:
        return ""
    lines = ["【市场认知】"]
    for r in rows:
        sector = r.get("sector", "")
        pos = _POSITION_MAP.get(r.get("position", ""), r.get("position", ""))
        trend = _TREND_MAP.get(r.get("fund_trend", ""), r.get("fund_trend", ""))
        assessment = r.get("assessment", "")
        line = f"• {sector}: {pos} + {trend} — \"{assessment}\""
        if sum(len(x) for x in lines) + len(line) > _COGNITION_BUDGET:
            break
        lines.append(line)
    return "\n".join(lines)
```

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/test_evolution_feedback.py -v`
Expected: all tests PASS.

- [ ] **Step 3: Commit**

```bash
git add alpha_agents/evolution/feedback.py tests/test_evolution_feedback.py
git commit -m "feat(evolution): inject_cognition — surfaces market_cognition to agents"
```

---

## Task 9: Write failing test for `inject_vpa_signal_history`

**Files:**
- Modify: `tests/test_evolution_feedback.py`

- [ ] **Step 1: Append tests**

Append to `tests/test_evolution_feedback.py`:
```python
def test_inject_vpa_signal_history_formats_confirmed_and_pending():
    fake_signals = [
        {"signal_type": "射击十字星", "signal_date": "2026-04-14",
         "direction": "偏空", "status": "confirmed",
         "resolved_by": "今日跌3.6%", "resolved_date": "2026-04-15"},
        {"signal_type": "缩量止跌", "signal_date": "2026-04-16",
         "direction": "偏多", "status": "pending",
         "resolved_by": "", "resolved_date": ""},
    ]
    with patch("alpha_agents.evolution.feedback._query_vpa_signals_for_code",
               return_value=fake_signals):
        from alpha_agents.evolution.feedback import inject_vpa_signal_history
        result = inject_vpa_signal_history("300274")
    assert "VPA信号历史" in result
    assert "射击十字星" in result and "✅" in result
    assert "缩量止跌" in result and "⏳" in result


def test_inject_vpa_signal_history_empty_code():
    from alpha_agents.evolution.feedback import inject_vpa_signal_history
    assert inject_vpa_signal_history("") == ""
    assert inject_vpa_signal_history("abc") == ""  # not 6 digits


def test_inject_vpa_signal_history_no_signals_returns_empty():
    with patch("alpha_agents.evolution.feedback._query_vpa_signals_for_code",
               return_value=[]):
        from alpha_agents.evolution.feedback import inject_vpa_signal_history
        assert inject_vpa_signal_history("300274") == ""
```

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/test_evolution_feedback.py -v -k vpa_signal_history`
Expected: FAIL — stub + `_query_vpa_signals_for_code` doesn't exist.

---

## Task 10: Implement `inject_vpa_signal_history`

**Files:**
- Modify: `alpha_agents/evolution/feedback.py`

- [ ] **Step 1: Add a query helper and replace the stub**

At the end of `feedback.py`:
```python
def _query_vpa_signals_for_code(code: str, days: int = 14) -> list[dict]:
    """Return VPA signals for this code within last N days, newest first.

    Pulled out so tests can mock this without a real DB.
    """
    from datetime import datetime, timedelta
    from alpha_agents.data.memory_store import _get_conn
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = _get_conn().execute(
        "SELECT signal_type, signal_date, direction, status, resolved_by, resolved_date "
        "FROM vpa_pending_signals "
        "WHERE code = ? AND signal_date >= ? "
        "ORDER BY signal_date DESC LIMIT 8",
        (code, cutoff),
    ).fetchall()
    return [dict(r) for r in rows]


_SIGNAL_STATUS_ICON = {
    "confirmed": "✅已确认",
    "denied": "❌已否定",
    "expired": "⌛已过期",
    "pending": "⏳待确认",
}
_VPA_SIGNAL_BUDGET = 400


def inject_vpa_signal_history(code: str) -> str:
    """Format this stock's recent VPA signal history for the VPA LLM prompt.

    Closes the feedback loop: signals predicted by prior VPA analyses are
    shown as confirmed/denied based on market outcomes, so the LLM can
    learn from its own track record.
    """
    if not code or not code.isdigit() or len(code) != 6:
        return ""
    signals = _query_vpa_signals_for_code(code)
    if not signals:
        return ""
    lines = ["【该股VPA信号历史】"]
    for s in signals:
        icon = _SIGNAL_STATUS_ICON.get(s.get("status", "pending"), "⏳待确认")
        date = s.get("signal_date", "")[5:]  # "2026-04-14" → "04-14"
        stype = s.get("signal_type", "")
        direction = s.get("direction", "")
        resolved = s.get("resolved_by", "")
        tail = f"（{resolved}）" if resolved else ""
        line = f"• {date} {stype}({direction}) → {icon}{tail}"
        if sum(len(x) for x in lines) + len(line) > _VPA_SIGNAL_BUDGET:
            break
        lines.append(line)
    return "\n".join(lines)
```

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/test_evolution_feedback.py -v`
Expected: all tests PASS.

- [ ] **Step 3: Commit**

```bash
git add alpha_agents/evolution/feedback.py tests/test_evolution_feedback.py
git commit -m "feat(evolution): inject_vpa_signal_history — VPA self-feedback loop"
```

---

## Task 11: Write failing test for `build_vpa_context`

**Files:**
- Create: `tests/test_evolution_context_builder.py`

- [ ] **Step 1: Write the test**

Create `tests/test_evolution_context_builder.py`:
```python
"""Tests for alpha_agents.evolution.context_builder."""
from unittest.mock import patch


def test_build_vpa_context_composes_signal_history():
    with patch("alpha_agents.evolution.context_builder.inject_vpa_signal_history",
               return_value="【该股VPA信号历史】\n• 04-14 射击十字星(偏空) → ✅已确认"):
        from alpha_agents.evolution.context_builder import build_vpa_context
        result = build_vpa_context("300274")
    assert "VPA信号历史" in result
    assert "射击十字星" in result


def test_build_vpa_context_empty_when_no_signals():
    with patch("alpha_agents.evolution.context_builder.inject_vpa_signal_history",
               return_value=""):
        from alpha_agents.evolution.context_builder import build_vpa_context
        result = build_vpa_context("300274")
    assert result == ""
```

- [ ] **Step 2: Run test**

Run: `uv run pytest tests/test_evolution_context_builder.py -v`
Expected: FAIL (stub returns "").

---

## Task 12: Implement `build_vpa_context`

**Files:**
- Modify: `alpha_agents/evolution/context_builder.py`

- [ ] **Step 1: Replace stub**

```python
def build_vpa_context(code: str) -> str:
    """Build the context block injected before VPA LLM analysis.

    Phase 1: only VPA signal history (L1 feedback).
    Phase 3 hook: will also include matched playbook info (see spec §VPA build).
    """
    signal_history = inject_vpa_signal_history(code)
    sections = [s for s in (signal_history,) if s]
    return "\n\n".join(sections)
```

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/test_evolution_context_builder.py -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add alpha_agents/evolution/context_builder.py tests/test_evolution_context_builder.py
git commit -m "feat(evolution): build_vpa_context composes L1 signal history"
```

---

## Task 13: Write failing test for `build_morning_context`

**Files:**
- Modify: `tests/test_evolution_context_builder.py`

- [ ] **Step 1: Append tests**

Append:
```python
def test_build_morning_context_includes_sentiment_and_cognition():
    with patch("alpha_agents.evolution.context_builder.inject_sentiment",
               return_value="【情绪周期】升温 — 可追强势"), \
         patch("alpha_agents.evolution.context_builder.inject_cognition",
               return_value="【市场认知】\n• CPO: 高位 + 资金流入 — \"强势延续\""):
        from alpha_agents.evolution.context_builder import build_morning_context
        result = build_morning_context(themes=[], stats="命中率62%")
    assert "【情绪周期】" in result
    assert "【市场认知】" in result
    assert "命中率62%" in result  # stats still passed through


def test_build_morning_context_handles_empty_sections():
    with patch("alpha_agents.evolution.context_builder.inject_sentiment",
               return_value=""), \
         patch("alpha_agents.evolution.context_builder.inject_cognition",
               return_value=""):
        from alpha_agents.evolution.context_builder import build_morning_context
        result = build_morning_context(themes=[], stats="")
    # Empty sections dropped, result is empty or minimal
    assert "【情绪周期】" not in result
    assert "【市场认知】" not in result
```

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/test_evolution_context_builder.py -v -k morning`
Expected: FAIL.

---

## Task 14: Implement `build_morning_context`

**Files:**
- Modify: `alpha_agents/evolution/context_builder.py`

- [ ] **Step 1: Replace stub**

```python
def build_morning_context(themes: list[dict], stats: str) -> str:
    """Build the enriched context injected into the morning agent.

    Phase 1: sentiment + cognition (both currently lost by morning_scan) +
    the existing stats string.
    Phase 2 hook: will also include principles + recent daily_lessons.
    Phase 3 hook: will also include active playbooks.

    The ``themes`` list is currently not used here (morning_scan passes it
    in a separate ``themes_ctx`` argument to run_morning_analysis). Kept in
    the signature so callers have a single entry point and future phases
    can leverage it (e.g., filtering lessons by active theme).
    """
    sections = []
    sentiment = inject_sentiment()
    if sentiment:
        sections.append(sentiment)
    cognition = inject_cognition()
    if cognition:
        sections.append(cognition)
    if stats:
        sections.append(stats)
    return "\n\n".join(sections)
```

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/test_evolution_context_builder.py -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add alpha_agents/evolution/context_builder.py tests/test_evolution_context_builder.py
git commit -m "feat(evolution): build_morning_context adds sentiment + cognition"
```

---

## Task 15: Write failing test for `build_chat_context`

**Files:**
- Modify: `tests/test_evolution_context_builder.py`

- [ ] **Step 1: Append tests**

```python
def test_build_chat_context_adds_sentiment_to_existing_sections():
    with patch("alpha_agents.evolution.context_builder.inject_sentiment",
               return_value="【情绪周期】升温"):
        from alpha_agents.evolution.context_builder import build_chat_context
        result = build_chat_context(
            portfolio_summary="总资金 100,000元",
            themes_summary="CPO强度10/10",
            stats_summary="命中率62%",
        )
    assert "总资金" in result
    assert "CPO强度" in result
    assert "命中率62%" in result
    assert "【情绪周期】" in result


def test_build_chat_context_empty_sentiment_skipped():
    with patch("alpha_agents.evolution.context_builder.inject_sentiment",
               return_value=""):
        from alpha_agents.evolution.context_builder import build_chat_context
        result = build_chat_context("持仓A", "主线B", "统计C")
    assert "【情绪周期】" not in result
    assert "持仓A" in result
```

- [ ] **Step 2: Run test**

Run: `uv run pytest tests/test_evolution_context_builder.py -v -k chat`
Expected: FAIL.

---

## Task 16: Implement `build_chat_context`

**Files:**
- Modify: `alpha_agents/evolution/context_builder.py`

- [ ] **Step 1: Replace stub**

```python
def build_chat_context(portfolio_summary: str, themes_summary: str,
                       stats_summary: str) -> str:
    """Build the chat agent's system-prompt context string.

    Phase 1: adds sentiment on top of the existing portfolio / themes / stats.
    Phase 2 hook: will also inject principles + recent lessons.
    """
    sections = []
    if portfolio_summary:
        sections.append(portfolio_summary)
    if themes_summary:
        sections.append(themes_summary)
    if stats_summary:
        sections.append(stats_summary)
    sentiment = inject_sentiment()
    if sentiment:
        sections.append(sentiment)
    return "\n\n".join(sections)
```

- [ ] **Step 2: Run tests**

Run: `uv run pytest tests/test_evolution_context_builder.py -v`
Expected: all PASS.

- [ ] **Step 3: Commit**

```bash
git add alpha_agents/evolution/context_builder.py tests/test_evolution_context_builder.py
git commit -m "feat(evolution): build_chat_context adds sentiment section"
```

---

## Task 17: Wire `build_vpa_context` into `compute_vpa_with_llm`

**Files:**
- Modify: `alpha_agents/tools/vpa.py` (around line 1820 where `user_content` is assembled inside `_call_llm_vpa`)

- [ ] **Step 1: Locate the user_content assembly**

Open `alpha_agents/tools/vpa.py` and find where `user_content` is built before the `client.chat.completions.create(...)` call (around line 1820 based on earlier exploration). The structure is approximately:
```python
user_content = f"以下是 {code} 的量价预计算数据..."
if previous_analysis:
    user_content = (
        f"【上次分析参考】...{previous_analysis[:2000]}\n\n"
        f"---\n\n"
        f"【最新数据】{user_content}"
    )
```

- [ ] **Step 2: Inject VPA signal history before user_content**

Right after `user_content` is fully assembled (after the `previous_analysis` conditional), add:
```python
# Phase 1 L1: inject this stock's VPA signal history (confirmed/denied/pending)
# so the LLM can see its own track record and re-evaluate recent calls.
try:
    from alpha_agents.evolution import build_vpa_context
    signal_ctx = build_vpa_context(code)
    if signal_ctx:
        user_content = signal_ctx + "\n\n" + user_content
except Exception as e:
    logger.debug("VPA context injection failed (non-fatal): %s", e)
```

- [ ] **Step 3: Syntax check**

Run: `uv run python -m py_compile alpha_agents/tools/vpa.py`
Expected: exit 0.

- [ ] **Step 4: Smoke test**

In chat mode, run `/vpa 300274` on any stock that has prior VPA analyses. Verify in `data/scheduler.log` that the LLM request includes the signal history header (search for "VPA信号历史"). The VPA report itself should work as before — this only adds context.

- [ ] **Step 5: Commit**

```bash
git add alpha_agents/tools/vpa.py
git commit -m "feat(evolution): inject VPA signal history into Anna Coulling LLM prompt"
```

---

## Task 18: Wire `build_morning_context` into `morning_scan`

**Files:**
- Modify: `alpha_agents/pipeline/tasks/morning_scan.py`

- [ ] **Step 1: Locate the call to run_morning_analysis**

Open `morning_scan.py` and find the `run_morning_analysis(events_ctx, themes_ctx, stats_ctx)` call (around line 265). Nearby there should be a `cognition = get_all_cognition_latest()` that's read but never passed (the dropped data point — Phase 1 fixes this).

- [ ] **Step 2: Replace stats_ctx with enriched context**

Just before the `run_morning_analysis(...)` call, add:
```python
# Phase 1: enrich stats_ctx with sentiment + cognition via evolution module.
# Previously `cognition` was fetched above but silently dropped.
from alpha_agents.evolution import build_morning_context
stats_ctx = build_morning_context(themes=themes, stats=stats_ctx)
```

Leave the `run_morning_analysis(events_ctx, themes_ctx, stats_ctx)` call unchanged — it now receives the enriched stats_ctx.

If there's a now-unused `cognition = get_all_cognition_latest()` line, either remove it or add a comment: `# Note: cognition now flows through build_morning_context()`. Whichever keeps the diff cleaner.

- [ ] **Step 3: Syntax check**

Run: `uv run python -m py_compile alpha_agents/pipeline/tasks/morning_scan.py`
Expected: exit 0.

- [ ] **Step 4: Smoke test — run morning scan**

In chat mode, run `/morning`. Check `data/scheduler.log` for a log entry around the morning agent call that shows the context passed in. Alternatively, add a one-shot `logger.info("Morning context enriched: %d chars", len(stats_ctx))` right after the enrichment, commit a separate follow-up if needed.

- [ ] **Step 5: Commit**

```bash
git add alpha_agents/pipeline/tasks/morning_scan.py
git commit -m "feat(evolution): morning scan uses build_morning_context (injects sentiment+cognition)"
```

---

## Task 19: Wire `build_chat_context` into `chat.py`

**Files:**
- Modify: `alpha_agents/agents/chat.py`

- [ ] **Step 1: Locate `_build_context()`**

Open `alpha_agents/agents/chat.py` and find `_build_context()`. It returns a concatenation of `portfolio_summary`, `themes_summary`, `stats_summary` (plus `chat_memories`).

- [ ] **Step 2: Delegate the first three sections to build_chat_context**

Inside `_build_context()`, replace the manual concatenation of portfolio/themes/stats with a call to `build_chat_context()`. Preserve the chat_memories section. Approximately:

```python
def _build_context() -> str:
    portfolio_summary = get_open_positions_summary()
    # ... existing themes_summary / stats_summary assembly ...
    themes_summary = ...
    stats_summary = ...

    from alpha_agents.evolution import build_chat_context
    base_ctx = build_chat_context(
        portfolio_summary=portfolio_summary,
        themes_summary=themes_summary,
        stats_summary=stats_summary,
    )

    # Preserve chat_memories (not yet part of evolution module)
    memories = get_recent_chat_memories(limit=3)
    if memories:
        mem_text = "\n\n【上次对话记忆】\n" + "\n".join(
            f"- {m['summary']}" for m in memories
        )
        return base_ctx + mem_text
    return base_ctx
```

Match the existing variable names in the current `_build_context()` — do not rename.

- [ ] **Step 3: Syntax check**

Run: `uv run python -m py_compile alpha_agents/agents/chat.py`
Expected: exit 0.

- [ ] **Step 4: Smoke test**

Start chat mode. In the first turn, ask any free-form question (e.g., "当前市场情绪如何"). The agent should now reference the sentiment phase in its answer. Inspect `data/scheduler.log` for the constructed system prompt — the "【情绪周期】" section should appear.

- [ ] **Step 5: Commit**

```bash
git add alpha_agents/agents/chat.py
git commit -m "feat(evolution): chat _build_context delegates to build_chat_context"
```

---

## Task 20: Final regression check — all existing tests still pass

**Files:** — no code changes, verification only

- [ ] **Step 1: Run the full test suite**

Run: `uv run pytest tests/ -x --ignore=tests/test_embeddings.py -q`
(Skip `test_embeddings.py` if it requires API keys or external network.)

Expected: all tests pass. If a previously-passing test now fails, investigate — Phase 1 should not break any existing behavior.

- [ ] **Step 2: Manual end-to-end check**

In chat mode, exercise each wired entry point:
1. `/vpa <code>` — VPA report generated, signal history visible in log
2. `/morning` — report mentions sentiment phase and sector cognition at least implicitly
3. Natural-language chat — agent answers mentioning sentiment/cognition when relevant

- [ ] **Step 3: Commit (no-op)**

If any touch-ups were needed, commit them; otherwise proceed.

---

## Task 21: Document Phase 1 in spec revision history

**Files:**
- Modify: `docs/superpowers/specs/2026-04-16-agent-evolution-design.md`

- [ ] **Step 1: Append a "Phase 1 Shipped" note**

At the very bottom of the spec file, append:
```markdown

---

## Phase 1 Shipped — 2026-04-XX

L1 Feedback + `features_json` schema extension landed in:
- `alpha_agents/evolution/{__init__, feedback, context_builder}.py`
- `alpha_agents/data/memory_store.py` (column + save_prediction signature)
- Wired into `intraday_monitor.py`, `vpa.py`, `morning_scan.py`, `chat.py`

Verified: sentiment/cognition/vpa-signal-history now reach the morning, chat,
and VPA agents. Features populated on every new intraday prediction — enabling
Phase 3 playbook clustering after ≥2 weeks of data accumulation.
```

Replace `2026-04-XX` with the actual ship date.

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/specs/2026-04-16-agent-evolution-design.md
git commit -m "docs(evolution): mark Phase 1 shipped in spec revision history"
```

---

## Done Criteria

- [ ] `features_json` column exists on `predictions` (idempotent migration)
- [ ] `save_prediction()` accepts `features=...` kwarg, writes JSON
- [ ] Intraday predictions populated with features
- [ ] `alpha_agents/evolution/` module imports cleanly
- [ ] `inject_sentiment`, `inject_cognition`, `inject_vpa_signal_history` each have ≥2 unit tests passing
- [ ] `build_morning_context`, `build_chat_context`, `build_vpa_context` each have ≥2 unit tests passing
- [ ] Morning scan surfaces sentiment + cognition to the morning agent
- [ ] VPA LLM prompt includes signal history when a code has prior signals
- [ ] Chat agent's system prompt includes sentiment
- [ ] Full test suite passes
- [ ] Spec updated with "Phase 1 Shipped" note
