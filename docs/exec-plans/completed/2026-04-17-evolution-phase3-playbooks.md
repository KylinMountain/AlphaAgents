# Agent Evolution Phase 3 — L3 Playbooks

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan. Wait until ≥2 weeks of `features_json`-populated predictions exist before starting execution — auto-creation needs real clustering data.

**Goal:** Codify successful trading patterns as playbooks with rule-driven weight adjustments and LLM-annotated degradation. Playbooks match candidates at intraday matching time and multiply score by weight.

**Architecture:** New `alpha_agents/evolution/playbook.py` with (a) pattern matching against candidate dicts, (b) rule-driven lifecycle (active/degraded/deprecated with weights 1.5/1.0/0.5/0.0), (c) auto-creation from feature clusters in `predictions.features_json`, (d) LLM annotation when thresholds trigger. Wired into `intraday_monitor.py` (score multiplication) and `post_review()` (daily stats update). New chat command `/playbook`.

**Tech Stack:** same as Phase 1/2 — Python 3.11, SQLite, pytest. LLM only for optional annotation (most days skipped).

**Spec:** `docs/superpowers/specs/2026-04-16-agent-evolution-design.md` §Playbooks.
**Branch:** `feat/evolution-phase1` (continue).

---

## Prerequisites

Before starting execution, verify:
- [ ] `predictions` table has ≥2 weeks of rows with non-empty `features_json` AND `hit IS NOT NULL` AND `report_type='intraday'` (not `intraday_signal`)
- [ ] Enough hit predictions per feature cluster: run a probe query to confirm ≥3 patterns have ≥3 hits each, otherwise auto-creation produces nothing

Probe query:
```sql
SELECT
    json_extract(features_json, '$.vpa_verdict') as vpa,
    json_extract(features_json, '$.theme') as theme,
    COUNT(*) FILTER (WHERE hit=1) as wins,
    COUNT(*) as total
FROM predictions
WHERE report_type='intraday'
  AND hit IS NOT NULL
  AND features_json != '{}'
  AND date >= date('now', '-14 days')
GROUP BY vpa, theme
HAVING wins >= 3
ORDER BY wins DESC;
```

If result is empty or sparse (<3 clusters), hold off and give it another week.

---

## File Map

### Created
- `alpha_agents/evolution/playbook.py` — `match_playbook`, `update_playbook_stats`, `annotate_degraded`, `create_playbook_from_pattern`, `scan_and_auto_create`
- `tests/test_evolution_playbook.py` — unit tests for matching + rule engine + auto-create (LLM mocked)

### Modified
- `alpha_agents/data/memory_store.py` — add `playbooks` table + CRUD helpers
- `alpha_agents/evolution/__init__.py` — export `match_playbook` and `post_review` now dispatches playbook updates
- `alpha_agents/evolution/lessons.py` — `post_review` also calls `update_playbook_stats`
- `alpha_agents/pipeline/tasks/intraday_monitor.py` — after VPA gate / before final sort, apply `match_playbook` and multiply `score`
- `alpha_agents/evolution/feedback.py` — new atom `inject_playbooks` (active + degraded listing with hit rate)
- `alpha_agents/evolution/context_builder.py` — `build_morning_context` includes playbooks section
- `alpha_agents/agents/chat_commands/handlers.py` — new `/playbook` command to list / show / approve

---

## Task 1: `playbooks` table + CRUD

**Files:**
- Modify: `alpha_agents/data/memory_store.py` (`_SCHEMA` + new helpers)
- Modify: `tests/test_evolution_schema.py` (append)

- [ ] **Step 1: Append failing tests**

```python
def test_playbooks_table_exists(tmp_path, monkeypatch):
    import alpha_agents.data.memory_store as ms
    db = tmp_path / "memory.db"
    monkeypatch.setattr(ms, "MEMORY_DB_PATH", db)
    if hasattr(ms._local, "conn"):
        monkeypatch.delattr(ms._local, "conn", raising=False)
    ms._get_conn()
    import sqlite3
    conn = sqlite3.connect(str(db))
    cols = {row[1] for row in conn.execute("PRAGMA table_info(playbooks)")}
    conn.close()
    expected = {"id", "name", "pattern_json", "created_date", "last_updated",
                "status", "weight", "total_trades", "wins", "hit_rate",
                "avg_return", "annotation", "annotation_date", "version_history"}
    assert expected <= cols


def test_create_and_get_active_playbooks(tmp_path, monkeypatch):
    import alpha_agents.data.memory_store as ms
    db = tmp_path / "memory.db"
    monkeypatch.setattr(ms, "MEMORY_DB_PATH", db)
    if hasattr(ms._local, "conn"):
        monkeypatch.delattr(ms._local, "conn", raising=False)

    pid = ms.create_playbook(
        name="CPO突破+机构买入",
        pattern_json={"conditions": [
            {"field": "vpa_verdict", "op": "==", "value": "bullish"},
            {"field": "theme", "op": "==", "value": "CPO"},
        ]},
        today="2026-04-17",
    )
    assert pid > 0
    rows = ms.get_active_playbooks()
    assert len(rows) == 1
    assert rows[0]["name"] == "CPO突破+机构买入"
    assert rows[0]["status"] == "active"
    assert rows[0]["weight"] == 1.0


def test_update_playbook_status_and_history(tmp_path, monkeypatch):
    import alpha_agents.data.memory_store as ms
    db = tmp_path / "memory.db"
    monkeypatch.setattr(ms, "MEMORY_DB_PATH", db)
    if hasattr(ms._local, "conn"):
        monkeypatch.delattr(ms._local, "conn", raising=False)
    pid = ms.create_playbook(name="X", pattern_json={"conditions": []}, today="2026-04-17")
    ms.update_playbook_status(pid, status="degraded", weight=0.5,
                               reason="胜率跌破40%", hit_rate_at_change=0.35,
                               today="2026-04-18")
    row = ms.get_all_playbooks()[0]
    assert row["status"] == "degraded"
    assert row["weight"] == 0.5
    import json
    history = json.loads(row["version_history"])
    assert len(history) == 1
    assert history[0]["new_status"] == "degraded"
    assert history[0]["reason"] == "胜率跌破40%"


def test_record_playbook_trade_updates_stats(tmp_path, monkeypatch):
    import alpha_agents.data.memory_store as ms
    db = tmp_path / "memory.db"
    monkeypatch.setattr(ms, "MEMORY_DB_PATH", db)
    if hasattr(ms._local, "conn"):
        monkeypatch.delattr(ms._local, "conn", raising=False)
    pid = ms.create_playbook(name="X", pattern_json={"conditions": []}, today="2026-04-17")
    ms.record_playbook_trade(pid, hit=True, return_pct=2.5)
    ms.record_playbook_trade(pid, hit=True, return_pct=1.0)
    ms.record_playbook_trade(pid, hit=False, return_pct=-1.5)
    row = ms.get_all_playbooks()[0]
    assert row["total_trades"] == 3
    assert row["wins"] == 2
    assert abs(row["hit_rate"] - 2/3) < 0.01
    assert abs(row["avg_return"] - (2.5 + 1.0 - 1.5) / 3) < 0.01
```

- [ ] **Step 2: Add schema**

Append inside `_SCHEMA`:
```sql
CREATE TABLE IF NOT EXISTS playbooks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    pattern_json TEXT NOT NULL,
    created_date TEXT NOT NULL,
    last_updated TEXT NOT NULL,
    status TEXT DEFAULT 'active',
    weight REAL DEFAULT 1.0,
    total_trades INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    hit_rate REAL DEFAULT 0.0,
    avg_return REAL DEFAULT 0.0,
    annotation TEXT DEFAULT '',
    annotation_date TEXT DEFAULT '',
    version_history TEXT DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_playbooks_status ON playbooks(status);
```

- [ ] **Step 3: Add CRUD**

Near the `trading_principles` helpers:
```python
def create_playbook(*, name: str, pattern_json: dict, today: str,
                    status: str = "active", weight: float = 1.0) -> int:
    with _write_lock:
        conn = _get_conn()
        cur = conn.execute(
            "INSERT INTO playbooks (name, pattern_json, created_date, last_updated, "
            " status, weight, version_history) "
            "VALUES (?, ?, ?, ?, ?, ?, '[]')",
            (name, json.dumps(pattern_json, ensure_ascii=False),
             today, today, status, weight),
        )
        conn.commit()
        return cur.lastrowid


def update_playbook_status(playbook_id: int, *, status: str, weight: float,
                            reason: str, hit_rate_at_change: float,
                            today: str) -> None:
    with _write_lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT status, version_history FROM playbooks WHERE id = ?",
            (playbook_id,),
        ).fetchone()
        if not row:
            return
        old_status = row["status"]
        history = json.loads(row["version_history"] or "[]")
        history.append({
            "date": today,
            "old_status": old_status,
            "new_status": status,
            "reason": reason,
            "hit_rate_at_change": hit_rate_at_change,
        })
        conn.execute(
            "UPDATE playbooks SET status = ?, weight = ?, last_updated = ?, "
            "version_history = ? WHERE id = ?",
            (status, weight, today,
             json.dumps(history, ensure_ascii=False), playbook_id),
        )
        conn.commit()


def record_playbook_trade(playbook_id: int, *, hit: bool,
                           return_pct: float) -> None:
    """Called when a prediction matched to a playbook is verified."""
    with _write_lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT total_trades, wins, avg_return FROM playbooks WHERE id = ?",
            (playbook_id,),
        ).fetchone()
        if not row:
            return
        total = row["total_trades"] + 1
        wins = row["wins"] + (1 if hit else 0)
        # Incremental running average
        prev_avg = row["avg_return"] or 0.0
        new_avg = (prev_avg * (total - 1) + return_pct) / total
        conn.execute(
            "UPDATE playbooks SET total_trades = ?, wins = ?, hit_rate = ?, "
            "avg_return = ? WHERE id = ?",
            (total, wins, wins / total, new_avg, playbook_id),
        )
        conn.commit()


def set_playbook_annotation(playbook_id: int, *, annotation: str,
                             today: str) -> None:
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE playbooks SET annotation = ?, annotation_date = ? "
            "WHERE id = ?",
            (annotation, today, playbook_id),
        )
        conn.commit()


def get_active_playbooks() -> list[dict]:
    rows = _get_conn().execute(
        "SELECT * FROM playbooks WHERE status = 'active' "
        "ORDER BY weight DESC, hit_rate DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_all_playbooks() -> list[dict]:
    rows = _get_conn().execute(
        "SELECT * FROM playbooks ORDER BY status, weight DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_active_or_degraded_playbooks() -> list[dict]:
    rows = _get_conn().execute(
        "SELECT * FROM playbooks WHERE status IN ('active', 'degraded') "
        "ORDER BY status, weight DESC"
    ).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 4: Run all schema tests** — 14 passing (10 existing + 4 new).

- [ ] **Step 5: Commit**
```bash
git add alpha_agents/data/memory_store.py tests/test_evolution_schema.py
git commit -m "feat(evolution): playbooks table + CRUD (Phase 3)"
```

---

## Task 2: `match_playbook` — pattern matcher

**Files:**
- Create: `alpha_agents/evolution/playbook.py`
- Create: `tests/test_evolution_playbook.py`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for alpha_agents.evolution.playbook."""
from unittest.mock import patch


SAMPLE_PLAYBOOK = {
    "id": 1,
    "name": "CPO+机构买入",
    "pattern_json": (
        '{"conditions":[{"field":"vpa_verdict","op":"==","value":"bullish"},'
        '{"field":"theme","op":"==","value":"CPO"},'
        '{"field":"institutional","op":"contains","value":"机构"}]}'
    ),
    "weight": 1.5, "status": "active",
    "total_trades": 10, "wins": 8, "hit_rate": 0.8,
}


def test_match_playbook_all_conditions_pass():
    candidate = {"vpa_verdict": "bullish", "theme": "CPO",
                 "institutional": "机构买入1.42亿", "score": 62}
    with patch("alpha_agents.evolution.playbook.get_active_playbooks",
               return_value=[SAMPLE_PLAYBOOK]):
        from alpha_agents.evolution.playbook import match_playbook
        pb = match_playbook(candidate)
    assert pb is not None
    assert pb["name"] == "CPO+机构买入"


def test_match_playbook_condition_fails_returns_none():
    candidate = {"vpa_verdict": "bullish", "theme": "数据中心",
                 "institutional": "机构买入", "score": 62}
    with patch("alpha_agents.evolution.playbook.get_active_playbooks",
               return_value=[SAMPLE_PLAYBOOK]):
        from alpha_agents.evolution.playbook import match_playbook
        assert match_playbook(candidate) is None


def test_match_playbook_regime_fallback_skips_when_too_few_active():
    """If fewer than 2 active playbooks, skip matching entirely."""
    with patch("alpha_agents.evolution.playbook.get_active_playbooks",
               return_value=[SAMPLE_PLAYBOOK]):  # only 1 active
        from alpha_agents.evolution.playbook import match_playbook
        candidate = {"vpa_verdict": "bullish", "theme": "CPO",
                     "institutional": "机构", "score": 62}
        assert match_playbook(candidate) is None


def test_match_playbook_first_match_wins():
    """If multiple match, return the highest-weight one first."""
    pb_a = {**SAMPLE_PLAYBOOK, "id": 1, "name": "A", "weight": 1.5}
    pb_b = {**SAMPLE_PLAYBOOK, "id": 2, "name": "B", "weight": 1.0}
    with patch("alpha_agents.evolution.playbook.get_active_playbooks",
               return_value=[pb_a, pb_b]):
        from alpha_agents.evolution.playbook import match_playbook
        candidate = {"vpa_verdict": "bullish", "theme": "CPO",
                     "institutional": "机构", "score": 62}
        pb = match_playbook(candidate)
        assert pb["name"] == "A"


def test_match_playbook_operators():
    """Support ==, in, contains, >=, <=, >, < operators."""
    pb = {"id": 1, "name": "T", "weight": 1.0, "status": "active",
          "pattern_json": '{"conditions":['
          '{"field":"score","op":">=","value":60},'
          '{"field":"vpa_verdict","op":"in","value":["bullish","neutral"]},'
          '{"field":"change_pct","op":"<=","value":5.0}'
          ']}'}
    pb2 = {**pb, "id": 2, "name": "T2"}
    with patch("alpha_agents.evolution.playbook.get_active_playbooks",
               return_value=[pb, pb2]):
        from alpha_agents.evolution.playbook import match_playbook
        assert match_playbook({"score": 60, "vpa_verdict": "neutral",
                                "change_pct": 3.0}) is not None
        assert match_playbook({"score": 59, "vpa_verdict": "bullish",
                                "change_pct": 1.0}) is None  # score < 60
        assert match_playbook({"score": 70, "vpa_verdict": "bearish",
                                "change_pct": 1.0}) is None  # vpa not in list
```

- [ ] **Step 2: Create `alpha_agents/evolution/playbook.py`**

```python
"""L3 Playbook — match candidates, adjust score by weight, track stats."""

from __future__ import annotations

import json
import logging

from alpha_agents.data.memory_store import get_active_playbooks

logger = logging.getLogger(__name__)

_MIN_ACTIVE_FOR_MATCHING = 2  # regime-change fallback threshold


def _check_condition(field_value, op: str, target) -> bool:
    """Evaluate one condition. Return False on any type mismatch — never raise."""
    try:
        if op == "==":
            return field_value == target
        if op == "in":
            return field_value in target
        if op == "contains":
            return isinstance(field_value, str) and str(target) in field_value
        if op == ">=":
            return float(field_value) >= float(target)
        if op == "<=":
            return float(field_value) <= float(target)
        if op == ">":
            return float(field_value) > float(target)
        if op == "<":
            return float(field_value) < float(target)
    except (TypeError, ValueError):
        return False
    logger.debug("Unknown playbook op: %s", op)
    return False


def _matches_all(candidate: dict, conditions: list[dict]) -> bool:
    """AND semantics across all conditions."""
    for cond in conditions:
        field = cond.get("field")
        op = cond.get("op")
        value = cond.get("value")
        if field is None or op is None:
            return False
        actual = candidate.get(field)
        if not _check_condition(actual, op, value):
            return False
    return True


def match_playbook(candidate: dict) -> dict | None:
    """Return the first playbook (by weight DESC from get_active_playbooks)
    whose conditions ALL match the candidate, or None.

    Regime-change fallback: if fewer than 2 active playbooks exist, skip
    matching entirely (return None). This prevents the system from ranking
    on a broken model during market regime transitions.
    """
    playbooks = get_active_playbooks()
    if len(playbooks) < _MIN_ACTIVE_FOR_MATCHING:
        return None
    for pb in playbooks:
        try:
            pattern = json.loads(pb.get("pattern_json", "{}"))
        except json.JSONDecodeError:
            continue
        conditions = pattern.get("conditions", []) or []
        if not conditions:
            continue  # degenerate empty playbook — no match
        if _matches_all(candidate, conditions):
            return pb
    return None
```

- [ ] **Step 3: Run 5 tests** — all pass.

- [ ] **Step 4: Commit**
```bash
git add alpha_agents/evolution/playbook.py tests/test_evolution_playbook.py
git commit -m "feat(evolution): match_playbook — pattern matcher with regime fallback"
```

---

## Task 3: Rule-driven lifecycle `update_playbook_stats`

**Files:**
- Modify: `alpha_agents/evolution/playbook.py`
- Modify: `tests/test_evolution_playbook.py`

- [ ] **Step 1: Write failing tests**

Append:
```python
def test_update_playbook_stats_degrades_low_hit_rate():
    pb = {"id": 1, "name": "X", "status": "active", "weight": 1.0,
          "total_trades": 6, "wins": 2, "hit_rate": 0.33, "avg_return": -1.0,
          "pattern_json": "{}", "annotation": ""}
    with patch("alpha_agents.evolution.playbook.get_all_playbooks",
               return_value=[pb]), \
         patch("alpha_agents.evolution.playbook.update_playbook_status") as m_upd, \
         patch("alpha_agents.evolution.playbook.annotate_degraded",
               return_value="主线资金退潮"):
        from alpha_agents.evolution.playbook import update_playbook_stats
        ops = update_playbook_stats("2026-04-17")
    assert any("degraded" in o.lower() for o in ops)
    m_upd.assert_called_once()
    assert m_upd.call_args.kwargs["status"] == "degraded"
    assert m_upd.call_args.kwargs["weight"] == 0.5


def test_update_playbook_stats_restores_degraded_on_recovery():
    # Degraded playbook with recent win streak (mocked via helper query)
    pb = {"id": 1, "name": "Y", "status": "degraded", "weight": 0.5,
          "total_trades": 10, "wins": 3, "hit_rate": 0.3,
          "pattern_json": "{}", "annotation": "hit_rate<40%",
          "version_history": "[]"}
    with patch("alpha_agents.evolution.playbook.get_all_playbooks",
               return_value=[pb]), \
         patch("alpha_agents.evolution.playbook._recent_hit_rate",
               return_value=(0.8, 5)), \
         patch("alpha_agents.evolution.playbook.update_playbook_status") as m_upd:
        from alpha_agents.evolution.playbook import update_playbook_stats
        ops = update_playbook_stats("2026-04-17")
    assert any("active" in o.lower() for o in ops)
    assert m_upd.call_args.kwargs["status"] == "active"
    assert m_upd.call_args.kwargs["weight"] == 1.0


def test_update_playbook_stats_deprecates_after_14_days():
    from datetime import datetime, timedelta
    fourteen_ago = (datetime.now() - timedelta(days=15)).strftime("%Y-%m-%d")
    pb = {"id": 1, "name": "Z", "status": "degraded", "weight": 0.5,
          "total_trades": 20, "wins": 6, "hit_rate": 0.3,
          "pattern_json": "{}", "annotation": "",
          "last_updated": fourteen_ago,
          "version_history": "[]"}
    with patch("alpha_agents.evolution.playbook.get_all_playbooks",
               return_value=[pb]), \
         patch("alpha_agents.evolution.playbook._recent_hit_rate",
               return_value=(0.3, 5)), \
         patch("alpha_agents.evolution.playbook.update_playbook_status") as m_upd:
        from alpha_agents.evolution.playbook import update_playbook_stats
        ops = update_playbook_stats("2026-04-17")
    assert any("deprecated" in o.lower() for o in ops)
    assert m_upd.call_args.kwargs["status"] == "deprecated"
    assert m_upd.call_args.kwargs["weight"] == 0.0


def test_update_playbook_stats_boosts_high_performer():
    pb = {"id": 1, "name": "H", "status": "active", "weight": 1.0,
          "total_trades": 15, "wins": 12, "hit_rate": 0.80, "avg_return": 3.5,
          "pattern_json": "{}"}
    with patch("alpha_agents.evolution.playbook.get_all_playbooks",
               return_value=[pb]), \
         patch("alpha_agents.evolution.playbook.update_playbook_status") as m_upd:
        from alpha_agents.evolution.playbook import update_playbook_stats
        ops = update_playbook_stats("2026-04-17")
    # weight jumped from 1.0 to 1.5
    assert any("boost" in o.lower() or "1.5" in o for o in ops)
    assert m_upd.call_args.kwargs["weight"] == 1.5
    assert m_upd.call_args.kwargs["status"] == "active"
```

- [ ] **Step 2: Implement**

Append to `playbook.py`:
```python
from datetime import datetime, timedelta

from alpha_agents.data.memory_store import (
    get_all_playbooks,
    update_playbook_status,
    set_playbook_annotation,
    create_playbook,
)


_DEGRADE_MIN_TRADES = 5
_DEGRADE_HIT_THRESHOLD = 0.4
_RESTORE_MIN_TRADES = 5
_RESTORE_HIT_THRESHOLD = 0.6
_BOOST_MIN_TRADES = 10
_BOOST_HIT_THRESHOLD = 0.7
_DEPRECATE_DAYS = 14


def _recent_hit_rate(playbook_id: int, days: int = 7) -> tuple[float, int]:
    """Compute hit rate on predictions matched to this playbook over last N days.

    Uses predictions.features_json joined via a virtual match (not a real FK).
    For now — simple: scan recent predictions, re-match to this playbook's
    pattern, tally hit/total. Conservative: returns (0.0, 0) on error.
    """
    import sqlite3
    from alpha_agents.data.memory_store import _get_conn

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        # Fetch the playbook's pattern
        row = _get_conn().execute(
            "SELECT pattern_json FROM playbooks WHERE id = ?", (playbook_id,)
        ).fetchone()
        if not row:
            return (0.0, 0)
        pattern = json.loads(row["pattern_json"])
        conditions = pattern.get("conditions", [])
        if not conditions:
            return (0.0, 0)

        preds = _get_conn().execute(
            "SELECT features_json, hit FROM predictions "
            "WHERE report_type = 'intraday' AND hit IS NOT NULL AND date >= ?",
            (cutoff,),
        ).fetchall()
        hits = 0
        total = 0
        for p in preds:
            try:
                features = json.loads(p["features_json"] or "{}")
            except json.JSONDecodeError:
                continue
            if _matches_all(features, conditions):
                total += 1
                if p["hit"] == 1:
                    hits += 1
        return (hits / total, total) if total else (0.0, 0)
    except Exception as e:
        logger.debug("_recent_hit_rate error: %s", e)
        return (0.0, 0)


def update_playbook_stats(today: str) -> list[str]:
    """Run the daily rule engine. Returns a list of human-readable operation strings.

    Rule priority (applied in order; each playbook hits at most one rule per day):
      1. active, hit_rate<0.4 with ≥5 trades → degraded + weight=0.5 + LLM annotate
      2. degraded, recent_hit_rate>=0.6 with ≥5 trades → active + weight=1.0
      3. degraded, days_since_degrade>14 → deprecated + weight=0.0
      4. active, hit_rate>=0.7 with ≥10 trades → weight 1.0→1.5
    """
    ops: list[str] = []
    playbooks = get_all_playbooks()
    for pb in playbooks:
        pid = pb["id"]
        name = pb["name"]
        status = pb["status"]
        total = pb["total_trades"]
        hr = pb["hit_rate"] or 0.0

        if status == "active" and total >= _DEGRADE_MIN_TRADES and hr < _DEGRADE_HIT_THRESHOLD:
            annotation = annotate_degraded(pb)
            update_playbook_status(pid, status="degraded", weight=0.5,
                                    reason=f"hit_rate={hr:.2f} < {_DEGRADE_HIT_THRESHOLD}",
                                    hit_rate_at_change=hr, today=today)
            set_playbook_annotation(pid, annotation=annotation, today=today)
            ops.append(f"{name}: active→degraded ({hr:.1%}) — {annotation}")
            continue

        if status == "degraded":
            rec_hr, rec_n = _recent_hit_rate(pid, days=7)
            if rec_n >= _RESTORE_MIN_TRADES and rec_hr >= _RESTORE_HIT_THRESHOLD:
                update_playbook_status(pid, status="active", weight=1.0,
                                        reason=f"recent hit_rate={rec_hr:.2f} ≥ {_RESTORE_HIT_THRESHOLD}",
                                        hit_rate_at_change=rec_hr, today=today)
                ops.append(f"{name}: degraded→active (近7天 {rec_hr:.1%})")
                continue
            # Check deprecation age
            try:
                last = datetime.strptime(pb["last_updated"], "%Y-%m-%d")
                today_dt = datetime.strptime(today, "%Y-%m-%d")
                if (today_dt - last).days > _DEPRECATE_DAYS:
                    update_playbook_status(pid, status="deprecated", weight=0.0,
                                            reason="degraded超过14天未恢复",
                                            hit_rate_at_change=hr, today=today)
                    ops.append(f"{name}: degraded→deprecated (超时)")
                    continue
            except (ValueError, TypeError):
                pass

        if (status == "active" and total >= _BOOST_MIN_TRADES
                and hr >= _BOOST_HIT_THRESHOLD and pb.get("weight", 1.0) < 1.5):
            update_playbook_status(pid, status="active", weight=1.5,
                                    reason=f"hit_rate={hr:.2f} ≥ {_BOOST_HIT_THRESHOLD} boost",
                                    hit_rate_at_change=hr, today=today)
            ops.append(f"{name}: weight 1.0→1.5 (boost, {hr:.1%})")
    return ops


def annotate_degraded(playbook: dict) -> str:
    """Ask LLM (1 short call) why this playbook's hit rate dropped. On failure,
    return a generic string — never raise."""
    try:
        from openai import OpenAI
        from alpha_agents.config import AGENT_API_KEY, AGENT_BASE_URL, AGENT_MODEL

        pattern = json.loads(playbook.get("pattern_json", "{}"))
        msg = (
            f"Playbook: {playbook['name']}\n"
            f"Pattern: {pattern.get('description', '')} / {pattern.get('conditions')}\n"
            f"Stats: hit_rate={playbook.get('hit_rate', 0):.1%}, "
            f"total={playbook.get('total_trades', 0)}, "
            f"avg_return={playbook.get('avg_return', 0):.2f}%\n\n"
            "用一句话（不超过30字）解释这个 playbook 近期为什么失灵。"
        )
        client = OpenAI(api_key=AGENT_API_KEY, base_url=AGENT_BASE_URL)
        resp = client.chat.completions.create(
            model=AGENT_MODEL or "qwen-plus",
            messages=[{"role": "user", "content": msg}],
            max_tokens=80,
            timeout=30,
        )
        return (resp.choices[0].message.content or "").strip()[:60]
    except Exception as e:
        logger.debug("annotate_degraded failed: %s", e)
        return "近期胜率下滑"
```

- [ ] **Step 3: Run 9 playbook tests — all pass.

- [ ] **Step 4: Commit**
```bash
git add alpha_agents/evolution/playbook.py tests/test_evolution_playbook.py
git commit -m "feat(evolution): playbook lifecycle rule engine + LLM annotation"
```

---

## Task 4: Auto-create playbooks from feature clusters

**Files:**
- Modify: `alpha_agents/evolution/playbook.py`
- Modify: `tests/test_evolution_playbook.py`

- [ ] **Step 1: Write failing tests**

```python
def test_scan_and_auto_create_ignores_signal_rows():
    """Only report_type='intraday' counts — intraday_signal (limit-up obs) excluded."""
    from alpha_agents.evolution.playbook import scan_and_auto_create
    # The function's job is to call _query_hit_clusters with filter applied;
    # we assert that filter at the query level via a targeted mock
    with patch("alpha_agents.evolution.playbook._query_hit_clusters") as m:
        m.return_value = []  # no clusters found
        result = scan_and_auto_create("2026-04-17")
    assert result == []
    # Inspect the query-time filter
    # (_query_hit_clusters implementation test below asserts the SQL filter;
    #  here we just confirm the public function calls it.)
    m.assert_called_once()


def test_scan_and_auto_create_creates_new_playbook():
    cluster = {"vpa_verdict": "bullish", "theme": "CPO",
               "institutional_present": True,
               "hits": 4, "total": 5, "avg_return": 4.2}
    with patch("alpha_agents.evolution.playbook._query_hit_clusters",
               return_value=[cluster]), \
         patch("alpha_agents.evolution.playbook.get_all_playbooks",
               return_value=[]), \
         patch("alpha_agents.evolution.playbook.create_playbook",
               return_value=42) as m_create:
        from alpha_agents.evolution.playbook import scan_and_auto_create
        created = scan_and_auto_create("2026-04-17")
    assert len(created) == 1
    m_create.assert_called_once()
    args = m_create.call_args.kwargs
    assert "bullish" in args["pattern_json"]["conditions"][0]["value"]
    assert "CPO" in str(args["pattern_json"]["conditions"])


def test_scan_and_auto_create_skips_existing_pattern():
    cluster = {"vpa_verdict": "bullish", "theme": "CPO",
               "institutional_present": False,
               "hits": 3, "total": 4, "avg_return": 2.0}
    existing = [{"id": 1, "name": "Auto: CPO-bullish",
                 "pattern_json": '{"conditions":[{"field":"theme","op":"==","value":"CPO"},{"field":"vpa_verdict","op":"==","value":"bullish"}]}',
                 "status": "active"}]
    with patch("alpha_agents.evolution.playbook._query_hit_clusters",
               return_value=[cluster]), \
         patch("alpha_agents.evolution.playbook.get_all_playbooks",
               return_value=existing), \
         patch("alpha_agents.evolution.playbook.create_playbook") as m_create:
        from alpha_agents.evolution.playbook import scan_and_auto_create
        created = scan_and_auto_create("2026-04-17")
    assert created == []
    m_create.assert_not_called()
```

- [ ] **Step 2: Implement**

Append to `playbook.py`:
```python
_AUTO_CREATE_MIN_WINS = 3
_AUTO_CREATE_MIN_TOTAL = 3  # same as min_wins: all cases must hit
_AUTO_CREATE_LOOKBACK_DAYS = 14


def _query_hit_clusters(days: int = _AUTO_CREATE_LOOKBACK_DAYS) -> list[dict]:
    """Group recent verified intraday predictions by decision-feature cluster.
    Returns rows where wins ≥ _AUTO_CREATE_MIN_WINS. CRITICAL: only uses
    report_type='intraday' (excludes 'intraday_signal' limit-up observations
    which would dominate clustering with useless patterns)."""
    from alpha_agents.data.memory_store import _get_conn

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    q = """
    SELECT
        json_extract(features_json, '$.vpa_verdict') as vpa_verdict,
        json_extract(features_json, '$.theme') as theme,
        CASE WHEN json_extract(features_json, '$.institutional') IS NOT NULL
                  AND json_extract(features_json, '$.institutional') != ''
             THEN 1 ELSE 0 END as institutional_present,
        COUNT(*) FILTER (WHERE hit=1) as hits,
        COUNT(*) as total,
        AVG(CASE WHEN hit=1 THEN next_day_return ELSE 0 END) as avg_return
    FROM predictions
    WHERE report_type = 'intraday'
      AND hit IS NOT NULL
      AND features_json IS NOT NULL
      AND features_json != '{}'
      AND date >= ?
    GROUP BY vpa_verdict, theme, institutional_present
    HAVING hits >= ?
    ORDER BY hits DESC
    """
    rows = _get_conn().execute(q, (cutoff, _AUTO_CREATE_MIN_WINS)).fetchall()
    return [dict(r) for r in rows]


def _pattern_from_cluster(cluster: dict) -> dict:
    """Build the pattern_json for a feature cluster."""
    conditions = []
    if cluster.get("theme"):
        conditions.append({"field": "theme", "op": "==", "value": cluster["theme"]})
    if cluster.get("vpa_verdict"):
        conditions.append({"field": "vpa_verdict", "op": "==",
                           "value": cluster["vpa_verdict"]})
    if cluster.get("institutional_present"):
        conditions.append({"field": "institutional", "op": "contains",
                           "value": "机构"})
    desc_parts = []
    if cluster.get("theme"):
        desc_parts.append(cluster["theme"])
    if cluster.get("vpa_verdict"):
        desc_parts.append(f"VPA{cluster['vpa_verdict']}")
    if cluster.get("institutional_present"):
        desc_parts.append("机构买入")
    return {"description": "+".join(desc_parts), "conditions": conditions}


def _pattern_signature(pattern_json_str: str) -> frozenset:
    """Return a comparable signature (field+op+value) of a pattern's conditions."""
    try:
        pattern = json.loads(pattern_json_str)
    except json.JSONDecodeError:
        return frozenset()
    return frozenset(
        (c.get("field"), c.get("op"), str(c.get("value")))
        for c in pattern.get("conditions", [])
    )


def scan_and_auto_create(today: str) -> list[int]:
    """Scan hit clusters and create a new playbook for each novel pattern.
    Returns list of newly-created playbook IDs."""
    clusters = _query_hit_clusters()
    if not clusters:
        return []

    existing = get_all_playbooks()
    existing_sigs = {_pattern_signature(pb.get("pattern_json", "{}"))
                     for pb in existing}

    created = []
    for c in clusters:
        if c["total"] < _AUTO_CREATE_MIN_TOTAL:
            continue  # not enough evidence
        pattern = _pattern_from_cluster(c)
        sig = frozenset(
            (cond["field"], cond["op"], str(cond["value"]))
            for cond in pattern["conditions"]
        )
        if sig in existing_sigs:
            continue
        name_parts = []
        if c.get("theme"):
            name_parts.append(str(c["theme"]))
        if c.get("vpa_verdict"):
            name_parts.append(str(c["vpa_verdict"]))
        if c.get("institutional_present"):
            name_parts.append("机构")
        name = f"Auto: {'-'.join(name_parts)}"
        pid = create_playbook(name=name, pattern_json=pattern, today=today)
        existing_sigs.add(sig)
        created.append(pid)
        logger.info("Auto-created playbook #%d: %s (hits=%d/%d)",
                    pid, name, c["hits"], c["total"])
    return created
```

- [ ] **Step 3: Run 12 playbook tests — all pass.

- [ ] **Step 4: Commit**
```bash
git add alpha_agents/evolution/playbook.py tests/test_evolution_playbook.py
git commit -m "feat(evolution): auto-create playbooks from hit feature clusters"
```

---

## Task 5: Wire playbook into `post_review` + intraday

**Files:**
- Modify: `alpha_agents/evolution/lessons.py` (extend post_review)
- Modify: `alpha_agents/evolution/__init__.py` (export match_playbook, update_playbook_stats)
- Modify: `alpha_agents/pipeline/tasks/intraday_monitor.py` (apply match_playbook)

- [ ] **Step 1: Extend `post_review` in `lessons.py`**

Replace the existing `post_review` body to also call playbook stats + auto-create:
```python
async def post_review(today: str, review_report: str) -> str:
    import asyncio

    lesson_count = await asyncio.to_thread(extract_daily_lessons, review_report, today)
    if lesson_count > 0:
        counts = await asyncio.to_thread(consolidate_principles, today)
    else:
        counts = {"created": 0, "reinforced": 0, "weakened": 0}

    # Phase 3: playbook daily lifecycle
    from alpha_agents.evolution.playbook import (
        update_playbook_stats, scan_and_auto_create,
    )
    try:
        playbook_ops = await asyncio.to_thread(update_playbook_stats, today)
    except Exception as e:
        logger.warning("Playbook stats update failed: %s", e)
        playbook_ops = []
    try:
        created_ids = await asyncio.to_thread(scan_and_auto_create, today)
    except Exception as e:
        logger.warning("Playbook auto-create failed: %s", e)
        created_ids = []

    # Assemble report
    nothing = (lesson_count == 0 and sum(counts.values()) == 0
               and not playbook_ops and not created_ids)
    if nothing:
        return ""

    lines = ["【经验沉淀】"]
    if lesson_count > 0:
        lines.append(f"• 今日提取 {lesson_count} 条 lessons")
    if counts["created"]:
        lines.append(f"• 新增 {counts['created']} 条 principles")
    if counts["reinforced"]:
        lines.append(f"• 强化 {counts['reinforced']} 条 principles")
    if counts["weakened"]:
        lines.append(f"• 减弱 {counts['weakened']} 条 principles")
    if playbook_ops:
        lines.append("【Playbook 变化】")
        for op in playbook_ops[:5]:
            lines.append(f"• {op}")
    if created_ids:
        lines.append(f"• 自动发现 {len(created_ids)} 条新 Playbook（id={created_ids}）")
    return "\n".join(lines)
```

- [ ] **Step 2: Export from `__init__.py`**

Add:
```python
from alpha_agents.evolution.playbook import match_playbook, update_playbook_stats
```
(Update `__all__`.)

- [ ] **Step 3: Wire into intraday_monitor**

In `alpha_agents/pipeline/tasks/intraday_monitor.py`, after `actionable` is sorted (currently `actionable.sort(key=lambda x: x["score"], reverse=True); actionable = actionable[:5]`), insert BEFORE the sort:

```python
# Phase 3: apply playbook weights (regime-aware — returns None if <2 active)
try:
    from alpha_agents.evolution import match_playbook
    for a in actionable:
        pb = match_playbook(a)
        if pb:
            a["playbook"] = pb["name"]
            a["playbook_weight"] = pb.get("weight", 1.0)
            a["score"] *= pb.get("weight", 1.0)
except Exception as e:
    logger.debug("Playbook matching failed (non-fatal): %s", e)
```

Also update the report rendering (the 【可操作标的】 table around line 634-660) to show playbook name when present. Find the line building the action column and add playbook tag:
```python
pb_name = a.get("playbook", "")
pb_bit = f" [{pb_name}×{a.get('playbook_weight', 1.0):.1f}]" if pb_name else ""
```
Append `pb_bit` to the end of the action string.

- [ ] **Step 4: Syntax + smoke**

```bash
uv run python -m py_compile alpha_agents/evolution/lessons.py alpha_agents/evolution/__init__.py alpha_agents/pipeline/tasks/intraday_monitor.py
uv run python -c "from alpha_agents.evolution import match_playbook, update_playbook_stats, post_review; print('OK')"
```

- [ ] **Step 5: Commit**
```bash
git add alpha_agents/evolution/lessons.py alpha_agents/evolution/__init__.py alpha_agents/pipeline/tasks/intraday_monitor.py
git commit -m "feat(evolution): playbook wired into post_review + intraday matching"
```

---

## Task 6: `inject_playbooks` + context + `/playbook` chat command

**Files:**
- Modify: `alpha_agents/evolution/feedback.py` (new `inject_playbooks`)
- Modify: `alpha_agents/evolution/context_builder.py` (use it)
- Modify: `tests/test_evolution_feedback.py` (tests for `inject_playbooks`)
- Modify: `alpha_agents/agents/chat_commands/handlers.py` (`/playbook` command)

- [ ] **Step 1: Tests for `inject_playbooks`**

Append to `tests/test_evolution_feedback.py`:
```python
def test_inject_playbooks_formats_active_and_degraded():
    fake = [
        {"name": "CPO突破+机构", "status": "active", "weight": 1.5,
         "hit_rate": 0.8, "total_trades": 10, "wins": 8, "annotation": ""},
        {"name": "数据中心追强", "status": "degraded", "weight": 0.5,
         "hit_rate": 0.3, "total_trades": 6, "wins": 2,
         "annotation": "主线资金退潮"},
    ]
    from unittest.mock import patch
    with patch("alpha_agents.evolution.feedback.get_active_or_degraded_playbooks",
               return_value=fake):
        from alpha_agents.evolution.feedback import inject_playbooks
        result = inject_playbooks()
    assert "Playbook" in result or "playbook" in result
    assert "CPO突破+机构" in result and "80%" in result
    assert "数据中心追强" in result and ("⚠️" in result or "degraded" in result.lower())
    assert "主线资金退潮" in result


def test_inject_playbooks_empty():
    from unittest.mock import patch
    with patch("alpha_agents.evolution.feedback.get_active_or_degraded_playbooks",
               return_value=[]):
        from alpha_agents.evolution.feedback import inject_playbooks
        assert inject_playbooks() == ""
```

- [ ] **Step 2: Implement**

Update `feedback.py`'s imports:
```python
from alpha_agents.data.memory_store import (
    get_all_cognition_latest,
    get_all_principles_including_weakened,
    get_recent_daily_lessons,
    get_active_or_degraded_playbooks,
)
```

Add at end:
```python
_PLAYBOOKS_BUDGET = 400


def inject_playbooks() -> str:
    rows = get_active_or_degraded_playbooks()
    if not rows:
        return ""
    lines = [f"【活跃 Playbook】（{len(rows)}条）"]
    for pb in rows:
        hr = pb.get("hit_rate", 0) or 0
        ttl = pb.get("total_trades", 0)
        wins = pb.get("wins", 0)
        w = pb.get("weight", 1.0)
        tag = "⚠️" if pb["status"] == "degraded" else ("⭐" if w >= 1.5 else "")
        ann = pb.get("annotation", "")
        ann_bit = f" — {ann}" if ann else ""
        line = (f"• {tag}{pb['name']} — 胜率{hr*100:.0f}%"
                f"（{wins}/{ttl}）weight={w:.1f}{ann_bit}")
        if sum(len(x) for x in lines) + len(line) > _PLAYBOOKS_BUDGET:
            break
        lines.append(line)
    return "\n".join(lines)
```

- [ ] **Step 3: Update `context_builder.py`**

Add to imports:
```python
from alpha_agents.evolution.feedback import (
    inject_cognition, inject_sentiment, inject_vpa_signal_history,
    inject_principles, inject_recent_lessons, inject_playbooks,
)
```

Extend `build_morning_context`:
```python
def build_morning_context(themes: list[dict], stats: str) -> str:
    sections = []
    for part in (inject_sentiment(), inject_cognition(),
                 inject_principles(), inject_recent_lessons(),
                 inject_playbooks()):
        if part:
            sections.append(part)
    if stats:
        sections.append(stats)
    return "\n\n".join(sections)
```

(`build_chat_context` omits playbooks to stay compact — add if needed later.)

Update the 2 affected Phase 1/2 tests for `build_morning_context` in `tests/test_evolution_context_builder.py` to also mock `inject_playbooks` returning `""`.

Add a new test:
```python
def test_build_morning_context_includes_playbooks():
    with patch("alpha_agents.evolution.context_builder.inject_sentiment", return_value=""), \
         patch("alpha_agents.evolution.context_builder.inject_cognition", return_value=""), \
         patch("alpha_agents.evolution.context_builder.inject_principles", return_value=""), \
         patch("alpha_agents.evolution.context_builder.inject_recent_lessons", return_value=""), \
         patch("alpha_agents.evolution.context_builder.inject_playbooks",
               return_value="【活跃 Playbook】\n• CPO+机构 — 胜率80%"):
        from alpha_agents.evolution.context_builder import build_morning_context
        result = build_morning_context(themes=[], stats="")
    assert "Playbook" in result
    assert "CPO+机构" in result
```

- [ ] **Step 4: `/playbook` chat command**

In `alpha_agents/agents/chat_commands/handlers.py`, add a new command. The existing registry follows a pattern with an `@register(...)` decorator or similar. Check the current file structure and follow it:

```python
async def handle_playbook(arg: str, ctx: ChatContext) -> None:
    """List active + degraded playbooks with their stats."""
    from alpha_agents.data.memory_store import get_all_playbooks
    rows = get_all_playbooks()
    if not rows:
        ctx.console.print("[yellow]尚无 Playbook。至少积累 2 周 intraday 数据后由复盘自动发现。[/yellow]")
        return
    lines = [f"共 {len(rows)} 条 Playbook:"]
    for pb in rows:
        hr = pb.get("hit_rate", 0) or 0
        status = pb["status"]
        color = {"active": "green", "degraded": "yellow", "deprecated": "dim"}.get(status, "white")
        lines.append(
            f"[{color}]• {pb['name']} ({status}, weight={pb.get('weight', 1.0):.1f}) "
            f"— 胜率{hr*100:.0f}% ({pb.get('wins',0)}/{pb.get('total_trades',0)})[/]"
        )
        if pb.get("annotation"):
            lines.append(f"    [dim]注：{pb['annotation']}[/dim]")
    from rich.panel import Panel
    ctx.console.print(Panel("\n".join(lines), title="Playbook 列表", border_style="cyan"))
```

Register it in the existing `REGISTRY` (or whatever pattern is used). Group: 自动化. Summary: "查看 Playbook 列表及胜率". Exact wiring depends on the existing handler structure.

- [ ] **Step 5: Run all evolution tests** — 48+ passing.

- [ ] **Step 6: Commit**
```bash
git add alpha_agents/evolution/feedback.py alpha_agents/evolution/context_builder.py tests/test_evolution_feedback.py tests/test_evolution_context_builder.py alpha_agents/agents/chat_commands/handlers.py
git commit -m "feat(evolution): inject_playbooks + /playbook command"
```

---

## Task 7: Playbook trade recording when predictions verify

**Files:**
- Modify: `alpha_agents/pipeline/tasks/review.py` (in the existing `_verify_predictions` or in `_verify_today_predictions` path)

- [ ] **Step 1: Locate the verification loop**

Open `review.py`. Find where `update_prediction_result(pred["id"], next_day_return=return_pct, hit=hit_val)` is called.

- [ ] **Step 2: After that call, also record the playbook hit if features match a playbook**

```python
# Phase 3: if this prediction matched an active playbook, record the trade outcome
try:
    import json as _json
    from alpha_agents.evolution import match_playbook
    from alpha_agents.data.memory_store import record_playbook_trade
    features = _json.loads(pred.get("features_json") or "{}")
    if features:
        pb = match_playbook(features)
        if pb:
            record_playbook_trade(pb["id"], hit=bool(hit_val), return_pct=return_pct)
except Exception as e:
    logger.debug("Playbook trade recording failed for pred #%d: %s", pred["id"], e)
```

Note: `_verify_predictions` currently reads `pred.get("code")`, `pred.get("entry_price")`, etc. — add a SELECT of `features_json` too. Look at how pending predictions are fetched. If `get_pending_predictions` doesn't return `features_json`, update that function in `memory_store.py` to include it.

- [ ] **Step 3: Syntax check + commit**

```bash
uv run python -m py_compile alpha_agents/pipeline/tasks/review.py
git add alpha_agents/pipeline/tasks/review.py alpha_agents/data/memory_store.py
git commit -m "feat(evolution): record playbook trade stats on prediction verification"
```

---

## Task 8: Smoke test + mark Phase 3 shipped

- [ ] **Step 1: Probe DB for cluster readiness**

```bash
sqlite3 data/memory.db "
SELECT json_extract(features_json, '\$.vpa_verdict') as vpa,
       json_extract(features_json, '\$.theme') as theme,
       COUNT(*) FILTER (WHERE hit=1) as wins, COUNT(*) as total
FROM predictions
WHERE report_type='intraday' AND hit IS NOT NULL AND features_json != '{}'
GROUP BY vpa, theme
HAVING wins >= 3
ORDER BY wins DESC LIMIT 10;
"
```

If rows show, proceed to Step 2. If empty, Phase 3 is structurally complete but will sit idle until data accumulates.

- [ ] **Step 2: Manual scan_and_auto_create + list**

```bash
uv run python -c "
from alpha_agents.evolution.playbook import scan_and_auto_create, update_playbook_stats
from alpha_agents.data.memory_store import get_all_playbooks
created = scan_and_auto_create('2026-04-17')
print('Auto-created:', created)
print('All playbooks now:')
for pb in get_all_playbooks():
    print(f\"  {pb['name']} ({pb['status']}, weight={pb['weight']})\")
"
```

- [ ] **Step 3: Mark Phase 3 shipped in spec**

Append a "Phase 3 Shipped — 2026-MM-DD" note to `docs/superpowers/specs/2026-04-16-agent-evolution-design.md` parallel to the Phase 1/2 sections. Include:
- File inventory (playbook.py + CRUD + context injector + /playbook command)
- Rule engine threshold constants
- Auto-creation minimum: ≥3 hits per feature cluster, excludes intraday_signal
- Test count
- Commit SHAs

- [ ] **Step 4: Commit**
```bash
git add docs/superpowers/specs/2026-04-16-agent-evolution-design.md
git commit -m "docs(evolution): mark Phase 3 shipped"
```

---

## Done Criteria (Phase 3)

- [ ] `playbooks` table + CRUD + `version_history` JSON tracking
- [ ] `match_playbook()` with ==/in/contains/>=/<=/>/< operators and regime-change fallback (<2 active → None)
- [ ] `update_playbook_stats()` rule engine: active→degraded (hit<40% with ≥5), degraded→active (recent hit≥60% with ≥5), degraded→deprecated (>14d), active→boost (hit≥70% with ≥10)
- [ ] `annotate_degraded()` LLM call (≤80 tokens, graceful degrade on failure)
- [ ] `scan_and_auto_create()` — clusters intraday-only predictions by (vpa_verdict, theme, institutional_present); creates playbook when ≥3 hits in a cluster and pattern is novel
- [ ] `post_review()` invokes stats + auto-create
- [ ] `intraday_monitor.py` applies match_playbook and multiplies score
- [ ] `record_playbook_trade()` called from `_verify_predictions` loop
- [ ] `inject_playbooks` in morning context
- [ ] `/playbook` chat command lists all playbooks with stats
- [ ] All previous tests still passing; new Phase 3 tests: schema 4 + playbook 12 + feedback 2 + context_builder 1 = 19 new
