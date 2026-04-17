# Agent Evolution Phase 2 — L2 Lessons + Trading Principles

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan.

**Goal:** Complete the learning loop — daily review emits structured lessons, LLM consolidates them into lasting trading principles (Anna Coulling style), and both surface in next day's agent context.

**Architecture:** `review_agent` prompt gains a `<!-- LESSONS: [...] -->` tag requirement. New `alpha_agents/evolution/lessons.py` parses that tag (`extract_daily_lessons`) and calls LLM (`consolidate_principles`) to manage `trading_principles`. New public entrypoint `post_review(today, report)` in evolution module; wired into `review.py` end. `build_morning_context` and `build_chat_context` extended to inject principles + recent lessons with budget-aware truncation.

**Tech Stack:** same as Phase 1 — Python 3.11, SQLite, pytest. LLM via existing `alpha_agents.config.AGENT_*` env vars.

**Spec:** `docs/superpowers/specs/2026-04-16-agent-evolution-design.md` §Data Layer, §Call Flow, §Context Injection.

**Branch:** `feat/evolution-phase1` (continuing same branch — no new branch).

---

## File Map

### Created
- `alpha_agents/evolution/lessons.py` — `extract_daily_lessons`, `consolidate_principles`, plus the `post_review` public entry
- `tests/test_evolution_lessons.py` — unit tests for extraction + consolidation (LLM mocked)

### Modified
- `alpha_agents/data/memory_store.py` — add `daily_lessons` + `trading_principles` tables to `_SCHEMA` + CRUD helpers
- `alpha_agents/prompts/review.md` — append `<!-- LESSONS: [...] -->` output requirement
- `alpha_agents/pipeline/tasks/review.py` — call `await post_review(today, report)` at end
- `alpha_agents/evolution/__init__.py` — export `post_review`, `consolidate_principles`, `extract_daily_lessons`
- `alpha_agents/evolution/context_builder.py` — `build_morning_context` and `build_chat_context` gain principles + recent lessons injection
- `alpha_agents/evolution/feedback.py` — add `inject_principles` and `inject_recent_lessons` atoms

---

## Task 1: Schema — add `daily_lessons` and `trading_principles` tables

**Files:**
- Modify: `alpha_agents/data/memory_store.py` (the `_SCHEMA` constant near line 30-ish)
- Modify: `tests/test_evolution_schema.py` (append tests)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_evolution_schema.py`:
```python
def test_daily_lessons_table_exists(tmp_path, monkeypatch):
    import alpha_agents.data.memory_store as ms
    db = tmp_path / "memory.db"
    monkeypatch.setattr(ms, "MEMORY_DB_PATH", db)
    if hasattr(ms._local, "conn"):
        monkeypatch.delattr(ms._local, "conn", raising=False)
    ms._get_conn()
    import sqlite3
    conn = sqlite3.connect(str(db))
    cols = {row[1] for row in conn.execute("PRAGMA table_info(daily_lessons)")}
    conn.close()
    expected = {"id", "date", "lesson_type", "theme", "content",
                "source", "relevance_tags", "consolidated_into"}
    assert expected <= cols


def test_trading_principles_table_exists(tmp_path, monkeypatch):
    import alpha_agents.data.memory_store as ms
    db = tmp_path / "memory.db"
    monkeypatch.setattr(ms, "MEMORY_DB_PATH", db)
    if hasattr(ms._local, "conn"):
        monkeypatch.delattr(ms._local, "conn", raising=False)
    ms._get_conn()
    import sqlite3
    conn = sqlite3.connect(str(db))
    cols = {row[1] for row in conn.execute("PRAGMA table_info(trading_principles)")}
    conn.close()
    expected = {"id", "principle", "pattern_description", "category",
                "action_guidance", "evidence", "evidence_count", "win_rate",
                "first_learned", "last_reinforced", "status"}
    assert expected <= cols
```

Run: `uv run pytest tests/test_evolution_schema.py::test_daily_lessons_table_exists tests/test_evolution_schema.py::test_trading_principles_table_exists -v`
Expected: FAIL (tables don't exist yet).

- [ ] **Step 2: Add schemas to `_SCHEMA` constant**

Find the `_SCHEMA = """..."""` constant in `memory_store.py` and append (inside the triple quotes, after the last `CREATE TABLE`):

```sql
CREATE TABLE IF NOT EXISTS daily_lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    lesson_type TEXT NOT NULL,
    theme TEXT,
    content TEXT NOT NULL,
    source TEXT DEFAULT 'review',
    relevance_tags TEXT DEFAULT '',
    consolidated_into INTEGER,
    UNIQUE(date, content)
);
CREATE INDEX IF NOT EXISTS idx_lessons_date ON daily_lessons(date);

CREATE TABLE IF NOT EXISTS trading_principles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    principle TEXT NOT NULL UNIQUE,
    pattern_description TEXT NOT NULL,
    category TEXT NOT NULL,
    action_guidance TEXT NOT NULL,
    evidence TEXT NOT NULL DEFAULT '[]',
    evidence_count INTEGER DEFAULT 1,
    win_rate REAL,
    first_learned TEXT NOT NULL,
    last_reinforced TEXT NOT NULL,
    status TEXT DEFAULT 'active'
);
CREATE INDEX IF NOT EXISTS idx_principles_status ON trading_principles(status);
CREATE INDEX IF NOT EXISTS idx_principles_category ON trading_principles(category);
```

- [ ] **Step 3: Run tests** — both pass.

- [ ] **Step 4: Commit**
```bash
git add alpha_agents/data/memory_store.py tests/test_evolution_schema.py
git commit -m "feat(evolution): daily_lessons + trading_principles tables (Phase 2)"
```

---

## Task 2: CRUD helpers in memory_store.py

**Files:**
- Modify: `alpha_agents/data/memory_store.py` (add functions)
- Test: `tests/test_evolution_schema.py` (append CRUD tests)

- [ ] **Step 1: Write tests for CRUD**

Append to `tests/test_evolution_schema.py`:
```python
def test_insert_daily_lesson_and_query(tmp_path, monkeypatch):
    import alpha_agents.data.memory_store as ms
    db = tmp_path / "memory.db"
    monkeypatch.setattr(ms, "MEMORY_DB_PATH", db)
    if hasattr(ms._local, "conn"):
        monkeypatch.delattr(ms._local, "conn", raising=False)

    ms.insert_daily_lesson("2026-04-17", "failure", "数据中心",
                            "东方国信破5日线才预警", tags="5日线,止损时机")
    rows = ms.get_recent_daily_lessons(days=7)
    assert len(rows) == 1
    assert rows[0]["lesson_type"] == "failure"
    assert rows[0]["theme"] == "数据中心"
    assert "东方国信" in rows[0]["content"]


def test_insert_daily_lesson_deduplicates(tmp_path, monkeypatch):
    """UNIQUE(date, content) — second insert same day same content is ignored."""
    import alpha_agents.data.memory_store as ms
    db = tmp_path / "memory.db"
    monkeypatch.setattr(ms, "MEMORY_DB_PATH", db)
    if hasattr(ms._local, "conn"):
        monkeypatch.delattr(ms._local, "conn", raising=False)
    ms.insert_daily_lesson("2026-04-17", "insight", None, "相同内容")
    ms.insert_daily_lesson("2026-04-17", "insight", None, "相同内容")
    rows = ms.get_recent_daily_lessons(days=1)
    assert len(rows) == 1


def test_upsert_trading_principle_create_and_reinforce(tmp_path, monkeypatch):
    import alpha_agents.data.memory_store as ms
    db = tmp_path / "memory.db"
    monkeypatch.setattr(ms, "MEMORY_DB_PATH", db)
    if hasattr(ms._local, "conn"):
        monkeypatch.delattr(ms._local, "conn", raising=False)

    # Create
    pid = ms.create_trading_principle(
        principle="巨量长下影 = 买入高峰",
        pattern_description="跌幅>5%下影>30%",
        category="vpa_signal",
        action_guidance="等缩量不破低再介入",
        evidence=[{"code": "300274", "date": "04-01", "outcome": "+8.2% in 10d"}],
        today="2026-04-17",
    )
    assert pid > 0

    # Reinforce
    ms.reinforce_trading_principle(pid, today="2026-04-18",
                                    new_case={"code": "000001", "date": "04-18", "outcome": "+5% in 5d"})
    row = ms.get_active_principles()[0]
    assert row["evidence_count"] == 2
    assert row["last_reinforced"] == "2026-04-18"


def test_weaken_and_retire_principle(tmp_path, monkeypatch):
    import alpha_agents.data.memory_store as ms
    db = tmp_path / "memory.db"
    monkeypatch.setattr(ms, "MEMORY_DB_PATH", db)
    if hasattr(ms._local, "conn"):
        monkeypatch.delattr(ms._local, "conn", raising=False)

    pid = ms.create_trading_principle(
        principle="P", pattern_description="d", category="x",
        action_guidance="a", evidence=[], today="2026-04-17",
    )
    ms.set_principle_status(pid, "weakened")
    assert ms.get_active_principles() == []
    all_rows = ms.get_all_principles_including_weakened()
    assert len(all_rows) == 1
    assert all_rows[0]["status"] == "weakened"
```

- [ ] **Step 2: Implement in memory_store.py**

Add these functions near the cognition-related functions (`get_all_cognition_latest` area). Use `_get_conn()` and `_write_lock` patterns already used elsewhere.

```python
# ── Phase 2: daily lessons ────────────────────────────────────
def insert_daily_lesson(date: str, lesson_type: str, theme: str | None,
                        content: str, tags: str = "", source: str = "review") -> None:
    """Insert a lesson; silently skips if (date, content) already exists."""
    with _write_lock:
        conn = _get_conn()
        try:
            conn.execute(
                "INSERT INTO daily_lessons (date, lesson_type, theme, content, source, relevance_tags) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (date, lesson_type, theme, content, source, tags),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            pass  # duplicate (date, content)


def get_recent_daily_lessons(days: int = 7, themes: list[str] | None = None) -> list[dict]:
    """Return lessons from last N days. If themes given, ONLY matching ones."""
    from datetime import datetime, timedelta
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    q = "SELECT * FROM daily_lessons WHERE date >= ?"
    params: list = [cutoff]
    if themes:
        placeholders = ",".join("?" * len(themes))
        q += f" AND theme IN ({placeholders})"
        params.extend(themes)
    q += " ORDER BY date DESC, id DESC"
    return [dict(r) for r in _get_conn().execute(q, params).fetchall()]


def get_historical_lessons_by_themes(themes: list[str], older_than_days: int = 7,
                                      limit: int = 10) -> list[dict]:
    """For filtering old lessons by currently active themes (budget-aware injection)."""
    if not themes:
        return []
    from datetime import datetime, timedelta
    cutoff = (datetime.now() - timedelta(days=older_than_days)).strftime("%Y-%m-%d")
    placeholders = ",".join("?" * len(themes))
    q = (f"SELECT * FROM daily_lessons WHERE date < ? AND theme IN ({placeholders}) "
         f"ORDER BY date DESC LIMIT ?")
    return [dict(r) for r in _get_conn().execute(q, [cutoff, *themes, limit]).fetchall()]


# ── Phase 2: trading principles ───────────────────────────────
def create_trading_principle(*, principle: str, pattern_description: str,
                              category: str, action_guidance: str,
                              evidence: list[dict], today: str,
                              win_rate: float | None = None) -> int:
    with _write_lock:
        conn = _get_conn()
        cur = conn.execute(
            "INSERT INTO trading_principles "
            "(principle, pattern_description, category, action_guidance, "
            " evidence, evidence_count, win_rate, first_learned, last_reinforced, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')",
            (principle, pattern_description, category, action_guidance,
             json.dumps(evidence, ensure_ascii=False), len(evidence),
             win_rate, today, today),
        )
        conn.commit()
        return cur.lastrowid


def reinforce_trading_principle(principle_id: int, *, today: str,
                                 new_case: dict | None = None,
                                 win_rate: float | None = None) -> None:
    with _write_lock:
        conn = _get_conn()
        row = conn.execute(
            "SELECT evidence, evidence_count FROM trading_principles WHERE id = ?",
            (principle_id,),
        ).fetchone()
        if not row:
            return
        evidence = json.loads(row["evidence"] or "[]")
        if new_case:
            evidence.append(new_case)
        updates = [
            "evidence = ?",
            "evidence_count = ?",
            "last_reinforced = ?",
            "status = 'active'",
        ]
        params: list = [json.dumps(evidence, ensure_ascii=False),
                        len(evidence), today]
        if win_rate is not None:
            updates.append("win_rate = ?")
            params.append(win_rate)
        params.append(principle_id)
        conn.execute(
            f"UPDATE trading_principles SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        conn.commit()


def set_principle_status(principle_id: int, status: str) -> None:
    """status ∈ {'active', 'weakened', 'retired'}"""
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE trading_principles SET status = ? WHERE id = ?",
            (status, principle_id),
        )
        conn.commit()


def get_active_principles() -> list[dict]:
    rows = _get_conn().execute(
        "SELECT * FROM trading_principles WHERE status = 'active' "
        "ORDER BY evidence_count DESC, last_reinforced DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_all_principles_including_weakened() -> list[dict]:
    rows = _get_conn().execute(
        "SELECT * FROM trading_principles WHERE status IN ('active', 'weakened') "
        "ORDER BY status, evidence_count DESC"
    ).fetchall()
    return [dict(r) for r in rows]
```

`json` should already be imported at file top (confirmed from Phase 1).

- [ ] **Step 3: Run tests** — all 4 new CRUD tests pass + all 6 Phase 1 schema tests pass.

- [ ] **Step 4: Commit**
```bash
git add alpha_agents/data/memory_store.py tests/test_evolution_schema.py
git commit -m "feat(evolution): CRUD for daily_lessons and trading_principles"
```

---

## Task 3: Review agent prompt — add LESSONS output requirement

**Files:**
- Modify: `alpha_agents/prompts/review.md` (append a section)

- [ ] **Step 1: Append to review.md**

At the end of the existing review.md, append:

```markdown

---

## 结构化输出（必须）

在报告末尾追加一行机读标签（和报告正文之间空一行），格式固定，JSON 必须合法：

<!-- LESSONS: [{"type":"success","theme":"CPO","content":"具体经验（1-2句，带数据）","tags":"关键词1,关键词2"}] -->

- type: success | failure | insight
- theme: 关联主线名（如 "数据中心"、"CPO"），若与具体主线无关则填 ""
- content: 1-2 句话，必须含**具体股票或案例**（不要空话）
- tags: 逗号分隔关键词，便于后续检索

每日至少 2 条（可多），覆盖：
- 今日做对的关键决策（类型 success）
- 今日做错或该改进的（类型 failure）
- 观察到的市场规律（类型 insight）

错误示范（空话）：`"当前主线强度较高"`
正确示范：`"CPO+机构买入组合命中率高，协创数据+9.4%验证"` — 带主体、带数据。
```

- [ ] **Step 2: Commit**
```bash
git add alpha_agents/prompts/review.md
git commit -m "feat(evolution): review agent emits structured LESSONS tag"
```

No test in this task — the prompt is text, validated indirectly via Task 4 extract_daily_lessons tests that parse the tag.

---

## Task 4: `extract_daily_lessons` — parse LESSONS tag

**Files:**
- Create: `alpha_agents/evolution/lessons.py`
- Create: `tests/test_evolution_lessons.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_evolution_lessons.py`:
```python
"""Tests for alpha_agents.evolution.lessons."""
from unittest.mock import patch


def test_extract_daily_lessons_parses_tag():
    report = """
复盘报告正文 blah blah...

<!-- LESSONS: [
  {"type":"success","theme":"CPO","content":"协创数据+9.4%命中","tags":"CPO,机构买入"},
  {"type":"failure","theme":"数据中心","content":"东方国信破5日线才减仓","tags":"止损时机"}
] -->
"""
    with patch("alpha_agents.evolution.lessons.insert_daily_lesson") as m:
        from alpha_agents.evolution.lessons import extract_daily_lessons
        count = extract_daily_lessons(report, "2026-04-17")
    assert count == 2
    assert m.call_count == 2
    call1 = m.call_args_list[0].kwargs
    assert call1["lesson_type"] == "success"
    assert call1["theme"] == "CPO"
    assert "协创数据" in call1["content"]


def test_extract_daily_lessons_no_tag_returns_zero():
    report = "报告里完全没有 LESSONS 标签"
    with patch("alpha_agents.evolution.lessons.insert_daily_lesson") as m:
        from alpha_agents.evolution.lessons import extract_daily_lessons
        count = extract_daily_lessons(report, "2026-04-17")
    assert count == 0
    assert m.call_count == 0


def test_extract_daily_lessons_malformed_json_returns_zero():
    report = "<!-- LESSONS: [this is not valid json] -->"
    with patch("alpha_agents.evolution.lessons.insert_daily_lesson") as m:
        from alpha_agents.evolution.lessons import extract_daily_lessons
        count = extract_daily_lessons(report, "2026-04-17")
    assert count == 0
    assert m.call_count == 0


def test_extract_daily_lessons_skips_empty_content():
    report = """<!-- LESSONS: [
        {"type":"success","content":""},
        {"type":"success","theme":"","content":"真经验"}
    ] -->"""
    with patch("alpha_agents.evolution.lessons.insert_daily_lesson") as m:
        from alpha_agents.evolution.lessons import extract_daily_lessons
        count = extract_daily_lessons(report, "2026-04-17")
    assert count == 1  # empty content row skipped
```

- [ ] **Step 2: Implement `lessons.py`**

Create `alpha_agents/evolution/lessons.py`:
```python
"""L2 Lessons — extract structured lessons from review report, consolidate into principles."""

from __future__ import annotations

import json
import logging
import re

from alpha_agents.data.memory_store import insert_daily_lesson

logger = logging.getLogger(__name__)

# Parallel to vpa.py's <!-- VERDICT: {...} --> pattern.
_LESSONS_TAG_RE = re.compile(r"<!--\s*LESSONS:\s*(\[.*?\])\s*-->", re.DOTALL)


def extract_daily_lessons(report: str, today: str) -> int:
    """Parse the <!-- LESSONS: [...] --> tag from a review report and persist rows.

    Returns the count of lessons successfully inserted. Unparseable/missing
    tag returns 0 (warn-logs only — don't raise, the review itself succeeded).
    """
    m = _LESSONS_TAG_RE.search(report)
    if not m:
        logger.info("No LESSONS tag in review report; skipping extraction")
        return 0

    raw = m.group(1).strip()
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("LESSONS JSON malformed: %s", e)
        return 0

    if not isinstance(items, list):
        logger.warning("LESSONS payload is not a list")
        return 0

    count = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        content = (item.get("content") or "").strip()
        lesson_type = (item.get("type") or "").strip()
        if not content or lesson_type not in ("success", "failure", "insight"):
            continue
        try:
            insert_daily_lesson(
                date=today,
                lesson_type=lesson_type,
                theme=(item.get("theme") or None) or None,
                content=content,
                tags=(item.get("tags") or ""),
            )
            count += 1
        except Exception as e:
            logger.warning("Failed to insert lesson %s: %s", content[:40], e)
    return count
```

Note — the import of `insert_daily_lesson` is at module top so mocks target `alpha_agents.evolution.lessons.insert_daily_lesson`.

- [ ] **Step 3: Run tests** — 4 pass.

- [ ] **Step 4: Commit**
```bash
git add alpha_agents/evolution/lessons.py tests/test_evolution_lessons.py
git commit -m "feat(evolution): extract_daily_lessons parses LESSONS tag"
```

---

## Task 5: `consolidate_principles` — LLM creates/reinforces/weakens

**Files:**
- Modify: `alpha_agents/evolution/lessons.py` (add function)
- Modify: `tests/test_evolution_lessons.py` (append tests)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_evolution_lessons.py`:
```python
def test_consolidate_principles_no_new_lessons_noop():
    with patch("alpha_agents.evolution.lessons.get_recent_daily_lessons",
               return_value=[]):
        from alpha_agents.evolution.lessons import consolidate_principles
        result = consolidate_principles("2026-04-17")
    assert result["created"] == 0
    assert result["reinforced"] == 0
    assert result["weakened"] == 0


def test_consolidate_principles_creates_new():
    lessons = [{"id": 1, "date": "2026-04-17", "lesson_type": "success",
                "theme": "CPO", "content": "协创数据+9.4%", "relevance_tags": "CPO,机构买入"}]
    fake_llm_response = {
        "operations": [
            {"op": "create",
             "principle": "CPO+机构买入 = 高胜率模式",
             "pattern_description": "CPO板块+龙虎榜机构买入",
             "category": "theme_timing",
             "action_guidance": "优先介入",
             "evidence": [{"code": "300857", "date": "04-17", "outcome": "+9.4%"}]},
        ]
    }
    with patch("alpha_agents.evolution.lessons.get_recent_daily_lessons",
               return_value=lessons), \
         patch("alpha_agents.evolution.lessons.get_all_principles_including_weakened",
               return_value=[]), \
         patch("alpha_agents.evolution.lessons._call_consolidation_llm",
               return_value=fake_llm_response), \
         patch("alpha_agents.evolution.lessons.create_trading_principle",
               return_value=1) as m_create:
        from alpha_agents.evolution.lessons import consolidate_principles
        result = consolidate_principles("2026-04-17")
    assert result["created"] == 1
    m_create.assert_called_once()


def test_consolidate_principles_reinforces_existing():
    lessons = [{"id": 2, "date": "2026-04-17", "lesson_type": "success",
                "theme": "CPO", "content": "另一只CPO命中", "relevance_tags": ""}]
    existing = [{"id": 7, "principle": "CPO+机构买入 = 高胜率模式",
                 "status": "active", "evidence_count": 3}]
    fake_llm = {"operations": [
        {"op": "reinforce", "principle_id": 7,
         "new_case": {"code": "688xxx", "date": "04-17", "outcome": "+5%"}},
    ]}
    with patch("alpha_agents.evolution.lessons.get_recent_daily_lessons",
               return_value=lessons), \
         patch("alpha_agents.evolution.lessons.get_all_principles_including_weakened",
               return_value=existing), \
         patch("alpha_agents.evolution.lessons._call_consolidation_llm",
               return_value=fake_llm), \
         patch("alpha_agents.evolution.lessons.reinforce_trading_principle") as m_reinf:
        from alpha_agents.evolution.lessons import consolidate_principles
        result = consolidate_principles("2026-04-17")
    assert result["reinforced"] == 1
    m_reinf.assert_called_once()


def test_consolidate_principles_weakens_on_bad_evidence():
    """Counter-evidence: if recent evidence win rate <40% with ≥3 cases, weaken."""
    lessons = [{"id": 3, "date": "2026-04-17", "lesson_type": "failure",
                "theme": "数据中心", "content": "数据中心再次失败", "relevance_tags": ""}]
    existing = [{"id": 9, "principle": "数据中心 = 稳健加仓方向",
                 "status": "active", "evidence_count": 5,
                 "evidence": '[{"outcome":"-2%"},{"outcome":"-1%"},{"outcome":"-3%"}]'}]
    fake_llm = {"operations": [
        {"op": "weaken", "principle_id": 9, "reason": "近3次全亏"},
    ]}
    with patch("alpha_agents.evolution.lessons.get_recent_daily_lessons",
               return_value=lessons), \
         patch("alpha_agents.evolution.lessons.get_all_principles_including_weakened",
               return_value=existing), \
         patch("alpha_agents.evolution.lessons._call_consolidation_llm",
               return_value=fake_llm), \
         patch("alpha_agents.evolution.lessons.set_principle_status") as m_status:
        from alpha_agents.evolution.lessons import consolidate_principles
        result = consolidate_principles("2026-04-17")
    assert result["weakened"] == 1
    m_status.assert_called_once_with(9, "weakened")
```

- [ ] **Step 2: Implement `consolidate_principles`**

Append to `alpha_agents/evolution/lessons.py`:
```python
from alpha_agents.data.memory_store import (
    get_recent_daily_lessons,
    get_all_principles_including_weakened,
    create_trading_principle,
    reinforce_trading_principle,
    set_principle_status,
)


_CONSOLIDATION_SYSTEM_PROMPT = """你是交易经验沉淀师。
读今天新增的 daily_lessons（实盘观察）+ 已有 trading_principles（历史沉淀的量价经验，风格类似 Anna Coulling《量价分析》的法则），判断是否：
- CREATE: 今天的 lesson 揭示了新的可复用模式，创建新 principle（必须带具体 pattern_description + action_guidance + 至少1条 evidence）
- REINFORCE: 今天的 lesson 是已有 principle 的新一个佐证案例
- WEAKEN: 今天的 lesson 反驳了某条 principle（或 principle 的 evidence 胜率已低于40%）

输出**只能**是这个 JSON（不要其他内容）：
{"operations": [
  {"op": "create", "principle": "...", "pattern_description": "...", "category": "vpa_signal|theme_timing|entry|exit|risk", "action_guidance": "...", "evidence": [{"code":"...", "date":"...", "outcome":"..."}]},
  {"op": "reinforce", "principle_id": 123, "new_case": {"code":"...", "date":"...", "outcome":"..."}},
  {"op": "weaken", "principle_id": 456, "reason": "..."}
]}

原则:
- principle 要具体到**量价形态+位置+量能配合**，不要空话
- 每条 principle 必须有证据支撑
- 若今天没值得沉淀的东西，输出 {"operations": []}"""


def _call_consolidation_llm(lessons: list[dict], principles: list[dict]) -> dict:
    """Call the consolidation LLM. Returns parsed JSON dict with 'operations' list."""
    from openai import OpenAI
    from alpha_agents.config import AGENT_API_KEY, AGENT_BASE_URL, AGENT_MODEL

    user_content = (
        "【今天新 lessons】\n" + json.dumps(lessons, ensure_ascii=False, indent=2)
        + "\n\n【已有 principles】\n" + json.dumps(
            [{"id": p["id"], "principle": p["principle"], "status": p["status"],
              "evidence_count": p.get("evidence_count", 0)} for p in principles],
            ensure_ascii=False, indent=2,
        )
    )
    client = OpenAI(api_key=AGENT_API_KEY, base_url=AGENT_BASE_URL)
    resp = client.chat.completions.create(
        model=AGENT_MODEL or "qwen-plus",
        messages=[
            {"role": "system", "content": _CONSOLIDATION_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        max_tokens=3000,
        timeout=60,
    )
    content = (resp.choices[0].message.content or "").strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        # Try to extract JSON from a code fence or loose text
        m = re.search(r'\{.*"operations".*\}', content, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
        logger.warning("Consolidation LLM returned unparseable content: %s",
                       content[:200])
        return {"operations": []}


def consolidate_principles(today: str) -> dict:
    """Run the daily consolidation: LLM decides create/reinforce/weaken.

    Returns dict {created, reinforced, weakened} counts.
    """
    lessons = get_recent_daily_lessons(days=1)  # only today's
    if not lessons:
        return {"created": 0, "reinforced": 0, "weakened": 0}

    principles = get_all_principles_including_weakened()

    try:
        result = _call_consolidation_llm(lessons, principles)
    except Exception as e:
        logger.warning("Consolidation LLM call failed: %s", e)
        return {"created": 0, "reinforced": 0, "weakened": 0}

    ops = result.get("operations", []) if isinstance(result, dict) else []
    counts = {"created": 0, "reinforced": 0, "weakened": 0}
    for op in ops:
        if not isinstance(op, dict):
            continue
        kind = op.get("op")
        try:
            if kind == "create":
                create_trading_principle(
                    principle=op["principle"],
                    pattern_description=op["pattern_description"],
                    category=op.get("category", "insight"),
                    action_guidance=op["action_guidance"],
                    evidence=op.get("evidence", []),
                    today=today,
                )
                counts["created"] += 1
            elif kind == "reinforce":
                reinforce_trading_principle(
                    op["principle_id"], today=today,
                    new_case=op.get("new_case"),
                )
                counts["reinforced"] += 1
            elif kind == "weaken":
                set_principle_status(op["principle_id"], "weakened")
                counts["weakened"] += 1
        except Exception as e:
            logger.warning("Consolidation op %s failed: %s", op, e)
    return counts
```

- [ ] **Step 3: Run tests** — 4 new pass + 4 previous pass (8 total in lessons file).

- [ ] **Step 4: Commit**
```bash
git add alpha_agents/evolution/lessons.py tests/test_evolution_lessons.py
git commit -m "feat(evolution): consolidate_principles — LLM manages principle lifecycle"
```

---

## Task 6: `post_review` public entry + wire into review.py

**Files:**
- Modify: `alpha_agents/evolution/lessons.py` (add `post_review`)
- Modify: `alpha_agents/evolution/__init__.py` (export)
- Modify: `alpha_agents/pipeline/tasks/review.py` (call at end)

- [ ] **Step 1: Add `post_review` to `lessons.py`**

At the end:
```python
async def post_review(today: str, review_report: str) -> str:
    """Phase 2 entry point: extract lessons, consolidate into principles.

    Called from review.py after the review report is generated. Returns a
    short summary string to append to the report (or empty string if nothing
    happened).
    """
    import asyncio

    # Step ①: extract structured lessons from the report's LESSONS tag
    lesson_count = await asyncio.to_thread(extract_daily_lessons, review_report, today)

    # Step ②: LLM consolidation (only if we got new lessons today)
    if lesson_count > 0:
        counts = await asyncio.to_thread(consolidate_principles, today)
    else:
        counts = {"created": 0, "reinforced": 0, "weakened": 0}

    if lesson_count == 0 and sum(counts.values()) == 0:
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
    return "\n".join(lines)
```

- [ ] **Step 2: Export from `__init__.py`**

Edit `alpha_agents/evolution/__init__.py`:
```python
from alpha_agents.evolution.lessons import (
    extract_daily_lessons,
    consolidate_principles,
    post_review,
)
```
Add them to `__all__`.

- [ ] **Step 3: Wire into `review.py`**

At the very end of `run_review()` (inside `alpha_agents/pipeline/tasks/review.py`), just before the `return report` line:
```python
# Phase 2: extract lessons + consolidate principles
try:
    from alpha_agents.evolution import post_review
    evolution_report = await post_review(today, report)
    if evolution_report:
        report += "\n\n" + evolution_report
except Exception as e:
    logger.warning("Evolution post_review failed: %s", e)
```

- [ ] **Step 4: Syntax check + import smoke**

```bash
uv run python -m py_compile alpha_agents/evolution/lessons.py alpha_agents/evolution/__init__.py alpha_agents/pipeline/tasks/review.py
uv run python -c "from alpha_agents.evolution import post_review, extract_daily_lessons, consolidate_principles; print('OK')"
```

- [ ] **Step 5: Commit**
```bash
git add alpha_agents/evolution/lessons.py alpha_agents/evolution/__init__.py alpha_agents/pipeline/tasks/review.py
git commit -m "feat(evolution): post_review entry wired into review.py"
```

---

## Task 7: Inject principles + recent lessons into contexts

**Files:**
- Modify: `alpha_agents/evolution/feedback.py` (add `inject_principles`, `inject_recent_lessons`)
- Modify: `alpha_agents/evolution/context_builder.py` (use them)
- Modify: `tests/test_evolution_feedback.py` (append tests)
- Modify: `tests/test_evolution_context_builder.py` (append integration tests)

- [ ] **Step 1: Write failing tests for the two new injectors**

Append to `tests/test_evolution_feedback.py`:
```python
def test_inject_principles_formats_by_category():
    fake = [
        {"id": 1, "principle": "高位缩量新高长上影 = 派发",
         "pattern_description": "...", "category": "vpa_signal",
         "action_guidance": "减仓", "win_rate": 0.8, "evidence_count": 5,
         "status": "active"},
        {"id": 2, "principle": "板块分化 = 主线虚胖",
         "pattern_description": "...", "category": "theme_timing",
         "action_guidance": "降级", "win_rate": 0.3, "evidence_count": 3,
         "status": "active"},
    ]
    with patch("alpha_agents.evolution.feedback.get_all_principles_including_weakened",
               return_value=fake):
        from alpha_agents.evolution.feedback import inject_principles
        result = inject_principles()
    assert "【交易经验手册】" in result
    assert "高位缩量新高" in result
    assert "vpa_signal" in result.lower() or "VPA信号" in result or "派发" in result


def test_inject_principles_empty_returns_empty():
    with patch("alpha_agents.evolution.feedback.get_all_principles_including_weakened",
               return_value=[]):
        from alpha_agents.evolution.feedback import inject_principles
        assert inject_principles() == ""


def test_inject_recent_lessons_formats():
    fake = [
        {"date": "2026-04-17", "lesson_type": "failure", "theme": "数据中心",
         "content": "东方国信破5日线"},
        {"date": "2026-04-16", "lesson_type": "success", "theme": "CPO",
         "content": "协创数据+9.4%"},
    ]
    with patch("alpha_agents.evolution.feedback.get_recent_daily_lessons",
               return_value=fake):
        from alpha_agents.evolution.feedback import inject_recent_lessons
        result = inject_recent_lessons()
    assert "【近期教训】" in result or "【近期经验】" in result
    assert "04-17" in result or "2026-04-17" in result
    assert "东方国信" in result
    assert "协创数据" in result


def test_inject_recent_lessons_empty():
    with patch("alpha_agents.evolution.feedback.get_recent_daily_lessons",
               return_value=[]):
        from alpha_agents.evolution.feedback import inject_recent_lessons
        assert inject_recent_lessons() == ""
```

- [ ] **Step 2: Implement injectors in `feedback.py`**

Add to module top imports:
```python
from alpha_agents.data.memory_store import (
    get_all_cognition_latest,
    get_all_principles_including_weakened,
    get_recent_daily_lessons,
)
```

Add functions:
```python
_PRINCIPLES_BUDGET = 800
_LESSONS_BUDGET = 600

_CATEGORY_HEADERS = {
    "vpa_signal": "VPA信号",
    "theme_timing": "主线择时",
    "entry": "入场",
    "exit": "出场",
    "risk": "风控",
}


def inject_principles() -> str:
    """Render active (and weakened) trading principles as an Anna-Coulling-style
    manual, grouped by category. Budget: 800 chars."""
    rows = get_all_principles_including_weakened()
    if not rows:
        return ""
    by_cat: dict[str, list[dict]] = {}
    for r in rows:
        by_cat.setdefault(r.get("category", "insight"), []).append(r)

    lines = [f"【交易经验手册】（{len(rows)}条）"]
    for cat, items in by_cat.items():
        header = _CATEGORY_HEADERS.get(cat, cat)
        lines.append(f"■ {header}")
        for r in items:
            wr = r.get("win_rate")
            ec = r.get("evidence_count", 0)
            tag = "⚠️" if r.get("status") == "weakened" else ""
            wr_str = f"胜率{wr*100:.0f}%" if wr is not None else ""
            meta = f"（{wr_str}, {ec}例）" if wr_str else f"（{ec}例）"
            line = f"• {tag}{r['principle']}{meta} → {r.get('action_guidance', '')}"
            if sum(len(x) for x in lines) + len(line) > _PRINCIPLES_BUDGET:
                return "\n".join(lines)
            lines.append(line)
    return "\n".join(lines)


def inject_recent_lessons(days: int = 7) -> str:
    """Render last N days of daily_lessons. Budget: 600 chars."""
    rows = get_recent_daily_lessons(days=days)
    if not rows:
        return ""
    lines = ["【近期教训】"]
    for r in rows:
        date = r.get("date", "")[5:]  # "04-17"
        ltype = r.get("lesson_type", "")
        theme = r.get("theme", "") or ""
        content = r.get("content", "")
        theme_bit = f"[{theme}] " if theme else ""
        line = f"• [{date}] {ltype}: {theme_bit}{content}"
        if sum(len(x) for x in lines) + len(line) > _LESSONS_BUDGET:
            break
        lines.append(line)
    return "\n".join(lines)
```

- [ ] **Step 3: Update `context_builder.py` compositions**

Replace `build_morning_context` and `build_chat_context`:
```python
from alpha_agents.evolution.feedback import (
    inject_cognition,
    inject_sentiment,
    inject_vpa_signal_history,
    inject_principles,
    inject_recent_lessons,
)


def build_morning_context(themes: list[dict], stats: str) -> str:
    sections = []
    for part in (inject_sentiment(), inject_cognition(),
                 inject_principles(), inject_recent_lessons()):
        if part:
            sections.append(part)
    if stats:
        sections.append(stats)
    return "\n\n".join(sections)


def build_chat_context(portfolio_summary: str, themes_summary: str,
                       stats_summary: str) -> str:
    sections = []
    for part in (portfolio_summary, themes_summary, stats_summary,
                 inject_sentiment(), inject_principles(),
                 inject_recent_lessons(days=3)):
        if part:
            sections.append(part)
    return "\n\n".join(sections)
```

- [ ] **Step 4: Update tests in `tests/test_evolution_context_builder.py`**

The Phase 1 morning/chat composition tests need to mock the new `inject_principles` and `inject_recent_lessons` calls too. Append a new test that verifies the new sections appear, and update existing tests if they break (`inject_principles` and `inject_recent_lessons` default to real DB → may return non-empty strings that break the empty-expected assertions).

Update the Phase 1 empty-case tests to also mock the new injectors as returning "". Example:
```python
def test_build_morning_context_handles_empty_sections():
    with patch("alpha_agents.evolution.context_builder.inject_sentiment", return_value=""), \
         patch("alpha_agents.evolution.context_builder.inject_cognition", return_value=""), \
         patch("alpha_agents.evolution.context_builder.inject_principles", return_value=""), \
         patch("alpha_agents.evolution.context_builder.inject_recent_lessons", return_value=""):
        from alpha_agents.evolution.context_builder import build_morning_context
        result = build_morning_context(themes=[], stats="")
    assert "【情绪周期】" not in result
    assert "【市场认知】" not in result
    assert "【交易经验手册】" not in result
```

Apply the same `inject_principles` and `inject_recent_lessons` patch to `test_build_morning_context_includes_sentiment_and_cognition`, `test_build_chat_context_adds_sentiment_to_existing_sections`, and `test_build_chat_context_empty_sentiment_skipped` — returning `""` in all of them to keep the assertions focused on what each test actually verifies.

Add a new integration test:
```python
def test_build_morning_context_includes_principles_and_lessons():
    with patch("alpha_agents.evolution.context_builder.inject_sentiment", return_value=""), \
         patch("alpha_agents.evolution.context_builder.inject_cognition", return_value=""), \
         patch("alpha_agents.evolution.context_builder.inject_principles",
               return_value="【交易经验手册】\n• 巨量长下影 = 买入高峰"), \
         patch("alpha_agents.evolution.context_builder.inject_recent_lessons",
               return_value="【近期教训】\n• [04-17] success: CPO命中"):
        from alpha_agents.evolution.context_builder import build_morning_context
        result = build_morning_context(themes=[], stats="")
    assert "【交易经验手册】" in result
    assert "【近期教训】" in result
```

- [ ] **Step 5: Run tests** — all feedback (12) + context_builder (7+) + schema (8) + lessons (8) = ~35+ pass.

- [ ] **Step 6: Commit**
```bash
git add alpha_agents/evolution/feedback.py alpha_agents/evolution/context_builder.py tests/test_evolution_feedback.py tests/test_evolution_context_builder.py
git commit -m "feat(evolution): inject principles + recent lessons into morning/chat contexts"
```

---

## Task 8: Smoke test + mark Phase 2 shipped

**Files:**
- Modify: `docs/superpowers/specs/2026-04-16-agent-evolution-design.md` (append Phase 2 shipped note)

- [ ] **Step 1: Smoke test the injection pipeline**

```bash
uv run python -c "
from alpha_agents.evolution import post_review, build_morning_context
import asyncio
# Fake report with a LESSONS tag
fake_report = '测试复盘报告。\n<!-- LESSONS: [{\"type\":\"insight\",\"theme\":\"Test\",\"content\":\"Phase 2 smoke test\"}] -->'
result = asyncio.run(post_review('2026-04-17', fake_report))
print('post_review:', repr(result))

ctx = build_morning_context(themes=[], stats='')
print('morning context head:', ctx[:500])
"
```

Expected:
- `post_review` returns a summary mentioning "1 条 lessons" (extraction works; consolidation may create/reinforce/weaken depending on LLM availability — failure is logged but not raised)
- `build_morning_context` output now includes a 【交易经验手册】 section (if any principles exist from consolidation)

- [ ] **Step 2: Mark Phase 2 shipped in spec**

Append to `docs/superpowers/specs/2026-04-16-agent-evolution-design.md` after the "Phase 1 Shipped" section:
```markdown

---

## Phase 2 Shipped — 2026-04-17

L2 Lessons + Trading Principles landed on `feat/evolution-phase1`:

**New tables:** `daily_lessons`, `trading_principles` with CRUD helpers.

**New module** `alpha_agents/evolution/lessons.py`:
- `extract_daily_lessons(report, today)` — parses `<!-- LESSONS: [...] -->` tag from review output
- `consolidate_principles(today)` — LLM reads today's lessons + existing principles → creates/reinforces/weakens
- `post_review(today, report)` — async entrypoint called from `review.py`

**Prompt change:** `alpha_agents/prompts/review.md` now requires a structured `<!-- LESSONS: [{type, theme, content, tags}] -->` tag.

**Context upgrade:** `build_morning_context` and `build_chat_context` now inject 【交易经验手册】 (active + weakened principles) and 【近期教训】 (recent daily_lessons).

**Unblocks Phase 3:** Principles form the qualitative side of evolution; Phase 3 Playbooks add the quantitative weight-adjustment side.
```

- [ ] **Step 3: Commit**
```bash
git add docs/superpowers/specs/2026-04-16-agent-evolution-design.md
git commit -m "docs(evolution): mark Phase 2 shipped"
```

---

## Done Criteria (Phase 2)

- [ ] `daily_lessons` and `trading_principles` tables exist (idempotent migration)
- [ ] CRUD helpers for both tables with unit tests
- [ ] `review.md` prompt emits `<!-- LESSONS: [...] -->` tag
- [ ] `extract_daily_lessons` parses the tag, skips malformed JSON gracefully
- [ ] `consolidate_principles` calls the LLM; handles create/reinforce/weaken; degrades gracefully on LLM error
- [ ] `post_review` wired at end of `run_review()`
- [ ] `build_morning_context` and `build_chat_context` inject principles + recent lessons
- [ ] All Phase 1 tests still pass (10 feedback + 6 context_builder = 16)
- [ ] New tests pass: 6 schema + 4 lessons extraction + 4 lessons consolidation + 4 new feedback + 1 new context_builder = 19 new tests
