"""Persistent memory for the analyst — theme lines, predictions, market cognition.

Follows the same pattern as report_store.py: thread-local SQLite connections,
plain functions, JSON for flexible fields.
"""

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from alpha_agents.config import MEMORY_DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS theme_lines (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    status TEXT DEFAULT 'watching',
    -- Two numbers, two questions. `strength` accumulates one delta per
    -- day and answers "how long has this line been confirmed" — it drives
    -- the lifecycle (watching→active→peak→declining) and the ≥4 gate that
    -- keeps a pending order alive. `daily_score` is today's raw
    -- bullish−bearish, rewritten every cycle, and answers "how strong is
    -- this line today" — the only one of the two that can rank lines
    -- against each other, since a two-week-old line always out-accumulates
    -- one that broke out this morning.
    strength INTEGER DEFAULT 0,
    daily_score INTEGER DEFAULT 0,
    last_scored_date TEXT,
    created_at TEXT,
    updated_at TEXT,
    catalyst TEXT,
    core_stocks TEXT,
    leader_code TEXT,
    notes TEXT
);

-- A position is held because of a thesis, and the thesis — not the fill —
-- is what gets graded. See alpha_agents/data/thesis.py and
-- docs/thesis_design.md for why the invalidation conditions are a closed
-- vocabulary rather than free text.
CREATE TABLE IF NOT EXISTS theses (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL,
    name TEXT,
    theme TEXT,
    claim TEXT,                 -- what the agent believes will happen
    horizon_days INTEGER,       -- how long it gave itself to be right
    prob REAL,                  -- its own 0-1, not derived by code
    conviction REAL,            -- drives position size
    entry_fraction REAL DEFAULT 1.0,  -- 首笔建多少，其余留给加仓
    conditions TEXT,            -- JSON: the invalidation vocabulary
    status TEXT DEFAULT 'active',
    position_id INTEGER,        -- virtual_portfolio row, once filled
    created_by TEXT,
    created_at TEXT,
    closed_at TEXT,
    close_kind TEXT,            -- which condition fired, empty on blind_spot
    close_note TEXT,
    checkpoints TEXT,           -- JSON: each re-read of a live thesis
    trader_id TEXT DEFAULT 'default'
);

CREATE INDEX IF NOT EXISTS idx_theses_status ON theses(status);
CREATE INDEX IF NOT EXISTS idx_theses_position ON theses(position_id);

CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    report_type TEXT,
    code TEXT NOT NULL,
    name TEXT,
    direction TEXT,
    confidence TEXT,
    theme_line TEXT,
    entry_price REAL,
    reason TEXT,
    next_day_return REAL,
    week_return REAL,
    hit INTEGER,
    review_note TEXT,
    features_json TEXT DEFAULT '{}',
    -- G1: probabilistic forecast and its market-derived scores. Declared
    -- here so a fresh database has them natively; the ALTER TABLE
    -- migrations in _get_conn exist for databases created before this.
    -- Wall-clock of the call. `date` alone cannot order the intraday
    -- signals of one session, which is exactly what the 实时异动 timeline
    -- needs: it rendered every row's time as a dash.
    created_at TEXT,
    prob REAL,
    brier REAL,
    log_score REAL,
    excess_return REAL,
    residual_alpha REAL,
    scored_at TEXT,
    -- Whose call this was. Part of the uniqueness key, so two traders
    -- recommending the same stock on the same day are two predictions to
    -- be graded separately rather than one overwriting the other.
    trader_id TEXT DEFAULT 'default'
);
CREATE INDEX IF NOT EXISTS idx_pred_date ON predictions(date);
CREATE INDEX IF NOT EXISTS idx_pred_code ON predictions(code);

CREATE TABLE IF NOT EXISTS market_cognition (
    id INTEGER PRIMARY KEY,
    sector TEXT NOT NULL,
    date TEXT NOT NULL,
    position TEXT,
    fund_trend TEXT,
    pe_percentile REAL,
    recent_events TEXT,
    assessment TEXT,
    UNIQUE(sector, date)
);
CREATE INDEX IF NOT EXISTS idx_cognition_sector ON market_cognition(sector);

CREATE TABLE IF NOT EXISTS daily_snapshots (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    data_type TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(date, data_type)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_date ON daily_snapshots(date);

CREATE TABLE IF NOT EXISTS virtual_portfolio (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL,
    name TEXT,
    theme TEXT,
    -- Pending order fields (set at recommendation time)
    order_date TEXT NOT NULL,             -- 挂单日
    entry_low REAL,                       -- 介入区间下限
    entry_high REAL,                      -- 介入区间上限
    stop_loss REAL,                       -- 止损价
    target_price REAL,                    -- 止盈目标价
    expire_days INTEGER DEFAULT 2,        -- 挂单有效天数
    -- Fill fields (set when order triggers)
    open_date TEXT,                        -- 实际建仓日
    open_price REAL,                       -- 实际建仓价
    shares INTEGER DEFAULT 0,
    -- Status: pending → open → stopped/target_hit/expired/cancelled
    status TEXT DEFAULT 'pending',
    close_date TEXT,
    close_price REAL,
    holding_days INTEGER DEFAULT 0,
    return_pct REAL,
    return_amount REAL,
    peak_return_pct REAL DEFAULT 0,
    max_drawdown_pct REAL DEFAULT 0,
    source TEXT,
    reason TEXT,
    close_reason TEXT,
    -- Whose book this is. Capital, positions and every learning
    -- statistic are per-trader: sharing them would defeat the comparison
    -- the traders exist for, since two strategies drawing from one pot
    -- measure ordering rather than skill.
    trader_id TEXT DEFAULT 'default',
    -- The prediction that produced this order, recorded at creation time.
    -- The closing path used to re-derive this by matching stock plus a
    -- nearby date, which could hand one trader's realised result to
    -- another trader's prediction. NULL means unknown, and unknown stays
    -- unknown: it is never filled in by a later guess.
    prediction_id INTEGER,
    legacy_realized_amount REAL,
    -- The thesis this order serves. Design §4's chain is
    -- thesis_id → order_id → fill_id → ledger_entry_id; this column is
    -- the order end of it and position_exits.thesis_id is the ledger end.
    -- There is no separate fills table, so the entry fill is this row.
    -- Before the column existed the link lived only on
    -- theses.position_id, which meant "given an order, which idea is it
    -- for" was answerable only by scanning live theses for the same stock
    -- — and a scan is not an ownership relation.
    thesis_id INTEGER,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_portfolio_status ON virtual_portfolio(status);
CREATE INDEX IF NOT EXISTS idx_portfolio_date ON virtual_portfolio(open_date);

-- Every realised exit, one row per leg. virtual_portfolio.return_amount is
-- a display total that the final exit used to overwrite; this table is the
-- record the totals are derived from, so a trimmed position's realised P&L
-- is summed rather than lost. Append-only: see alpha_agents/data/trade_ledger.py.
CREATE TABLE IF NOT EXISTS position_exits (
    id INTEGER PRIMARY KEY,
    position_id INTEGER NOT NULL,
    trader_id TEXT NOT NULL,
    code TEXT NOT NULL,
    exit_date TEXT NOT NULL,
    price REAL NOT NULL,
    shares INTEGER NOT NULL,
    cost_basis REAL NOT NULL,
    gross_amount REAL NOT NULL,
    costs REAL NOT NULL,
    net_amount REAL NOT NULL,
    return_pct REAL NOT NULL,
    reason TEXT,
    command_id TEXT,
    request_json TEXT,
    -- Denormalised from the position so a realised leg answers "which
    -- thesis paid for this" without a join through a row that may have
    -- been re-opened. Attribution has to survive the position.
    thesis_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (position_id, command_id)
);
CREATE INDEX IF NOT EXISTS idx_exits_trader ON position_exits(trader_id);
CREATE INDEX IF NOT EXISTS idx_exits_position ON position_exits(position_id);
-- idx_exits_thesis is created after the migrations, not here: this script
-- runs before them, and a pre-thesis_id database would fail on the missing
-- column before the ALTER ever got a chance to add it.

-- The information boundary of one decision, frozen when it is made.
--
-- Invariant 4: a decision may use only what was available at decision
-- time, and later corrections must not rewrite that boundary. That is
-- only true if the boundary is written down at the moment of deciding —
-- so this table is append-only, and the triggers below make it so rather
-- than trusting every future caller to remember. Nothing updates a
-- snapshot; a revised view is a new row that names the one it supersedes.
CREATE TABLE IF NOT EXISTS decision_snapshots (
    id INTEGER PRIMARY KEY,
    thesis_id INTEGER,
    order_id INTEGER,
    prediction_id INTEGER,
    trader_id TEXT NOT NULL,
    code TEXT NOT NULL,
    -- The latest instant whose information the decision was allowed to
    -- use. Not when it ran: a 09:00 scan may legitimately read
    -- yesterday's close and nothing after.
    information_cutoff TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    -- Declared decision inputs, as the agent stated them. Frozen verbatim
    -- so a later edit to the thesis cannot retcon what was decided.
    payload_json TEXT NOT NULL,
    -- Provenance of the producers whose output fed this decision.
    policy_ref TEXT,
    model_ref TEXT,
    sources_json TEXT NOT NULL DEFAULT '[]',
    -- sha256 over the frozen fields. A consumer can detect that a
    -- snapshot was rewritten after the fact, which the triggers should
    -- already prevent — belt and braces, because the invariant is load
    -- bearing for every downstream evaluation.
    content_hash TEXT NOT NULL,
    supersedes_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_snapshots_thesis ON decision_snapshots(thesis_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_order ON decision_snapshots(order_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_trader ON decision_snapshots(trader_id);

CREATE TRIGGER IF NOT EXISTS decision_snapshots_no_update
BEFORE UPDATE ON decision_snapshots
BEGIN
    SELECT RAISE(ABORT, 'decision_snapshots is append-only');
END;

CREATE TRIGGER IF NOT EXISTS decision_snapshots_no_delete
BEFORE DELETE ON decision_snapshots
BEGIN
    SELECT RAISE(ABORT, 'decision_snapshots is append-only');
END;

-- Cash reservations for pending orders. A pending order is an intent to
-- spend, not a spent amount; the old get_available_capital reported a
-- number two simultaneous fills could both respect, because nothing had
-- earmarked the cash. This table closes the gap: one held row per
-- (order, kind) pair, the kind lets future slices add inventory
-- reservations without a schema change. Lifecycle: held (order open) →
-- consumed (filled) or released (cancelled/expired/rejected). Reconciliation
-- reads both this table and position_exits to verify the two agree.
CREATE TABLE IF NOT EXISTS reservations (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL,
    trader_id TEXT NOT NULL,
    code TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'cash_reserve',
    amount REAL NOT NULL,
    consumed_amount REAL NOT NULL DEFAULT 0,
    state TEXT NOT NULL CHECK(state IN ('held','consumed','released')),
    reason TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    released_at TEXT,
    UNIQUE (order_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_reservations_state ON reservations(state);
CREATE INDEX IF NOT EXISTS idx_reservations_trader ON reservations(trader_id);

-- Audit log of every cross-table invariant check. Reconciliation reads
-- the production tables (virtual_portfolio, position_exits, reservations)
-- and writes only to these two: the run records when the check happened
-- and what the derived totals were, and the diffs record each
-- discrepancy with severity so a fix can be prioritised. No FK is
-- declared because the audit log is meant to outlive the production
-- tables — a table dropped in a refactor should not erase the history
-- of finding it inconsistent.
CREATE TABLE IF NOT EXISTS reconciliation_runs (
    id INTEGER PRIMARY KEY,
    run_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    status TEXT NOT NULL CHECK(status IN ('clean','dirty','error')),
    diff_count INTEGER NOT NULL DEFAULT 0,
    trader_count INTEGER NOT NULL DEFAULT 0,
    summary_json TEXT NOT NULL DEFAULT '{}',
    finished_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS reconciliation_diffs (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    trader_id TEXT NOT NULL,
    invariant TEXT NOT NULL,            -- e.g. 'orphan_exit', 'consumed_amount_mismatch'
    severity TEXT NOT NULL CHECK(severity IN ('critical','major','minor')),
    detail_json TEXT NOT NULL DEFAULT '{}',
    position_id INTEGER,
    reservation_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_recon_diffs_run ON reconciliation_diffs(run_id);
CREATE INDEX IF NOT EXISTS idx_recon_diffs_trader ON reconciliation_diffs(trader_id);
CREATE INDEX IF NOT EXISTS idx_recon_diffs_severity ON reconciliation_diffs(severity);

-- T+1 share-side settlement. Each fill (open or add) records one lot
-- with its own settle_date (open_date + 1 day). Sells consume lots FIFO
-- by settle_date so that "same position, partially sellable" is a
-- first-class notion rather than a derived guess from open_date. A
-- position's ``shares`` column is the sum of lot.remaining_shares; the
-- reconciliation check on oversold_position uses position_exits to
-- police that total, and this table is the authoritative source of
-- "which shares are sellable today". Legacy rows have no lots and fall
-- back to the original open_date < today semantic.
CREATE TABLE IF NOT EXISTS settlement_lots (
    id INTEGER PRIMARY KEY,
    position_id INTEGER NOT NULL,
    trader_id TEXT NOT NULL,
    code TEXT NOT NULL,
    shares INTEGER NOT NULL,           -- original lot size
    remaining_shares INTEGER NOT NULL, -- shares still held in this lot
    open_date TEXT NOT NULL,           -- when the lot was bought
    settle_date TEXT NOT NULL,         -- when shares become sellable
    open_price REAL NOT NULL,          -- fill price for this lot
    source TEXT,                       -- 'initial' | 'add' | 'migration'
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_settlement_lots_position
    ON settlement_lots(position_id);
CREATE INDEX IF NOT EXISTS idx_settlement_lots_trader
    ON settlement_lots(trader_id);
CREATE INDEX IF NOT EXISTS idx_settlement_lots_settle
    ON settlement_lots(settle_date);

-- T+1 cash-side settlement. When shares are sold the proceeds are not
-- available for new positions until exit_date + 1 day. Each leg of
-- position_exits gets one row here; release_due_settlements flips
-- ``released=1`` once the date passes so get_available_capital can
-- treat the cash as spendable. Before release the cash is counted in
-- get_total_capital but excluded from get_available_capital, which is
-- what the plan means by "available_cash 与 total_cash 正式分家".
CREATE TABLE IF NOT EXISTS pending_settlements (
    id INTEGER PRIMARY KEY,
    exit_id INTEGER NOT NULL,
    trader_id TEXT NOT NULL,
    code TEXT NOT NULL,
    net_amount REAL NOT NULL,           -- cash from the sale
    exit_date TEXT NOT NULL,            -- when the sale happened
    settle_date TEXT NOT NULL,          -- when cash becomes available
    released INTEGER NOT NULL DEFAULT 0, -- 1 once settle_date has passed
    released_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(exit_id)
);
CREATE INDEX IF NOT EXISTS idx_pending_settle_trader
    ON pending_settlements(trader_id);
CREATE INDEX IF NOT EXISTS idx_pending_settle_released
    ON pending_settlements(released);
CREATE INDEX IF NOT EXISTS idx_pending_settle_settle
    ON pending_settlements(settle_date);

-- Every business action that changes the book goes through one entry
-- point, and this table is the record that it did. Before S5 the four
-- write paths (create_pending_order / open_position / add_to_position /
-- close_position, plus the cancel path) were reachable independently,
-- so "what did the system decide to do, and was it accepted?" had no
-- single answer — it was scattered across the resulting rows. A row
-- here is written *before* the action runs (status='submitted'), then
-- flipped to 'accepted' or 'rejected' with the reason. A row left at
-- 'submitted' means the process died mid-action, which is itself worth
-- finding.
--
-- No FK to virtual_portfolio: an intent may be rejected before any
-- position row exists (a duplicate, a theorem-weakness refusal), and
-- the audit must record the attempt, not only the successes.
CREATE TABLE IF NOT EXISTS intents (
    id INTEGER PRIMARY KEY,
    action TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('submitted','accepted','rejected')),
    trader_id TEXT NOT NULL,
    code TEXT,
    position_id INTEGER,
    order_id INTEGER,
    information_cutoff TEXT,
    policy_ref TEXT,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    reject_reason TEXT,
    result_json TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_intents_status ON intents(status);
CREATE INDEX IF NOT EXISTS idx_intents_trader ON intents(trader_id);
CREATE INDEX IF NOT EXISTS idx_intents_action ON intents(action);
CREATE INDEX IF NOT EXISTS idx_intents_cutoff ON intents(information_cutoff);

CREATE TABLE IF NOT EXISTS custom_tasks (
    id INTEGER PRIMARY KEY,
    prompt TEXT NOT NULL,
    schedule_time TEXT,
    interval TEXT DEFAULT 'once',
    status TEXT DEFAULT 'active',
    last_run TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS price_alerts (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL,
    name TEXT,
    condition TEXT NOT NULL,
    target_price REAL NOT NULL,
    reason TEXT,
    status TEXT DEFAULT 'active',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    triggered_at TEXT
);

CREATE TABLE IF NOT EXISTS sector_betas (
    id INTEGER PRIMARY KEY,
    concept TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    beta_20d REAL,
    beta_60d REAL,
    beta_120d REAL,
    beta_weighted REAL,
    avg_daily_amount REAL,
    updated_at TEXT,
    UNIQUE(concept, code)
);

CREATE TABLE IF NOT EXISTS sentiment_phase (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL UNIQUE,
    phase TEXT NOT NULL,
    phase_en TEXT,
    confidence REAL,
    indicators TEXT,
    strategy TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS chat_memory (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    summary TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS vpa_analysis_history (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL,
    name TEXT,
    analysis_date TEXT NOT NULL,
    verdict TEXT,
    confidence REAL,
    phase TEXT,
    confirmed INTEGER DEFAULT 0,
    reason TEXT,
    report TEXT,
    signals_json TEXT,
    target_low REAL,                 -- v2.5: VPA推导目标价区间下限
    target_high REAL,                -- v2.5: VPA推导目标价区间上限
    created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_vpa_history_code_date ON vpa_analysis_history(code, analysis_date);

CREATE TABLE IF NOT EXISTS vpa_pending_signals (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL,
    name TEXT,
    signal_type TEXT NOT NULL,
    signal_date TEXT NOT NULL,
    direction TEXT NOT NULL,
    expected_confirmation TEXT,
    expected_denial TEXT,
    status TEXT DEFAULT 'pending',
    expire_date TEXT,
    source_analysis_id INTEGER,
    resolved_date TEXT,
    resolved_by TEXT,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (source_analysis_id) REFERENCES vpa_analysis_history(id)
);
CREATE INDEX IF NOT EXISTS idx_vpa_signals_status ON vpa_pending_signals(status);
CREATE INDEX IF NOT EXISTS idx_vpa_signals_code ON vpa_pending_signals(code);

-- v2.5 (problem 7): Scenario layer — a Wyckoff story told by multiple
-- signals together. Confirmation happens at scenario level (holistic price
-- action + signal consensus), not per individual bar.
CREATE TABLE IF NOT EXISTS vpa_scenarios (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL,
    name TEXT,                       -- 股票名
    scenario_name TEXT NOT NULL,     -- 如"终极派发"/"初期吸筹"
    phase TEXT,                      -- 对应 Wyckoff 阶段
    signal_names TEXT,               -- JSON array: 组成该 scenario 的底层信号名
    confirmation_criteria TEXT,      -- scenario整体确认条件
    denial_criteria TEXT,            -- scenario整体否定条件
    status TEXT DEFAULT 'pending',   -- pending/confirmed/denied/expired
    scenario_date TEXT NOT NULL,     -- 首次提出日期
    resolved_date TEXT,
    resolved_by TEXT,
    expire_date TEXT,
    source_analysis_id INTEGER,
    created_at TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (source_analysis_id) REFERENCES vpa_analysis_history(id)
);
CREATE INDEX IF NOT EXISTS idx_vpa_scenarios_status ON vpa_scenarios(status);
CREATE INDEX IF NOT EXISTS idx_vpa_scenarios_code ON vpa_scenarios(code);

CREATE TABLE IF NOT EXISTS financial_cache (
    code TEXT PRIMARY KEY,
    data TEXT NOT NULL,          -- JSON blob from get_financial_data_fn
    report_date TEXT,            -- 最新报告日 (e.g. "20241231")
    cached_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_financial_cached_at ON financial_cache(cached_at);

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
"""

_local = threading.local()
_write_lock = threading.Lock()


def _get_conn() -> sqlite3.Connection:
    """Get or create a thread-local connection to memory.db."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        MEMORY_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(MEMORY_DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        # Schema migrations for older DBs — CREATE IF NOT EXISTS doesn't add
        # columns to existing tables. Each ALTER is wrapped in try/except to
        # ignore "duplicate column" errors on already-migrated DBs.
        for migration in (
            "ALTER TABLE vpa_analysis_history ADD COLUMN target_low REAL",
            "ALTER TABLE vpa_analysis_history ADD COLUMN target_high REAL",
            # Phase 1 attribution: an order names the prediction it came from,
            # instead of the close path guessing it by stock and date.
            "ALTER TABLE virtual_portfolio ADD COLUMN prediction_id INTEGER",
            "ALTER TABLE virtual_portfolio ADD COLUMN legacy_realized_amount REAL",
            # Phase 1 attribution chain: the order names its thesis, and each
            # realised exit leg carries it too, so "which idea earned this"
            # survives both a re-opened position and a rewritten thesis.
            "ALTER TABLE virtual_portfolio ADD COLUMN thesis_id INTEGER",
            "ALTER TABLE position_exits ADD COLUMN thesis_id INTEGER",
        ):
            try:
                conn.execute(migration)
            except sqlite3.OperationalError:
                pass  # Column already exists
        # After the columns exist, not before — see the note in the schema.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_exits_thesis "
            "ON position_exits(thesis_id)"
        )
        conn.commit()
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

        # G1 migration: probabilistic forecasts and their scores.
        # A prediction states P(this beats the market over the horizon)
        # rather than a bare 看多/看空, so it can be graded with a proper
        # scoring rule — cumulative return needs years to reach
        # significance, Brier needs hundreds of observations. See
        # docs/self_improvement_roadmap.md G1.
        for migration in (
            "ALTER TABLE predictions ADD COLUMN prob REAL",
            "ALTER TABLE predictions ADD COLUMN brier REAL",
            "ALTER TABLE predictions ADD COLUMN log_score REAL",
            "ALTER TABLE predictions ADD COLUMN excess_return REAL",
            "ALTER TABLE predictions ADD COLUMN residual_alpha REAL",
            "ALTER TABLE predictions ADD COLUMN scored_at TEXT",
            "ALTER TABLE predictions ADD COLUMN created_at TEXT",
        ):
            try:
                conn.execute(migration)
            except sqlite3.OperationalError:
                pass  # Column already exists

        # Split theme strength into lifecycle vs today. The old single
        # `strength` was incremented on every intraday cycle — 48 times a
        # session — so a line with steady inflow saturated at 10 within
        # half an hour and a weak one floored at 0, and the number meant
        # "how many cycles in a row did the signal fire", not strength.
        # A pending order was cancelled on 金属铜 强度3 on a day that line
        # ran +2.0% vs the market on 55億 of inflow.
        # Sizing became a decision the agent makes, so the thesis carries
        # the share of the book it asked for. Declared in _SCHEMA too; this
        # is for databases created before that.
        for migration in (
            "ALTER TABLE theses ADD COLUMN entry_fraction REAL DEFAULT 0",
            "ALTER TABLE theses ADD COLUMN trader_id TEXT DEFAULT 'default'",
            "ALTER TABLE virtual_portfolio ADD COLUMN trader_id TEXT DEFAULT 'default'",
            "ALTER TABLE predictions ADD COLUMN trader_id TEXT DEFAULT 'default'",
            "ALTER TABLE theme_lines ADD COLUMN daily_score INTEGER DEFAULT 0",
            "ALTER TABLE theme_lines ADD COLUMN last_scored_date TEXT",
            # Indexes live here rather than in _SCHEMA: executescript runs
            # before the ALTERs above, so indexing a column an older
            # database has not gained yet fails the whole schema pass.
            "CREATE INDEX IF NOT EXISTS idx_portfolio_trader "
            "ON virtual_portfolio(trader_id)",
            "CREATE INDEX IF NOT EXISTS idx_theses_trader ON theses(trader_id)",
        ):
            try:
                conn.execute(migration)
            except sqlite3.OperationalError as e:
                # Only the re-run case is expected. A bare `pass` here
                # would also swallow a locked or corrupt database and
                # leave the gate reading a column that does not exist.
                if "duplicate column name" not in str(e).lower():
                    raise
        conn.commit()

        _local.conn = conn
    return conn


# ── Custom Tasks ─────────────────────────────────────────────

def create_custom_task(prompt: str, schedule_time: str, interval: str = "once") -> int:
    """Create a custom scheduled task.

    Args:
        prompt: Natural language instruction for the agent
        schedule_time: Time to run, e.g. "14:00"
        interval: "once" / "daily" / "weekday" (trading days)
    """
    with _write_lock:
        conn = _get_conn()
        cursor = conn.execute(
            "INSERT INTO custom_tasks (prompt, schedule_time, interval) VALUES (?, ?, ?)",
            (prompt, schedule_time, interval),
        )
        conn.commit()
        return cursor.lastrowid


def get_active_custom_tasks() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM custom_tasks WHERE status = 'active' ORDER BY schedule_time"
    ).fetchall()
    return [dict(r) for r in rows]


def update_custom_task_last_run(task_id: int) -> None:
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE custom_tasks SET last_run = datetime('now','localtime') WHERE id = ?",
            (task_id,),
        )
        # If one-time task, mark as done
        conn.execute(
            "UPDATE custom_tasks SET status = 'done' WHERE id = ? AND interval = 'once'",
            (task_id,),
        )
        conn.commit()


def delete_custom_task(task_id: int) -> None:
    with _write_lock:
        conn = _get_conn()
        conn.execute("DELETE FROM custom_tasks WHERE id = ?", (task_id,))
        conn.commit()


# ── Price Alerts ─────────────────────────────────────────────

def create_price_alert(code: str, name: str, condition: str, target_price: float, reason: str = "") -> int:
    """Create a price alert. condition: 'above' or 'below'."""
    with _write_lock:
        conn = _get_conn()
        cursor = conn.execute(
            "INSERT INTO price_alerts (code, name, condition, target_price, reason) "
            "VALUES (?, ?, ?, ?, ?)",
            (code, name, condition, target_price, reason),
        )
        conn.commit()
        return cursor.lastrowid


def get_active_price_alerts() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM price_alerts WHERE status = 'active' ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def trigger_price_alert(alert_id: int) -> None:
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE price_alerts SET status = 'triggered', triggered_at = datetime('now','localtime') WHERE id = ?",
            (alert_id,),
        )
        conn.commit()


def delete_price_alert(alert_id: int) -> None:
    with _write_lock:
        conn = _get_conn()
        conn.execute("DELETE FROM price_alerts WHERE id = ?", (alert_id,))
        conn.commit()


# ── Chat Memory ─────────────────────────────────────────────

def save_chat_memory(summary: str) -> None:
    """Save chat session summary for cross-session memory."""
    from datetime import datetime
    with _write_lock:
        conn = _get_conn()
        today = datetime.now().strftime("%Y-%m-%d")
        # Upsert: one summary per day (latest wins)
        conn.execute(
            "INSERT INTO chat_memory (date, summary) VALUES (?, ?) ",
            (today, summary),
        )
        conn.commit()


def get_recent_chat_memories(days: int = 7) -> list[str]:
    """Get recent chat session summaries for context."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT summary FROM chat_memory ORDER BY id DESC LIMIT ?",
        (days,),
    ).fetchall()
    return [r["summary"] for r in rows]


# ── Theme Lines ──────────────────────────────────────────────

def get_active_themes(min_status: str = "watching",
                      order_by: str = "strength") -> list[dict]:
    """Get all non-archived theme lines.

    ``order_by="strength"`` (the default) ranks by lifecycle position —
    how long the line has been confirmed. ``order_by="daily_score"`` ranks
    by today's raw signal, which is what "哪条主线今天最强" asks: a line
    running two weeks always out-accumulates one that broke out this
    morning, so strength cannot answer that question.
    """
    conn = _get_conn()
    order = ("daily_score DESC, strength DESC" if order_by == "daily_score"
             else "strength DESC, daily_score DESC")
    rows = conn.execute(
        f"SELECT * FROM theme_lines WHERE status != 'archived' ORDER BY {order}"
    ).fetchall()
    return [dict(r) for r in rows]


def get_theme_by_name(name: str) -> dict | None:
    """Get a single theme line by name."""
    conn = _get_conn()
    row = conn.execute("SELECT * FROM theme_lines WHERE name = ?", (name,)).fetchone()
    return dict(row) if row else None


def upsert_theme(
    name: str,
    *,
    status: str | None = None,
    strength: int | None = None,
    daily_score: int | None = None,
    last_scored_date: str | None = None,
    catalyst: str | None = None,
    core_stocks: list[dict] | None = None,
    leader_code: str | None = None,
    notes: str | None = None,
) -> int:
    """Create or update a theme line. Returns the theme id."""
    now = datetime.now().isoformat()
    with _write_lock:
        conn = _get_conn()
        existing = conn.execute(
            "SELECT id FROM theme_lines WHERE name = ?", (name,)
        ).fetchone()

        if existing:
            sets, vals = [], []
            if status is not None:
                sets.append("status = ?"); vals.append(status)
            if strength is not None:
                sets.append("strength = ?"); vals.append(strength)
            if daily_score is not None:
                sets.append("daily_score = ?"); vals.append(daily_score)
            if last_scored_date is not None:
                sets.append("last_scored_date = ?"); vals.append(last_scored_date)
            if catalyst is not None:
                sets.append("catalyst = ?"); vals.append(catalyst)
            if core_stocks is not None:
                sets.append("core_stocks = ?"); vals.append(json.dumps(core_stocks, ensure_ascii=False))
            if leader_code is not None:
                sets.append("leader_code = ?"); vals.append(leader_code)
            if notes is not None:
                sets.append("notes = ?"); vals.append(notes)
            sets.append("updated_at = ?"); vals.append(now)
            vals.append(existing["id"])
            conn.execute(f"UPDATE theme_lines SET {', '.join(sets)} WHERE id = ?", vals)
            conn.commit()
            return existing["id"]
        else:
            cur = conn.execute(
                "INSERT INTO theme_lines (name, status, strength, daily_score, last_scored_date, "
                "catalyst, core_stocks, leader_code, notes, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name, status or "watching", strength or 0, daily_score or 0,
                 last_scored_date, catalyst,
                 json.dumps(core_stocks or [], ensure_ascii=False), leader_code, notes, now, now),
            )
            conn.commit()
            return cur.lastrowid


def archive_theme(name: str) -> None:
    """Archive a theme line (soft delete)."""
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE theme_lines SET status = 'archived', updated_at = ? WHERE name = ?",
            (datetime.now().isoformat(), name),
        )
        conn.commit()


# ── Predictions ──────────────────────────────────────────────

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
    prob: float | None = None,
    trader_id: str = "default",
) -> int:
    """Record a stock recommendation.

    ``features`` (Phase 1): optional dict of decision-time features used by the
    Playbook clustering in Phase 3. Serialized to ``features_json`` column.
    Pass None for legacy callers (stored as empty '{}').

    ``trader_id``: whose call this was. The uniqueness key includes it, so
    two traders recommending the same stock on the same day are two
    predictions to be graded separately rather than one overwriting the
    other.

    ``prob`` (G1): P(this beats the market over the scoring horizon). This
    is what makes a prediction gradable with a proper scoring rule —
    a bare 看多/看空 can only be scored on accuracy, which says nothing
    about confidence and needs years of P&L to reach significance. Legacy
    callers pass None and are simply never scored.
    """
    features_json = json.dumps(features or {}, ensure_ascii=False)
    with _write_lock:
        conn = _get_conn()
        # Intraday monitoring re-saves its whole top-5 every cycle, so a
        # single recommendation used to land dozens of times a day. That
        # inflates playbooks.total_trades by an order of magnitude and
        # turns the "hits >= 3" auto-create threshold into "one stock went
        # up once". One row per (date, code, report_type); later cycles
        # refresh it in place.
        existing = conn.execute(
            "SELECT id FROM predictions "
            "WHERE date = ? AND code = ? AND report_type = ? "
            "AND trader_id = ?",
            (date, code, report_type, trader_id),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE predictions SET name = ?, direction = ?, confidence = ?, "
                "theme_line = ?, entry_price = COALESCE(entry_price, ?), "
                "reason = ?, features_json = ?, prob = COALESCE(?, prob) "
                "WHERE id = ?",
                (name, direction, confidence, theme_line, entry_price,
                 reason, features_json, prob, existing["id"]),
            )
            conn.commit()
            return existing["id"]

        # created_at is set on first insert only: it marks when the call
        # was made, and a later cycle refreshing the row must not move it.
        cur = conn.execute(
            "INSERT INTO predictions (date, report_type, code, name, direction, "
            "confidence, theme_line, entry_price, reason, features_json, prob, "
            "created_at, trader_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (date, report_type, code, name, direction, confidence, theme_line,
             entry_price, reason, features_json, prob,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"), trader_id),
        )
        conn.commit()
        return cur.lastrowid


def get_predictions_due_for_scoring(as_of: str, horizon_days: int = 5,
                                    limit: int = 200) -> list[dict]:
    """Probabilistic predictions whose horizon has elapsed but are unscored.

    Only rows carrying a ``prob`` can be graded — legacy rows without one
    are skipped rather than back-filled with a guess.
    """
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, date, code, name, prob FROM predictions "
        "WHERE prob IS NOT NULL AND scored_at IS NULL "
        "AND date <= date(?, ?) ORDER BY date LIMIT ?",
        (as_of, f"-{horizon_days} days", limit),
    ).fetchall()
    return [dict(r) for r in rows]


def save_prediction_score(pred_id: int, score: dict) -> None:
    """Persist a graded prediction.

    ``hit`` is kept in sync with the scored outcome so the legacy hit-rate
    reports agree with the Brier numbers instead of drifting apart.
    """
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE predictions SET brier = ?, log_score = ?, "
            "excess_return = ?, residual_alpha = ?, scored_at = ?, hit = ? "
            "WHERE id = ?",
            (score.get("brier"), score.get("log_score"),
             score.get("excess_return"), score.get("residual_alpha"),
             score.get("scored_at"), 1 if score.get("outcome") else 0,
             pred_id),
        )
        conn.commit()


def get_scored_predictions(days: int = 30, report_type: str | None = None) -> list[dict]:
    """Graded predictions from the last ``days``, newest first."""
    conn = _get_conn()
    q = ["SELECT * FROM predictions WHERE scored_at IS NOT NULL "
         "AND date >= date('now', ?)"]
    params: list = [f"-{days} days"]
    if report_type:
        q.append("AND report_type = ?")
        params.append(report_type)
    q.append("ORDER BY date DESC")
    return [dict(r) for r in conn.execute(" ".join(q), params).fetchall()]


def update_prediction_result(pred_id: int, *, next_day_return: float | None = None,
                              week_return: float | None = None, hit: int | None = None,
                              review_note: str | None = None) -> None:
    """Fill in backtesting results for a prediction."""
    with _write_lock:
        conn = _get_conn()
        sets, vals = [], []
        if next_day_return is not None:
            sets.append("next_day_return = ?"); vals.append(next_day_return)
        if week_return is not None:
            sets.append("week_return = ?"); vals.append(week_return)
        if hit is not None:
            sets.append("hit = ?"); vals.append(hit)
        if review_note is not None:
            sets.append("review_note = ?"); vals.append(review_note)
        if sets:
            vals.append(pred_id)
            conn.execute(f"UPDATE predictions SET {', '.join(sets)} WHERE id = ?", vals)
            conn.commit()


def get_pending_predictions(date: str) -> list[dict]:
    """Get predictions that haven't been reviewed yet for a given date."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM predictions WHERE date = ? AND hit IS NULL", (date,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_pending_prediction_dates(before_date: str, days: int = 14) -> list[str]:
    """Recent prediction dates before ``before_date`` still awaiting review."""
    from datetime import datetime as _dt, timedelta as _td
    try:
        cutoff = (_dt.strptime(before_date, "%Y-%m-%d") - _td(days=days)).strftime("%Y-%m-%d")
    except ValueError:
        cutoff = "0000-00-00"
    conn = _get_conn()
    rows = conn.execute(
        "SELECT DISTINCT date FROM predictions "
        "WHERE hit IS NULL AND date < ? AND date >= ? "
        "ORDER BY date",
        (before_date, cutoff),
    ).fetchall()
    return [r["date"] for r in rows]


def get_today_intraday_predictions() -> list[dict]:
    """Get today's intraday predictions for context continuity."""
    conn = _get_conn()
    today = datetime.now().strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT id, date, created_at, code, name, direction, confidence, "
        "theme_line, entry_price, reason, next_day_return, hit "
        "FROM predictions WHERE date = ? AND report_type LIKE 'intraday%' "
        "ORDER BY id DESC",
        (today,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_prediction_stats(days: int = 7) -> dict:
    """Get hit rate statistics for recent predictions."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT direction, confidence, hit FROM predictions "
        "WHERE hit IS NOT NULL ORDER BY date DESC LIMIT ?",
        (days * 20,),
    ).fetchall()
    if not rows:
        return {"total": 0, "hits": 0, "hit_rate": 0.0, "by_confidence": {}}
    total = len(rows)
    hits = sum(1 for r in rows if r["hit"] == 1)
    by_conf = {}
    for r in rows:
        c = r["confidence"] or "unknown"
        by_conf.setdefault(c, {"total": 0, "hits": 0})
        by_conf[c]["total"] += 1
        if r["hit"] == 1:
            by_conf[c]["hits"] += 1
    for v in by_conf.values():
        v["hit_rate"] = round(v["hits"] / v["total"] * 100, 1) if v["total"] else 0
    return {"total": total, "hits": hits, "hit_rate": round(hits / total * 100, 1), "by_confidence": by_conf}


# ── Market Cognition ─────────────────────────────────────────

def upsert_cognition(sector: str, date: str, *, position: str | None = None,
                      fund_trend: str | None = None, pe_percentile: float | None = None,
                      recent_events: list[str] | None = None, assessment: str | None = None) -> None:
    """Update the analyst's understanding of a sector."""
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO market_cognition (sector, date, position, fund_trend, pe_percentile, recent_events, assessment) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(sector, date) DO UPDATE SET "
            "position=COALESCE(excluded.position, position), "
            "fund_trend=COALESCE(excluded.fund_trend, fund_trend), "
            "pe_percentile=COALESCE(excluded.pe_percentile, pe_percentile), "
            "recent_events=COALESCE(excluded.recent_events, recent_events), "
            "assessment=COALESCE(excluded.assessment, assessment)",
            (sector, date, position, fund_trend, pe_percentile,
             json.dumps(recent_events or [], ensure_ascii=False) if recent_events else None, assessment),
        )
        conn.commit()


def get_cognition(sector: str) -> dict | None:
    """Get the latest cognition for a sector."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM market_cognition WHERE sector = ? ORDER BY date DESC LIMIT 1", (sector,)
    ).fetchone()
    return dict(row) if row else None


def get_all_cognition_latest() -> list[dict]:
    """Get latest cognition for all tracked sectors."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT m1.* FROM market_cognition m1 "
        "INNER JOIN (SELECT sector, MAX(date) as max_date FROM market_cognition GROUP BY sector) m2 "
        "ON m1.sector = m2.sector AND m1.date = m2.max_date "
        "ORDER BY m1.sector"
    ).fetchall()
    return [dict(r) for r in rows]


# ── VPA Analysis History ──────────────────────────────────────

def save_vpa_analysis(code: str, name: str, analysis_date: str,
                      verdict: str, confidence: float, phase: str,
                      confirmed: bool, reason: str, report: str,
                      signals_json: str = "",
                      target_low: float | None = None,
                      target_high: float | None = None) -> int:
    """Save a VPA analysis report to history. Returns row id.

    target_low/target_high (v2.5): Optional VPA-derived target price zone,
    used by portfolio.py to set take-profit on positions opened from this
    analysis. Based on Wyckoff cause-and-effect: longer accumulation →
    larger target.
    """
    with _write_lock:
        conn = _get_conn()
        cur = conn.execute(
            "INSERT INTO vpa_analysis_history "
            "(code, name, analysis_date, verdict, confidence, phase, "
            " confirmed, reason, report, signals_json, target_low, target_high) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (code, name, analysis_date, verdict, confidence, phase,
             1 if confirmed else 0, reason, report, signals_json,
             target_low, target_high),
        )
        conn.commit()
        return cur.lastrowid


def get_latest_vpa_analysis(code: str, as_of: str | None = None) -> dict | None:
    """Get the most recent VPA analysis for a stock.

    ``as_of`` (YYYY-MM-DD): only return analyses dated strictly BEFORE as_of
    — used by backtest replay to prevent future-data leakage.
    """
    conn = _get_conn()
    if as_of:
        row = conn.execute(
            "SELECT * FROM vpa_analysis_history WHERE code = ? AND analysis_date < ? "
            "ORDER BY analysis_date DESC, id DESC LIMIT 1",
            (code, as_of),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM vpa_analysis_history WHERE code = ? "
            "ORDER BY analysis_date DESC, id DESC LIMIT 1",
            (code,),
        ).fetchone()
    return dict(row) if row else None


def get_vpa_history(code: str, limit: int = 5) -> list[dict]:
    """Get recent VPA analyses for a stock."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM vpa_analysis_history WHERE code = ? ORDER BY analysis_date DESC LIMIT ?",
        (code, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ── VPA Pending Signals ──────────────────────────────────────

def save_vpa_signal(code: str, name: str, signal_type: str, signal_date: str,
                    direction: str, expected_confirmation: str = "",
                    expected_denial: str = "", expire_days: int = 3,
                    source_analysis_id: int | None = None) -> int:
    """Create a pending VPA signal to track. Returns row id. Deduplicates by (code, signal_type, signal_date)."""
    from datetime import datetime, timedelta
    # LLM may return "04-14" or "2026-04-14" — normalize
    if len(signal_date) <= 5:
        signal_date = f"{datetime.now().year}-{signal_date}"
    try:
        expire = (datetime.strptime(signal_date, "%Y-%m-%d") + timedelta(days=expire_days)).strftime("%Y-%m-%d")
    except ValueError:
        expire = (datetime.now() + timedelta(days=expire_days)).strftime("%Y-%m-%d")

    # Dedup: skip if same signal already pending for this stock
    conn = _get_conn()
    existing = conn.execute(
        "SELECT id FROM vpa_pending_signals WHERE code = ? AND signal_type = ? AND signal_date = ? AND status = 'pending'",
        (code, signal_type, signal_date),
    ).fetchone()
    if existing:
        return existing["id"]
    with _write_lock:
        conn = _get_conn()
        cur = conn.execute(
            "INSERT INTO vpa_pending_signals "
            "(code, name, signal_type, signal_date, direction, expected_confirmation, "
            " expected_denial, status, expire_date, source_analysis_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
            (code, name, signal_type, signal_date, direction,
             expected_confirmation, expected_denial, expire, source_analysis_id),
        )
        conn.commit()
        return cur.lastrowid


def get_pending_vpa_signals() -> list[dict]:
    """Get all pending (unresolved) VPA signals."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM vpa_pending_signals WHERE status = 'pending' ORDER BY signal_date",
    ).fetchall()
    return [dict(r) for r in rows]


def resolve_vpa_signal(signal_id: int, status: str, resolved_by: str = "") -> None:
    """Mark a VPA signal as confirmed/denied/expired."""
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE vpa_pending_signals SET status = ?, resolved_date = date('now'), resolved_by = ? WHERE id = ?",
            (status, resolved_by, signal_id),
        )
        conn.commit()


def expire_old_vpa_signals(today: str) -> int:
    """Expire signals past their expire_date. Returns count expired."""
    with _write_lock:
        conn = _get_conn()
        cur = conn.execute(
            "UPDATE vpa_pending_signals SET status = 'expired', resolved_date = ? "
            "WHERE status = 'pending' AND expire_date < ?",
            (today, today),
        )
        conn.commit()
        return cur.rowcount


# ── VPA Scenarios (problem 7: signal-group-level confirmation) ─────

def save_vpa_scenario(code: str, name: str, scenario_name: str,
                      phase: str, signal_names: list[str],
                      confirmation: str, denial: str,
                      scenario_date: str,
                      source_analysis_id: int | None = None,
                      expire_days: int = 10) -> int | None:
    """Record a Wyckoff scenario hypothesis for later confirmation.

    Scenarios are DEDUPLICATED on (code, scenario_name, status='pending') —
    re-running VPA on the same day for the same stock that re-proposes the
    same scenario is a no-op, not a duplicate row. Expiry defaults to 10
    days (scenarios tell longer stories than per-bar signals).

    Returns row id, or None if duplicate.
    """
    from datetime import datetime as _dt, timedelta as _td
    try:
        expire = (_dt.strptime(scenario_date, "%Y-%m-%d") + _td(days=expire_days)).strftime("%Y-%m-%d")
    except ValueError:
        expire = scenario_date

    with _write_lock:
        conn = _get_conn()
        # Dedup: same stock + same scenario_name still pending
        existing = conn.execute(
            "SELECT id FROM vpa_scenarios "
            "WHERE code = ? AND scenario_name = ? AND status = 'pending'",
            (code, scenario_name),
        ).fetchone()
        if existing:
            return None
        cur = conn.execute(
            "INSERT INTO vpa_scenarios "
            "(code, name, scenario_name, phase, signal_names, "
            " confirmation_criteria, denial_criteria, scenario_date, "
            " source_analysis_id, expire_date, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
            (code, name, scenario_name, phase,
             json.dumps(signal_names, ensure_ascii=False),
             confirmation, denial, scenario_date,
             source_analysis_id, expire),
        )
        conn.commit()
        return cur.lastrowid


def get_pending_vpa_scenarios(code: str = "") -> list[dict]:
    """Get pending scenarios. If code is given, filter to that code."""
    conn = _get_conn()
    if code:
        rows = conn.execute(
            "SELECT * FROM vpa_scenarios WHERE code = ? AND status = 'pending' "
            "ORDER BY scenario_date DESC",
            (code,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM vpa_scenarios WHERE status = 'pending' "
            "ORDER BY scenario_date DESC",
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["signal_names"] = json.loads(d.get("signal_names") or "[]")
        except (json.JSONDecodeError, TypeError):
            d["signal_names"] = []
        out.append(d)
    return out


def resolve_vpa_scenario(scenario_id: int, status: str, resolved_by: str = "") -> None:
    """Mark a scenario as confirmed/denied/expired."""
    with _write_lock:
        conn = _get_conn()
        conn.execute(
            "UPDATE vpa_scenarios SET status = ?, resolved_date = date('now'), "
            "resolved_by = ? WHERE id = ?",
            (status, resolved_by, scenario_id),
        )
        conn.commit()


def expire_old_vpa_scenarios(today: str) -> int:
    """Expire scenarios past their expire_date. Returns count expired."""
    with _write_lock:
        conn = _get_conn()
        cur = conn.execute(
            "UPDATE vpa_scenarios SET status = 'expired', resolved_date = ? "
            "WHERE status = 'pending' AND expire_date < ?",
            (today, today),
        )
        conn.commit()
        return cur.rowcount


# ── Financial data cache ────────────────────────────────────
# Quarterly financials rarely change — cache for 30 days to avoid
# hammering akshare on every cross-validation run.

def get_cached_financials(code: str, max_age_days: int = 30) -> dict | None:
    """Return cached financial data if fresh, else None.

    Args:
        code: 6-digit stock code
        max_age_days: TTL in days. Default 30 covers a quarterly cycle;
            new quarterly reports should prompt manual cache invalidation
            or natural expiry.
    """
    conn = _get_conn()
    row = conn.execute(
        "SELECT data, cached_at FROM financial_cache WHERE code = ?", (code,)
    ).fetchone()
    if not row:
        return None
    try:
        cached_at = datetime.fromisoformat(row["cached_at"])
    except (ValueError, TypeError):
        return None
    age_days = (datetime.now() - cached_at).days
    if age_days > max_age_days:
        return None
    try:
        return json.loads(row["data"])
    except (json.JSONDecodeError, TypeError):
        return None


def save_cached_financials(code: str, data: dict) -> None:
    """Save financial data to cache. Overwrites existing entry."""
    with _write_lock:
        conn = _get_conn()
        report_date = data.get("report_date", "") if isinstance(data, dict) else ""
        conn.execute(
            "INSERT OR REPLACE INTO financial_cache (code, data, report_date, cached_at) "
            "VALUES (?, ?, ?, datetime('now','localtime'))",
            (code, json.dumps(data, ensure_ascii=False), report_date),
        )
        conn.commit()


# ── Theme strength snapshots (for velocity detection) ───────
# Snapshot daily at end of morning_scan so we can detect rapid theme decay
# (e.g., strength dropping from 9 → 5 in 2 days) which is a sell signal
# even if the current strength hasn't yet crossed the exit threshold.

def save_theme_snapshot(date: str, themes: list[dict]) -> None:
    """Snapshot current theme strengths for the given date.

    Args:
        date: "YYYY-MM-DD"
        themes: [{"name": "...", "strength": 8, "status": "..."}, ...]
    """
    with _write_lock:
        conn = _get_conn()
        payload = json.dumps(
            [{"name": t.get("name"), "strength": t.get("strength"), "status": t.get("status")}
             for t in themes],
            ensure_ascii=False,
        )
        conn.execute(
            "INSERT OR REPLACE INTO daily_snapshots (date, data_type, data) "
            "VALUES (?, 'theme_strengths', ?)",
            (date, payload),
        )
        conn.commit()


def get_theme_strength_history(theme_name: str, days: int = 5) -> list[dict]:
    """Get strength history for a theme over the last N days.

    Returns list sorted newest-first: [{"date": "...", "strength": N}, ...].
    Missing days are skipped (not padded).
    """
    conn = _get_conn()
    rows = conn.execute(
        "SELECT date, data FROM daily_snapshots "
        "WHERE data_type = 'theme_strengths' "
        "ORDER BY date DESC LIMIT ?",
        (days,),
    ).fetchall()
    history = []
    for r in rows:
        try:
            themes = json.loads(r["data"])
        except (json.JSONDecodeError, TypeError):
            continue
        for t in themes:
            if t.get("name") == theme_name and t.get("strength") is not None:
                history.append({"date": r["date"], "strength": t["strength"]})
                break
    return history


def get_recent_sentiment_phases(n: int = 2) -> list[dict]:
    """Get the N most recent sentiment phases, newest first.

    Returns: [{"date": "...", "phase": "..."}, ...]
    """
    conn = _get_conn()
    rows = conn.execute(
        "SELECT date, phase FROM sentiment_phase ORDER BY date DESC LIMIT ?",
        (n,),
    ).fetchall()
    return [dict(r) for r in rows]


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


# ── Phase 3: playbooks ────────────────────────────────────────
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


# ── Phase 4: evolution metrics ────────────────────────────────
def upsert_evolution_metrics(date: str, fields: dict) -> None:
    """Insert or replace a daily metrics row. `fields` maps column names to values."""
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

# ── VPA prior state (v7) ─────────────────────────────────────
# Read by the vpa package to carry the previous session's phase
# into the next analysis. Live mode requires the exact previous
# trading day; backtest mode takes the newest row strictly before
# the as-of date.

def get_prior_state_live(code: str) -> dict | None:
    """v7 spec §2.5: return the most recent VPA analysis row whose date
    equals the previous trading day. Used by live mode.

    Returns None if the most recent row is older than yesterday's trading day
    (treat as cold start).

    v7 review I#82: selected_candidate_id is now read from the persisted
    column (default None for legacy rows) rather than hardcoded to None.
    """
    from alpha_agents.tools.vpa import _prev_trading_day
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    expected = _prev_trading_day(today)
    if not expected:
        return None
    conn = _get_conn()
    row = conn.execute(
        """
        SELECT analysis_date, phase, verdict, signals_json, reason,
               selected_candidate_id
        FROM vpa_analysis_history
        WHERE code = ? AND analysis_date = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (code, expected),
    ).fetchone()
    if not row:
        return None
    return {
        "analysis_date": row["analysis_date"],
        "phase": row["phase"],
        "verdict": row["verdict"],
        "selected_candidate_id": row["selected_candidate_id"],
        "rationale": row["reason"] or "",
    }


def get_prior_state_backtest(code: str, as_of: str) -> dict | None:
    """v7 spec §2.5: return the largest-dated VPA analysis row strictly
    before ``as_of`` for ``code``. Used by backtest harness.

    Returns dict with keys {analysis_date, phase, verdict, selected_candidate_id,
    rationale} or None if no prior row exists.

    v7 review I#82: selected_candidate_id is now read from the persisted
    column (default None for legacy rows) rather than hardcoded to None.
    """
    conn = _get_conn()
    row = conn.execute(
        """
        SELECT analysis_date, phase, verdict, signals_json, reason,
               selected_candidate_id
        FROM vpa_analysis_history
        WHERE code = ? AND analysis_date < ?
        ORDER BY analysis_date DESC
        LIMIT 1
        """,
        (code, as_of[:10]),
    ).fetchone()
    if not row:
        return None
    return {
        "analysis_date": row["analysis_date"],
        "phase": row["phase"],
        "verdict": row["verdict"],
        "selected_candidate_id": row["selected_candidate_id"],
        "rationale": row["reason"] or "",
    }
