"""M1 — replay a Trader's day, walk-forward, against a read-only corpus.

What this is
------------
A **time-isolated** Trader lifecycle runner, not a T+1 backtest of a signal.
Each simulated trading day is three instants, because the plan requires the
decision and the fill to see different things:

    09:00  decide    — sees T-1 and earlier. The order is dated T.
    09:30  settle    — the open. Fills are decided by ``data/t1_settlement``.
    close  value     — sees T. Marks the book; never feeds a decision.

``replay_mode``'s three as-of semantics are exactly these three: a bare date is
"after the close", an ``HH:MM`` before 15:00 rolls EOD-only readers back a day.
So the decision cannot see today's close by construction rather than by
discipline, and ``clock.today()`` returns T inside every block — which is what
lets the kernel date its fills, its T+1 lots and its pending proceeds in the
replayed window.

What it reuses
--------------
Everything that matters. Orders go through ``create_pending_order`` (the intent
door, which writes the audit row, the reservation and the episode). Fills go
through ``check_pending_orders`` and ``_fill_order`` — the same functions the
live intraday loop calls, with the open handed in as the day's price. Exits go
through ``close_position``. This runner owns the **calendar**, the **decision**,
and the **report**; it owns no trading logic, and that is deliberate: §7 of the
plan says the T+1 mode is a Trader/PolicyVariant on the existing chain, not a
second chain beside it.

What it refuses
---------------
* It refuses to run against the production data directory. ``config.DATA_DIR``
  is computed at import time and every database path derives from it, so the
  replay directory is chosen *before* the package is imported and a run pointed
  at ``data/`` exits with a message instead of writing there.
* It refuses to run without declaring whether the model may be called, and it
  **checks** that answer: the placeholder decider must produce zero journal
  records. A silent model call would make the run irreproducible and the report
  a lie, so it is a failure rather than a surcharge.
* It does not resolve an intraday path. `intraday_ambiguous` is counted and
  reported, never guessed at.

The honest ceiling
------------------
This is **mechanistic walk-forward, not a point-in-time model backtest** (plan
§6): today's model weights have already seen these dates. M1 calls no model at
all, so what it can show is that the clock, the isolation, the T+1 settlement
and the book work end to end over a real window. The decider below is a
**placeholder whose only job is to produce orders** — it is not a claim about
alpha, and the report says so in as many words.

Usage
-----
    .venv/bin/python scripts/walk_bootstrap.py --target /tmp/walk
    ALPHAAGENTS_DATA_DIR=/tmp/walk .venv/bin/python scripts/walk_forward.py \\
        --start 2025-07-01 --days 30 --trader pullback
    # or, equivalently, with the directory on the command line:
    .venv/bin/python scripts/walk_forward.py --target /tmp/walk --start …
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_PRODUCTION_DIR = _PROJECT_ROOT / "data"

#: The flag whose answer has to exist before ``alpha_agents`` is importable.
_EARLY = argparse.ArgumentParser(add_help=False)
_EARLY.add_argument("--target", type=Path)


def _choose_data_dir(argv: list[str]) -> Path | None:
    """The replay directory, resolved before anything imports ``config``.

    ``--target`` wins over the environment variable: the person typing a path
    is making a fresher statement than the one their shell is carrying. Both
    are read here rather than in ``main`` because ``config.DATA_DIR`` is fixed
    at import time and every database path in the system derives from it.
    """
    known, _ = _EARLY.parse_known_args(argv)
    raw = known.target or os.environ.get("ALPHAAGENTS_DATA_DIR") or ""
    return Path(raw).expanduser().resolve() if str(raw).strip() else None


_REPLAY_DIR = _choose_data_dir(sys.argv[1:])


def _refuse_production(replay: Path | None) -> Path:
    """Refuse a directory that this run would be wrong to write.

    Two ways to be wrong, and both are refused rather than warned about: no
    directory at all (the run would silently use production), and the
    production directory itself. This runner places real orders and books real
    fills; the one thing it must never do is book them into the live book.
    """
    if replay is None:
        raise SystemExit(
            "No replay data directory. This runner writes a ledger, and the "
            "live one is not where a walk-forward belongs.\n"
            "  .venv/bin/python scripts/walk_bootstrap.py --target /tmp/walk\n"
            "  ALPHAAGENTS_DATA_DIR=/tmp/walk .venv/bin/python "
            "scripts/walk_forward.py --start 2025-07-01 --days 30")
    if replay == _PRODUCTION_DIR.resolve():
        raise SystemExit(
            f"The replay directory resolves to the production data directory "
            f"({replay}). Refusing: a walk-forward must not write the live "
            "book. Point --target (or ALPHAAGENTS_DATA_DIR) at a directory "
            "made by scripts/walk_bootstrap.py.")
    if not (replay / "memory.db").exists():
        raise SystemExit(
            f"{replay}/memory.db does not exist. Create the replay directory "
            "first:\n  .venv/bin/python scripts/walk_bootstrap.py "
            f"--target {replay}")
    return replay


def _bind_to(replay: Path | None) -> Path:
    """Point the whole process at ``replay``, or refuse to run at all.

    Order matters and is the whole point: ``ALPHAAGENTS_DATA_DIR`` has to be in
    the environment *before* ``alpha_agents.config`` is first imported, because
    ``DATA_DIR`` is computed once at import time and every database path in the
    system derives from it. A binding that happens after that import is not a
    binding — it is a comment.
    """
    replay = _refuse_production(replay)
    os.environ["ALPHAAGENTS_DATA_DIR"] = str(replay)
    return replay


#: Whether this file is the process's entry point. An *import* — a test, a REPL,
#: a wrapper — must not move the process-wide data directory and must not refuse
#: to load, so the binding below happens only when this file is what is being
#: run, and ``run()`` re-establishes it rather than trusting it.
_RUN_AS_SCRIPT = Path(sys.argv[0]).resolve() == Path(__file__).resolve()

_REPLAY_DIR = _bind_to(_choose_data_dir(sys.argv[1:])) if _RUN_AS_SCRIPT else None

from alpha_agents import llm_journal  # noqa: E402
from alpha_agents.config import DATA_DIR, TRADABLE_PREFIXES  # noqa: E402
from alpha_agents.data import (  # noqa: E402
    corpus_access, market_history as mh, market_rules, portfolio as P,
    portfolio_exit, reservations, t1_settlement as S,
)
from alpha_agents.data.t1_execution import capacity_shares  # noqa: E402
from alpha_agents.data.memory_store import upsert_theme  # noqa: E402
from alpha_agents.data.portfolio_intent import create_pending_order  # noqa: E402
from alpha_agents.evolution.replay_mode import get_replay_as_of, replay_as_of  # noqa: E402

def _assert_actually_bound(replay: Path) -> None:
    """The claim ``_bind_to`` is allowed to make, checked rather than assumed.

    ``DATA_DIR`` is fixed the moment ``alpha_agents.config`` is first imported,
    so an ``alpha_agents`` already living in ``sys.modules`` — a test process, a
    REPL that imported it before setting the variable — keeps production
    underneath while every path in this file says "replay". That combination
    books a replay's orders into the live book, which is the one outcome this
    whole file exists to prevent, so it is refused rather than survived.
    ``_refuse_production`` cannot catch it: it knows what was *asked for*, never
    what actually got imported.
    """
    if Path(DATA_DIR).resolve() != replay:
        raise SystemExit(
            f"ALPHAAGENTS_DATA_DIR was set to {replay} but the imported config "
            f"resolved to {DATA_DIR} — alpha_agents was already imported before "
            "this runner started. Refusing: the run would write the live book.\n"
            "  Run it as its own process:\n"
            f"  ALPHAAGENTS_DATA_DIR={replay} .venv/bin/python "
            "scripts/walk_forward.py --start 2025-07-01 --days 30")


if _REPLAY_DIR is not None:
    _assert_actually_bound(_REPLAY_DIR)

logger = logging.getLogger("walk_forward")

#: The corpus files the replay shares by reference. Hashed by size+mtime before
#: and after: a write to a shared corpus file is already impossible at the
#: SQLite layer (``corpus_access`` opens it ``mode=ro``), and this is the second
#: claim — that nothing tried.
CORPUS_FILES = ("market_history.db", "market_snapshots.db", "stocks.db")

#: The placeholder decider's name, carried into every report so a reader cannot
#: mistake the window's result for a statement about a strategy.
DECIDER = "momentum_placeholder"

#: A change this large or more is treated as the previous session's own limit-up,
#: and the name is skipped. Without it the placeholder would spend most of its
#: picks on stocks already locked, and the run would measure the one-way-limit
#: rule rather than the book.
LIMIT_UP_SKIP_PCT = 9.5

#: The entry zones the two trader YAMLs describe in prose. M1 reads the prompt
#: is not its job — the mapping is declared here and named in the report.
ENTRY_ZONES = {
    "pullback": (0.970, 1.005),   # wait for the price to come to you
    "breakout": (1.005, 1.030),   # pay up for confirmation
}

#: The report's own list of things a reader must not read this run as.
LIMITATIONS = (
    "mechanistic walk-forward, not a point-in-time model backtest: today's "
    "model weights have already seen these dates",
    "the decider is a placeholder that exists to generate orders; its return "
    "is not evidence about any strategy",
    "the theme is a synthetic line with no score, so the theme gate passes "
    "every order by design — M1 does not test the gate",
    "ST status comes from stocks.db, which holds the current name rather than "
    "the name as of the replayed date; codes absent from the previous "
    "session's bars are excluded, but a later ST marking is not knowable",
    "capacity is measured (ADV20 at the decision) and reported, but not yet "
    "enforced: ``_fill_order`` sizes from capital and takes no capacity input",
    "no news, no theses, no predictions, and no exits other than stop/target",
)


# ── the corpus, read through the kernel's own readers ───────────────────────


def _corpus_fingerprint(data_dir: Path) -> dict:
    """Size and mtime of every shared corpus file, for the before/after check."""
    out = {}
    for name in CORPUS_FILES:
        path = data_dir / name
        if not path.exists():
            out[name] = None
            continue
        st = path.stat()
        out[name] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns,
                     "symlink": path.is_symlink()}
    return out


def _corpus_root() -> str:
    """Where the shared history actually lives, resolved through the links.

    ``walk_bootstrap`` plants symlinks, so the replay directory itself says
    which corpus it was built against — there is no separate knob to get wrong,
    and a report can name the history it read.
    """
    path = _REPLAY_DIR / CORPUS_FILES[0]
    if path.is_symlink():
        return str(path.resolve().parent)
    return str(_REPLAY_DIR)


def _sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_instruments(data_dir: Path) -> dict[str, dict]:
    """``code → {name, is_st, is_suspended}`` from the shared stock list.

    Read-only, and through ``corpus_access`` so the read is refused at the
    SQLite layer if this directory is ever wrongly treated as writable.
    """
    path = data_dir / "stocks.db"
    if not path.exists():
        return {}
    conn = corpus_access.connect(path)
    try:
        rows = conn.execute(
            "SELECT code, name, is_st, is_suspended FROM stocks").fetchall()
    finally:
        conn.close()
    return {r[0]: {"name": r[1] or "", "is_st": bool(r[2]),
                   "is_suspended": bool(r[3])} for r in rows}


class Corpus:
    """The read-only history, and the two derived maps a day needs.

    Calendar and per-code context are cached because the runner asks for them
    once per day rather than once per order, and a cache that is never
    invalidated is safe here for the one reason it usually is not: the corpus
    is read-only for the whole run.
    """

    def __init__(self, data_dir: Path):
        self.days: list[str] = sorted(mh.get_available_dates())
        self.index = {d: i for i, d in enumerate(self.days)}
        self.instruments = _load_instruments(data_dir)
        self._bars: dict[str, dict[str, dict]] = {}
        self._adv: dict[tuple[str, str], float | None] = {}

    def window(self, start: str, days: int) -> list[str]:
        rest = [d for d in self.days if d >= start]
        if not rest:
            raise SystemExit(f"no sessions at or after {start} in the corpus")
        if len(rest) < days:
            logger.warning("only %d sessions from %s; running that many",
                           len(rest), start)
        return rest[:days]

    def previous(self, day: str) -> str | None:
        i = self.index.get(day)
        if i is None or i == 0:
            return None
        return self.days[i - 1]

    def bars(self, day: str) -> dict[str, dict]:
        """Every instrument's row for ``day``, by code."""
        if day not in self._bars:
            self._bars[day] = {r["code"]: r for r in mh.get_klines_for_date(day)}
        return self._bars[day]

    def adv20(self, code: str, before: str) -> float | None:
        """ADV20 over the twenty sessions **ending at** ``before``.

        ``before`` is always the previous session, so this is "as of T-1" — the
        plan's rule. ``None`` when fewer than twenty sessions exist: a stand-in
        average is the one thing the caller asked not to be handed.
        """
        key = (code, before)
        if key not in self._adv:
            hist = mh.get_local_history(code, days=20, as_of=before)
            vols = [float(b.get("volume") or 0) for b in hist] if hist else None
            self._adv[key] = (sum(vols) / len(vols)) if vols else None
        return self._adv[key]


# ── the placeholder decider ─────────────────────────────────────────────────


def _decide(ctx, day: str, prev_day: str) -> list[dict]:
    """Pick what to order today. **A placeholder, not a strategy.**

    Ranked by the previous session's change, skipping names already at that
    session's limit and names whose liquidity cannot be measured. Then the
    entry zone comes from the trader's declared style. That is the whole rule,
    and it exists only so the kernel has orders to settle — the report says so.
    """
    bars_prev = ctx.corpus.bars(prev_day)
    ranked = []
    for code, row in bars_prev.items():
        if not code.startswith(TRADABLE_PREFIXES):
            continue
        meta = ctx.corpus.instruments.get(code)
        if meta is None or meta["is_st"] or meta["is_suspended"]:
            continue
        chg = row.get("change_pct")
        close = row.get("close")
        if chg is None or not close or float(close) <= 0:
            continue
        if float(chg) >= LIMIT_UP_SKIP_PCT:
            continue
        ranked.append((float(chg), code))
    ranked.sort(reverse=True)

    low_pct, high_pct = ctx.entry_zone
    placed, seen = [], set()
    for _chg, code in ranked:
        if len(placed) >= ctx.picks:
            break
        if code in seen:
            continue
        seen.add(code)
        adv = ctx.corpus.adv20(code, prev_day)
        cap = capacity_shares(adv, participation=ctx.participation)
        if adv is None or cap <= 0:
            # No measurable liquidity is not the same as illiquid, and the plan
            # says a security whose numbers cannot be established is not
            # selected. Skipping rather than ordering-and-hoping.
            continue
        close = float(bars_prev[code]["close"])
        order_id = create_pending_order(
            code=code,
            name=ctx.corpus.instruments[code]["name"],
            theme=ctx.theme,
            order_date=day,
            entry_low=round(close * low_pct, 2),
            entry_high=round(close * high_pct, 2),
            stop_loss=round(close * (1 - ctx.stop_pct / 100), 2),
            source="walk_forward",
            reason=f"{DECIDER}: T-1 涨幅 {_chg:.2f}%",
            trader_id=ctx.trader)
        if order_id is None:
            # The intent door refused it and wrote down why; the refusal is
            # evidence, not an error, so it is counted rather than raised.
            ctx.counters["intent_refused"] += 1
            continue
        ctx.capacity[code] = cap
        placed.append({"code": code, "order_id": order_id, "prev_change_pct": _chg})
    return placed


# ── one simulated day ───────────────────────────────────────────────────────


def _classify(ctx, day: str, pending: list[dict]):
    """Ask the settlement rules about every pending order, and build the map.

    The map is the whole interface to the kernel: a code is present with a
    price when the open may settle it, and **absent** when it may not. Absent
    means ``check_pending_orders`` skips the order entirely — no fill and no
    expiry — which is right for a suspended name (there was no session to
    expire against) and for a one-way limit (the open is the limit, so the
    in-zone test would otherwise fill it).

    ``no_fill`` and ``intraday_ambiguous`` are deliberately **included**: their
    open is by construction outside the zone, so the kernel's own predicate
    will not trigger, and expiry must keep running. An unclaimed fill must not
    also freeze the order's clock — the order's horizon is the agent's, not
    ours.
    """
    bars_today = ctx.corpus.bars(day)
    prev = ctx.corpus.previous(day)
    bars_prev = ctx.corpus.bars(prev) if prev else {}
    price_map: dict[str, float] = {}
    counts = Counter()
    events = []
    for order in pending:
        code = order["code"]
        row = bars_today.get(code)
        prow = bars_prev.get(code)
        prev_close = prow.get("close") if prow else None
        try:
            rule = market_rules.market_rules(
                code, day, name=(ctx.corpus.instruments.get(code) or {}).get("name"))
        except ValueError:
            rule = None
        verdict = S.entry_verdict(
            entry_low=order.get("entry_low"), entry_high=order.get("entry_high"),
            bar=S.bar_from_row(row), prev_close=prev_close, rule=rule)
        counts[verdict.status] += 1
        if verdict.status in (S.FILLED_AT_OPEN, S.NO_FILL, S.INTRADAY_AMBIGUOUS):
            price_map[code] = row["open"]
        if verdict.status != S.NO_FILL:
            events.append({"date": day, "order_id": order["id"], "code": code,
                           "status": verdict.status,
                           "entry_low": order.get("entry_low"),
                           "entry_high": order.get("entry_high"),
                           "open": row.get("open") if row else None,
                           "reason": verdict.reason})
    return price_map, counts, events


def _cancel_class(reason: str) -> str:
    """The cancel's *kind*, with the instance's numbers dropped.

    An alert's reason is written for a human reading one line about one order:
    ``价格涨走(7.51)`` names the price. Used as a grouping key it becomes a
    class of one, so on the 2025-07-01 window six run-aways ranked *below* three
    expiries and never reached the summary at all — the outcome this repository
    spent a week making visible, hidden again by a string format. The ASCII
    parenthetical is the instance; what precedes it is the reason.

    The **full-width** parentheses in ``到期未到价（5天）`` are left alone on
    purpose: five days and seven days are different reasons, not one reason
    with a different number. Only the detail is aggregated away.

    **This key is not the ledger's spelling.** The book records its own, longer
    wording — ``价格已涨走(7.51远超介入上限6.01)`` where the alert says
    ``价格涨走``, ``挂单到期未到价（挂5天，期限5天）`` where it says
    ``到期未到价（5天）``. Measured on the 2025-07-01 window, **three of the four
    reasons this line prints** matched no row in the book by that label (only
    ``资金不足`` did, as the head of a longer string). The detail does survive,
    but the label to search it by does not; that gap is D30.
    """
    return reason.split("(", 1)[0].strip()


def _settle_entries(ctx, day: str, pending: list[dict]) -> dict:
    price_map, counts, events = _classify(ctx, day, pending)
    alerts = P.check_pending_orders(price_map, today=day, trader_id=ctx.trader)
    fills, cancels = [], []
    for alert in alerts:
        if alert.get("type") == "filled":
            cap = ctx.capacity.get(alert["code"])
            fills.append({
                "date": day, "side": "buy", "code": alert["code"],
                "name": alert.get("name", ""), "shares": alert.get("shares"),
                "price": alert.get("fill_price"), "amount": alert.get("cost"),
                "capacity_shares": cap,
                "capacity_oversize": bool(
                    cap is not None and (alert.get("shares") or 0) > cap),
                "reason": "open in entry zone"})
        else:
            cancels.append(_cancel_class(alert.get("reason", "")))
    return {"counts": counts, "events": events, "fills": fills,
            "cancels": Counter(cancels)}


def _settle_exits(ctx, day: str) -> dict:
    """Stop and target, at the open, and **only** for positions bought earlier.

    T+1 is asked through ``t1_settlement.may_sell`` rather than open-coded here,
    so the session a position was bought in cannot sell it. The plan's rule is
    stronger than the kernel's: a position opened today is not even *valued*
    against today's high and low for an exit, because a bar cannot say whether
    the entry or the stop came first.
    """
    prev = ctx.corpus.previous(day)
    bars_today = ctx.corpus.bars(day)
    bars_prev = ctx.corpus.bars(prev) if prev else {}
    counts = Counter()
    events, fills = [], []
    for pos in P.get_open_positions(ctx.trader):
        if not S.may_sell(pos.get("open_date"), day):
            counts["held_by_t_plus_one"] += 1
            continue
        code = pos["code"]
        prow = bars_prev.get(code)
        prev_close = prow.get("close") if prow else pos.get("open_price")
        name = (ctx.corpus.instruments.get(code) or {}).get("name") or pos.get("name", "")
        try:
            rule = market_rules.market_rules(code, day, name=name)
        except ValueError:
            rule = None
        verdict = S.exit_verdict(
            bar=S.bar_from_row(bars_today.get(code)), prev_close=prev_close,
            rule=rule, stop_loss=pos.get("stop_loss"),
            target_price=pos.get("target_price"))
        counts[verdict.status] += 1
        if verdict.status == S.INTRADAY_AMBIGUOUS:
            events.append({"date": day, "position_id": pos["id"], "code": code,
                           "status": verdict.status, "reason": verdict.reason})
            continue
        if verdict.status != S.FILLED_AT_OPEN:
            continue
        booked = portfolio_exit.close_position(
            pos["id"], close_price=verdict.price,
            close_reason=f"walk_forward 开盘{verdict.reason[:60]}",
            command_id=f"walk:{ctx.run_id}:{day}:{pos['id']}")
        if not booked:
            counts["close_refused"] += 1
            events.append({"date": day, "position_id": pos["id"], "code": code,
                           "status": "close_refused", "reason": verdict.reason})
            continue
        fills.append({
            "date": day, "side": "sell", "code": code, "name": name,
            "shares": pos.get("shares"), "price": verdict.price,
            "amount": (pos.get("shares") or 0) * verdict.price,
            "capacity_shares": None, "capacity_oversize": False,
            "reason": verdict.reason})
    return {"counts": counts, "events": events, "fills": fills}


def _value(ctx, day: str) -> dict:
    """Mark the book at ``day``'s close, carrying a suspended name at its last price."""
    positions = P.get_open_positions(ctx.trader)
    bars_today = ctx.corpus.bars(day)
    market_value = unrealized = 0.0
    stale = []
    for pos in positions:
        shares = pos.get("shares") or 0
        row = bars_today.get(pos["code"])
        if row and row.get("close"):
            price = float(row["close"])
        else:
            hist = mh.get_local_history(pos["code"], days=1, as_of=day)
            price = float(hist[-1]["close"]) if hist else (pos.get("open_price") or 0)
            stale.append(pos["code"])
        market_value += price * shares
        unrealized += (price - (pos.get("open_price") or 0)) * shares
    total = P.get_total_capital(ctx.trader)
    return {
        "date": day,
        "cash": P.get_available_capital(ctx.trader) + reservations.unconsumed_total(
            P._get_conn(), ctx.trader),
        "invested": P.get_invested_capital(ctx.trader),
        "market_value": round(market_value, 2),
        "realized": round(total - P.trader_capital(ctx.trader), 2),
        "unrealized": round(unrealized, 2),
        "equity": round(total + unrealized, 2),
        "open_positions": len(positions),
        "pending_orders": len(P.get_pending_orders(ctx.trader)),
        "valued_at_a_stale_price": ",".join(sorted(stale)),
    }


# ── the run ─────────────────────────────────────────────────────────────────


class Context:
    def __init__(self, args):
        self.args = args
        self.corpus = Corpus(_REPLAY_DIR)
        self.trader = args.trader
        self.picks = args.picks
        self.theme = args.theme
        self.participation = args.participation
        self.stop_pct = args.stop_pct
        self.entry_zone = ENTRY_ZONES.get(args.trader, ENTRY_ZONES["pullback"])
        self.run_id = args.run_id
        self.capacity: dict[str, int] = {}
        self.counters: Counter = Counter()


def _seed_theme(ctx) -> None:
    """One synthetic line, so an order has a theme to hang on.

    ``trend_score`` is left unset, which is the gate's documented
    "unmeasurable, therefore pass" branch. Naming it here is the honest form:
    the alternative is an order with no theme, which ``check_pending_orders``
    cancels on sight.

    It carries **no constituents** on purpose. A concept's members are held in
    ``stocks.db`` as they are today, and seeding today's membership into a 2020
    day would be exactly the as-of violation the plan's acceptance list names.
    """
    upsert_theme(ctx.theme, status="watching", strength=3, daily_score=1,
                 catalyst="synthetic line seeded by walk_forward",
                 notes="M1 placeholder — not a real theme, carries no score")


def run(args) -> dict:
    # A run is only ever its own process. Importing this module does not bind
    # the data directory (see ``_RUN_AS_SCRIPT``), so reaching here without a
    # bound replay directory means the caller is in the wrong process — and the
    # wrong process is production.
    if _REPLAY_DIR is None:
        raise SystemExit(
            "This runner has no replay directory because it was imported "
            "rather than run. A walk-forward must be its own process:\n"
            "  ALPHAAGENTS_DATA_DIR=/tmp/walk .venv/bin/python "
            "scripts/walk_forward.py --start 2025-07-01 --days 30")
    _assert_actually_bound(_REPLAY_DIR)

    ctx = Context(args)
    _seed_theme(ctx)

    window = ctx.corpus.window(args.start, args.days)
    journal_before = _journal_records(_REPLAY_DIR)
    prod_hash_before = _sha256(_PRODUCTION_DIR / "memory.db")
    corpus_before = _corpus_fingerprint(_REPLAY_DIR)

    equity_rows, fill_rows, settle_rows, event_rows = [], [], [], []
    by_status = Counter()
    cancels = Counter()
    errors = []

    for day in window:
        prev_day = ctx.corpus.previous(day)
        if prev_day is None:
            continue
        try:
            with replay_as_of(f"{day} 09:00"):
                placed = _decide(ctx, day, prev_day)
            logger.info("%s: ordered %d", day, len(placed))
            with replay_as_of(f"{day} 09:30"):
                entries = _settle_entries(ctx, day, P.get_pending_orders(ctx.trader))
                logger.info("%s: settled entries %s", day,
                            dict(entries["counts"]))
                exits = _settle_exits(ctx, day)
                logger.info("%s: settled exits %s", day, dict(exits["counts"]))
            with replay_as_of(day):
                equity = _value(ctx, day)
            logger.info("%s: equity %s (open %d, pending %d)", day,
                        equity["equity"], equity["open_positions"],
                        equity["pending_orders"])
        except Exception as exc:                      # noqa: BLE001
            logger.exception("%s failed", day)
            errors.append({"date": day, "error": f"{type(exc).__name__}: {exc}"})
            if not args.keep_going:
                raise
            continue

        by_status.update(entries["counts"])
        cancels.update(entries["cancels"])
        for bucket, kind in ((entries, "entry"), (exits, "exit")):
            for event in bucket["events"]:
                event["kind"] = kind
                event_rows.append(event)
            fill_rows.extend(bucket["fills"])
        settle_rows.append({
            "date": day,
            "ordered": len(placed),
            **{k: entries["counts"].get(k, 0) for k in S.ALL_STATUSES},
            "exits_valued": sum(exits["counts"].values()),
            "exits_held_by_t_plus_one": exits["counts"].get("held_by_t_plus_one", 0),
            "exits_ambiguous": exits["counts"].get(S.INTRADAY_AMBIGUOUS, 0),
        })
        equity_rows.append(dict(equity, ordered=len(placed)))

    journal_after = _journal_records(_REPLAY_DIR)
    prod_hash_after = _sha256(_PRODUCTION_DIR / "memory.db")
    corpus_after = _corpus_fingerprint(_REPLAY_DIR)

    return {
        "ctx": ctx, "window": window, "equity": equity_rows, "fills": fill_rows,
        "settlement": settle_rows, "events": event_rows,
        "by_status": by_status, "cancels": cancels, "errors": errors,
        "model_calls": {
            "mode": os.environ.get("ALPHAAGENTS_LLM_MODE", "live"),
            "journal_before": journal_before, "journal_after": journal_after},
        "production": {"hash_before": prod_hash_before,
                       "hash_after": prod_hash_after,
                       "unchanged": prod_hash_before == prod_hash_after},
        "corpus": {"before": corpus_before, "after": corpus_after,
                   "untouched": corpus_before == corpus_after},
    }


def _journal_records(data_dir: Path) -> int:
    """How many model exchanges this replay directory has recorded."""
    directory = data_dir / llm_journal.JOURNAL_DIRNAME
    if not directory.is_dir():
        return 0
    total = 0
    for path in directory.glob("*.jsonl"):
        with path.open("r", encoding="utf-8") as fh:
            total += sum(1 for line in fh if line.strip())
    return total


# ── the report ──────────────────────────────────────────────────────────────


def _write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_report(result: dict, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    ctx = result["ctx"]
    window = result["window"]
    model = result["model_calls"]
    # The placeholder decider must not have called a model. A silent call would
    # make the window irreproducible and turn the report into a claim it cannot
    # support, so it is a failure of the run rather than a footnote.
    no_model = model["journal_after"] == model["journal_before"] == 0

    meta = {
        "run_id": ctx.run_id,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "decider": DECIDER,
        "trader": ctx.trader,
        "theme": ctx.theme,
        "picks_per_day": ctx.picks,
        "participation": ctx.participation,
        "stop_pct": ctx.stop_pct,
        "entry_zone": list(ctx.entry_zone),
        "window": {"start": window[0], "end": window[-1],
                   "trading_days": len(window)},
        "replay_dir": str(_REPLAY_DIR),
        "corpus_dir": _corpus_root(),
        "llm_mode": model["mode"],
        "model_calls_made": model["journal_after"],
        "placeholder_decider_called_no_model": no_model,
        "production_db_unchanged": result["production"]["unchanged"],
        "corpus_untouched": result["corpus"]["untouched"],
        "errors": result["errors"],
        "limitations": list(LIMITATIONS),
    }
    (out_dir / "run.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    _write_csv(out_dir / "equity.csv", result["equity"], [
        "date", "cash", "invested", "market_value", "realized", "unrealized",
        "equity", "open_positions", "pending_orders", "ordered",
        "valued_at_a_stale_price"])
    _write_csv(out_dir / "fills.csv", result["fills"], [
        "date", "side", "code", "name", "shares", "price", "amount",
        "capacity_shares", "capacity_oversize", "reason"])
    _write_csv(out_dir / "settlement.csv", result["settlement"], [
        "date", "ordered", *S.ALL_STATUSES, "exits_valued",
        "exits_held_by_t_plus_one", "exits_ambiguous"])
    _write_csv(out_dir / "events.csv", result["events"], [
        "date", "kind", "status", "code", "order_id", "position_id",
        "entry_low", "entry_high", "open", "reason"])

    summary = _summary(result, out_dir, no_model)
    (out_dir / "summary.txt").write_text(summary, encoding="utf-8")
    return {"meta": meta, "summary": summary, "out_dir": out_dir}


def _summary(result: dict, out_dir: Path, no_model: bool) -> str:
    ctx = result["ctx"]
    window = result["window"]
    equity = result["equity"]
    fills = result["fills"]
    by_status = result["by_status"]
    first, last = equity[0], equity[-1]
    buys = [f for f in fills if f["side"] == "buy"]
    sells = [f for f in fills if f["side"] == "sell"]
    oversize = [f for f in fills if f["capacity_oversize"]]
    start_capital = P.trader_capital(ctx.trader)

    lines = [
        "M1 — 最小可跑的 T+1 走前回放",
        "=" * 62,
        f"窗口          {window[0]} → {window[-1]}（{len(window)} 个交易日）",
        f"交易员        {ctx.trader}（入口区间 {ctx.entry_zone[0]:.3f}–"
        f"{ctx.entry_zone[1]:.3f} × T-1 收盘）",
        f"决策器        {DECIDER}（**占位用途，不代表任何策略**）",
        f"回放目录      {_REPLAY_DIR}",
        f"本金          {start_capital:,.0f}",
        "",
        "— 净值 —",
        f"期末 equity   {last['equity']:,.2f}",
        f"区间收益      {(last['equity'] / start_capital - 1) * 100:+.3f}%",
        f"区间最大回撤  {_max_drawdown([r['equity'] for r in equity]) * 100:.3f}%",
        f"已实现 / 浮盈 {first['realized']:,.0f} → {last['realized']:,.0f} real / "
        f"{last['unrealized']:,.0f} unreal",
        f"期末持仓/挂单 {last['open_positions']} / {last['pending_orders']}",
        "",
        "— 成交 —",
        f"买入 {len(buys)} 笔 {sum(b['amount'] or 0 for b in buys):,.0f} 元 / "
        f"卖出 {len(sells)} 笔 {sum(s['amount'] or 0 for s in sells):,.0f} 元",
        f"成交量超 ADV20 参与上限的笔数：{len(oversize)}（**已计数、尚未强制**）",
        "",
        "— 结算判定（每个挂单-日一次）—",
    ]
    for status in S.ALL_STATUSES:
        lines.append(f"  {status:<20} {by_status.get(status, 0):>6}")
    unexercised = [s for s in S.ALL_STATUSES if not by_status.get(s)]
    lines += [
        f"  {'挂单撤销':<20} {sum(result['cancels'].values()):>6}"
        f"  （{_top_reasons(result['cancels'])}）",
        f"  本窗口未出现的判定    {('、'.join(unexercised)) or '无'}",
        "  （0 表示本窗口没遇到，不表示规则没生效——规则由 "
        "tests/test_t1_settlement.py 与 test_walk_forward.py 钉住）",
        "",
        "— 口径检查 —",
        f"生产库内容 hash 未变        {'是' if result['production']['unchanged'] else '**否**'}",
        f"共享语料 size+mtime 未变    {'是' if result['corpus']['untouched'] else '**否**'}",
        f"占位决策器未调用模型        {'是' if no_model else '**否**'}",
        f"结算过程抛错的交易日        {len(result['errors'])}",
        "",
        "— 本次运行不能说明什么 —",
    ]
    lines += [f"  · {item}" for item in LIMITATIONS]
    lines += ["", f"报告目录 {out_dir}"]
    return "\n".join(lines)


def _max_drawdown(values: list[float]) -> float:
    peak, worst = float("-inf"), 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            worst = min(worst, value / peak - 1)
    return abs(worst)


def _top_reasons(counter: Counter, limit: int = 3) -> str:
    """The busiest cancel reasons, and **what was left out of them**.

    Truncating a list for a summary is fine. A truncated list that reads as a
    complete one is not: the line prints a total, the parts add up to less than
    it, and a reader has no way to tell a missing reason from a miscount. On the
    2025-07-01 window that was 47 cancels against parts summing to 41 — and the
    six that were missing were missing for the class-of-one reason ``_cancel_class``
    fixes, so the two defects were the same six events seen from two directions.
    Naming the remainder is what makes the line self-checking.
    """
    if not counter:
        return "无"
    ranked = counter.most_common()
    text = "；".join(f"{reason[:34]}×{n}" for reason, n in ranked[:limit])
    rest = ranked[limit:]
    if rest:
        text += (f"；**另有 {sum(n for _, n in rest)} 条未列出**"
                 f"（{len(rest)} 个原因）")
    return text


# ── cli ─────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", type=Path,
                        help="the replay's data directory (or set "
                             "ALPHAAGENTS_DATA_DIR; --target wins).")
    parser.add_argument("--start", required=True,
                        help="first simulated session, YYYY-MM-DD.")
    parser.add_argument("--days", type=int, default=30,
                        help="how many sessions to run (default 30).")
    parser.add_argument("--trader", default="pullback",
                        help="which trader's declared entry style to use.")
    parser.add_argument("--picks", type=int, default=2,
                        help="orders the placeholder decider places per day.")
    parser.add_argument("--theme", default="WALK-PLACEHOLDER",
                        help="the synthetic theme every order hangs on.")
    parser.add_argument("--stop-pct", type=float, default=8.0,
                        help="stop distance below the T-1 close, in percent.")
    parser.add_argument("--participation", type=float, default=0.10,
                        help="the declared share of ADV20 one order may take.")
    parser.add_argument("--run-id", default=None,
                        help="names the run and the exit command ids.")
    parser.add_argument("--out", type=Path, default=None,
                        help="report directory (default <target>/walk-reports/<run-id>).")
    parser.add_argument("--keep-going", action="store_true",
                        help="record a failed day and continue instead of stopping.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    run_id = args.run_id or f"{args.start}-{args.days}d-{args.trader}"
    args.run_id = run_id
    args.out = args.out or (_REPLAY_DIR / "walk-reports" / run_id)
    # INFO by default: a replay is long, and "which day is it on" is the first
    # question anyone asks of a run that looks stuck.
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        force=True)
    result = run(args)
    report = write_report(result, args.out)
    print(report["summary"])
    ok = (report["meta"]["production_db_unchanged"]
          and report["meta"]["corpus_untouched"]
          and report["meta"]["placeholder_decider_called_no_model"])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
