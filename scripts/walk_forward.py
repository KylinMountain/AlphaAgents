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
import asyncio
import csv
import hashlib
import json
import logging
import os
import sqlite3
import sys
import time
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

#: The corpus files the replay shares by reference. The **pass/fail** question
#: is whether the replay opened each one read-only (D39); the size+mtime
#: fingerprint is kept as a diagnostic only, because production writes one of
#: these files continuously while a window runs.
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
#: Split by decider on purpose: a caveat that describes a decider which did
#: not run is not a hedge, it is a false statement, and the previous round's
#: summary kept saying "no news" over a run that read news.
LIMITATIONS = (
    "mechanistic walk-forward, not a point-in-time model backtest: today's "
    "model weights have already seen these dates",
    "the theme is a synthetic line with no score, so the theme gate passes "
    "every order by design — this window does not test the gate",
    "ST status comes from stocks.db, which holds the current name rather than "
    "the name as of the replayed date; codes absent from the previous "
    "session's bars are excluded, but a later ST marking is not knowable",
    "capacity is measured (ADV20 at the decision) and reported, but not yet "
    "enforced: ``_fill_order`` sizes from capital and takes no capacity input",
    "no theses and no predictions: exits are stop/target only",
    "the learning step **records** observations, it does not promote them: "
    "every candidate stays in the ``observation`` state because n is far below "
    "the 50 this repository requires, and ``advance_candidate`` is deliberately "
    "not called by the pipeline — so 'the agent learned something' here means "
    "'a dated note stating its n was written and cited', not 'behaviour changed'",
)

#: What a placeholder run additionally cannot show.
PLACEHOLDER_LIMITATIONS = (
    "the decider is a placeholder that exists to generate orders; its return "
    "is not evidence about any strategy",
    "the placeholder calls no model and reads no news",
)

#: What a model-backed run additionally cannot show.
LLM_LIMITATIONS = (
    "the model's weights have already read these dates, so a strong result "
    "is at least as likely to be recall as signal — this is the reason "
    "historical replay screens candidates and never promotes them",
    "the news feed is the free flash stream only; the paid columns are not "
    "replayable, so nothing here speaks for them",
    "the model chooses *within* a fixed panel of the previous session's "
    "movers, so this measures selection inside a panel, not the panel itself",
    "one sampled model call per day: recorded and replayable, but a single "
    "sample is not a distribution over the model's judgement",
    "the {knowledge} block in a replay is this run's **own** observations, not "
    "the production retrieval path: production reaches a decision only through "
    "an approved knowledge snapshot (feedback.inject_principles), and a replay "
    "has none. Feeding a trader its own journal is not the same as promoting a "
    "note to a rule — but it does mean the replay's decision context differs "
    "from production's, and no window here says what production would decide",
)


def _limitations(ctx) -> tuple[str, ...]:
    extra = LLM_LIMITATIONS if ctx.decider == "llm" else PLACEHOLDER_LIMITATIONS
    return LIMITATIONS + extra


# ── the corpus, read through the kernel's own readers ───────────────────────


def _corpus_fingerprint(data_dir: Path) -> dict:
    """Size and mtime of every shared corpus file — **diagnostic** (D39).

    Kept because a reader wants to know what moved, and because a bare boolean
    cannot be diagnosed after the fact. Not a verdict: one of these files
    belongs to production (see :func:`_corpus_access_check`).
    """
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


def _corpus_access_check(data_dir: Path) -> dict:
    """Did *the replay* open every corpus file read-only? (D39)

    This replaces a bare ``size+mtime`` comparison, and the replacement is the
    whole point of the debt entry. The old check asked "is this file unchanged"
    and reported one boolean; on a machine where the scheduled pipeline ingests
    news continuously the honest answer is always no — ``market_snapshots.db``
    grew by 53 KB during a 120-day window — so **the check could not pass on a
    live box, and its red was never a fact about the replay**. It also gated the
    exit status, so every long run returned 1.

    "Did the replay write" is answerable from the replay's own directory.
    ``corpus_access`` opens a file read-only exactly when it is a symlink, so a
    file the bootstrap linked is one the store cannot write; the refusal itself
    is pinned by ``tests/test_corpus_access.py``, on throwaway files.

    **This deliberately does not probe.** Every detector of "is this handle
    read-only" is a real write attempt — measured: ``PRAGMA user_version = 0``
    and ``CREATE TABLE`` both raise ``attempt to write a readonly database``,
    while ``BEGIN IMMEDIATE`` does *not* raise and so detects nothing. A probe
    would therefore mutate the corpus in exactly the case this check exists to
    catch, which is a bad trade for a check whose job is to protect it.
    """
    files = {}
    for name in CORPUS_FILES:
        path = data_dir / name
        if not path.exists():
            files[name] = {"present": False}
            continue
        files[name] = {"present": True,
                       "shared": corpus_access.is_shared(path)}
    present = [f for f in files.values() if f["present"]]
    return {
        "files": files,
        # No corpus file at all is a failure, not a vacuous pass: a run that
        # read no history demonstrated nothing about reading it.
        "read_only": bool(present) and all(f["shared"] for f in present),
    }


def _corpus_changes(before: dict, after: dict) -> list[dict]:
    """Which shared files moved while the window ran, and by how much.

    **Diagnostic, not a verdict** (D39). Production writes
    ``market_snapshots.db`` continuously, so a non-empty list here is the normal
    case and says nothing about the replay. It exists because the first two
    write-ups of this defect named the wrong file twice — a bare boolean cannot
    be diagnosed after the fact, and finding the real writer took a manual
    ``stat`` on a guess about which directory was watched.
    """
    changes = []
    for name in sorted(set(before) | set(after)):
        old, new = before.get(name), after.get(name)
        if old == new:
            continue
        changes.append({
            "file": name, "before": old, "after": new,
            "size_delta": ((new or {}).get("size") or 0)
                          - ((old or {}).get("size") or 0),
        })
    return changes


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
        #: ``code → first session the corpus holds``. The selection-side
        #: as-of universe, and the only evidence of listing that exists:
        #: there is no dated name table in this repository (see
        #: ``market_history.first_bar_dates``).
        self.first_bar: dict[str, str] = mh.first_bar_dates()
        self._bars: dict[str, dict[str, dict]] = {}
        self._adv: dict[tuple[str, str], float | None] = {}

    def is_listed(self, code: str, day: str) -> bool:
        """Was ``code`` already trading strictly before decision day ``day``?

        Strictly before, and the anchor is the decision day rather than the
        session the decider reads, because those are the same test:

        * First session is ``day`` itself — an IPO today. It has no previous
          close, so the decider can neither rank nor price it. Not in the
          pool.
        * First session is the previous session — it listed yesterday. It
          *does* have a previous close, and by 09:00 today it is trading.
          In the pool.

        What this excludes is the case the requirement is about: a model
        asked about a 2020 window naming a security that had not listed
        yet. ``first_bar`` is the only evidence available — this repository
        has no dated name table (see ``market_history.first_bar_dates``).
        """
        first = self.first_bar.get(code)
        return first is not None and first < day

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

        The ``>= 20`` is the docstring above enforced rather than promised. The
        first version averaged whatever history it found, so a name listed the
        previous session was handed an "ADV20" that was one bar — a stand-in
        average, from the function whose own docstring says the caller asked
        not to be handed one. It matters little in a 2025 window (6 codes) and
        a great deal in a 2020 one, which is the case the rule exists for.
        """
        key = (code, before)
        if key not in self._adv:
            hist = mh.get_local_history(code, days=20, as_of=before)
            vols = [float(b.get("volume") or 0) for b in hist] if hist else []
            self._adv[key] = (sum(vols) / len(vols)) if len(vols) >= 20 else None
        return self._adv[key]


# ── the universe as-of ──────────────────────────────────────────────────────


def _eligibility(ctx, code: str, day: str, prev_day: str) -> str | None:
    """Why ``code`` may not be ordered on ``day``, or ``None`` if it may.

    One gate, because "which securities existed" has to have one answer.
    Both deciders iterate the **whole instrument table** rather than the
    previous session's bars, and that is the only reason this is reachable:
    a candidate set drawn from T-1 bars can only ever contain listed names,
    so a gate applied to it returns ``None`` for every row and is dead code.

    That was measured, not reasoned, and it is why the loop below is over
    ``ctx.corpus.instruments``. On the real corpus a 2020 window has 1,986
    codes that had not listed yet, and *every one of them* also lacks a T-1
    bar — so under the old loop they were excluded by accident rather than
    by this rule, and the rule could not have been tested.

    Iterating instruments does not change which names are rankable: a name
    with no T-1 bar has no change to rank on either way, so the panel is
    identical. What changes is that each exclusion is counted instead of
    silently skipped. A silent ``continue`` is how a shrinking universe
    goes unnoticed.

    Returns the reason as a value rather than a bare boolean so a refusal
    can be counted.
    """
    if not code.startswith(TRADABLE_PREFIXES):
        return "board"
    if not ctx.corpus.is_listed(code, day):
        return "not_listed"
    meta = ctx.corpus.instruments.get(code)
    if meta is None:
        return "unknown_instrument"
    if meta["is_st"]:
        return "st"
    if meta["is_suspended"]:
        return "suspended"
    if code not in ctx.corpus.bars(prev_day):
        return "no_prior_bar"
    return None


# ── the model-backed decider ────────────────────────────────────────────────


def _build_panel(ctx, day: str, prev_day: str, limit: int) -> list[dict]:
    """The securities the decision may order, and the only ones.

    Drawn from the previous session's bars, so listing and suspension are
    satisfied by construction rather than by a later check — a security with
    no bar on T-1 cannot be ranked or priced, so it is not in the pool. What
    the panel adds is that the pool is now *stated to the model and enforced
    on its answer*: a model asked about a 2020 window has read 2026 and will
    name a security that had not listed, and the panel is what turns that
    into a counted refusal instead of a purchase.

    The candidate set is the **whole instrument table**, not the previous
    session's bars. A set drawn from T-1 bars can only contain listed names,
    so a gate applied to it would be unreachable — and this repository has
    paid for declared-but-unreachable three times. Iterating instruments
    changes nothing about which names are *rankable* (a name with no T-1 bar
    has no change to rank on either way); what it changes is that every
    exclusion is counted, so "the universe shrank" is a number rather than
    an absence.

    ADV20 is computed only for the top ``limit * 3`` by change, because it
    costs a history read per code and most of the pool is never shown.
    """
    bars_prev = ctx.corpus.bars(prev_day)
    ranked = []
    for code in ctx.corpus.instruments:
        reason = _eligibility(ctx, code, day, prev_day)
        if reason is not None:
            ctx.counters[f"eligibility:{reason}"] += 1
            continue
        # The gate above already proved this code has a bar on ``prev_day``.
        row = bars_prev[code]
        chg = row.get("change_pct")
        close = row.get("close")
        if chg is None or not close or float(close) <= 0:
            continue
        if float(chg) >= LIMIT_UP_SKIP_PCT:
            continue
        ranked.append((float(chg), code, row))
    ranked.sort(key=lambda t: t[0], reverse=True)

    panel: list[dict] = []
    for chg, code, row in ranked[:limit * 3]:
        adv = ctx.corpus.adv20(code, prev_day)
        if adv is None or adv <= 0:
            # No measurable liquidity is not the same as illiquid, and the
            # plan's rule is that a security whose numbers cannot be
            # established is not offered at all.
            continue
        panel.append({
            "code": code,
            "name": ctx.corpus.instruments[code]["name"],
            "close": float(row["close"]),
            "change_pct": round(chg, 2),
            "adv20": adv,
        })
        if len(panel) >= limit:
            break
    return panel


def _news_window(day: str, prev_day: str, limit: int) -> list[dict]:
    """Flash news published between the previous close and 09:00 on ``day``.

    The window is the whole as-of statement: news is continuous, so "the
    latest N items" would silently drop whatever arrived beyond N and re-read
    what it already saw. ``as_of`` and ``since`` are both full timestamps,
    which is what makes ``read_news`` honour them as instants rather than
    widening them to the day.
    """
    from alpha_agents.data.snapshot_store import read_news
    try:
        return read_news(None, as_of=f"{day} 09:00:00",
                         since=f"{prev_day} 15:00:00", limit=limit)
    except Exception as exc:                          # noqa: BLE001
        logger.warning("%s: news window unavailable: %s", day, exc)
        return []


def _book_and_knowledge(ctx, day: str) -> tuple[str, str]:
    """What the agent holds, and what it has learned, as of the replay day.

    Read through the same functions the live morning scan uses, so a replay
    cannot drift from production about what "its own book" means. Both read
    the replay's own ``memory.db``, which ``walk_bootstrap`` creates empty —
    a 2020 window must not inherit 2026's principles.

    ``day`` is not decoration: the third source is this trader's own
    observations, which grow during the window, so the context has to be read
    per day. A block computed once at the start would be the 2026 leak in
    miniature — day 40 deciding with day 1's empty journal.
    """
    from alpha_agents.evolution import feedback
    try:
        book = feedback.inject_portfolio(trader_id=ctx.trader)
    except Exception as exc:                          # noqa: BLE001
        logger.warning("portfolio context unavailable: %s", exc)
        book = ""
    parts = []
    for fn in (feedback.inject_sentiment, feedback.inject_principles,
               feedback.inject_playbooks):
        try:
            parts.append(fn())
        except Exception as exc:                      # noqa: BLE001
            logger.warning("%s unavailable: %s", fn.__name__, exc)
    parts.append(_knowledge_block(ctx, day))
    return book, "\n\n".join(p for p in parts if p)


def _load_prompt_text() -> str:
    """The decider's prompt, read once per run.

    Imported lazily so a placeholder run — which is most of them — does not
    depend on the ``agents`` layer at all.
    """
    from alpha_agents.agents import t1_decider
    return t1_decider.load_prompt()


def _trader_note(ctx) -> str:
    """The trader's own words, so the style is a file and not a branch."""
    try:
        from alpha_agents.data.trader import load_traders
        trader = load_traders().get(ctx.trader)
        note = getattr(trader, "extra_prompt", "") or ""
    except Exception as exc:                          # noqa: BLE001
        logger.warning("trader note unavailable: %s", exc)
        note = ""
    return note


# ── the learning step ───────────────────────────────────────────────────────


#: How many observations the knowledge block shows. The newest few: the prompt
#: is a budget, and an observation that has not been promoted is not more
#: informative for being old.
#:
#: The trade-count floor and the median live in
#: ``alpha_agents.evolution.evidence`` now, beside the analysis that uses them.
_KNOWLEDGE_LIMIT = 3


def _closed_trades(ctx, day: str, conn) -> list[dict]:
    """Every position closed on or before ``day``, with its entry feature.

    The feature is the one the decider ranks on — the previous session's
    change — and it is **not** stored on the position, so it is re-read from
    the corpus at the order's own T-1. That is deliberate rather than
    convenient: it makes the claim reproducible from the corpus plus the
    book, with nothing in between that only this process knows.

    ``episode_id`` is the join that makes a citation possible at all. A
    position with no episode is kept and its feature still measured — it
    counts towards n — but it cannot be cited, so it is dropped by the
    distillation rather than silently cited as something it is not.
    """
    rows = conn.execute(
        "SELECT p.id, p.code, p.order_date, p.close_date, p.return_pct,"
        "       p.close_reason, e.id AS episode_id"
        "  FROM virtual_portfolio p"
        "  LEFT JOIN episodes e ON e.position_id = p.id"
        " WHERE p.close_date IS NOT NULL AND p.close_date <= ?"
        "   AND p.return_pct IS NOT NULL"
        " ORDER BY p.close_date, p.id", (day,)).fetchall()
    out = []
    for row in rows:
        prev = ctx.corpus.previous(row["order_date"])
        chg = None
        if prev is not None:
            bar = ctx.corpus.bars(prev).get(row["code"])
            if bar is not None:
                chg = bar.get("change_pct")
        out.append({
            "position_id": int(row["id"]),
            "code": row["code"],
            "episode_id": int(row["episode_id"]) if row["episode_id"] else None,
            "close_date": row["close_date"],
            "return_pct": float(row["return_pct"]),
            "close_reason": row["close_reason"],
            "t1_change": None if chg is None else float(chg),
        })
    return out


def _distil(ctx, day: str, trades: list[dict]) -> dict | None:
    """One dated, checkable statement about T-1 change, or ``None`` if too thin.

    The analysis itself lives in ``alpha_agents.evolution.evidence`` — the
    LEARN half of the loop, and a component rather than a private function of
    this runner. It used to be sixty lines here, which meant the Evidence
    Analyzer the design calls for existed only inside one script: outside the
    package, outside the layer rules, and unreachable from anything else.

    What stays here is the part that is genuinely about a *replay*: reading
    the T-1 feature out of the corpus at the order's own date. The analyzer
    takes :class:`~alpha_agents.evolution.evidence.Trade` values and knows
    nothing about where they came from.

    The proposition, the median-to-median comparison, the empty-list rule and
    the reason the per-trade test is against the window's median rather than
    zero are all documented on that module.
    """
    from alpha_agents.evolution import evidence as EV

    observations = [
        EV.Trade(
            position_id=t["position_id"],
            code=t["code"],
            return_pct=t["return_pct"],
            close_date=t["close_date"],
            t1_change=t["t1_change"],
            episode_id=t["episode_id"],
            close_reason=t["close_reason"],
        )
        for t in trades
    ]
    return EV.analyse_and_save(
        observations,
        source="walk_forward_replay",
        source_date=day,
        trader=ctx.trader,
        entry_zone=ctx.entry_zone,
        window_start=ctx.window_start,
    )


def _learn(ctx, day: str) -> dict:
    """Close the loop for one replay day: label it, then say what it means.

    Called after the day's exits have settled and the book has been marked,
    because both halves read what the day *did* rather than what it intended.

    Three links, in this order for a reason:

    1. **Label.** ``sweep_trade_labels`` writes each closed position's realised
       result as an ``outcome`` row carrying an ``available_at``. Without it
       the evidence is a number in a ledger rather than a dated label, and
       nothing downstream can say *when* it became knowable — which is the one
       thing a forward-only validation needs.
    2. **Distil.** One dated statement about the rule the decider used, citing
       the episodes it came from (``_distil``).
    3. **Publish.** It is written as a ``learning_candidate``, so
       ``candidates_citing`` can enumerate it and ``_knowledge_block`` can read
       it into the next day's prompt. A lesson nothing can retrieve is the
       "declared but never called" defect with better manners.

    The lifecycle is deliberately **not** advanced. ``learning_candidates``'
    own design note is explicit that the pipeline must not drive ``status``:
    ``advance_candidate`` is its only writer and promotion is an audited human
    act. With n this small the honest state is ``observation``, which is where
    the write already puts it — so the ceiling this reaches is *recorded and
    cited*, and it is reported as exactly that.
    """
    from alpha_agents.data.memory_store import _get_conn
    from alpha_agents.evolution import outcome_labels as OL

    conn = _get_conn()
    labels = OL.sweep_trade_labels(conn, as_of=day)
    conn.commit()

    trades = _closed_trades(ctx, day, conn)
    # Only when something closed **today**. The measurement is cumulative
    # ("the window so far"), so re-stating it on a day with no new close
    # writes the same claim again under a new ``source_date`` — a different
    # fingerprint, so ``save_candidate`` cannot dedupe it. Measured on the
    # first 180-day window: **11 candidates for 2 distinct facts**, because
    # every day after the fourth close wrote another copy of the same
    # sentence. A candidate list that is mostly duplicates makes
    # ``counts()`` meaningless, and the duplicates are not even wrong —
    # which is why nothing else would have caught it.
    closed_today = [t for t in trades if t["close_date"] == day]
    distilled = _distil(ctx, day, trades) if closed_today else None
    ctx.counters["learning_days"] += 1
    if distilled is not None:
        ctx.counters["learning_candidates"] += 1
        logger.info("%s: observation #%d over n=%d (%d support / %d oppose)",
                    day, distilled["candidate_id"], distilled["n"],
                    distilled["supporting"], distilled["opposing"])
    return {"date": day, "labels": labels, "closed_total": len(trades),
            "closed_today": len(closed_today), "distilled": distilled}


def _knowledge_block(ctx, day: str) -> str:
    """What this trader has observed about its own trading, as of ``day``.

    **A replay-only channel, and the limitations say so.** In production a
    *rule* reaches a decision only through an approved knowledge snapshot
    (``feedback.inject_principles`` / ``inject_playbooks``), and a replay has
    no snapshot — so in production this block is empty and the agent decides
    exactly as it did before. What is fed back here is not a rule: it is this
    trader's own dated notes about its own closed trades. That is the journal
    a trader reads, not a change to its mandate.

    The distinction is the design, and it is worth stating plainly: the gate
    belongs on *promotion* — on a note becoming a rule — not on a trader
    remembering what happened to it. A loop with no feedback at all is not a
    conservative loop; it is not a loop.

    ``day`` is used, not decorative: an observation is filtered to
    ``source_date < day``. The learning step runs at the *close* of a day and
    this block is read at the *open* of the next, so in a single pass the two
    never overlap — but the filter is what makes that a property of the code
    rather than of the loop's shape, and it is the difference between "no
    lookahead today" and "no lookahead by construction".
    """
    try:
        from alpha_agents.data import learning_candidates as LC
        rows = [row for row in LC.candidates_by_status(LC.OBSERVATION, limit=200)
                if (row["source_date"] or "") < day]
    except Exception as exc:                          # noqa: BLE001
        logger.warning("learning candidates unavailable: %s", exc)
        return ""
    if not rows:
        return ""
    lines = ["【你自己的交易记录（本次回放写下的观察；样本很小，是观察不是结论）】"]
    for row in rows[-_KNOWLEDGE_LIMIT:]:
        lines.append(f"· {row['source_date']}｜{row['claim']}")
        cited = json.loads(row["evidence_episode_ids"] or "{}")
        ids = sorted(set(cited.get("supporting") or [])
                     | set(cited.get("opposing") or []))
        if ids:
            lines.append("  证据 episode：" + "、".join(f"#{i}" for i in ids))
    return "\n".join(lines)


def _build_model(timeout: float | None):
    """The journaled chat model, with the run's timeout applied.

    Imported lazily so a placeholder run — which is most of them — does not
    depend on the ``agents`` layer at all.
    """
    from alpha_agents.model_factory import create_model
    return create_model(timeout=timeout)


def _decide_llm(ctx, day: str, prev_day: str) -> list[dict]:
    """One model call, then the same intent door the placeholder uses."""
    from alpha_agents.agents import t1_decider

    panel = _build_panel(ctx, day, prev_day, ctx.panel_size)
    ctx.counters["panel_size"] += len(panel)
    if not panel:
        logger.info("%s: empty panel, nothing to decide", day)
        return []
    # Read per day, not once: the book changes as positions open and close,
    # and the knowledge changes as the learning step writes. A context
    # computed at the start of the window would be the 2026 leak in
    # miniature — the agent deciding on day 40 with day 1's empty book.
    book, knowledge = _book_and_knowledge(ctx, day)
    verdict = t1_decider.propose_sync(
        day=day, prev_day=prev_day, panel=panel,
        news=_news_window(day, prev_day, ctx.news_limit),
        book=book, knowledge=knowledge,
        trader_note=ctx.trader_note, picks=ctx.picks,
        template=ctx.prompt, model=ctx.model, loop=ctx.loop)
    if verdict["parse_error"]:
        # A reply we could not read is not the same as "it chose nothing",
        # and the two must not share a counter.
        ctx.counters["decider_unreadable"] += 1
        logger.warning("%s: decider reply unreadable: %s", day,
                       verdict["parse_error"])
        return []
    for refusal in verdict["refused"]:
        ctx.counters[f"decider_refused:{refusal['why']}"] += 1
        logger.info("%s: refused %s (%s) %s", day, refusal["code"],
                    refusal["why"], refusal["detail"])

    by_code = {row["code"]: row for row in panel}
    placed: list[dict] = []
    for order in verdict["orders"]:
        cap = capacity_shares(by_code[order["code"]]["adv20"],
                              participation=ctx.participation)
        if cap <= 0:
            ctx.counters["decider_refused:no_capacity"] += 1
            continue
        order_id = create_pending_order(
            code=order["code"],
            name=by_code[order["code"]]["name"],
            theme=ctx.theme,
            order_date=day,
            entry_low=order["entry_low"],
            entry_high=order["entry_high"],
            stop_loss=order["stop_loss"],
            source="walk_forward",
            reason=f"{t1_decider.DECIDER_NAME}: {order['reason']}",
            trader_id=ctx.trader)
        if order_id is None:
            # The intent door refused it and wrote down why; the refusal is
            # evidence, not an error.
            ctx.counters["intent_refused"] += 1
            continue
        ctx.capacity[order["code"]] = cap
        placed.append({"code": order["code"], "order_id": order_id,
                       "reason": order["reason"]})
    return placed


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
    for code in ctx.corpus.instruments:
        reason = _eligibility(ctx, code, day, prev_day)
        if reason is not None:
            ctx.counters[f"eligibility:{reason}"] += 1
            continue
        # The gate above already proved this code has a bar on ``prev_day``.
        row = bars_prev[code]
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
        self.decider = args.decider
        self.panel_size = args.panel_size
        self.news_limit = args.news_limit
        #: The window's first session, carried so an observation can name the
        #: window it was measured over. A claim about "the window" that does
        #: not say which window is not checkable against anything.
        self.window_start = args.start
        #: Read **once**, at the start of the window — and only when a model
        #: will read it. The prompt lives on disk and every other loader in
        #: this repository re-reads it per call, which is right for a
        #: scheduler that should pick up an edit and wrong for a replay: a
        #: file edited at day 13 would silently make days 1–12 and 13–40
        #: different experiments. It happened — the first 40-day run had its
        #: template edited mid-flight and 27 of its 40 days died with
        #: ``KeyError: 'news'``, while the report still described one window.
        #: The hash goes in the report so a reader can tell which prompt a
        #: window actually ran under.
        self.prompt = _load_prompt_text() if args.decider == "llm" else None
        self.prompt_sha256 = (hashlib.sha256(self.prompt.encode("utf-8"))
                              .hexdigest() if self.prompt else None)
        #: Static, so read once: the trader's prompt file does not change
        #: inside a window, and re-reading it per day would make the run
        #: depend on when a file was edited.
        self.trader_note = _trader_note(self)
        self.model_timeout = args.model_timeout
        #: Built **once**, not per day. ``llm_journal.journal()`` is a process
        #: singleton, so a model per day would not break the journal — but it
        #: would leak a connection pool per simulated session, and this is the
        #: object the timeout lives on.
        self.model = (_build_model(args.model_timeout)
                      if args.decider == "llm" else None)
        #: One event loop for the whole window, and it exists **because** the
        #: model above is built once. httpx pools connections bound to the loop
        #: that opened them, so ``asyncio.run`` per day made each day's first
        #: request reuse a connection from a loop that was already closed —
        #: a transport failure with no status, answered by the SDK's retry.
        #: Measured at exactly one wasted request per day over 120 days, every
        #: day, which is the shape of a cause that is per-loop rather than
        #: per-call.
        #:
        #: ``None`` for the placeholder decider, which calls no model and so
        #: has nothing to bind. ``run()`` closes it; a ``Context`` built and
        #: dropped by a test would leave it open, which is why nothing but
        #: ``run()`` builds one with a model in it.
        self.loop = (asyncio.new_event_loop()
                     if args.decider == "llm" else None)
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


def _decider_name(ctx) -> str:
    """What to call the decider in the report. Never a hard-coded string:
    a report naming the placeholder while the model decided is the exact
    failure the model-usage check exists to catch."""
    if ctx.decider == "llm":
        from alpha_agents.agents.t1_decider import DECIDER_NAME
        return DECIDER_NAME
    return DECIDER


def _decider_note(ctx) -> str:
    """The caveat that belongs to the decider that actually ran.

    The placeholder needs to be read as "not a strategy". The model-backed
    one needs to be read as "a sampled model reading an as-of panel", which
    is a different and smaller claim — and printing the placeholder's
    warning over it would be a lie in the flattering direction.
    """
    if ctx.decider == "llm":
        return (f"（模型读 as-of 面板选股，最多 {ctx.picks} 单；"
                f"面板 {ctx.panel_size} 只，新闻窗口截至当日 09:00）")
    return "（**占位用途，不代表任何策略**）"


def _run_decider(ctx, day: str, prev_day: str) -> list[dict]:
    """Route to the declared decider. One place, so the report cannot
    describe a decider the run did not use."""
    if ctx.decider == "llm":
        return _decide_llm(ctx, day, prev_day)
    return _decide(ctx, day, prev_day)


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
    _assert_model_mode(args.decider)

    ctx = Context(args)
    # The window is a function of its own for one reason: the loop the model's
    # connection pool is bound to has to be closed on the way out, including
    # when a day raises. ``run`` is the only thing that knows when the window
    # begins and ends, so it is the only thing that can own that.
    try:
        return _run_window(ctx, args)
    finally:
        _close_decider_loop(ctx)


def _close_decider_loop(ctx) -> None:
    """Close the window's event loop, if this run built one.

    Closed rather than left to the interpreter. "One loop per window" is the
    property that keeps the shared client's connections valid, and a property
    nothing ever checks is how this one got broken in the first place — the
    loop is closed here so that a second loop cannot quietly appear later and
    still leave every test green.
    """
    if ctx.loop is None:
        return
    ctx.loop.close()
    ctx.loop = None


def _run_window(ctx, args) -> dict:
    _seed_theme(ctx)

    window = ctx.corpus.window(args.start, args.days)
    journal_before = _journal_records(_REPLAY_DIR)
    prod_hash_before = _sha256(_PRODUCTION_DIR / "memory.db")
    #: The verdict (did the replay get read-only handles) and the diagnostic
    #: (what moved) are separate readings, because only the first is about us.
    corpus_access_before = _corpus_access_check(_REPLAY_DIR)
    corpus_before = _corpus_fingerprint(_REPLAY_DIR)

    equity_rows, fill_rows, settle_rows, event_rows = [], [], [], []
    learning_rows = []
    by_status = Counter()
    cancels = Counter()
    errors = []

    for day in window:
        prev_day = ctx.corpus.previous(day)
        if prev_day is None:
            continue
        # The decision and the settlement are separate failures, and they were
        # one ``try`` until a 180-day run made the difference visible. A
        # provider rate limit in the decider took the whole day down with it:
        # the pending orders were never settled, the exits were never
        # processed and no equity row was written, so the curve had a hole
        # where the market had a session. Settlement is book mechanics and
        # owes the model nothing — a failure to decide costs the decision.
        try:
            with replay_as_of(f"{day} 09:00"):
                placed = _run_decider(ctx, day, prev_day)
            logger.info("%s: ordered %d", day, len(placed))
        except Exception as exc:                      # noqa: BLE001
            logger.exception("%s: the decider failed; settling the book anyway",
                             day)
            errors.append({"date": day, "stage": "decide",
                           "error": f"{type(exc).__name__}: {exc}"})
            if not args.keep_going:
                raise
            placed = []
        try:
            with replay_as_of(f"{day} 09:30"):
                entries = _settle_entries(ctx, day, P.get_pending_orders(ctx.trader))
                logger.info("%s: settled entries %s", day,
                            dict(entries["counts"]))
                exits = _settle_exits(ctx, day)
                logger.info("%s: settled exits %s", day, dict(exits["counts"]))
            with replay_as_of(day):
                equity = _value(ctx, day)
                # Inside the day's as-of, and after the book is marked: the
                # labels are derived from the ledger, so they have to be
                # written at a moment where "today" is the day that just
                # closed rather than the day about to open.
                learning = _learn(ctx, day)
            logger.info("%s: equity %s (open %d, pending %d)", day,
                        equity["equity"], equity["open_positions"],
                        equity["pending_orders"])
        except Exception as exc:                      # noqa: BLE001
            logger.exception("%s failed", day)
            errors.append({"date": day, "stage": "settle",
                           "error": f"{type(exc).__name__}: {exc}"})
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
        learning_rows.append(learning)
        # Pacing is a property of the *provider*, not of the simulation, so it
        # is applied after the day is fully written and cannot change any of
        # its numbers. It exists because a replay's whole point is one model
        # call per day, and asking for them as fast as they arrive spends a
        # free tier's per-minute token budget in about a dozen sessions.
        if args.pace_seconds:
            time.sleep(args.pace_seconds)

    journal_after = _journal_records(_REPLAY_DIR)
    prod_hash_after = _sha256(_PRODUCTION_DIR / "memory.db")
    corpus_after = _corpus_fingerprint(_REPLAY_DIR)

    return {
        "ctx": ctx, "window": window, "equity": equity_rows, "fills": fill_rows,
        "settlement": settle_rows, "events": event_rows,
        "by_status": by_status, "cancels": cancels, "errors": errors,
        "learning": learning_rows,
        "model_calls": {
            "mode": os.environ.get("ALPHAAGENTS_LLM_MODE", "live"),
            "journal_before": journal_before, "journal_after": journal_after},
        "production": {"hash_before": prod_hash_before,
                       "hash_after": prod_hash_after,
                       "unchanged": prod_hash_before == prod_hash_after},
        "corpus": {"access": corpus_access_before,
                   "before": corpus_before, "after": corpus_after,
                   "changes": _corpus_changes(corpus_before, corpus_after)},
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


def _model_usage(ctx, model: dict) -> tuple[bool, str]:
    """Did the model get called exactly as this run declared?

    Three cases, and the difference between them is the whole point:

    * ``record`` **must** write a recording. An empty journal under this mode
      is the easier lie to ship, because the report still looks complete.
    * ``replay-recorded`` **must not** write one. New records would mean it
      re-sampled the model, which is the thing replay exists to prevent; and
      the journal must already hold something, or "we replayed" means "we
      answered from nothing".
    * the placeholder must do neither.

    The first version of this check compared ``journal_after > 0``, which is
    wrong for a replay: a replay reads a journal that is already non-empty and
    adds nothing, so a correct replay failed the check. It was found by running
    one, which is the only way it could have been found — the record pass
    passed.
    """
    calls = model["journal_after"] - model["journal_before"]
    mode = model["mode"]
    if ctx.decider != "llm":
        return calls == 0, f"占位决策器：新增 {calls} 条记录（应为 0）"
    if mode == llm_journal.RECORD:
        return calls > 0, f"record：新增 {calls} 条记录（应 > 0）"
    if mode == llm_journal.REPLAY:
        ok = calls == 0 and model["journal_after"] > 0
        return ok, (f"replay：新增 {calls} 条（应为 0）；"
                    f"日志原有 {model['journal_before']} 条（应 > 0）")
    return False, (f"live 模式下的模型回放无法核对（新增 {calls} 条）："
                   "live 不记录，两次同窗口运行会因采样而不同")


def _assert_model_mode(decider: str) -> None:
    """Refuse a model-backed replay that cannot be reproduced.

    ``live`` records nothing by design, so under it the run is irreproducible
    and its report is a claim about one sample that no second run can check.
    Refusing here rather than warning is the plan's rule: the comparison
    between two arms of an experiment is only readable if the model was not
    re-sampled between them.
    """
    if decider != "llm":
        return
    mode = os.environ.get(llm_journal.MODE_ENV, llm_journal.LIVE)
    if mode == llm_journal.LIVE:
        raise SystemExit(
            f"--decider llm requires {llm_journal.MODE_ENV}="
            f"{llm_journal.RECORD} (the first pass, which writes the journal) "
            f"or {llm_journal.MODE_ENV}={llm_journal.REPLAY} (later passes, "
            f"which answer from it) — not {llm_journal.LIVE}. A live run "
            "records nothing, so the same window would answer differently on "
            "the next run for reasons that have nothing to do with the market.")


# ── the report ──────────────────────────────────────────────────────────────


def _write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _learning_meta(learning: list[dict]) -> dict:
    """What the loop actually did, as numbers rather than as a claim.

    ``matured`` is summed because ``sweep_trade_labels`` counts only the
    labels it *moved* — a label already matured is counted as ``unchanged``,
    so summing cannot double-count it. ``pending`` is not summed for the
    mirror reason: an open position is re-counted every day, so a sum would
    be "position-days" while reading as "positions". It is taken from the
    last day instead, which is what it says it is.
    """
    if not learning:
        return {"days": 0, "matured_total": 0, "pending_open": 0,
                "closed_trades_total": 0, "observations_written": 0,
                "observation_days": [], "last_observation": None}
    observed = [row for row in learning if row["distilled"]]
    return {
        "days": len(learning),
        "matured_total": sum(r["labels"].get("matured", 0) for r in learning),
        "pending_open": learning[-1]["labels"].get("pending", 0),
        "closed_trades_total": learning[-1]["closed_total"],
        "observations_written": len(observed),
        "observation_days": [row["date"] for row in observed],
        "last_observation": observed[-1]["distilled"] if observed else None,
    }


def write_report(result: dict, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    ctx = result["ctx"]
    window = result["window"]
    model = result["model_calls"]
    model_usage_ok, model_usage_detail = _model_usage(ctx, model)

    meta = {
        "run_id": ctx.run_id,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "decider": _decider_name(ctx),
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
        "prompt_sha256": ctx.prompt_sha256,
        "model_expected": ctx.decider == "llm",
        "model_calls_made": model["journal_after"] - model["journal_before"],
        "model_journal_total": model["journal_after"],
        "model_usage_ok": model_usage_ok,
        "model_usage_detail": model_usage_detail,
        "decider_counters": dict(ctx.counters),
        "learning": _learning_meta(result["learning"]),
        "placeholder_decider_called_no_model": (
            (model["journal_after"] - model["journal_before"]) == 0
            if ctx.decider != "llm" else None),
        "production_db_unchanged": result["production"]["unchanged"],
        "corpus_read_only": result["corpus"]["access"]["read_only"],
        "corpus_files": result["corpus"]["access"]["files"],
        #: Diagnostic, and named file-by-file on purpose: D39's first two
        #: write-ups guessed the wrong writer twice, which a bare boolean
        #: makes unavoidable.
        "corpus_changes": result["corpus"]["changes"],
        "errors": result["errors"],
        "limitations": list(_limitations(ctx)),
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

    summary = _summary(result, out_dir, model_usage_ok, model_usage_detail)
    (out_dir / "summary.txt").write_text(summary, encoding="utf-8")
    return {"meta": meta, "summary": summary, "out_dir": out_dir}


def _summary(result: dict, out_dir: Path, model_usage_ok: bool,
             model_usage_detail: str) -> str:
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
        f"决策器        {_decider_name(ctx)}{_decider_note(ctx)}",
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
        f"回放对语料只读              {'是' if result['corpus']['access']['read_only'] else '**否**'}"
        f"（{_corpus_access_line(result['corpus']['access'])}）",
        f"共享语料 size+mtime（诊断） {_corpus_changes_line(result['corpus']['changes'])}",
        f"模型调用与声明一致          {'是' if model_usage_ok else '**否**'}"
        f"（{model_usage_detail}）",
        f"抛错的日-阶段               {len(result['errors'])}"
        f"{_error_kinds(result['errors'])}",
        "",
        "— 决策器计数 —",
    ]
    if ctx.counters:
        lines += [f"  {k:<28} {v:>6}" for k, v in sorted(ctx.counters.items())]
    else:
        lines.append("  （无）")
    lines += _learning_lines(result)
    lines += [
        "",
        "— 本次运行不能说明什么 —",
    ]
    lines += [f"  · {item}" for item in _limitations(ctx)]
    lines += ["", f"报告目录 {out_dir}"]
    return "\n".join(lines)


def _learning_lines(result: dict) -> list[str]:
    """The loop's own numbers, including the state it did **not** reach.

    The last two lines are the point. A summary that reports only "1
    observation written" invites the reader to think the agent now knows
    something; what it knows is a dated note with n stated, that reached
    exactly one decision context, and that no pipeline step promoted.
    """
    meta = _learning_meta(result["learning"])
    days = "（" + "、".join(meta["observation_days"]) + "）" if meta["observation_days"] else ""
    lines = [
        "",
        "— 学习闭环 —",
        f"  平仓并打标的仓位            {meta['closed_trades_total']} 笔"
        f"（本次新增 matured {meta['matured_total']}）",
        f"  仍在持仓、标为 pending      {meta['pending_open']} 笔",
        f"  写下的观察                  {meta['observations_written']} 条{days}",
    ]
    last = meta["last_observation"]
    if last:
        lines.append(
            f"  最近一条                    n={last['n']}，支持 {last['supporting']} / "
            f"反对 {last['opposing']}；中位收益 {last['high_median']:+.2f}%"
            f"（高 T-1 涨幅）vs {last['low_median']:+.2f}%（低）")
    lines += [
        "  观察的读者                  _knowledge_block → 次日提示词的 {knowledge}"
        "（**仅回放**，见下方限制）",
        "  生命周期                    全部停在 observation：n 远低于 50，"
        "且晋升由人审批、管线不驱动 status",
    ]
    return lines


def _error_kinds(errors: list[dict]) -> str:
    """Which stage, which exception, how many — because "14 errors" is not one fact.

    The line used to read ``结算过程抛错的交易日 14``, which a reader takes for
    a broken settlement. On the 180-day window every one of those fourteen was
    ``openai.RateLimitError`` from the provider, in the *decider*: the book
    settled fine and the day simply had no decision. A count with no kinds
    invites the wrong repair — it sends you to the settlement code, which is
    not where the fault is. The stage is in the key for the same reason.
    """
    if not errors:
        return ""
    kinds = Counter(
        f"{e.get('stage', '?')}:{(e.get('error') or '?').split(':')[0].strip()}"
        for e in errors)
    return "（" + "；".join(f"{k}×{n}" for k, n in kinds.most_common()) + "）"


def _corpus_access_line(access: dict) -> str:
    """Which corpus files were shared, and which were not — the failure, named.

    A bare 否 would leave the reader to guess which file the replay could have
    written, which is the same defect one level down.
    """
    present = [n for n, f in access["files"].items() if f["present"]]
    writable = [n for n, f in access["files"].items()
                if f["present"] and not f["shared"]]
    if not present:
        return "语料目录里没有共享文件，这次运行没有读到历史"
    if writable:
        return (f"{len(present)} 个共享文件中 {len(writable)} 个可写："
                + "、".join(writable))
    return f"{len(present)} 个文件全部以只读打开"


def _corpus_changes_line(changes: list[dict]) -> str:
    """What moved, file by file, with the reason this is not a verdict (D39).

    On this machine the answer is normally "something moved", production moved
    it, and the line has to say so rather than let a reader score the replay by
    it. It names the file and the delta because the first two write-ups of this
    defect blamed the wrong writer twice, and a bare boolean cannot be
    diagnosed after the fact.
    """
    if not changes:
        return "无（没有任何共享文件变化）"
    parts = [f"{c['file']} {c['size_delta']:+d}B" for c in changes]
    return (f"{len(changes)} 个文件有变化：{'、'.join(parts)}"
            "——生产侧的定时入库也在写这些文件，这一行不是对回放的判定")


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
    parser.add_argument("--decider", choices=("placeholder", "llm"),
                        default="placeholder",
                        help="placeholder: rank T-1 change, no model (default). "
                             "llm: the model chooses from an as-of panel.")
    parser.add_argument("--panel-size", type=int, default=40,
                        help="securities the model may choose from (llm only).")
    parser.add_argument("--news-limit", type=int, default=60,
                        help="news items in the 09:00 window (llm only).")
    parser.add_argument("--model-timeout", type=float, default=120.0,
                        help="seconds to wait for one decider reply before "
                             "calling it a failure (default 120). A throttled "
                             "provider that queues instead of answering 429 "
                             "would otherwise hold the run for the SDK's own "
                             "ten minutes, and then retry twice.")
    parser.add_argument("--pace-seconds", type=float, default=0.0,
                        help="sleep this long after each simulated session "
                             "(default 0). A replay asks one model call per "
                             "day as fast as the provider answers, which on a "
                             "free tier exceeds the tokens-per-minute budget "
                             "within a dozen sessions — and the budget is "
                             "spent by the *prompt*, so the failure arrives "
                             "earlier the bigger the panel is.")
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
    # The exit status is about what this run did, so it is built from the
    # readings that are about this run. ``corpus_read_only`` (did we get
    # read-only handles) is ours; "did a file another process owns move" is not,
    # and gating on it made every long run on a live box return 1 (D39).
    ok = (report["meta"]["production_db_unchanged"]
          and report["meta"]["corpus_read_only"]
          and report["meta"]["model_usage_ok"])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
