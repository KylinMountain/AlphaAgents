"""The memory database's tables, and how they got that way.

824 lines of DDL, moved out of ``memory_store.py`` because a schema is a
*declaration* and that file was also carrying 62 functions. They are edited for
different reasons — a table is added when a fact needs a home, a function when a
query changes — so keeping them in one file meant scrolling past half a thousand
lines of ``CREATE TABLE`` to change a ``SELECT``.

``memory_store`` re-exports ``_SCHEMA``, so the tests that do
``from alpha_agents.data.memory_store import _SCHEMA`` and build a database to
poke at keep working, and the name cannot drift: it is the same string object.
"""

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
    -- Today's *cross-sectionally normalised* strength, 0–1, refreshed every
    -- cycle. A third number because the first two answer the wrong question for
    -- a gate: `strength` is a count of confirmed sessions and `daily_score` is a
    -- raw ±1 sum, so a 0.01億 inflow and a 55億 inflow score the same. This one
    -- is where the theme sits in the whole board today (flow percentile,
    -- relative-strength percentile, confirmation), and it is what admission and
    -- cancellation read. Null means "not scored yet" — never "scored zero".
    trend_score REAL,
    -- Consecutive daily closes at which the board did not name this theme.
    --
    -- Measured 2026-09-14: the THS endpoint behind the board serves a *partial*
    -- list — 287, 328, 333 and 380 rows on four consecutive calls, with
    -- individual boards (`共封装光学(CPO)`) present in some and absent in
    -- others. Absence from one frame is therefore mostly truncation, not
    -- evidence, and a theme must not be retired for it. This counter is why the
    -- retirement path waits: one miss is the network, several in a row are the
    -- theme. Reset whenever the board does name the theme.
    unmeasured_days INTEGER DEFAULT 0,
    last_scored_date TEXT,
    created_at TEXT,
    updated_at TEXT,
    catalyst TEXT,
    core_stocks TEXT,
    leader_code TEXT,
    notes TEXT
);

-- One theme's score on one past day, so a replay can ask what the gate would
-- have said **then**.
--
-- `theme_lines.trend_score` is a single present-day column: it is rewritten
-- every cycle and holds only today's answer. Rebuilding history into it would
-- therefore mean every replayed day reading the *last* rebuilt day's score —
-- the future written into the past, which is the one thing a replay exists to
-- prevent. Hence a dated table instead of overwriting a value.
--
-- Written only by `scripts/rebuild_theme_scores.py`, which derives it from
-- `market_snapshots.sector_flow_snapshots` (the only per-day board history
-- this repository holds, and it starts 2026-09-08). Read only through
-- `theme_gate`, and only when a replay as-of is set — production keeps reading
-- the live column, because in production the live column *is* today's truth.
--
-- `confirm` is recorded per row because the reconstruction cannot derive it
-- (production's confirmation counts confirmed sessions, which is not in the
-- snapshot), and a score whose missing term is invisible is a score nobody can
-- audit.
CREATE TABLE IF NOT EXISTS theme_score_history (
    id INTEGER PRIMARY KEY,
    theme TEXT NOT NULL,
    as_of TEXT NOT NULL,          -- the day whose close this score describes
    score REAL NOT NULL,
    flow_pct REAL,
    rel_pct REAL,
    confirm REAL,
    board TEXT,                   -- the board row the theme was matched to
    board_scope TEXT,             -- 'concept' | 'industry'
    board_rank INTEGER,
    board_of INTEGER,
    source TEXT,                  -- which table it was derived from
    UNIQUE(theme, as_of)
);
CREATE INDEX IF NOT EXISTS idx_theme_score_history
    ON theme_score_history(theme, as_of);

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
    trader_id TEXT DEFAULT 'default',
    -- The horizon this forecast *declared*, and the day it therefore
    -- matures on. Both NULL for rows written before the declaration
    -- existed: the evaluator falls back to the global default for those
    -- and says so in the outcome's evidence, rather than back-filling a
    -- claim the author never made. A forecast's maturity is part of what
    -- it asserted, so it cannot be a global constant.
    horizon_days INTEGER,
    deadline TEXT
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
    stop_loss REAL,                       -- 止损价（会被移动止损抬高）
    -- The stop this position was *opened* with, written once at the fill and
    -- never raised. ``stop_loss`` above ratchets with the trailing rule, so it
    -- stops recording where the position started — and a rule that measures its
    -- distance from the entry stop must not read it back, or it measures its own
    -- output (D29: the trailing stop diverged to 15,352,643.13 on a 16.31 entry).
    initial_stop_loss REAL,               -- 开仓止损，写一次，不随移动止损变
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

-- Cash-side settlement, and the 可用/可取 split. Selling on T makes the
-- proceeds spendable **immediately** — the A-share rule — and only
-- withdrawable on T+1. Each leg of position_exits gets one row here;
-- release_due_settlements flips ``released=1`` once the settle date
-- passes. The row therefore answers "how much of this trader's cash
-- cannot leave the account yet", NOT "how much it cannot spend": this
-- table used to be subtracted from get_available_capital, which applied
-- the withdrawal rule to buying power and forbade sell-then-buy on the
-- same day (tech-debt D19, fixed 2026-09-16). The trade read model
-- reports it as ``cash_in_transit``.
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

-- Episodes: the learning unit (§9). One row per *decision*, not per position.
-- A decision that was refused, cancelled or never filled is still an episode,
-- which is the whole point — it makes selection and coverage countable instead
-- of only the decisions that happened to make money. Before this the only
-- trace of a decision was thesis → order → exits, which by construction can
-- only express the ones that traded.
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY,
    trader_id TEXT NOT NULL,
    -- NULL, not '', when the decision named a book row that does not exist or
    -- a malformed intent arrived before any instrument was mentioned. The
    -- decision still happened and still belongs in the record — dropping it
    -- would remove exactly the malformed ones from the coverage count, which
    -- is the bias this table exists to remove. '' would read as a stock code
    -- that is merely empty; NULL says "no instrument to name".
    code TEXT,
    -- open     — a position exists and has not been fully exited
    -- closed   — the position was fully exited, or the episode ran to a
    --            terminal non-trade (cancelled / refused)
    status TEXT NOT NULL CHECK(status IN ('open', 'closed')),
    -- The frozen boundary this decision was made on. NULL for rows that
    -- pre-date decision_snapshots or were written outside the door; "unknown
    -- basis" is not the same as "no basis", so it stays NULL rather than 0.
    decision_snapshot_id INTEGER,
    intent_id INTEGER,
    thesis_id INTEGER,
    prediction_id INTEGER,
    order_id INTEGER,
    position_id INTEGER,
    information_cutoff TEXT,
    opened_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_episodes_trader ON episodes(trader_id);
CREATE INDEX IF NOT EXISTS idx_episodes_code ON episodes(code);
CREATE INDEX IF NOT EXISTS idx_episodes_status ON episodes(status);
CREATE INDEX IF NOT EXISTS idx_episodes_order ON episodes(order_id);
CREATE INDEX IF NOT EXISTS idx_episodes_position ON episodes(position_id);

-- An episode may be linked as its refs appear (the position exists only once
-- the pending order fills) and closed once. It may not be renamed and it may
-- not be reopened: "which decision was this" has to have one answer forever,
-- or a unit of learning can be reassigned to a different decision after the
-- result is known, which is the shape of every hindsight bug.
CREATE TRIGGER IF NOT EXISTS episodes_identity_is_fixed
BEFORE UPDATE ON episodes
WHEN NEW.trader_id <> OLD.trader_id
  OR NEW.code IS NOT OLD.code
  OR NEW.opened_at <> OLD.opened_at
  OR (OLD.status = 'closed' AND NEW.status <> 'closed')
  OR (OLD.closed_at IS NOT NULL AND NEW.closed_at IS NOT OLD.closed_at)
  OR (OLD.decision_snapshot_id IS NOT NULL
      AND NEW.decision_snapshot_id IS NOT OLD.decision_snapshot_id)
BEGIN
    SELECT RAISE(ABORT, 'an episode''s identity and its end are fixed');
END;

-- Everything that happened because of one decision, in order. `ref_id` points
-- at the row that carries the detail (an intent, an order, a position, an exit
-- leg); the amount is never copied here, so this cannot become a third book.
CREATE TABLE IF NOT EXISTS episode_events (
    id INTEGER PRIMARY KEY,
    episode_id INTEGER NOT NULL,
    -- Only kinds this repository actually writes. §9 also names holds and
    -- abstentions; nothing decides "do not buy today" anywhere, so there is no
    -- write path to record and inventing one would be a promise without a
    -- caller. ``expire`` is absent for the same reason: the pending-order
    -- expiry is declared (PENDING_EXPIRE_DAYS) and computed (days_pending) and
    -- then never read — every order that dies unfinished dies as a cancel.
    -- Declaring the kind would have made the schema claim a capability the
    -- code does not have. See TRADER_CORE_IMPLEMENTATION.md.
    kind TEXT NOT NULL CHECK(kind IN (
        'intent', 'order', 'fill', 'add', 'trim', 'close', 'cancel')),
    ref_id INTEGER,
    at TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_episode_events_episode
    ON episode_events(episode_id);
CREATE INDEX IF NOT EXISTS idx_episode_events_kind ON episode_events(kind);

-- Append-only, like decision_snapshots: an event is a fact about what
-- happened, and a fact that can be edited is not evidence.
CREATE TRIGGER IF NOT EXISTS episode_events_no_update
BEFORE UPDATE ON episode_events
BEGIN
    SELECT RAISE(ABORT, 'episode_events is append-only');
END;

CREATE TRIGGER IF NOT EXISTS episode_events_no_delete
BEFORE DELETE ON episode_events
BEGIN
    SELECT RAISE(ABORT, 'episode_events is append-only');
END;

-- Outcome labels with a lifecycle (§9), append-only, one linear revision chain
-- per subject. Deliberately stores the *label and its state*, never the money:
-- amounts stay in position_exits / predictions and are referenced from
-- evidence_json, so this cannot drift from the ledger.
CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK(kind IN ('forecast', 'trade', 'process')),
    state TEXT NOT NULL CHECK(state IN (
        'pending', 'matured', 'censored', 'revised')),
    subject_type TEXT NOT NULL,
    subject_id INTEGER NOT NULL,
    episode_id INTEGER,
    -- Which version of the evaluator produced this label. Without it a label
    -- is unfalsifiable after the fact: you cannot tell a changed rule from a
    -- changed market.
    evaluator_version TEXT,
    -- Refs, never values. A copied amount is a second source of truth.
    evidence_json TEXT NOT NULL DEFAULT '{}',
    -- When the label became knowable, as distinct from when it was computed.
    -- Under replay the former is the one that matters.
    available_at TEXT,
    supersedes_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
-- Exactly one *initial* label per subject: a subject either has no label yet or
-- has one that revisions hang off. Partial indexes because SQLite treats NULLs
-- in a UNIQUE index as distinct, so a plain constraint would not bite.
CREATE UNIQUE INDEX IF NOT EXISTS idx_outcomes_initial
    ON outcomes(kind, subject_type, subject_id) WHERE supersedes_id IS NULL;
-- One revision per row, so the chain is linear and "the current label" is a
-- single answer rather than a fork.
CREATE UNIQUE INDEX IF NOT EXISTS idx_outcomes_supersedes
    ON outcomes(supersedes_id) WHERE supersedes_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_outcomes_subject
    ON outcomes(subject_type, subject_id);
CREATE INDEX IF NOT EXISTS idx_outcomes_state ON outcomes(state);
CREATE INDEX IF NOT EXISTS idx_outcomes_episode ON outcomes(episode_id);

-- Immutability by database, not by convention (§9: "Label corrections append
-- revisions; later knowledge must not alter what an earlier learning run could
-- access"). A revised label is a new row naming the one it supersedes.
CREATE TRIGGER IF NOT EXISTS outcomes_no_update
BEFORE UPDATE ON outcomes
BEGIN
    SELECT RAISE(ABORT, 'outcomes is append-only; append a revision instead');
END;

CREATE TRIGGER IF NOT EXISTS outcomes_no_delete
BEFORE DELETE ON outcomes
BEGIN
    SELECT RAISE(ABORT, 'outcomes is append-only');
END;

-- An approval, recorded. §10: "validation does not itself activate
-- knowledge", and a candidate "must stay outside production decision
-- retrieval until it is included in an approved policy snapshot". These
-- two tables are the machine form of that sentence — and only that. A
-- snapshot is the record of a human decision, not a switch: nothing reads
-- them to change what the system does (see §7).
--
-- content_hash covers the declared fields *and* the item set, so a
-- consumer can recompute it (knowledge_snapshots.verify_snapshot) and
-- detect a snapshot rewritten after the fact. The triggers below should
-- already prevent that; this is the second lock on the same door, for the
-- same reason decision_snapshots has one.
CREATE TABLE IF NOT EXISTS knowledge_snapshots (
    id INTEGER PRIMARY KEY,
    -- Who approved it. A snapshot with no named approver is an anonymous
    -- assertion, which is the thing this table exists to prevent.
    approved_by TEXT NOT NULL,
    -- The instant the approval is dated, as declared by the approver.
    -- Part of the hash.
    approved_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    notes TEXT,
    content_hash TEXT NOT NULL,
    -- When this row was written, from the kernel clock. Deliberately not
    -- part of the hash: it is a write-time fact like created_at, not a
    -- declaration. Under replay it differs from approved_at, which is why
    -- both are kept.
    frozen_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Which knowledge versions the snapshot contains. version_hash is the hash
-- of the *knowledge row* as projected through a declared per-entity field
-- tuple, computed by the writer and never supplied by the caller: "which
-- version did we approve" has to be answerable from the record alone.
CREATE TABLE IF NOT EXISTS knowledge_snapshot_items (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL,
    entity_type TEXT NOT NULL CHECK(entity_type IN ('principle', 'playbook')),
    entity_id INTEGER NOT NULL,
    -- The candidate this approval came from, when there was one. Not
    -- required: a person may approve something the learner never proposed.
    candidate_id INTEGER,
    version_hash TEXT NOT NULL,
    -- One entry per knowledge row per snapshot. Two rows for the same
    -- entity inside one snapshot would make "which version is approved"
    -- a question with two answers.
    UNIQUE(snapshot_id, entity_type, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_ksnap_items_snapshot
    ON knowledge_snapshot_items(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_ksnap_items_candidate
    ON knowledge_snapshot_items(candidate_id);
CREATE INDEX IF NOT EXISTS idx_ksnap_items_entity
    ON knowledge_snapshot_items(entity_type, entity_id);

CREATE TRIGGER IF NOT EXISTS knowledge_snapshots_no_update
BEFORE UPDATE ON knowledge_snapshots
BEGIN
    SELECT RAISE(ABORT, 'knowledge_snapshots is append-only');
END;

CREATE TRIGGER IF NOT EXISTS knowledge_snapshots_no_delete
BEFORE DELETE ON knowledge_snapshots
BEGIN
    SELECT RAISE(ABORT, 'knowledge_snapshots is append-only');
END;

CREATE TRIGGER IF NOT EXISTS knowledge_snapshot_items_no_update
BEFORE UPDATE ON knowledge_snapshot_items
BEGIN
    SELECT RAISE(ABORT, 'knowledge_snapshot_items is append-only');
END;

CREATE TRIGGER IF NOT EXISTS knowledge_snapshot_items_no_delete
BEFORE DELETE ON knowledge_snapshot_items
BEGIN
    SELECT RAISE(ABORT, 'knowledge_snapshot_items is append-only');
END;

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

-- One review per closed position, written by the trader that held it.
-- ``facts_json`` is computed from daily_kline, sealed at close_date; the
-- trader's words are in ``lesson_json``. The numbers in the prompt's summary
-- line come from facts_json only, never from the words.
CREATE TABLE IF NOT EXISTS trade_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER NOT NULL UNIQUE,
    trader_id TEXT NOT NULL,
    code TEXT NOT NULL,
    close_date TEXT NOT NULL,
    facts_json TEXT NOT NULL,
    lesson_json TEXT,
    facts_available_on TEXT,
    review_status TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_attempt_on TEXT,
    review_available_on TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_trade_reviews_trader
    ON trade_reviews(trader_id, close_date);

CREATE TABLE IF NOT EXISTS trade_review_attempts (
    id INTEGER PRIMARY KEY,
    position_id INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    attempted_on TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('started','failed','complete')),
    lesson_json TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    UNIQUE(position_id, attempt, status)
);
CREATE TRIGGER IF NOT EXISTS trade_review_attempts_no_update
BEFORE UPDATE ON trade_review_attempts BEGIN
    SELECT RAISE(ABORT, 'trade_review_attempts is append-only');
END;
CREATE TRIGGER IF NOT EXISTS trade_review_attempts_no_delete
BEFORE DELETE ON trade_review_attempts BEGIN
    SELECT RAISE(ABORT, 'trade_review_attempts is append-only');
END;
CREATE TABLE IF NOT EXISTS trader_session_events (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    trader_id TEXT NOT NULL,
    session_day TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('morning_input','entry_observation')),
    code TEXT NOT NULL DEFAULT '',
    observed_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trader_session_events
    ON trader_session_events(run_id,trader_id,session_day,kind,code,observed_at);
CREATE TRIGGER IF NOT EXISTS trader_session_events_no_update
BEFORE UPDATE ON trader_session_events BEGIN
    SELECT RAISE(ABORT, 'trader_session_events is append-only');
END;
CREATE TRIGGER IF NOT EXISTS trader_session_events_no_delete
BEFORE DELETE ON trader_session_events BEGIN
    SELECT RAISE(ABORT, 'trader_session_events is append-only');
END;

-- M2 is a new event stream; do not rebuild the M1 CHECK constraint.
CREATE TABLE IF NOT EXISTS decision_capture_events (
    id TEXT PRIMARY KEY,
    invocation_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    trader_id TEXT NOT NULL,
    session_day TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('decision_input','decision_output')),
    code TEXT NOT NULL DEFAULT '',
    observed_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    UNIQUE(invocation_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_decision_capture_events
    ON decision_capture_events(run_id,trader_id,session_day,kind,observed_at);
CREATE TRIGGER IF NOT EXISTS decision_capture_events_no_update
BEFORE UPDATE ON decision_capture_events BEGIN
    SELECT RAISE(ABORT, 'decision_capture_events is append-only');
END;
CREATE TRIGGER IF NOT EXISTS decision_capture_events_no_delete
BEFORE DELETE ON decision_capture_events BEGIN
    SELECT RAISE(ABORT, 'decision_capture_events is append-only');
END;

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
