# Agent Evolution System — Design Spec

**Date:** 2026-04-16
**Goal:** Give AlphaAgents a closed learning loop: review findings feed back into next-day decisions, trading experience accumulates as reusable principles, and successful patterns become auto-weighted playbooks.

**Reference:** Hermes Agent (skills + memory), OpenAI Agents SDK v0.14.0 (extract → store → inject pattern). Adapted for a trading-specific domain with rule-driven weight adjustments + LLM annotations.

---

## Three-Layer Architecture

| Layer | What | Trigger |
|-------|------|---------|
| L1 — Feedback (接断线) | Connect existing data that's written but not read: VPA signal confirmations, market cognition, sentiment cycle | Every VPA call + every morning scan |
| L2 — Lessons (经验记忆) | Extract structured lessons from daily review, consolidate into lasting trading principles (Anna Coulling style) | Daily review |
| L3 — Playbooks (策略进化) | Codify successful trading patterns, track hit rate, auto-adjust weights, LLM annotates degraded ones | Daily review + intraday matching |

All three layers share a single write entry point (`post_review()`) and a unified read interface (`context_builder`).

---

## Data Layer

### New Table: `daily_lessons`

```sql
CREATE TABLE IF NOT EXISTS daily_lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    lesson_type TEXT NOT NULL,        -- 'success' | 'failure' | 'insight'
    theme TEXT,                       -- associated theme line (nullable)
    content TEXT NOT NULL,
    source TEXT DEFAULT 'review',
    relevance_tags TEXT DEFAULT '',   -- comma-separated keywords for retrieval
    consolidated_into INTEGER,        -- FK to trading_principles.id (NULL = not yet consolidated)
    UNIQUE(date, content)
);
```

Permanent storage. No expiry. Injection filtered by recency + theme relevance.

### New Table: `trading_principles`

Two-tier memory: daily_lessons are raw observations; trading_principles are distilled experience — like Anna Coulling's VPA rules, but learned from our own trades.

```sql
CREATE TABLE IF NOT EXISTS trading_principles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    principle TEXT NOT NULL,              -- experience rule (1-2 sentences, Anna Coulling style)
    pattern_description TEXT NOT NULL,    -- volume-price pattern (what K-line + what volume + what position)
    category TEXT NOT NULL,              -- 'vpa_signal' | 'theme_timing' | 'entry' | 'exit' | 'risk'
    action_guidance TEXT NOT NULL,        -- what to do when this pattern appears
    evidence TEXT NOT NULL,              -- JSON array: [{code, name, date, outcome}]
    evidence_count INTEGER DEFAULT 1,
    win_rate REAL,                       -- historical win rate of this pattern
    first_learned TEXT NOT NULL,
    last_reinforced TEXT NOT NULL,
    status TEXT DEFAULT 'active',        -- active | weakened | retired
    UNIQUE(principle)
);
```

**Lifecycle:**
- Active: default state, always injected into agent prompts
- Weakened: >30 days without reinforcement, still injected but marked
- Retired: >60 days without reinforcement, not injected
- **Counter-evidence weakening** (Gap 7): during `consolidate_principles`, if a principle's recent evidence (last 7 days, ≥3 cases) has win rate <40%, immediately mark `weakened` regardless of time. Reinforcement path still available — doesn't need to wait 30 days to self-correct.

**Example principle:**

> **principle:** 巨量长下影阴线在吸筹区底部出现是买入高峰信号，后续未破低点即确认
> **pattern_description:** 跌幅>5%，下影占比≥30%，量比≥1.5，位于近20日低位区
> **action_guidance:** 不追跌，等次日缩量不破低点后介入，止损设在当日低点下方2%
> **evidence:** [{"code":"300274","name":"阳光电源","date":"04-01","outcome":"+8.2% in 10d"}]

### New Table: `playbooks`

```sql
CREATE TABLE IF NOT EXISTS playbooks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    pattern_json TEXT NOT NULL,           -- trigger conditions (structured JSON)
    created_date TEXT NOT NULL,
    last_updated TEXT NOT NULL,
    status TEXT DEFAULT 'active',         -- active | degraded | deprecated
    weight REAL DEFAULT 1.0,
    total_trades INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0,
    hit_rate REAL DEFAULT 0.0,
    avg_return REAL DEFAULT 0.0,
    annotation TEXT DEFAULT '',           -- LLM annotation (why degraded)
    annotation_date TEXT DEFAULT '',
    version_history TEXT DEFAULT '[]'     -- Gap 8: JSON array of {date, old_status, new_status, reason, hit_rate_at_change}
);
```

**pattern_json structure:**
```json
{
  "conditions": [
    {"field": "vpa_verdict", "op": "in", "value": ["bullish"]},
    {"field": "theme_strength", "op": ">=", "value": 7},
    {"field": "lhb_institutional", "op": "contains", "value": "机构买入"},
    {"field": "change_pct", "op": "<=", "value": 5.0}
  ],
  "description": "VPA偏多 + 强主线 + 龙虎榜机构买入 + 涨幅未过热"
}
```

Available fields: `vpa_verdict`, `vpa_phase`, `theme_strength`, `theme_status`, `change_pct`, `score`, `institutional`, `price_vs_20d_high`.

**Lifecycle (rule-driven):**

| Condition | Action |
|-----------|--------|
| `total_trades >= 5` AND `hit_rate < 0.4` | `status = degraded`, `weight = 0.5`, trigger LLM annotation |
| `degraded` AND last 5 trades `hit_rate >= 0.6` | `status = active`, `weight = 1.0` |
| `degraded` for >14 days without recovery | `status = deprecated`, `weight = 0.0` |
| `active` AND `hit_rate >= 0.7` AND `total_trades >= 10` | `weight = 1.5` (boosted recommendation) |

**Auto-creation:** Review scans last 7 days of hit predictions where `report_type = 'intraday'` (Gap 4: exclude `intraday_signal` 涨停观察, which would dominate clustering with useless patterns). If ≥3 share the same feature cluster (same vpa_verdict + theme_status + institutional flag), auto-create as active playbook.

**Regime-change fallback (Gap 6):** When fewer than 2 playbooks are `active` (e.g., market regime shift caused widespread degradation), `match_playbook()` returns `None` for all candidates and intraday falls back to pure score ranking. Prevents the system from ranking on a broken model during turbulent markets.

### Predictions Table Extension (Gap 1 — Critical)

Current `predictions` schema stores `direction, confidence, theme_line, reason` but **lacks the features needed for playbook clustering**: VPA verdict, score, institutional flag, theme strength at time of recommendation.

**Change:** Add column `features_json TEXT DEFAULT '{}'` to `predictions`. At `save_prediction()` time in `intraday_monitor.py`, serialize the decision features:

```json
{
  "vpa_verdict": "bullish",
  "vpa_phase": "吸筹尾声",
  "theme_strength": 9,
  "theme_status": "peak",
  "institutional": "机构买入1.42亿",
  "score": 62,
  "change_pct": 2.48,
  "price_vs_20d_high": 0.87
}
```

Backfill old rows with `NULL`-safe defaults in playbook clustering (skip rows where `features_json IS NULL OR = '{}'`).

### No new table for strategy_stats

Aggregated on-the-fly from `predictions.features_json` + `playbooks` via SQL. No redundant table.

---

## Module Structure

```
alpha_agents/evolution/
├── __init__.py              # Public API: post_review(), build_context()
├── feedback.py              # L1: inject_vpa_signal_history, inject_cognition, inject_sentiment
├── lessons.py               # L2: extract_daily_lessons, consolidate_principles (LLM)
├── playbook.py              # L3: match_playbook, update_playbook_stats, annotate_degraded (LLM), create_playbook_from_pattern
└── context_builder.py       # Unified context: build_morning_context, build_chat_context, build_vpa_context
```

---

## Call Flow

### Daily Review (write path)

```
review.py :: run_review()
    ... existing logic (verify predictions, update themes, cognition, archive) ...
    
    from alpha_agents.evolution import post_review
    evolution_report = await post_review(today, review_report)
    
    # post_review internally:
    #   ① extract_daily_lessons(review_report, today)
    #      → parse review report → write daily_lessons rows
    #      → PREREQUISITE (Gap 2): review_agent.py prompt must require a structured
    #        <!-- LESSONS: [{"type":"success|failure|insight","theme":"...","content":"..."}] -->
    #        tag at end of report. extract_daily_lessons reads this tag (not fuzzy-matching
    #        the free-form 经验总结 section). If tag missing, fall back to empty list + log warning.
    #   ② consolidate_principles(today)
    #      → LLM reads today's lessons + existing principles
    #      → creates new / reinforces / weakens principles
    #      → also checks counter-evidence: principle's recent win rate <40% over ≥3 cases → weaken
    #   ③ update_playbook_stats(today)
    #      → rule engine adjusts weight/status (and appends to version_history)
    #      → if degraded threshold hit: annotate_degraded() (LLM)
    #      → scan for auto-create candidates (report_type='intraday' only)
    #   ④ evolution_metrics(today) — weekly only (Gap 9)
    #      → compute week-over-week hit rate trend, playbook contribution delta,
    #        principle utilization rate. Snapshot to daily_snapshots.
    #        Surfaces in weekly_report.
    
    report += "\n\n" + evolution_report
```

**LLM calls per review: max 2** (lessons consolidation + playbook annotation if triggered). Most days only 1.

### Principle vs Playbook Conflict Resolution (Gap 5)

Principles and playbooks are **orthogonal** by design — no tie-breaking needed:
- **Playbooks** influence numeric `score` (weight × score), affecting ranking and filtering.
- **Principles** are injected into agent prompts as qualitative guidance, affecting the agent's free-form judgment.

The Morning/Chat agent prompt includes an explicit instruction: *"Playbook 权重已体现在候选 score 中；不要基于 principle 对同一候选重复降级或升级。Principle 适用于 playbook 未覆盖的情景判断。"* This prevents double-counting.

### Morning Scan (read path)

```
morning_scan.py :: run_morning_scan()
    ... fetch news, discover themes ...
    
    from alpha_agents.evolution.context_builder import build_morning_context
    enhanced_ctx = build_morning_context(themes, stats)
    # Injects: cognition, sentiment, principles, lessons, playbooks
    
    report = await run_morning_analysis(events_ctx, themes_ctx, enhanced_ctx)
```

### Intraday VPA (read path)

```
vpa.py :: compute_vpa_with_llm(code, name)
    ... existing _load_ohlcv + _compute_derived ...
    
    from alpha_agents.evolution.feedback import inject_vpa_signal_history
    signal_history = inject_vpa_signal_history(code)
    # → "04-14 射击十字星(看空) → ✅已确认（04-15跌3.6%）"
    
    if signal_history:
        user_content = signal_history + "\n\n" + user_content
```

### Intraday Matching (playbook scoring)

```
intraday_monitor.py :: after VPA gate, before ranking
    from alpha_agents.evolution.playbook import match_playbook
    
    for a in actionable:
        pb = match_playbook(a)
        if pb:
            a["playbook"] = pb.name
            a["score"] *= pb.weight  # 1.5 boost / 0.5 degrade / 0.0 exclude
```

### Chat Agent (read path)

```
chat.py :: _build_context() replaced by:
    from alpha_agents.evolution.context_builder import build_chat_context
```

---

## Context Injection Spec

### build_morning_context output format

```
【情绪周期】升温 — 可追强势，高beta优先

【市场认知】
• CPO: 高位 + 资金流入 — "强势延续"
• 数据中心: 中位 + 资金流入 — "资金驱动但缺龙头"

【交易经验手册】（12条 active）
■ VPA信号
① 巨量长下影阴线在吸筹区底部 = 买入高峰（胜率75%, 4例）→ 等缩量不破低再介入
② 高位缩量新高+长上影放量 = 派发尾声（胜率80%, 5例）→ 减仓或剔除
■ 主线择时
③ 板块涨但个股全线分化 = 主线虚胖（胜率30%, 3例）→ 降级，暂停开仓
...

【近期教训】
• [04-16] failure: 东方国信破5日线才预警，应在逼近均线时就降级
• [04-15] success: CPO+机构买入组合命中率高，协创数据+9.4%

【活跃 Playbook】
• "CPO突破+机构买入" — 胜率78%(9/7), weight=1.5 ⭐
• "吸筹尾声+缩量长下影" — 胜率60%(5/3), weight=1.0
```

### Token budgets

| Section | Morning | Chat | VPA |
|---------|---------|------|-----|
| sentiment | 50 | 50 | — |
| cognition | 300 | — | — |
| principles | 800 | 400 | 200 |
| daily_lessons | 600 | 300 | — |
| playbooks | 400 | — | — |
| vpa_signal_history | — | — | 400 |
| **Extra total** | **~2150 chars** | **~750 chars** | **~600 chars** |

### Injection retrieval strategy (daily_lessons)

- **Recent window:** `date >= 7 days ago` → all lessons injected
- **Historical retrieval:** `date < 7 days ago` AND `theme IN (active themes)` → top 10 by date DESC
- **Truncation:** total not exceeding budget, oldest cut first

### Injection retrieval strategy (trading_principles)

- All `status = active` principles injected (expected ~10-20, very concise)
- `status = weakened` included with ⚠️ marker
- `status = retired` excluded

### build_vpa_context additions (Gap 3)

When a code matches an active playbook, append playbook info so the VPA LLM knows the pattern has historical backing:

```
【该股当前匹配的 Playbook】
• "CPO突破+机构买入" — 历史胜率 78%（9胜7负）
  → LLM 在分析中可引用此模式作为历史参照
```

If no playbook matches, this section is omitted.

### A/B mode for evolution validation (Gap 10)

`build_morning_context(themes, stats, mode="full")` accepts:

| mode | Included sections |
|------|-------------------|
| `"full"` (default) | sentiment + cognition + principles + lessons + playbooks + stats |
| `"baseline"` | stats only (pre-evolution behavior) |

A `/evolution ab` chat command runs two morning analyses (one full, one baseline) and diffs the recommendation sets. Used periodically to verify the evolution system actually improves decisions vs baseline.

---

## Files Changed

### New files

```
alpha_agents/evolution/__init__.py           ~30 lines
alpha_agents/evolution/feedback.py           ~80 lines
alpha_agents/evolution/lessons.py            ~150 lines
alpha_agents/evolution/playbook.py           ~200 lines
alpha_agents/evolution/context_builder.py    ~180 lines
```

### Modified files

| File | Change |
|------|--------|
| `alpha_agents/data/memory_store.py` | Add 3 table schemas + CRUD functions; add `features_json` column to `predictions` |
| `alpha_agents/pipeline/tasks/review.py` | Add `await post_review(today, report)` at end |
| `alpha_agents/pipeline/tasks/morning_scan.py` | Replace context assembly with `build_morning_context()` |
| `alpha_agents/tools/vpa.py` | Add `inject_vpa_signal_history(code)` + `build_vpa_context(code)` before LLM call |
| `alpha_agents/agents/chat.py` | Replace `_build_context()` with `build_chat_context()` |
| `alpha_agents/pipeline/tasks/intraday_monitor.py` | Add `match_playbook()` after VPA gate; serialize `features_json` into `save_prediction()` |
| `alpha_agents/agents/review_agent.py` | Prompt adds `<!-- LESSONS: [...] -->` structured output requirement (Gap 2) |
| `alpha_agents/agents/chat_commands/handlers.py` | Add `/playbook` and `/evolution` commands |
| `alpha_agents/pipeline/tasks/weekly_report.py` | Surface `evolution_metrics` snapshot in weekly report |

### Untouched

| File | Reason |
|------|--------|
| `alpha_agents/agents/morning.py` | Prompt template unchanged; richer context comes via build_morning_context |
| `alpha_agents/pipeline/scheduler.py` | Schedule unchanged |
| `alpha_agents/tools/vpa.py` ANNA_COULLING_PROMPT | Prompt unchanged; signal history injected via user_content |

---

## Design Constraints

1. **Single write entry point:** Only `post_review()` writes to lessons/principles/playbooks. Morning scan and intraday only read. No concurrent write conflicts.
2. **LLM calls capped:** `post_review()` makes at most 2 LLM calls (lesson consolidation + playbook annotation if threshold triggered). Most days just 1.
3. **Token budgets enforced:** `context_builder` sections have character limits. Overflow truncated by priority (oldest lessons first, lowest-evidence principles first).
4. **Playbook weights are rule-driven:** No LLM in the scoring loop. LLM only provides annotation text explaining why a playbook degraded.
5. **Principles are evidence-backed:** Every principle must reference at least one real trade. No abstract platitudes.
6. **Signal-vs-actionable separation:** Playbook auto-creation uses only `report_type='intraday'` predictions. `intraday_signal` (涨停观察) is excluded — it's not a trading decision.
7. **Regime-change safety:** When `active` playbooks drop below 2, intraday skips playbook weighting and falls back to raw score ranking.
8. **Orthogonal principles vs playbooks:** Playbooks affect score (quantitative); principles guide agent judgment (qualitative). Agent prompt instructs no double-counting.
9. **Evolution observability:** Weekly `evolution_metrics` tracks whether the evolution system itself improves outcomes — if not, the whole architecture is questionable, and this must be visible.

## Implementation Phasing

The spec supports the dependency chain L1 → L2 → L3 as discrete phases, each independently shippable:

**Phase 1 — L1 Feedback + predictions schema extension**
- `feedback.py` + `context_builder.py` shell
- Add `features_json` to predictions, backfill start
- Inject cognition/sentiment/vpa_signal_history into existing agents
- Unblocks: L2 and L3 (they need features_json to work)

**Phase 2 — L2 Lessons + Principles**
- `lessons.py` (extract + consolidate)
- Review agent prompt gets `<!-- LESSONS: [...] -->` requirement
- `daily_lessons` + `trading_principles` tables + counter-evidence weakening
- Unblocks: Principles appear in agent context immediately

**Phase 3 — L3 Playbooks**
- `playbook.py` (match + stats + annotate + auto-create)
- `playbooks` table + `version_history`
- Intraday integration + regime fallback
- `/playbook` command
- Ships when ≥2 weeks of `features_json`-populated predictions exist (enough to bootstrap auto-creation)

**Phase 4 — Evolution metrics + A/B** (optional polish)
- `evolution_metrics()` in post_review
- `build_morning_context(mode="baseline")` + `/evolution ab`
- Weekly report surface
- Validates the whole system actually works

## Design Revision History

The initial draft of this spec was reviewed via `/autoresearch` (3 iterations) which surfaced 10 gaps now resolved above:

1. ~~predictions table lacks playbook-clustering features~~ → added `features_json`
2. ~~free-form lesson extraction is fragile~~ → review agent emits `<!-- LESSONS: [...] -->`
3. ~~build_vpa_context missed matched playbook info~~ → added "当前匹配的 Playbook" section
4. ~~signal noise would dominate playbook clustering~~ → restrict to `report_type='intraday'`
5. ~~principle vs playbook conflict~~ → declared orthogonal + agent prompt instruction
6. ~~no regime-change fallback~~ → active playbook < 2 skips weighting
7. ~~principle weakening only time-based~~ → counter-evidence triggers immediate weakening
8. ~~no playbook version snapshot~~ → `version_history` JSON column
9. ~~no meta-metric for evolution effectiveness~~ → `evolution_metrics` weekly
10. ~~no A/B validation of evolution benefit~~ → `build_morning_context(mode="baseline")`
