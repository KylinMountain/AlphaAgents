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

# Load .env before anything imports ``config``, which reads os.environ at
# import time. Without this the runner cannot reach a provider and dies on
# ``Missing credentials`` — D41, where the same command is not the same run
# because the key arrives only when something injects it. ``main.py`` always
# did this; the scripts did not. The import is from ``env_file`` rather than
# ``config`` precisely so that importing the loader does not freeze the
# constants it is meant to precede.
from alpha_agents.env_file import load_env  # noqa: E402

load_env()

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
from alpha_agents.config import DATA_DIR, PROMPTS_DIR  # noqa: E402
from alpha_agents.data import (  # noqa: E402
    corpus_access, frozen_direction_archive, market_history as mh,
    market_rules, order_theme_exposure, portfolio as P, security_eligibility,
    policy_registry, portfolio_exit, reservations,
    research_packet, sector_membership, sector_panel, sector_selection,
    selection_policy, t1_settlement as S,
)
from alpha_agents.data.t1_execution import capacity_shares  # noqa: E402
from alpha_agents.data.memory_store import upsert_theme  # noqa: E402
from alpha_agents.data.portfolio_intent import create_pending_order  # noqa: E402
from alpha_agents.evolution import (  # noqa: E402
    performance, replay_capabilities, sector_experiment, selection_experiment,
    world_read_set,
)
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

#: Turns the buy-side decider gets per decision.
#:
#: **Not defined here.** The number belongs to ``t1_decider``, which owns the
#: decision budget, and ``--max-turns`` defaults to ``None`` meaning "whatever
#: the decider says". It used to be defined in both places and the two drifted:
#: the decider was raised to 8 while this file kept 3, the runner always passes
#: its own value, so the function default became unreachable — a 20-day window
#: then spent 2.5 hours producing 40 unreadable decisions and zero trades.
#: One number, one owner.

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
    "capacity is measured from pre-decision ADV20 and enforced as a hard "
    "share cap on open-time fills; close-time synthetic buys remain a separate "
    "execution path and are reported separately",
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
    "the close-phase decision is a **synthetic close assumption**, not a "
    "strict point-in-time 14:55 decision: it is shown the day's final "
    "open/high/low/close/volume and its sells fill at that close, because the "
    "business moment being modelled is 'just before the close' and a daily "
    "bar is the finest data a replay holds. The last five minutes are not "
    "modelled, so a run must not be read as a genuine 14:55 information set",
    "the {knowledge} block in a replay is this run's **own** observations, not "
    "the production retrieval path: production reaches a decision only through "
    "an approved knowledge snapshot (feedback.inject_principles), and a replay "
    "has none. Feeding a trader its own journal is not the same as promoting a "
    "note to a rule — but it does mean the replay's decision context differs "
    "from production's, and no window here says what production would decide",
)

#: The ablation arm has its own caveat. It is separate from
#: LLM_LIMITATIONS because it describes an arm, not the decider: a run
#: without the flag must not carry a caveat about a column it showed.
CONCEPTS_ABLATED_LIMITATION = (
    "ablation arm: the concept column (current membership) was removed from "
    "the panel by --no-concepts; against a same-window run without the flag, "
    "every order difference is attributable to that column alone"
)


def _limitations(ctx) -> tuple[str, ...]:
    base = list(LIMITATIONS)
    extra = list(
        LLM_LIMITATIONS if ctx.decider == "llm" else PLACEHOLDER_LIMITATIONS)
    if (ctx.decider == "llm"
            and getattr(ctx, "selection_architecture", "") in {
                "sector_first_v0", "sector_first_simple_selector",
                "sector_first_no_flow"}):
        base = [
            item for item in base
            if "theme is a synthetic line" not in item
        ]
        extra = [
            item for item in extra
            if "fixed panel of the previous session's movers" not in item
            and "one sampled model call per day" not in item
        ]
        if ctx.selection_architecture == "sector_first_simple_selector":
            stage_note = (
                "sector_first_simple_selector replays B's frozen direction "
                "decision and uses a deterministic stock selector; only the "
                "shared trade planner is sampled")
        else:
            stage_note = (
                f"{ctx.selection_architecture} uses three sampled model stages "
                "at 09:00: direction selection, stock selection and the shared "
                "trade planner; one replay is not a distribution over them")
        extra.extend((
            stage_note,
            "sector membership is accepted only from the explicit PIT archive "
            "supplied to this run; the contract prevents current-only leakage "
            "but does not itself prove the provider's historical semantics",
            "selected directions are seeded into the existing theme gate "
            "without an independent trend score, so this window tests "
            "sector-first opportunity construction, not theme-gate alpha",
        ))
        if ctx.selection_architecture == "sector_first_no_flow":
            extra += (
                "direction-level structured fund-flow fields are ablated and "
                "direction news mentioning flow terms is removed; stock-level "
                "evidence remains unchanged so D isolates direction discovery",
            )
    if ctx.decider == "llm" and not getattr(ctx, "concepts", True):
        extra.append(CONCEPTS_ABLATED_LIMITATION)
    return tuple(base + extra)


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
    meta = ctx.corpus.instruments.get(code)
    return security_eligibility.reason(
        security_eligibility.SecurityFacts(
            code=code,
            listed=ctx.corpus.is_listed(code, day),
            known=meta is not None,
            is_st=bool(meta and meta["is_st"]),
            is_suspended=bool(meta and meta["is_suspended"]),
            has_prior_bar=code in ctx.corpus.bars(prev_day),
        ))


# ── the model-backed decider ────────────────────────────────────────────────


def _market_state(ctx, prev_day: str) -> dict:
    """The breadth of the previous session, from the corpus alone.

    Computed from ``daily_kline`` rather than read from
    ``market_breadth_snapshots``, and that is deliberate: the snapshot tables
    only begin 2026-09-08, so a window starting 2026-08-18 would have no
    breadth for its first fifteen sessions. Recomputing covers every session
    the corpus holds, and it cannot leak — the bar is dated, not timestamped.

    What this answers, and why a position decision needs it: "should I be
    buying today at all" is a different question on a session where 78% of
    names rose than on one where 8% did. Measured over the first replay
    window, the advancer share moved between 8.1% and 78.3% — the model was
    never told which kind of day it was in.
    """
    bars = ctx.corpus.bars(prev_day)
    changes = [float(r["change_pct"]) for r in bars.values()
               if r.get("change_pct") is not None]
    if not changes:
        return {}
    changes.sort()
    n = len(changes)
    advancers = sum(1 for c in changes if c > 0)
    median = (changes[n // 2] if n % 2 else
              (changes[n // 2 - 1] + changes[n // 2]) / 2)
    return {
        "prev_day": prev_day,
        "n": n,
        "advancers_pct": round(100 * advancers / n, 1),
        "median_change_pct": round(median, 2),
        "limit_up": sum(1 for c in changes if c >= 9.8),
        "limit_down": sum(1 for c in changes if c <= -9.8),
    }


def _resolve_concepts(ctx, prev_day: str, code: str) -> list[str]:
    """The concepts a name belongs to, or ``[]`` with the reason recorded.

    **Current membership, used knowingly.** ``stocks.db``'s
    ``concept_stocks`` has no as-of column (verified: two columns,
    ``concept_id`` and ``stock_code``), so a name's concept list is what it is
    *now*, not what it was on the replayed session. A concept assigned after
    the window therefore appears in it — a real lookahead, of a weaker kind
    than a price leak because it is a label rather than an outcome.

    It is included because sector membership is close to essential for A-share
    judgement — a name trading outside its 主线 is a different bet — and
    because the alternative available today is not "point-in-time concepts"
    but "no sector information at all". The honest middle is to include it
    *and* say so, which the panel does: the column is labelled 概念（当前成分）
    and the report carries the caveat. When a dated membership source exists,
    this function is the one place that changes.
    """
    try:
        from alpha_agents.data.stock_meta import concepts_for
        return list(concepts_for(code) or [])
    except Exception as exc:                          # noqa: BLE001
        ctx.counters["concepts_unavailable"] += 1
        logger.debug("%s: concepts unavailable for %s: %s", prev_day, code, exc)
        return []


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
    minimal_no_flow = getattr(ctx, "selection_architecture", "") == (
        "dual_rank_price_v1")
    concepts = {} if minimal_no_flow else _concepts_map(ctx)
    #: Both arms read the map: the ablation arm needs it to count what
    #: it removed, and the read touches no decision data.
    show_concepts = getattr(ctx, "concepts", True)
    limit_pool = {} if minimal_no_flow else _limit_pool_map(ctx, day)
    fund_flow = {} if minimal_no_flow else _fund_flow_map(ctx, prev_day)
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

    if not ranked:
        ctx.last_panel_candidate_pool = []
        return []

    # Ranked by change, as before — but the *pool* is widened before the cut
    # so the cut is no longer the only decision. Selection used to be
    # `top-N by T-1 change`, which meant the model could only ever pick
    # yesterday's biggest movers and the ranking, not the model, chose the
    # strategy. The pool is now the union of that ranking with the session's
    # most-traded names, so a quiet name with real turnover is offerable.
    by_change = sorted(ranked, key=lambda t: t[0], reverse=True)
    by_turnover = sorted(
        ranked, key=lambda t: (t[2].get("turnover_rate") or 0.0), reverse=True)

    # The historical behavior was strict alternation. It is now the explicit
    # default gene selection_rank.change_share=0.5, so changing the gene moves
    # this exact live path instead of an unrelated theme weight.
    ctx.last_panel_candidate_pool = selection_policy.candidate_pool_rows(
        by_change, by_turnover, lane_depth=limit * 4)
    chosen_codes = selection_policy.materialize_codes(
        ctx.last_panel_candidate_pool,
        limit=limit,
        params=None,
        eligible=lambda code: (
            (ctx.corpus.adv20(code, prev_day) or 0) > 0),
    )
    ctx.counters["panel_pool"] += min(
        limit * 4, len(by_change) + len(by_turnover))

    ranked_by_code = {
        code: (chg, row) for chg, code, row in ranked
    }
    panel: list[dict] = []
    for code in chosen_codes:
        chg, row = ranked_by_code[code]
        adv = ctx.corpus.adv20(code, prev_day)
        if not show_concepts and concepts.get(code):
            ctx.counters["concepts_ablated_rows"] += 1
        board = limit_pool.get(code) or {}
        panel.append({
            "code": code,
            "name": ctx.corpus.instruments[code]["name"],
            "close": float(row["close"]),
            "change_pct": round(chg, 2),
            "adv20": adv,
            "turnover_rate": round(float(row.get("turnover_rate") or 0.0), 2),
            # The ablation empties the column, not the explanation: the
            # header and the prompt still describe concepts, so the arms
            # differ by the data alone.
            "concepts": (concepts.get(code, []) if show_concepts else []),
            # Only present for names that were on the previous session's
            # 涨停 list. The column is blank for everything else, which is
            # the fact — not every name has a limit history.
            "consecutive_limits": board.get("consecutive_limits"),
            "limit_sector": board.get("sector"),
            "net_amount": (fund_flow.get(code) or {}).get("net_amount"),
        })
    ctx.counters["panel_ranked"] += len(by_change)
    # Names offered from *outside* the top `limit` by change. This is the
    # number that says the mix is real; it read 0 when the pool was
    # concatenated, which is how the defect above was found.
    top_by_change = {c for _, c, _ in by_change[:limit]}
    ctx.counters["panel_offered_beyond_top_change"] += sum(
        1 for p in panel if p["code"] not in top_by_change)
    return panel


def _fund_flow_map(ctx, prev_day: str) -> dict[str, dict]:
    """The previous session's whole-market fund flow, by code.

    Read from ``stock_fund_flow_daily``, which is **dated** — so the bound is
    the previous session and nothing later can be reached, unlike the
    timestamped snapshot tables. One query for the whole market rather than
    one per panel name.

    Empty for sessions the archive does not hold, which the panel renders as
    a blank column: "not archived" and "no fund flow" are different claims.
    """
    try:
        from alpha_agents.data import stock_meta
        return stock_meta.fund_flow_as_of(prev_day)
    except Exception as exc:                          # noqa: BLE001
        ctx.counters["fund_flow_unavailable"] += 1
        logger.debug("%s: fund flow unavailable: %s", prev_day, exc)
        return {}


def _limit_pool_map(ctx, day: str) -> dict[str, dict]:
    """The previous session's 涨停 list, as knowable at 09:00 on ``day``.

    Read from `limit_pool_snapshots`, which is **timestamped** rather than
    dated, so the cutoff is an instant and not a day. A decision at 09:00 may
    only use captures at or before it — and in this corpus no capture exists
    before 10:56 on any session, so "the latest snapshot today" would always
    be one taken after the decision. The previous session's evening captures
    are the ones a 09:00 decision could actually have seen.

    Empty for sessions the snapshot tables do not cover (they begin
    2026-09-08), which the panel renders as a blank column rather than as
    "not on the limit list" — the two are different claims.
    """
    try:
        from alpha_agents.data import stock_meta
        return stock_meta.limit_pool_as_of(f"{day} 09:00:00")
    except Exception as exc:                          # noqa: BLE001
        ctx.counters["limit_pool_unavailable"] += 1
        logger.debug("%s: limit pool unavailable: %s", day, exc)
        return {}


def _concepts_map(ctx) -> dict[str, list[str]]:
    """Concept membership for the panel, read once per run.

    Cached on the context because the file does not change inside a window and
    a read per day would be a query per simulated session for data that is
    ~3.7k rows. Read through the corpus (read-only when shared) like every
    other corpus file.
    """
    cached = getattr(ctx, "_concepts", None)
    if cached is None:
        from alpha_agents.data import stock_meta
        cached = stock_meta.concepts_by_code()
        ctx._concepts = cached
        if cached:
            logger.info("Concept membership: %d code(s) tagged", len(cached))
        else:
            logger.warning(
                "Concept membership is empty — the panel's 概念 column will "
                "be blank. stocks.db may not be shared into this replay.")
    return cached


def _news_window(day: str, prev_day: str, limit: int,
                 phase: str = "open") -> list[dict]:
    """Flash news knowable at ``phase`` on ``day``.

    ``"open"`` reads the overnight window ending 09:00; ``"close"`` reads the
    session's own window ending 14:55. The cutoff has to move with the phase
    or the prompt lies: the close context tells the agent it is 14:55 and
    that it can see the day, and then handing it news from 09:00 would
    contradict the sentence directly above the list.

    The window is the whole as-of statement: news is continuous, so "the
    latest N items" would silently drop whatever arrived beyond N and re-read
    what it already saw. ``as_of`` and ``since`` are both full timestamps,
    which is what makes ``read_news`` honour them as instants rather than
    widening them to the day.
    """
    from alpha_agents.data.snapshot_store import read_news
    if phase == "close":
        as_of, since = f"{day} 14:55:00", f"{day} 09:00:00"
    else:
        as_of, since = f"{day} 09:00:00", f"{prev_day} 15:00:00"
    try:
        return read_news(None, as_of=as_of, since=since, limit=limit)
    except Exception as exc:                          # noqa: BLE001
        logger.warning("%s: news window unavailable: %s", day, exc)
        return []


def _book_and_knowledge(
        ctx, day: str, phase: str = "open", *,
        include_learning: bool = True) -> tuple[str, str]:
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
    # Marks come from the previous session's close — the last price that
    # exists at 09:00 on ``day``. Reading today's bar would be the future
    # leak this runner exists to avoid, and reading no price at all is what
    # it did before: the agent was asked "should you sell" while unable to
    # see whether the position was up or down.
    # The mark follows the moment, exactly as the fill price does. At 09:00
    # the last price that exists is T-1's close. At 14:55 today's close
    # exists, and showing the agent yesterday's mark while telling it the
    # session is over would give it a stale P&L to reason from — a position
    # down 8% today would read as down 8% yesterday.
    price_map: dict[str, float] = {}
    if phase == "close":
        for code, row in ctx.corpus.bars(day).items():
            close = row.get("close")
            if close:
                price_map[code] = float(close)
    else:
        prev = ctx.corpus.previous(day)
        if prev:
            for code, row in ctx.corpus.bars(prev).items():
                close = row.get("close")
                if close:
                    price_map[code] = float(close)
    try:
        book = feedback.inject_portfolio(trader_id=ctx.trader,
                                         price_map=price_map)
    except Exception as exc:                          # noqa: BLE001
        logger.warning("portfolio context unavailable: %s", exc)
        book = ""
    if not include_learning:
        return book, ""
    parts = []
    for fn in (feedback.inject_sentiment, feedback.inject_principles,
               feedback.inject_playbooks):
        try:
            parts.append(fn())
        except Exception as exc:                          # noqa: BLE001
            logger.warning("%s unavailable: %s", fn.__name__, exc)
    parts.append(_knowledge_block(ctx, day))
    return book, "\n\n".join(p for p in parts if p)


def _load_prompt_text(selection_architecture: str = "dual_rank_v0") -> str:
    """The stock decider prompt, frozen once per replay window."""
    from alpha_agents.agents import t1_decider
    if selection_architecture in {
            "sector_first_v0", "sector_first_simple_selector",
            "sector_first_no_flow"}:
        return t1_decider.load_prompt(
            PROMPTS_DIR / "sector_trade_plan.md")
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


def _event_snapshot_refs(panel: list[dict], cutoff: str) -> list[dict]:
    """PIT event-vintage hashes that were part of this decision world.

    Missing event tables are a capability gap, not a trading failure. The
    journal stores an empty list and the run's Capability Matrix tells the
    reader whether event expectations existed in that historical window.
    """
    try:
        from alpha_agents.data import event_expectations as EE
        return EE.snapshot_refs(
            as_of=cutoff,
            subjects=[row["code"] for row in panel if row.get("code")],
            days_back=30,
            days_ahead=30)
    except Exception as exc:                          # noqa: BLE001
        logger.debug("%s: event snapshot refs unavailable: %s", cutoff, exc)
        return []


def _trader_tools(ctx):
    """The six question-shaped tools the buy-side decider may call, or [].

    Empty when the run was told not to give the model tools, which keeps the
    old bare-picker behaviour reachable for a comparison run.

    Imported lazily: a placeholder run makes no model calls and must not pull
    the ``agents`` SDK in through the tool wrappers.
    """
    if not getattr(ctx, "trader_tools", False):
        return []
    from alpha_agents.tools.trader_tools import TRADER_TOOLS
    return TRADER_TOOLS


def _sector_cards(ctx, day: str, ranking_day: str) -> tuple:
    """Build the PIT direction world and its transparent top-8 shortlist."""
    cutoff = f"{day} 09:00:00"
    membership = sector_membership.as_of(
        ctx.sector_membership_archive, cutoff)
    index = ctx.corpus.index.get(ranking_day)
    if index is None:
        raise ValueError(f"ranking day {ranking_day} is outside the corpus")
    sessions = ctx.corpus.days[max(0, index - 20):index + 1]
    bars_by_day = {session: ctx.corpus.bars(session) for session in sessions}
    flow = (
        None if ctx.selection_architecture in {
            "sector_first_no_flow", "sector_rank_price_v1"}
        else _fund_flow_map(ctx, ranking_day)
    )
    snapshots = sector_selection.build_sector_snapshots(
        membership=membership,
        decision_at=cutoff,
        as_of_session=ranking_day,
        sessions=sessions,
        bars_by_day=bars_by_day,
        market_codes=set(ctx.corpus.bars(ranking_day)),
        fund_flow_by_code=flow,
        strict_pit=True,
    )
    ranked = sector_selection.rank_sector_snapshots(snapshots)
    by_sector = {snapshot.sector_id: snapshot for snapshot in snapshots}
    cards = []
    for rank_row in ranked:
        snapshot = by_sector[rank_row["sector_id"]].compact()
        snapshot.update(rank_row)
        cards.append(snapshot)
    shortlist = [
        row for row in cards if row.get("rank") is not None
    ][:8]
    return membership, cards, shortlist


def _build_sector_panel(ctx, day: str, ranking_day: str, membership,
                        selected: list[str], limit: int) -> list[dict]:
    """Build stocks only after directions have been selected."""
    bars = ctx.corpus.bars(ranking_day)
    minimal_no_flow = getattr(ctx, "selection_architecture", "") == (
        "sector_rank_price_v1")
    limit_pool = {} if minimal_no_flow else _limit_pool_map(ctx, day)
    fund_flow = {} if minimal_no_flow else _fund_flow_map(ctx, ranking_day)
    concepts = (
        {} if minimal_no_flow
        else sector_membership.concepts_by_code(membership)
    )
    index = ctx.corpus.index.get(ranking_day)
    if index is None:
        raise ValueError(f"ranking day {ranking_day} is outside the corpus")
    if minimal_no_flow:
        leave_one_out = {}
    else:
        loo_sessions = ctx.corpus.days[max(0, index - 5):index + 1]
        loo_bars = {
            session: ctx.corpus.bars(session) for session in loo_sessions}
        leave_one_out = sector_selection.candidate_leave_one_out_5d(
            membership=membership,
            decision_at=f"{day} 09:00:00",
            as_of_session=ranking_day,
            sessions=loo_sessions,
            bars_by_day=loo_bars,
            market_codes=set(ctx.corpus.bars(ranking_day)),
            strict_pit=True,
        )

    candidate_codes = set()
    for sector in selected:
        candidate_codes.update(membership.members.get(sector, ()))

    candidates = {}
    raw_rows = {}
    for code in sorted(candidate_codes):
        reason = _eligibility(ctx, code, day, ranking_day)
        if reason is not None:
            ctx.counters[f"sector_eligibility:{reason}"] += 1
            continue
        row = bars.get(code) or {}
        change = row.get("change_pct")
        close = row.get("close")
        if change is None or not close or float(close) <= 0:
            continue
        if float(change) >= LIMIT_UP_SKIP_PCT:
            continue
        adv = ctx.corpus.adv20(code, ranking_day)
        if not adv or adv <= 0:
            ctx.counters["sector_eligibility:no_adv20"] += 1
            continue
        candidates[code] = {
            "code": code,
            "change_pct": float(change),
            "turnover_rate": float(row.get("turnover_rate") or 0.0),
        }
        raw_rows[code] = (row, adv)

    materialized = sector_panel.materialize(
        candidates=candidates,
        selected_sectors=selected,
        members=membership.members,
        limit=limit,
    )
    panel = []
    for item in materialized:
        code = item["code"]
        row, adv = raw_rows[code]
        board = limit_pool.get(code) or {}
        peer = (
            leave_one_out.get(item["primary_theme"], {}).get(code, {})
        )
        primary_relation = sector_membership.relation_evidence_id(
            membership, sector_id=item["primary_theme"], code=code)
        supporting_relations = [
            sector_membership.relation_evidence_id(
                membership, sector_id=theme, code=code)
            for theme in item["supporting_themes"]
        ]
        eligible_themes = [
            item["primary_theme"], *item["supporting_themes"]]
        relation_map = {
            item["primary_theme"]: primary_relation,
            **dict(zip(item["supporting_themes"], supporting_relations)),
        }
        panel.append({
            "code": code,
            "name": ctx.corpus.instruments[code]["name"],
            "close": float(row["close"]),
            "change_pct": round(float(row["change_pct"]), 2),
            "adv20": adv,
            "turnover_rate": round(float(row.get("turnover_rate") or 0.0), 2),
            "concepts": concepts.get(code, []),
            "primary_theme": item["primary_theme"],
            "supporting_themes": item["supporting_themes"],
            "eligible_themes": eligible_themes,
            "theme_relation_evidence_ids": relation_map,
            "membership_snapshot_id": membership.snapshot_id,
            "membership_hash": membership.content_hash,
            "primary_theme_relation_evidence_id": primary_relation,
            "supporting_theme_relation_evidence_ids": supporting_relations,
            "primary_theme_peer_covered": peer.get("peer_covered"),
            "primary_theme_peer_total": peer.get("peer_total"),
            "primary_theme_peer_5d_median_pct": peer.get(
                "peer_5d_median_pct"),
            "primary_theme_peer_relative_5d_pct": peer.get(
                "peer_relative_5d_pct"),
            "consecutive_limits": board.get("consecutive_limits"),
            "limit_sector": board.get("sector"),
            "net_amount": (fund_flow.get(code) or {}).get("net_amount"),
        })

    ctx.last_panel_candidate_pool = []
    ctx.last_sector_candidate_pool = [
        candidates[code] for code in sorted(candidates)]
    return panel


_FLOW_NEWS_TERMS = (
    "主力资金", "资金净流入", "资金流向", "净流入", "净买入",
    "北向资金", "南向资金",
)


def _direction_news(ctx, news: list[dict]) -> list[dict]:
    """The D arm cannot read structured flow back through direction news."""
    if ctx.selection_architecture != "sector_first_no_flow":
        return news
    out = []
    for row in news:
        text = f"{row.get('title') or ''} {row.get('content') or ''}"
        if any(term in text for term in _FLOW_NEWS_TERMS):
            continue
        out.append(row)
    return out


_MINIMAL_PLANNER_FIELDS = (
    "code", "name", "close", "change_pct", "adv20", "turnover_rate",
)


def _minimal_planner_panel(
        panel: list[dict], *, limit: int) -> list[dict]:
    """Closed planner view shared by CONTROL and SECTOR.

    Theme names, peer metrics, concepts, limit-sector labels and fund flow are
    deliberately absent. The SECTOR arm may use PIT themes to *discover* the
    candidates, but the shared planner cannot receive extra explanatory data.
    """
    if limit <= 0:
        return []
    return [
        {key: row.get(key) for key in _MINIMAL_PLANNER_FIELDS}
        for row in panel[:limit]
    ]


def _price_sector_stage(ctx, day: str, ranking_day: str) -> list[dict]:
    """Transparent top-3 price/breadth directions, no model and no flow."""
    from alpha_agents.data import theme_opportunity_journal as TOJ

    membership, cards, shortlist = _sector_cards(ctx, day, ranking_day)
    selected = [
        row["sector_id"] for row in shortlist[:3]
        if row.get("sector_id")
    ]
    cutoff = f"{day} 09:00:00"
    try:
        TOJ.record(
            run_id=str(ctx.run_id),
            trader_id=ctx.trader,
            day=day,
            phase="open",
            information_cutoff=cutoff,
            architecture=ctx.selection_architecture,
            snapshots=cards,
            shortlist=[row["sector_id"] for row in shortlist],
            selected=selected,
            research={
                "method": "transparent_price_breadth_top3",
                "selected_ids": selected,
            },
            refusals=[],
            parse_error=None,
        )
    except Exception as exc:                          # noqa: BLE001
        ctx.counters["theme_opportunity_journal_errors"] += 1
        logger.warning("%s: minimal theme journal failed: %s", day, exc)

    ctx.last_sector_context = {
        "membership_snapshot_id": membership.snapshot_id,
        "membership_hash": membership.content_hash,
        "shortlist": [row["sector_id"] for row in shortlist],
        "selected_themes": selected,
        "direction_research": [],
        "snapshot_hashes": {
            row["sector_id"]: row.get("snapshot_hash")
            for row in cards
        },
        "direction_trace": {
            "status": "selected" if selected else "empty",
            "method": "transparent_price_breadth_top3",
            "model_elapsed_ms": 0,
            "refused": 0,
        },
    }
    if not selected:
        return []
    return _build_sector_panel(
        ctx, day, ranking_day, membership, selected, ctx.panel_size)


def _sector_first_stage(ctx, day: str, ranking_day: str,
                        market: dict, news: list[dict]) -> tuple[list[dict], object]:
    """Resolve directions, journal them, then materialize the stock panel."""
    from alpha_agents.agents import sector_selector
    from alpha_agents.data import theme_opportunity_journal as TOJ
    from alpha_agents.tools.budget import ResearchBudget

    membership, cards, shortlist = _sector_cards(ctx, day, ranking_day)
    shortlist_ids = [row["sector_id"] for row in shortlist]
    cutoff = f"{day} 09:00:00"
    budget = None

    if ctx.selection_architecture == "sector_first_simple_selector":
        frozen = frozen_direction_archive.decision_for(
            ctx.frozen_directions,
            day=day,
            information_cutoff=cutoff,
            membership_snapshot_id=membership.snapshot_id,
            membership_hash=membership.content_hash,
            shortlist=shortlist_ids,
        )
        selected = list(frozen["selected"])
        verdict = {
            "themes": [{"sector_id": value} for value in selected],
            "refused": [],
            "parse_error": None,
            "frozen_from_run": frozen["source_run_id"],
            "frozen_archive_hash": frozen["archive_hash"],
        }
        research = {
            "themes": selected,
            "frozen_from_run": frozen["source_run_id"],
            "frozen_archive_hash": frozen["archive_hash"],
        }
    else:
        budget = ResearchBudget() if ctx.trader_tools else None
        verdict = sector_selector.propose_sync(
            day=day,
            as_of_session=ranking_day,
            sectors=shortlist,
            market=market,
            news=_direction_news(ctx, news),
            model=ctx.model,
            loop=ctx.loop,
            tools=[],
            research_budget=budget,
            max_turns=sector_selector.DEFAULT_MAX_TURNS,
        )
        selected = [
            row["sector_id"] for row in verdict.get("themes") or []
        ]
        research = {
            "themes": list(verdict.get("themes") or []),
            "selected_ids": selected,
        } if selected else None

    refusals = [
        {
            "sector_id": row.get("sector_id"),
            "reason": (
                f"{row.get('why') or ''}: {row.get('detail') or ''}"
            ).strip(": "),
        }
        for row in verdict.get("refused") or []
    ]

    try:
        TOJ.record(
            run_id=str(ctx.run_id),
            trader_id=ctx.trader,
            day=day,
            phase="open",
            information_cutoff=cutoff,
            architecture=ctx.selection_architecture,
            snapshots=cards,
            shortlist=shortlist_ids,
            selected=selected,
            research=research,
            refusals=refusals,
            parse_error=verdict.get("parse_error"),
        )
    except Exception as exc:                          # noqa: BLE001
        ctx.counters["theme_opportunity_journal_errors"] += 1
        logger.warning("%s: theme opportunity journal failed: %s", day, exc)

    ctx.last_sector_context = {
        "membership_snapshot_id": membership.snapshot_id,
        "membership_hash": membership.content_hash,
        "shortlist": shortlist_ids,
        "selected_themes": selected,
        "direction_research": list(verdict.get("themes") or []),
        "snapshot_hashes": {
            row["sector_id"]: row.get("snapshot_hash")
            for row in cards
        },
        "frozen_direction_source_run": verdict.get("frozen_from_run"),
        "frozen_direction_archive_hash": verdict.get("frozen_archive_hash"),
        "direction_trace": {
            "status": (
                "unreadable" if verdict.get("parse_error")
                else ("selected" if selected else "empty")
            ),
            "model_elapsed_ms": verdict.get("model_elapsed_ms"),
            "refused": len(verdict.get("refused") or []),
        },
    }

    if verdict.get("parse_error"):
        ctx.counters["sector_selector_unreadable"] += 1
        logger.warning(
            "%s: sector selector unreadable: %s",
            day, verdict["parse_error"])
        return [], budget
    if not selected:
        ctx.counters["sector_selector_empty"] += 1
        return [], budget

    for sector in selected:
        upsert_theme(
            sector,
            status="watching",
            strength=3,
            daily_score=1,
            catalyst=f"{ctx.selection_architecture} selected direction",
            notes=f"PIT membership snapshot {membership.snapshot_id}",
        )
    ctx.counters["sector_selected"] += len(selected)
    panel = _build_sector_panel(
        ctx, day, ranking_day, membership, selected, ctx.panel_size)
    return panel, budget



def _sector_stock_choice(ctx, *, day: str, prev_day: str,
                         panel: list[dict], news: list[dict],
                         market: dict, book: str, knowledge: str,
                         research_budget) -> dict:
    """B/D use the model; C swaps only this choice for a transparent rule."""
    from alpha_agents.agents import sector_stock_selector

    if ctx.selection_architecture == "sector_first_simple_selector":
        return sector_stock_selector.simple(panel, picks=ctx.picks)
    return sector_stock_selector.propose_sync(
        day=day,
        prev_day=prev_day,
        panel=panel,
        news=news,
        market=market,
        book=book,
        knowledge=knowledge,
        trader_note=ctx.trader_note,
        picks=ctx.picks,
        model=ctx.model,
        loop=ctx.loop,
        tools=_trader_tools(ctx),
        research_budget=research_budget,
        max_turns=min(
            ctx.max_turns or sector_stock_selector.DEFAULT_MAX_TURNS,
            sector_stock_selector.DEFAULT_MAX_TURNS),
    )


def _sector_trade_plan(ctx, *, day: str, prev_day: str,
                       panel: list[dict], news: list[dict],
                       market: dict, book: str, knowledge: str,
                       research_packet_payload: dict) -> dict:
    """Shared B/C/D order planner. It cannot research or widen the stock set."""
    from alpha_agents.agents import t1_decider

    if not panel:
        return {
            "orders": [], "refused": [], "raw": "",
            "parse_error": None, "research_budget": None,
        }
    return t1_decider.propose_sync(
        day=day,
        prev_day=prev_day,
        panel=panel,
        news=news,
        book=book,
        knowledge=knowledge,
        market=market,
        trader_note=ctx.trader_note,
        picks=len(panel),
        template=ctx.prompt,
        model=ctx.model,
        loop=ctx.loop,
        phase="open",
        tools=[],
        max_turns=ctx.max_turns,
        research_budget=None,
        research_packet=research_packet_payload,
    )

def _validate_sector_order_relations(
        ctx, panel: list[dict], orders: list[dict]) -> tuple[list[dict], list[dict]]:
    """Re-prove every Sector-First order through the shared relation gate."""
    by_code = {str(row.get("code") or ""): row for row in panel}
    accepted: list[dict] = []
    refused: list[dict] = []
    for order in orders:
        code = str(order.get("code") or "").strip()
        row = by_code.get(code)
        if row is None:
            detail = "outside_panel"
        else:
            try:
                research_packet.validate_relation_row(
                    ctx.sector_membership_archive, row)
            except research_packet.ResearchPacketError as exc:
                detail = str(exc)
            else:
                accepted.append(order)
                continue
        refused.append({
            "code": code,
            "why": "theme_unresolved",
            "detail": detail[:500],
            "stage": "relation_validation",
        })
    return accepted, refused


def _decision_world_read_set(
        ctx, *, day: str, ranking_day: str, phase: str,
        panel: list[dict], news: list[dict]) -> dict:
    """Freeze the actual source identities consumed by one buy decision."""
    cutoff = f"{day} {'14:55:00' if phase == 'close' else '09:00:00'}"

    input_identity = getattr(
        ctx, "input_identity", {"input_hash": "unbound"})
    price_ref = world_read_set.fact_ref(
        {
            "source": "market_history.db:daily_kline",
            "session": ranking_day,
            "input_hash": input_identity["input_hash"],
        },
        available_at=f"{ranking_day} 15:00:00",
    )

    membership_ref = None
    membership_archive = getattr(ctx, "sector_membership_archive", ())
    if membership_archive:
        membership = sector_membership.as_of(
            membership_archive, cutoff)
        membership_ref = {
            "snapshot_id": membership.snapshot_id,
            "content_hash": membership.content_hash,
            "available_at": membership.available_at,
        }

    # The local stocks table stores today's ST/suspension flags. Keep the
    # consumed status fact in the read set, but grade it C rather than letting
    # a historical replay silently claim it knew the historical status.
    security_ref = world_read_set.fact_ref(
        {
            "source": "stocks.db:stocks",
            "input_hash": input_identity["input_hash"],
            "codes": sorted(
                str(row.get("code") or "")
                for row in panel if row.get("code")),
        },
        point_in_time_grade="C",
        strict_replay_eligible=False,
    )

    flow_rows = [
        {"code": row.get("code"), "net_amount": row.get("net_amount")}
        for row in panel if row.get("net_amount") is not None
    ]
    fund_refs = []
    if flow_rows:
        fund_refs.append(world_read_set.fact_ref(
            {
                "source": "stock_fund_flow_daily",
                "session": ranking_day,
                "rows": flow_rows,
            },
            available_at=f"{ranking_day} 15:00:00",
            point_in_time_grade="B",
            strict_replay_eligible=False,
        ))

    minimal_mode = getattr(ctx, "selection_architecture", "") in {
        "dual_rank_price_v1", "sector_rank_price_v1",
    }
    event_refs = [] if minimal_mode else [
        {
            **dict(ref),
            "point_in_time_grade": "A",
            "strict_replay_eligible": True,
        }
        for ref in _event_snapshot_refs(panel, cutoff)
    ]

    extra_refs = []
    if news:
        extra_refs.append(world_read_set.fact_ref(
            {
                "source": "decision_news_window",
                "rows": [
                    {
                        "time": row.get("time"),
                        "source": row.get("source"),
                        "title": row.get("title"),
                    }
                    for row in news
                ],
            },
            point_in_time_grade="U",
            strict_replay_eligible=False,
        ))

    read_set = world_read_set.build(
        cutoff=cutoff,
        ranking_session=ranking_day,
        source_identity_hash=input_identity["input_hash"],
        code_ref=getattr(ctx, "code_ref", None) or "unbound",
        policy_ref=getattr(ctx, "policy_ref", None) or "unbound",
        membership=membership_ref,
        security_status=security_ref,
        price_refs=[price_ref],
        event_refs=event_refs,
        fund_flow_refs=fund_refs,
        extra_refs=extra_refs,
    )
    world_read_set.require_valid(read_set, strict=False)
    return read_set


def _decide_llm(ctx, day: str, prev_day: str,
                phase: str = "open") -> list[dict]:
    """Model-backed buy decision through the declared selection architecture."""
    from alpha_agents.agents import t1_decider

    ranking_day = day if phase == "close" else prev_day
    market = _market_state(ctx, prev_day)
    minimal_mode = ctx.selection_architecture in {
        "dual_rank_price_v1", "sector_rank_price_v1",
    }
    if minimal_mode and phase != "open":
        raise RuntimeError(
            "nf_discovery_v1 supports the reproducible 09:00 buy path only")
    news = (
        [] if minimal_mode
        else _news_window(day, prev_day, ctx.news_limit, phase)
    )
    if market:
        ctx.counters["market_state_days"] += 1

    ctx.last_sector_context = {}
    shared_budget = None
    sector_mode = ctx.selection_architecture in {
        "sector_first_v0", "sector_first_simple_selector",
        "sector_first_no_flow",
    }
    if minimal_mode:
        if ctx.selection_architecture == "sector_rank_price_v1":
            discovered = _price_sector_stage(ctx, day, ranking_day)
        else:
            discovered = _build_panel(
                ctx, day, ranking_day, ctx.panel_size)
        panel = _minimal_planner_panel(discovered, limit=ctx.picks)
    elif sector_mode:
        if phase != "open":
            raise RuntimeError(
                f"{ctx.selection_architecture} currently supports the strict "
                "09:00 buy path only")
        panel, shared_budget = _sector_first_stage(
            ctx, day, ranking_day, market, news)
    else:
        panel = _build_panel(ctx, day, ranking_day, ctx.panel_size)
        # A formal A/B/C/D comparison promises one shared research budget.
        # The incumbent path historically ran without one; keep that behavior
        # for ordinary dual_rank_v0 runs, but bind the preregistered A arm to
        # the same finite budget used by the Sector-First stock selector.
        if getattr(ctx, "experiment_manifest", None) is not None:
            from alpha_agents.tools.budget import ResearchBudget
            shared_budget = ResearchBudget()

    if phase == "close":
        unavailable = _unavailable_codes(ctx)
        before = len(panel)
        panel = [row for row in panel if row["code"] not in unavailable]
        ctx.counters["close_panel_held_removed"] += before - len(panel)

    ctx.counters["panel_size"] += len(panel)
    if not panel:
        logger.info("%s: empty panel, nothing to decide", day)
        return []

    book, knowledge = _book_and_knowledge(
        ctx, day, phase, include_learning=not minimal_mode)
    stock_choice = None
    planner_panel = panel

    if sector_mode:
        stock_choice = _sector_stock_choice(
            ctx,
            day=day,
            prev_day=prev_day,
            panel=panel,
            news=news,
            market=market,
            book=book,
            knowledge=knowledge,
            research_budget=shared_budget,
        )
        choice_error = stock_choice.get("parse_error")
        if choice_error:
            verdict = {
                "orders": [],
                "refused": stock_choice.get("refused") or [],
                "raw": stock_choice.get("raw") or "",
                "parse_error": f"stock_selector: {choice_error}",
                "research_budget": stock_choice.get("research_budget"),
                "research_trace": stock_choice.get("research_trace") or [],
                "model_elapsed_ms": stock_choice.get("model_elapsed_ms"),
            }
            planner_panel = []
        else:
            cutoff = f"{day} 09:00:00"
            try:
                planner_panel = research_packet.apply_stock_choices(
                    panel, stock_choice.get("stocks") or [])
            except research_packet.ResearchPacketError as exc:
                # A selector chose a theme whose frozen membership relation
                # cannot be proven. That is a structured decision refusal,
                # not an unreadable model reply or infrastructure failure.
                code = str(
                    ((stock_choice.get("stocks") or [{}])[0]).get("code")
                    or "")
                packet = None
                planner_panel = []
                verdict = {
                    "orders": [],
                    "refused": (
                        list(stock_choice.get("refused") or [])
                        + [{
                            "code": code,
                            "why": "theme_unresolved",
                            "detail": str(exc)[:500],
                            "stage": "research_packet_validation",
                        }]
                    ),
                    "raw": stock_choice.get("raw") or "",
                    "parse_error": None,
                    "research_budget": stock_choice.get("research_budget"),
                    "research_trace": stock_choice.get("research_trace") or [],
                }
            else:
                from alpha_agents.data import policy_registry
                try:
                    packet = research_packet.build(
                        day=day,
                        cutoff=cutoff,
                        architecture=ctx.selection_architecture,
                        policy_ref=policy_registry.active_ref(),
                        membership_snapshot_id=str(
                            ctx.last_sector_context.get(
                                "membership_snapshot_id") or ""),
                        membership_hash=str(
                            ctx.last_sector_context.get(
                                "membership_hash") or ""),
                        directions=list(
                            ctx.last_sector_context.get(
                                "direction_research") or []),
                        selected_panel=planner_panel,
                        stock_choices=list(stock_choice.get("stocks") or []),
                        research_trace=list(
                            stock_choice.get("research_trace") or []),
                        budget=stock_choice.get("research_budget"),
                        event_snapshot_refs=_event_snapshot_refs(
                            planner_panel, cutoff),
                    )
                    research_packet.require_valid(
                        packet,
                        cutoff=cutoff,
                        membership_snapshot_id=str(
                            ctx.last_sector_context.get(
                                "membership_snapshot_id") or ""),
                        membership_hash=str(
                            ctx.last_sector_context.get(
                                "membership_hash") or ""),
                    )
                except research_packet.ResearchPacketError as exc:
                    packet = None
                    planner_panel = []
                    verdict = {
                        "orders": [],
                        "refused": list(stock_choice.get("refused") or []),
                        "raw": stock_choice.get("raw") or "",
                        "parse_error": f"research_packet: {exc}",
                        "research_budget": stock_choice.get("research_budget"),
                        "research_trace":
                            stock_choice.get("research_trace") or [],
                    }
                else:
                    hard_refusals = research_packet.deterministic_refusals(
                        packet)
                    blocked = {
                        row["code"]
                        for row in hard_refusals if row.get("code")
                    }
                    planner_allowed = [
                        row for row in planner_panel
                        if row.get("code") not in blocked
                    ]
                    if planner_allowed:
                        verdict = _sector_trade_plan(
                            ctx,
                            day=day,
                            prev_day=prev_day,
                            panel=planner_allowed,
                            news=news,
                            market=market,
                            book=book,
                            knowledge=knowledge,
                            research_packet_payload=packet,
                        )
                    else:
                        verdict = {
                            "orders": [], "refused": [], "raw": "",
                            "parse_error": None, "research_budget": None,
                            "research_trace": [], "model_elapsed_ms": None,
                        }
                    verdict["research_packet"] = packet
                    verdict["research_budget"] = stock_choice.get(
                        "research_budget")
                    verdict["research_trace"] = stock_choice.get(
                        "research_trace") or []
                    verdict["refused"] = (
                        list(stock_choice.get("refused") or [])
                        + hard_refusals
                        + list(verdict.get("refused") or [])
                    )
    else:
        verdict = t1_decider.propose_sync(
            day=day,
            prev_day=prev_day,
            panel=panel,
            news=news,
            book=book,
            knowledge=knowledge,
            market=market,
            trader_note=ctx.trader_note,
            picks=ctx.picks,
            template=ctx.prompt,
            model=ctx.model,
            loop=ctx.loop,
            phase=phase,
            tools=[] if minimal_mode else _trader_tools(ctx),
            max_turns=1 if minimal_mode else ctx.max_turns,
            research_budget=None if minimal_mode else shared_budget,
        )

    if sector_mode and not verdict.get("parse_error"):
        valid_orders, relation_refusals = _validate_sector_order_relations(
            ctx, planner_panel, verdict.get("orders") or [])
        verdict["orders"] = valid_orders
        if relation_refusals:
            verdict["refused"] = (
                list(verdict.get("refused") or []) + relation_refusals
            )

    ctx.last_world_read_set = _decision_world_read_set(
        ctx, day=day, ranking_day=ranking_day, phase=phase,
        panel=(planner_panel if sector_mode else panel), news=news)
    world_hashes = getattr(ctx, "world_read_set_hashes", None)
    if world_hashes is None:
        world_hashes = []
        ctx.world_read_set_hashes = world_hashes
    world_hashes.append(ctx.last_world_read_set["read_set_hash"])

    try:
        from alpha_agents.data import opportunity_journal as OJ
        cutoff = f"{day} {'14:55:00' if phase == 'close' else '09:00:00'}"
        stock_preselection = (
            [row["code"] for row in (stock_choice or {}).get("stocks") or []]
            if sector_mode else None
        )
        OJ.record_decision(
            run_id=str(ctx.run_id),
            trader_id=ctx.trader,
            day=day,
            phase=phase,
            information_cutoff=cutoff,
            panel=panel,
            orders=verdict.get("orders") or [],
            refusals=verdict.get("refused") or [],
            research={
                **(verdict.get("research_budget") or {}),
                "packet_hash": (
                    (verdict.get("research_packet") or {}).get("packet_hash")),
            } if (
                verdict.get("research_budget")
                or verdict.get("research_packet")
            ) else None,
            parse_error=verdict.get("parse_error"),
            raw=verdict.get("raw") or "",
            context={
                "run_theme": ctx.theme,
                "ranking_day": ranking_day,
                "market": market,
                "candidate_pool": getattr(
                    ctx, "last_panel_candidate_pool", []),
                "panel_limit": ctx.panel_size,
                "selection_architecture": ctx.selection_architecture,
                "selection_rank": (
                    selection_policy.in_force_params()
                    if ctx.selection_architecture == "dual_rank_v0"
                    else ({
                        "method": "whole_market_change_turnover_v1"
                    } if ctx.selection_architecture == "dual_rank_price_v1"
                    else None)),
                "sector_first": getattr(
                    ctx, "last_sector_context", {}),
                "stock_preselection": stock_preselection,
                "stock_preselection_mode": (
                    "transparent_panel_prefix"
                    if ctx.selection_architecture
                    == "sector_first_simple_selector"
                    else ("llm" if sector_mode else None)),
                "planner_panel": [
                    row["code"] for row in planner_panel
                ] if sector_mode else None,
                "event_snapshot_refs": (
                    [] if minimal_mode
                    else _event_snapshot_refs(panel, cutoff)
                ),
                "research_packet": verdict.get("research_packet"),
                "research_trace": verdict.get("research_trace") or [],
                "world_read_set": ctx.last_world_read_set,
                "decision_trace": {
                    "direction": (
                        getattr(ctx, "last_sector_context", {}).get(
                            "direction_trace")
                        if sector_mode else None
                    ),
                    "stock": ({
                        "status": (
                            "unreadable" if (stock_choice or {}).get("parse_error")
                            else ("selected" if (stock_choice or {}).get("stocks")
                                  else "empty")
                        ),
                        "model_elapsed_ms": (
                            (stock_choice or {}).get("model_elapsed_ms")),
                        "refused": len(
                            (stock_choice or {}).get("refused") or []),
                    } if sector_mode else None),
                    "planner": {
                        "status": (
                            "unreadable" if verdict.get("parse_error")
                            else ("ordered" if verdict.get("orders") else "empty")
                        ),
                        "model_elapsed_ms": verdict.get("model_elapsed_ms"),
                        "refused": len(verdict.get("refused") or []),
                    },
                },
            })
    except Exception as exc:                          # noqa: BLE001
        ctx.counters["opportunity_journal_errors"] += 1
        logger.warning("%s %s: opportunity journal failed: %s",
                       day, phase, exc)

    research = verdict.get("research_budget")
    if research:
        ctx.counters["research_tool_calls"] += int(research.get("used") or 0)
        ctx.counters["research_budget_denied"] += int(
            research.get("denied") or 0)
        ctx.counters["research_deep_dive_names"] += len(
            research.get("deep_dive_names") or [])

    if verdict["parse_error"]:
        ctx.counters["decider_unreadable"] += 1
        logger.warning("%s: decider reply unreadable: %s",
                       day, verdict["parse_error"])
        return []

    for refusal in verdict["refused"]:
        why = refusal.get("why") or "unknown"
        ctx.counters[f"decider_refused:{why}"] += 1
        logger.info("%s: refused %s (%s) %s",
                    day, refusal.get("code") or "", why,
                    refusal.get("detail") or "")

    execution_panel = planner_panel if sector_mode else panel
    by_code = {row["code"]: row for row in execution_panel}
    placed = []
    for order in verdict["orders"]:
        row = by_code[order["code"]]
        cap = capacity_shares(
            row["adv20"], participation=ctx.participation)
        if cap <= 0:
            ctx.counters["decider_refused:no_capacity"] += 1
            continue
        order_theme = (
            str(row["primary_theme"])
            if sector_mode else ctx.theme
        )
        order_id = create_pending_order(
            code=order["code"],
            name=row["name"],
            theme=order_theme,
            order_date=day,
            entry_low=order["entry_low"],
            entry_high=order["entry_high"],
            stop_loss=order["stop_loss"],
            target_price=order.get("target_price"),
            source="walk_forward",
            reason=f"{t1_decider.DECIDER_NAME}: {order['reason']}",
            trader_id=ctx.trader,
            risk_themes=(
                list(row.get("supporting_themes") or [])
                if sector_mode else []
            ),
        )
        if order_id is None:
            ctx.counters["intent_refused"] += 1
            continue
        if row.get("primary_theme"):
            try:
                order_theme_exposure.record(
                    order_id=order_id,
                    primary_theme=order_theme,
                    supporting_themes=row.get("supporting_themes") or [],
                    source=ctx.selection_architecture,
                )
            except Exception as exc:                  # noqa: BLE001
                from alpha_agents.data.portfolio_intent import cancel_order
                cancel_order(
                    order_id,
                    "theme exposure attribution failed: "
                    f"{type(exc).__name__}")
                ctx.counters["theme_attribution_failed"] += 1
                logger.exception(
                    "%s: cancelled order #%s after theme attribution failed",
                    day, order_id)
                continue
        ctx.capacity[order["code"]] = cap
        placed.append({
            "code": order["code"],
            "order_id": order_id,
            "theme": order_theme,
            "supporting_themes": row.get("supporting_themes") or [],
            "membership_snapshot_id": row.get("membership_snapshot_id"),
            "membership_hash": row.get("membership_hash"),
            "primary_theme_relation_evidence_id": row.get(
                "primary_theme_relation_evidence_id"),
            "supporting_theme_relation_evidence_ids": row.get(
                "supporting_theme_relation_evidence_ids") or [],
            "research_packet_hash": (
                (verdict.get("research_packet") or {}).get("packet_hash")),
            "reason": order["reason"],
        })
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
    alerts = P.check_pending_orders(
        price_map, today=day, trader_id=ctx.trader,
        max_shares_by_code=ctx.capacity)
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


def _close_buys(ctx, day: str, prev_day: str, orders: list[dict]) -> list[dict]:
    """Fill today's close-time buys at today's close.

    The missing quadrant. The other three existed: an open buy rests a limit
    and settles at the open; open and close sells fill at their own moment.
    Buying *into the close* had no path at all, so a model that spotted a
    setup at 14:55 could only write it down for tomorrow — a different trade
    at a different price.

    Booked as an already-filled position rather than a pending order. A
    pending order settles at the **next** open, which is precisely the price
    this decision does not get: the whole point of the close decision is that
    it already knows today.

    The two levels are still honoured, read as the range the trader will
    accept rather than as a resting limit: the close must fall inside
    ``[entry_low, entry_high]``. A buy at a close outside its own stated zone
    would be the system accepting a price the trader said no to.
    """
    from alpha_agents.data import market_rules
    from alpha_agents.data import portfolio_intent as PI
    from alpha_agents.data import t1_execution as X

    bars = ctx.corpus.bars(day)
    prow = ctx.corpus.bars(prev_day) if prev_day else {}
    # The **same** panel the close decision was shown. It is rebuilt rather
    # than threaded through because `_decide_llm` is also called by the
    # placeholder path and by tests; the two calls have to agree on the
    # ranking day, and they do (`day` for a close decision) — a mismatch was
    # the first bug here, where the buy panel ranked on T-1 and the orders
    # were then validated against a panel ranked on T.
    #
    # Names already held or pending are dropped here **and** from the panel
    # the agent is shown, so the two agree. They used to be offered and then
    # refused: measured with an instrumented run, **every** close-buy refusal
    # was a duplicate — the agent ordered at the close the same name it had
    # ordered that morning, and `portfolio` correctly refused a second
    # position on one name. The rule is real; asking a question whose only
    # answer is "no" was the defect, exactly as with the T+1 filter above.
    held = _unavailable_codes(ctx)
    by_code = {row["code"]: row for row in
               _build_panel(ctx, day, day, ctx.panel_size)
               if row["code"] not in held}
    fills = []
    for order in orders:
        code = order["code"]
        if code in held:
            # The panel the agent was shown already excluded these, so this
            # is a model naming something it was not offered. Counted apart
            # from "not offerable" so the two cannot be read as one number:
            # one is a held name, the other a name with no bar today.
            ctx.counters["close_buy_already_held"] += 1
            continue
        row = bars.get(code)
        panel_row = by_code.get(code)
        if row is None or panel_row is None:
            # Not offerable today: no bar, or outside the panel. The intent
            # door records the refusal for the panel case; here it is counted.
            ctx.counters["close_buy_not_offerable"] += 1
            continue
        close = float(row["close"])
        name = ctx.corpus.instruments[code]["name"]
        try:
            rule = market_rules.market_rules(code, day, name=name)
        except ValueError:
            continue
        day_bar = X.DayBar(date=day, open=float(row["open"]),
                           high=float(row["high"]), low=float(row["low"]),
                           close=close)
        prev_close = (prow.get(code) or {}).get("close")
        got = X.market_at_close(day_bar, side="buy",
                                prev_close=(float(prev_close)
                                            if prev_close else None),
                                limit_pct=rule.price_limit_pct,
                                limit_rule=rule.reason)
        if got.status != "filled":
            # A close at the up limit has no seller. Counted rather than
            # silently dropped: "it wanted to buy and could not" is a fact
            # about the window.
            ctx.counters["close_buy_limit_blocked"] += 1
            logger.info("%s: close buy of %s blocked — %s", day, code,
                        got.reason)
            continue
        low, high = order.get("entry_low"), order.get("entry_high")
        if low is not None and close < low or high is not None and close > high:
            ctx.counters["close_buy_outside_zone"] += 1
            continue
        position_id = PI.open_position(
            code=code, name=name, theme=ctx.theme, open_date=day,
            open_price=close, stop_loss=order.get("stop_loss"),
            target_price=order.get("target_price"),
            source="walk_forward_close", reason=order.get("reason", ""),
            trader_id=ctx.trader)
        if position_id is None:
            ctx.counters["close_buy_refused"] += 1
            continue
        fills.append({
            "date": day, "side": "buy", "code": code, "name": name,
            "shares": None, "price": close, "amount": None,
            "capacity_shares": ctx.capacity.get(code),
            "capacity_oversize": False,
            "reason": "bought at the close"})
    return fills


def _fill_replay_peak_fields(ctx, positions: list[dict], day: str,
                             phase: str) -> None:
    """Compute what production's monitor would have written, as of ``day``.

    ``build_context`` renders 峰值 / 回撤 / 持仓天数 from
    ``peak_return_pct`` / ``holding_days``. In a live run
    ``position_monitor.check_positions`` maintains them; a replay does not
    run that module, so both columns were 0 every day — the agent was asked
    "should I sell" while shown a peak of zero, and every drawdown figure in
    its reasons was its own arithmetic on the current price rather than a
    fact the system had handed it.

    The peak is taken over **closes**, the same series a replay can see:
    from the fill day's own close (by the time the agent is asked, the fill
    day has already closed — T+1 is exactly why the position cannot have
    been sold first) up to T-1 in the open phase, or up to today in the
    close phase. Intraday highs are deliberately not used — the live
    monitor marks from realtime prices it polls, and a daily replay's
    honest equivalent is the close series.

    ``holding_days`` has no price in it, so its anchor is simply the
    decision day, the same arithmetic ``position_monitor`` runs.
    """
    for pos in positions:
        code = pos["code"]
        open_price = pos.get("open_price") or 0.0
        open_date = pos.get("open_date")
        if not open_price or not open_date:
            continue
        upto = day if phase == "close" else (ctx.corpus.previous(day) or "")
        # The fill day's close counts: the fill is at that day's open, so
        # its close is already in the agent's as-of past. Starting the
        # range *after* it made a position bought the previous session
        # show a peak of zero on the day the agent first could sell it.
        i_start = ctx.corpus.index.get(open_date, -1)
        i_end = ctx.corpus.index.get(upto, -1)
        peak = 0.0
        for i in range(i_start, i_end + 1):
            row = ctx.corpus.bars(ctx.corpus.days[i]).get(code)
            close = row.get("close") if row else None
            if close:
                peak = max(peak, (float(close) - open_price) / open_price * 100)
        try:
            from datetime import date as _date
            d0 = _date.fromisoformat(open_date)
            d1 = _date.fromisoformat(day)
            holding = max(0, (d1 - d0).days)
        except ValueError:
            holding = pos.get("holding_days", 0)
        pos["peak_return_pct"] = round(peak, 2)
        pos["holding_days"] = holding


def _agent_exits(ctx, day: str, phase: str = "open") -> list[dict]:
    """Ask the agent what to do with the positions the rules left open.

    Runs after ``_settle_exits``, never before: that ordering is what keeps
    the hard stop outside the model's reach. Everything here is the
    *discretionary* layer above it.

    Off unless ``--agent-exits`` is passed, so a run's model-call count is
    something the operator chose rather than a surprise: this doubles it.

    ``phase`` is which moment of the session this is, and it decides three
    things at once — what the agent may **see**, what a fill **settles at**,
    and therefore what the trade is allowed to know:

    ``"open"``
        Sees T-1's close (the last price that exists) plus T's open. Fills at
        T's open. This is the 09:00-09:35 decision.

    ``"close"``
        Sees T's full OHLCV — the session has happened. Fills at T's close.
        The user's rule: "收盘前我们看到的基本等于这个数据", so the close
        decision is taken with the day in hand and settled at its close.

    Getting these two wrong in either direction is the whole risk: a close
    decision that fills at the open has sold at a price it could only know
    later, and an open decision shown the close has been told the answer.
    """
    from alpha_agents.pipeline.tasks import exit_decision as ED

    positions = P.get_open_positions(ctx.trader)
    if not positions:
        return []
    prev = ctx.corpus.previous(day)
    if prev is None:
        return []
    # **Two maps, and they are not the same number.**
    #
    # `mark_map` is what the agent is shown a position is worth, and
    # `fill_map` is what an exit actually settles at. Using one map for both
    # was a real lookahead — measured on 600186, the agent decided on 08-20
    # and booked 11.61, which is 08-19's close, while 08-20 opened at 11.56.
    # The position was sold at yesterday's price, a free day of information.
    marks: dict[str, float] = {}
    fills: dict[str, float] = {}
    if phase == "close":
        # The session is over: mark and fill are both the close.
        for code, row in ctx.corpus.bars(day).items():
            close = row.get("close")
            if close:
                marks[code] = float(close)
                fills[code] = float(close)
    else:
        # 09:00: the last price that exists is T-1's close, and a sell fills
        # at T's opening auction.
        for code, row in ctx.corpus.bars(prev).items():
            close = row.get("close")
            if close:
                marks[code] = float(close)
        for code, row in ctx.corpus.bars(day).items():
            open_price = row.get("open")
            if open_price:
                fills[code] = float(open_price)
    mark_map, fill_map = marks, fills

    priced = [p for p in positions if mark_map.get(p["code"])]
    if not priced:
        logger.info("%s: no priced positions for the agent exit step", day)
        return []

    # T+1: shares bought today cannot be sold today. Asking the agent about
    # them wastes a model call and produces a decision that can only be
    # refused — measured on a real run, agent trims of a position bought the
    # previous session were refused by the exit path with "position has only
    # 0 settled shares today".
    #
    # Filtered here rather than left to the refusal: the refusal is the
    # correct backstop, but a question whose only possible answer is "no"
    # should not be asked. `_settle_exits` applies the same rule through
    # `may_sell`, so the two agree about who is sellable.
    from alpha_agents.data import t1_settlement as S
    sellable = [p for p in priced if S.may_sell(p.get("open_date"), day)]
    held_back = len(priced) - len(sellable)
    if held_back:
        ctx.counters["agent_exit_t_plus_one"] += held_back
        logger.info("%s: %d position(s) held by T+1, not offered to the agent",
                    day, held_back)
    if not sellable:
        return []
    priced = sellable

    news_by_theme = _news_by_theme(ctx, day, prev,
                                   {p.get("theme") for p in priced}, phase)
    trader = None
    try:
        from alpha_agents.data.trader import get_trader
        trader = get_trader(ctx.trader)
    except Exception:                                 # noqa: BLE001
        trader = None

    # `ctx.model` is None under the placeholder decider, because a placeholder
    # run makes no model calls. Asking the exit step anyway would make
    # `decide()` build its own live client — an unjournaled call inside a
    # replay, which is the exact defect this parameter exists to close.
    # Refused loudly rather than silently: a run that asked for agent exits
    # with a placeholder buy side is a misconfiguration, not a quiet no-op.
    if ctx.model is None:
        logger.warning(
            "%s: --agent-exits needs a model, but the decider is %r; "
            "skipping the exit step rather than building an unjournaled "
            "client", day, ctx.decider)
        return []

    # 峰值、回撤与持仓天数。生产由 `position_monitor.check_positions` 每个周期
    # 写回 `peak_return_pct` / `holding_days`，而回放不跑那个模块——三个字段
    # 恒为 0，agent 拿着"峰值 0%"回答"该不该卖"，它理由里写的回撤全是它自己
    # 用浮亏推的，不是系统喂的。这里按 as-of 收盘序列补算：open → 只走到 T-1
    # （今天还没发生），close → 走到今天。每日收盘价是当日事实，不属于未来。
    _fill_replay_peak_fields(ctx, priced, day, phase)

    # The journaled model, so the call is recorded and reproducible, and
    # **no tools**: every live tool answers from now, which inside a replay is
    # the future. The buy side passes none for the same reason.
    decisions = ctx.loop.run_until_complete(
        ED.decide_for_replay(priced, mark_map, day=day,
                             news_by_theme=news_by_theme, trader=trader,
                             model=ctx.model,
                             mechanical_stops=(ctx.mechanical_stop
                                               and ctx.mechanical_target),
                             phase=phase))
    if not decisions:
        return []

    # `apply` is the live executor and is reused deliberately: the buy side
    # already goes through the same intent door as production, and a replay
    # that executed sells differently would not be measuring this system.
    # The fill map, not the mark map: this is the line that was a lookahead.
    # A position with no bar today cannot be traded today, so it is dropped
    # rather than settled at a price that does not exist.
    tradable = []
    for p in priced:
        price = fill_map.get(p["code"])
        if not price:
            continue
        # A one-way board has no counterparty. Checked before the executor
        # rather than left to it: `exit_decision.apply` calls
        # `close_position`, which has no price-limit concept at all — it
        # books whatever price it is handed. So the check has to live on the
        # path that knows the day's bar, which is here.
        blocked = _limit_blocks(ctx, day, p["code"], price, side="sell")
        if blocked:
            ctx.counters["agent_exit_limit_blocked"] += 1
            logger.info("%s: %s cannot be sold at %s — %s",
                        day, p["code"], price, blocked)
            continue
        tradable.append(p)
    if not tradable:
        return []
    alerts = ED.apply(decisions, tradable, fill_map)
    fills = []
    claimed_legs: set[int] = set()
    for a in alerts:
        kind = str(a.get("type") or "agent_other")
        kind = kind if kind.startswith("agent_") else f"agent_{kind}"
        if a.get("type") == "agent_add":
            # An add buys; it is not a sell and has no leg in
            # `position_exits`. Counted so the run's activity is visible,
            # but it must not inflate the sell rows.
            ctx.counters[kind] += 1
            continue
        # Both a full exit and a trim wrote a real leg into `position_exits`,
        # and both are the agent selling. The first version passed only
        # `agent_exit` through and dropped `agent_trim` here — the executor
        # had done the trade, the book was updated, and the report printed
        # "卖出 5 笔" for a window in which the agent had sold on **18**
        # separate legs (6 full exits, 12 trims). The one number a reader
        # checks to answer "did it manage the book" was a third of the truth.
        #
        # Shares and amount are read back from the leg the executor just
        # wrote. They were hardcoded None once, so the report printed
        # "卖出 3 笔 0 元" while three real exits sat in `position_exits`.
        shares = _shares_for_exit(a.get("code"), day, claimed_legs)
        price = a.get("close_price")
        reason = a.get("reason", "")
        fills.append({
            "date": day, "side": "sell", "code": a["code"],
            "name": a.get("name", ""),
            "shares": shares, "price": price,
            "amount": (shares or 0) * price if price else None,
            "capacity_shares": None,
            "capacity_oversize": False,
            "reason": (reason if reason.startswith("agent")
                       else f"{kind}: {reason}")[:200],
        })
        ctx.counters[kind] += 1
    ctx.counters["agent_exit_calls"] += 1
    ctx.counters["agent_exits"] += len(fills)
    return fills


def _limit_blocks(ctx, day: str, code: str, price: float, side: str):
    """Why this name cannot transact at ``price`` in ``side``'s direction.

    Returns ``None`` when the trade is possible. The four quadrants are the
    point, and a symmetric implementation gets two of them wrong:

    * up limit blocks a **buy** (no seller) and allows a **sell**;
    * down limit blocks a **sell** (no buyer) and allows a **buy**.

    Both moments are checked, and they are different checks: at the open the
    reference is T-1's close against T's open, at the close it is T-1's close
    against T's close. A name that opened normally and closed limit-up is
    sellable at the open and not buyable at the close.
    """
    from alpha_agents.data import market_rules
    from alpha_agents.data import t1_execution as X

    prev = ctx.corpus.previous(day)
    if prev is None:
        return "no previous session, so no price limit can be checked"
    prow = ctx.corpus.bars(prev).get(code)
    prev_close = prow.get("close") if prow else None
    if prev_close is None:
        return "no previous close, so no price limit can be checked"
    name = (ctx.corpus.instruments.get(code) or {}).get("name") or ""
    try:
        rule = market_rules.market_rules(code, day, name=name)
    except ValueError:
        return "no market rule for this name"
    bar = None
    row = ctx.corpus.bars(day).get(code)
    if row is not None:
        bar = X.DayBar(date=day, open=float(row["open"]), high=float(row["high"]),
                       low=float(row["low"]), close=float(row["close"]))
    if bar is None:
        return "no bar today: the instrument did not trade"
    fill = X.market_at_close(bar, side=side, prev_close=float(prev_close),
                             limit_pct=rule.price_limit_pct,
                             limit_rule=rule.reason)
    return None if fill.status == "filled" else fill.reason


def _unavailable_codes(ctx) -> set[str]:
    """Names this book cannot open another position on today.

    One name, one position (``portfolio._open_position_impl``). Anything
    pending or open is unavailable, and offering it to the model produces a
    decision that can only be refused.

    Read from the book rather than tracked in memory so the filter cannot
    disagree with the guard that actually refuses.
    """
    try:
        from alpha_agents.data import portfolio as P
        return {p["code"] for p in P.get_open_positions(ctx.trader)} | {
            p["code"] for p in P.get_pending_orders(ctx.trader)}
    except Exception as exc:                          # noqa: BLE001
        logger.debug("Could not read the book for the close panel: %s", exc)
        return set()


def _shares_for_exit(code: str, day: str, used: set[int]) -> int | None:
    """What the executor actually sold, read back from the exit leg.

    The alert carries a price and a reason but not a quantity, and inventing
    one from the position would be a guess the moment a trim is involved.
    ``position_exits`` is the record the executor wrote, so it is the answer.

    ``used`` holds leg ids already claimed by an earlier alert this call.
    Without it, two trims of one position on the same day — which the window
    measured three times — would both read the newest leg and the report
    would double-count one and drop the other. Legs are booked in the order
    the alerts were applied, so the oldest unclaimed leg is the match.
    """
    try:
        from alpha_agents.data.memory_store import _get_conn
        rows = _get_conn().execute(
            "SELECT id, shares FROM position_exits WHERE code = ? AND exit_date = ? "
            "ORDER BY id ASC", (code, day)).fetchall()
        for row in rows:
            if row["id"] in used or not row["shares"]:
                continue
            used.add(row["id"])
            return int(row["shares"])
        return None
    except Exception as exc:                          # noqa: BLE001
        logger.debug("Exit shares for %s on %s unavailable: %s", code, day, exc)
        return None


def _news_by_theme(ctx, day: str, prev_day: str, themes: set,
                   phase: str = "open") -> dict[str, list[str]]:
    """This day's news, grouped by the theme each flash mentions.

    Read through the replay's own windowed reader — the same one the buy side
    uses — so the sell side cannot see further than the buy side. The live
    ``news_index`` search is deliberately not used: it reads by wall-clock
    offset and does not respect ``replay_mode``, so it would hand the agent
    flashes published after the day it is deciding on.
    """
    out: dict[str, list[str]] = {}
    names = {t for t in themes if t}
    if not names:
        return out
    items = _news_window(day, prev_day, 200, phase)
    for item in items:
        text = f"{item.get('title') or ''} {item.get('content') or ''}"
        for theme in names:
            if theme and theme in text:
                out.setdefault(theme, []).append(
                    f"[{(item.get('published_at') or '')[11:16]}] "
                    f"{(item.get('title') or '')[:70]}")
    return {k: v[:3] for k, v in out.items()}


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
        # The experiment switch. Hiding the levels — rather than changing a
        # constant — is what makes this honest: `exit_verdict` reads the
        # *position's* stop, so passing None removes exactly the two
        # mechanical exits and leaves every other rule (T+1, price limits,
        # suspension, gap handling) intact.
        #
        # Note what this does NOT touch: `portfolio.position_monitor`'s hard
        # exit, which is a production path the replay does not call.
        verdict = S.exit_verdict(
            bar=S.bar_from_row(bars_today.get(code)), prev_close=prev_close,
            rule=rule,
            stop_loss=(pos.get("stop_loss") if ctx.mechanical_stop else None),
            target_price=(pos.get("target_price")
                          if ctx.mechanical_target else None))
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


def _initial_account(ctx, first_session: str) -> dict:
    """Freeze the account before the first decision, fee or fill.

    The mark lives in run.json instead of equity.csv so it anchors the first
    daily return and drawdown without pretending to be another trading day.
    """
    previous = ctx.corpus.previous(first_session)
    positions = P.get_open_positions(ctx.trader)
    if previous is None:
        if positions:
            raise RuntimeError(
                "cannot value pre-existing positions without a prior session")
        total = P.get_total_capital(ctx.trader)
        mark = {
            "cash": P.get_available_capital(ctx.trader)
            + reservations.unconsumed_total(P._get_conn(), ctx.trader),
            "invested": P.get_invested_capital(ctx.trader),
            "market_value": 0.0,
            "realized": round(total - P.trader_capital(ctx.trader), 2),
            "unrealized": 0.0,
            "equity": round(total, 2),
            "open_positions": 0,
            "pending_orders": len(P.get_pending_orders(ctx.trader)),
            "valued_at_a_stale_price": "",
        }
    else:
        mark = _value(ctx, previous)

    return {
        "kind": "initial_mark",
        "as_of": previous,
        "next_session": first_session,
        **{key: value for key, value in mark.items() if key != "date"},
    }


# ── the run ─────────────────────────────────────────────────────────────────


_EXPERIMENT_ARMS = frozenset({"A", "B", "C", "D"})


def _experiment_contract(args, *, architecture: str,
                         membership_archive) -> tuple[dict | None, str | None]:
    """Bind one replay to one frozen experiment question, or run unbound."""
    path = getattr(args, "experiment_manifest", None)
    arm = getattr(args, "experiment_arm", None)
    if (path is None) != (arm is None):
        raise SystemExit(
            "--experiment-manifest and --experiment-arm must be supplied together")
    if path is None:
        return None, None
    if arm not in _EXPERIMENT_ARMS:
        raise SystemExit(f"unknown experiment arm {arm!r}")

    manifest = json.loads(path.read_text(encoding="utf-8"))
    try:
        digest = sector_experiment.require_valid(manifest)
    except sector_experiment.SectorExperimentError as exc:
        raise SystemExit(str(exc)) from exc

    arm_spec = (manifest.get("architecture_arms") or {}).get(arm) or {}
    expected = str(arm_spec.get("name") or "")
    if expected != architecture:
        raise SystemExit(
            f"experiment arm {arm} requires {expected}, got {architecture}")
    if args.decider != "llm":
        raise SystemExit(
            "A/B/C/D architecture experiments require --decider llm")
    if not membership_archive:
        raise SystemExit(
            "A/B/C/D architecture experiments require the same "
            "--sector-membership archive for cluster-risk attribution")
    return manifest, digest


_SELECTION_EXPERIMENT_ARMS = frozenset({"CONTROL", "SECTOR"})


def _selection_experiment_contract(
        args, *, architecture: str,
        membership_archive) -> tuple[dict | None, str | None]:
    path = getattr(args, "selection_experiment_manifest", None)
    arm = getattr(args, "selection_experiment_arm", None)
    if (path is None) != (arm is None):
        raise SystemExit(
            "--selection-experiment-manifest and "
            "--selection-experiment-arm must be supplied together")
    if path is None:
        return None, None
    if getattr(args, "experiment_manifest", None) is not None:
        raise SystemExit(
            "legacy A/B/C/D and nf_discovery_v1 manifests are mutually exclusive")
    if arm not in _SELECTION_EXPERIMENT_ARMS:
        raise SystemExit(f"unknown selection experiment arm {arm!r}")

    manifest = json.loads(path.read_text(encoding="utf-8"))
    try:
        digest = selection_experiment.require_valid(manifest)
    except selection_experiment.SelectionExperimentError as exc:
        raise SystemExit(str(exc)) from exc
    expected = manifest["arms"][arm]["architecture"]
    if architecture != expected:
        raise SystemExit(
            f"selection experiment arm {arm} requires {expected}, "
            f"got {architecture}")
    if args.decider != "llm":
        raise SystemExit(
            "nf_discovery_v1 uses one shared LLM planner; --decider must be llm")
    if not membership_archive:
        raise SystemExit(
            "nf_discovery_v1 requires the same --sector-membership archive "
            "for both arms, even when CONTROL does not use it for discovery")
    return manifest, digest


def _experiment_runtime_contract(args) -> dict:
    """Behavioral runtime facts a formal A/B/C/D run must freeze.

    A manifest that names costs/model/budget but does not bind them to the
    process is decoration. Keep this separate from the arm contract so tests
    can prove both sides independently and so ordinary non-experiment replays
    retain their existing flexibility.
    """
    from alpha_agents.data import portfolio, portfolio_exit
    from alpha_agents.model_factory import model_identity
    from alpha_agents.tools.budget import ResearchBudget

    budget = ResearchBudget()
    return {
        "decision_config": {
            "trader": str(args.trader),
            "picks_per_day": int(args.picks),
            "panel_size": int(args.panel_size),
            "participation": float(args.participation),
            "trader_tools_enabled": bool(args.trader_tools),
            "max_turns_per_decision": args.max_turns,
            "news_limit": int(args.news_limit),
            "model_timeout_seconds": float(args.model_timeout),
            "pace_seconds": float(args.pace_seconds),
        },
        "model": model_identity(),
        "research_budget": {
            "max_total_calls": budget.max_total_calls,
            "max_market_calls": budget.max_market_calls,
            "max_theme_calls": budget.max_theme_calls,
            "max_stock_calls": budget.max_stock_calls,
            "max_self_calls": budget.max_self_calls,
            "max_deep_dive_names": budget.max_deep_dive_names,
            "max_calls_per_name": budget.max_calls_per_name,
        },
        "cost_model": {
            "name": "virtual_a_share_v1",
            "commission_rate": portfolio_exit.COMMISSION_RATE,
            "min_commission_rmb": portfolio_exit.MIN_COMMISSION,
            "stamp_duty_sell_rate": portfolio_exit.STAMP_DUTY_SELL_RATE,
            "transfer_fee_rate": portfolio_exit.TRANSFER_FEE_RATE,
            "slippage_rate": portfolio_exit.SLIPPAGE_RATE,
        },
        "exit_policy": {
            "mechanical_stop": bool(args.mechanical_stop),
            "mechanical_target": bool(args.mechanical_target),
            "agent_exits": bool(args.agent_exits),
            "close_buys": bool(getattr(args, "close_buys", False)),
            "hard_stop_pct": float(portfolio.HARD_STOP_PCT),
        },
    }


def _selection_experiment_runtime_contract(args) -> dict:
    """Effective runtime facts for the minimal no-flow experiment."""
    from alpha_agents.data import portfolio, portfolio_exit
    from alpha_agents.model_factory import model_identity

    return {
        "decision_config": {
            "trader": str(args.trader),
            "picks_per_day": int(args.picks),
            "panel_size": int(args.panel_size),
            "participation": float(args.participation),
            "max_turns_per_decision": 1,
            "model_timeout_seconds": float(args.model_timeout),
            "pace_seconds": float(args.pace_seconds),
            "news_limit": 0,
            "trader_tools_enabled": False,
            "direction_limit": 3,
            "learning_input": "frozen",
            "run_theme": str(args.theme),
        },
        "model": model_identity(),
        "cost_model": {
            "name": "virtual_a_share_v1",
            "commission_rate": portfolio_exit.COMMISSION_RATE,
            "min_commission_rmb": portfolio_exit.MIN_COMMISSION,
            "stamp_duty_sell_rate": portfolio_exit.STAMP_DUTY_SELL_RATE,
            "transfer_fee_rate": portfolio_exit.TRANSFER_FEE_RATE,
            "slippage_rate": portfolio_exit.SLIPPAGE_RATE,
        },
        "exit_policy": {
            "mechanical_stop": bool(args.mechanical_stop),
            "mechanical_target": bool(args.mechanical_target),
            "agent_exits": bool(args.agent_exits),
            "close_buys": bool(getattr(args, "close_buys", False)),
            "hard_stop_pct": float(portfolio.HARD_STOP_PCT),
        },
    }


def _verify_selection_experiment_runtime(args, manifest: dict | None) -> None:
    if manifest is None:
        return
    actual = _selection_experiment_runtime_contract(args)
    mismatches = {}
    for field, observed in actual.items():
        frozen = manifest.get(field)
        if frozen != observed:
            mismatches[field] = {"manifest": frozen, "runtime": observed}
    if mismatches:
        raise SystemExit(
            "nf_discovery_v1 runtime does not match its manifest: "
            + json.dumps(mismatches, ensure_ascii=False, sort_keys=True))


def _verify_experiment_identity(
        manifest: dict | None, *, code_ref: str | None,
        policy_ref: str | None, input_hash: str) -> None:
    if manifest is None:
        return
    frozen = manifest.get("baseline_identity") or {}
    actual = {
        "code_ref": code_ref,
        "policy_ref": policy_ref,
        "input_hash": input_hash,
    }
    mismatches = {
        key: {"manifest": frozen.get(key), "runtime": value}
        for key, value in actual.items()
        if frozen.get(key) != value
    }
    if mismatches:
        raise SystemExit(
            "formal experiment baseline identity mismatch: "
            + json.dumps(mismatches, ensure_ascii=False, sort_keys=True))


def _verify_experiment_runtime(args, manifest: dict | None) -> None:
    if manifest is None:
        return
    actual = _experiment_runtime_contract(args)
    mismatches = {}
    for field, observed in actual.items():
        frozen = manifest.get(field)
        if frozen != observed:
            mismatches[field] = {"manifest": frozen, "runtime": observed}
    if mismatches:
        raise SystemExit(
            "formal experiment runtime does not match its frozen manifest: "
            + json.dumps(mismatches, ensure_ascii=False, sort_keys=True))


def _verify_experiment_window(ctx, window: list[str]) -> None:
    if ctx.experiment_manifest is None:
        return
    if not window:
        raise SystemExit("experiment replay resolved to an empty trading window")
    actual = {"start": window[0], "end": window[-1]}
    registered = [
        {
            "start": str(item.get("start") or "")[:10],
            "end": str(item.get("end") or "")[:10],
        }
        for item in ctx.experiment_manifest.get("validation_windows") or []
    ]
    if actual not in registered:
        raise SystemExit(
            f"run window {actual['start']}..{actual['end']} is not one of "
            "the preregistered validation windows")
    if getattr(ctx, "experiment_family", None) == selection_experiment.FAMILY:
        expected = int(
            ctx.experiment_manifest.get("expected_days_per_window") or 0)
        if len(window) != expected:
            raise SystemExit(
                f"nf_discovery_v1 window has {len(window)} trading days; "
                f"expected exactly {expected}")


class Context:
    def __init__(self, args):
        self.args = args
        self.corpus = Corpus(_REPLAY_DIR)
        self.trader = args.trader
        self.picks = args.picks
        self.theme = args.theme
        self.selection_architecture = getattr(
            args, "selection_architecture", "dual_rank_v0")
        membership_path = getattr(args, "sector_membership", None)
        self.sector_membership_archive = (
            sector_membership.load(membership_path)
            if membership_path is not None else ())
        if _REPLAY_DIR is None:
            # Context is also constructed directly by unit tests. A real run
            # refuses an unbound replay directory before reaching here; keep
            # direct non-formal construction explicit rather than inventing
            # paths from the process cwd.
            self.input_identity = {
                "files": {}, "input_hash": "unbound",
            }
        else:
            identity_paths = {
                name: _REPLAY_DIR / name for name in CORPUS_FILES
            }
            if membership_path is not None:
                identity_paths["sector_membership"] = membership_path
            self.input_identity = world_read_set.file_identity(identity_paths)
        formal_manifest_requested = (
            getattr(args, "experiment_manifest", None) is not None
            or getattr(args, "selection_experiment_manifest", None) is not None
        )
        self.code_ref = (
            world_read_set.git_code_ref(_PROJECT_ROOT)
            if formal_manifest_requested else None
        )
        self.policy_ref = policy_registry.active_ref()
        frozen_path = getattr(args, "frozen_directions", None)
        self.frozen_directions = (
            frozen_direction_archive.load(frozen_path)
            if frozen_path is not None else None)
        legacy_arm = getattr(args, "experiment_arm", None)
        legacy_manifest, legacy_hash = _experiment_contract(
            args,
            architecture=self.selection_architecture,
            membership_archive=self.sector_membership_archive,
        )
        selection_arm = getattr(args, "selection_experiment_arm", None)
        selection_manifest, selection_hash = _selection_experiment_contract(
            args,
            architecture=self.selection_architecture,
            membership_archive=self.sector_membership_archive,
        )
        self.experiment_arm = legacy_arm or selection_arm
        self.experiment_family = (
            selection_experiment.FAMILY
            if selection_manifest is not None else
            ("sector_abcd_v0" if legacy_manifest is not None else None)
        )
        self.experiment_manifest = legacy_manifest or selection_manifest
        self.experiment_manifest_hash = legacy_hash or selection_hash
        _verify_experiment_identity(
            self.experiment_manifest,
            code_ref=self.code_ref,
            policy_ref=self.policy_ref,
            input_hash=self.input_identity["input_hash"],
        )
        _verify_experiment_runtime(args, legacy_manifest)
        _verify_selection_experiment_runtime(args, selection_manifest)
        if self.selection_architecture in {
                "sector_first_v0", "sector_first_simple_selector",
                "sector_first_no_flow", "sector_rank_price_v1"}:
            if args.decider != "llm":
                raise SystemExit(
                    f"{self.selection_architecture} requires --decider llm")
            if not self.sector_membership_archive:
                raise SystemExit(
                    f"{self.selection_architecture} requires "
                    "--sector-membership with PIT snapshots")
            if self.selection_architecture == "sector_first_simple_selector":
                if self.frozen_directions is None:
                    raise SystemExit(
                        "sector_first_simple_selector requires "
                        "--frozen-directions exported from the B arm")
            elif self.frozen_directions is not None:
                raise SystemExit(
                    "--frozen-directions is only valid for "
                    "sector_first_simple_selector")
            _validate_close_buy_support(
                self.selection_architecture,
                bool(getattr(args, "close_buys", False)))
        self.participation = args.participation
        self.stop_pct = args.stop_pct
        self.entry_zone = ENTRY_ZONES.get(args.trader, ENTRY_ZONES["pullback"])
        self.run_id = args.run_id
        self.decider = args.decider
        #: Whether the sell side gets a model call too. Off by default:
        #: it doubles the run's model calls, and that should be a choice
        #: the operator made rather than a surprise on the bill.
        self.agent_exits = getattr(args, "agent_exits", False)
        #: Whether the buy-side gets an additional synthetic close decision.
        #: Independent from sell-side agent exits: enabling one must not
        #: silently add the other decision point.
        self.close_buys = getattr(args, "close_buys", False)
        #: Whether the mechanical **stop** is enforced. Off means nothing
        #: closes a position for being down: the agent has to decide.
        #:
        #: **Replay-only, and deliberately so.** The stop is the line that
        #: keeps a wrong call from becoming a blown account; removing it from
        #: production would be removing the only backstop under a model.
        #: Here the book is simulated, so the question "does it sell?" can be
        #: asked without paying for the answer.
        self.mechanical_stop = getattr(args, "mechanical_stop", True)
        #: Whether the mechanical **target** is enforced. Separate from the
        #: stop on purpose: they are different claims about behaviour, and
        #: "the agent never takes a profit" and "the agent never cuts a
        #: loss" are two findings that a single switch would merge into one
        #: number. The user asked to disable the take-profit as its own
        #: experiment.
        self.mechanical_target = getattr(args, "mechanical_target", True)
        #: Whether the buy-side decider may **call tools**. On by default for
        #: the llm decider: a trader that cannot ask a question is a scorer,
        #: and the whole point of the last review's finding was that the
        #: decider had ``tools=[]`` and one turn.
        #:
        #: Off is still reachable (``--no-trader-tools``) so the two
        #: configurations can be compared on one window — the tool-using
        #: trader against the bare picker, same panel, same days.
        self.trader_tools = getattr(args, "trader_tools", True)
        #: Ablation arm for the panel concept column, from --no-concepts.
        #: Default on, and read through getattr rather than assumed:
        #: hand-built Contexts in tests predate the flag and must keep the
        #: behaviour they were written against.
        self.concepts = getattr(args, "concepts", True)
        #: Turns allowed per decision. ``None`` means "the decider's own
        #: default" — see the note on ``--max-turns``; the number has one
        #: owner, and it is not this file.
        self.max_turns = getattr(args, "max_turns", None)
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
        self.prompt = (
            _load_prompt_text(self.selection_architecture)
            if args.decider == "llm" else None)
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
        self.last_world_read_set: dict | None = None
        self.world_read_set_hashes: list[str] = []


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


_CLOSE_BUY_UNSUPPORTED = frozenset({
    "sector_first_v0", "sector_first_simple_selector",
    "sector_first_no_flow", "sector_rank_price_v1",
})


def _validate_close_buy_support(architecture: str, enabled: bool) -> None:
    if enabled and architecture in _CLOSE_BUY_UNSUPPORTED:
        raise SystemExit(
            f"{architecture} does not support --close-buys; "
            "its reproducible buy path is 09:00 only")


def _close_buy_enabled(ctx) -> bool:
    return (
        getattr(ctx, "decider", None) == "llm"
        and bool(getattr(ctx, "close_buys", False))
    )


def _run_decider(ctx, day: str, prev_day: str,
                 phase: str = "open") -> list[dict]:
    """Route to the declared decider. One place, so the report cannot
    describe a decider the run did not use.

    ``phase`` reaches only the LLM decider: the placeholder exists to
    generate orders mechanically and has no notion of a moment.
    """
    if ctx.decider == "llm":
        return _decide_llm(ctx, day, prev_day, phase)
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
    _assert_an_exit_exists(args)

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
    _verify_experiment_window(ctx, window)
    if not window:
        raise RuntimeError("walk-forward window is empty")
    initial_account = _initial_account(ctx, window[0])
    journal_before = _journal_records(_REPLAY_DIR)
    #: Tool calls are read from the journal's own records rather than counted
    #: in memory, for the same reason the exit legs are read back from
    #: `position_exits`: the recording is what actually happened, and a
    #: counter kept beside it can disagree with it.
    tools_before = _journal_tool_calls(_REPLAY_DIR)
    prod_hash_before = _sha256(_PRODUCTION_DIR / "memory.db")
    #: The verdict (did the replay get read-only handles) and the diagnostic
    #: (what moved) are separate readings, because only the first is about us.
    corpus_access_before = _corpus_access_check(_REPLAY_DIR)
    corpus_before = _corpus_fingerprint(_REPLAY_DIR)

    equity_rows, fill_rows, settle_rows, event_rows = [], [], [], []
    theme_exposure_rows = []
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
            # The agent's sell side, run *after* the mechanical settlement and
            # inside its own replay_as_of block. The order matters and is the
            # contract: a position that gapped through its stop was closed
            # above, so the agent cannot argue a hard line away — it only ever
            # sees survivors.
            agent_exits = []
            for phase, stamp in (("open", f"{day} 09:35"),
                                 ("close", f"{day} 14:55")):
                if not ctx.agent_exits:
                    break
                try:
                    with replay_as_of(stamp):
                        got = _agent_exits(ctx, day, phase)
                    if got:
                        logger.info("%s: agent exits (%s) %s", day, phase,
                                    dict(Counter(f["side"] for f in got)))
                    agent_exits.extend(got)
                except Exception as exc:              # noqa: BLE001
                    # Holds rather than sells, like every other failure path
                    # on this side. A model outage must not liquidate the
                    # book.
                    logger.warning(
                        "%s: agent exit step (%s) failed (%s: %s) — holding",
                        day, phase, type(exc).__name__, exc)
                    errors.append({"date": day, "stage": f"agent_exit:{phase}",
                                   "error": f"{type(exc).__name__}: {exc}"})
            # The close-time **buy**, which is the second of the day's two
            # decisions on the buy side. Runs after the close exits so a
            # position sold at the close frees its capital for one bought at
            # the same close.
            if _close_buy_enabled(ctx):
                try:
                    with replay_as_of(f"{day} 14:55"):
                        close_orders = _run_decider(ctx, day, prev_day,
                                                    phase="close")
                        close_fills = _close_buys(ctx, day, prev_day,
                                                  close_orders)
                    if close_fills:
                        logger.info("%s: close buys %d", day, len(close_fills))
                    fill_rows.extend(close_fills)
                except Exception as exc:              # noqa: BLE001
                    logger.warning("%s: close buy step failed (%s: %s)",
                                   day, type(exc).__name__, exc)
                    errors.append({"date": day, "stage": "close_buy",
                                   "error": f"{type(exc).__name__}: {exc}"})
            with replay_as_of(day):
                equity = _value(ctx, day)
                close_marks = {
                    code: float(row["close"])
                    for code, row in ctx.corpus.bars(day).items()
                    if row.get("close") is not None
                }
                exposure = order_theme_exposure.snapshot(
                    trader_id=ctx.trader,
                    price_map=close_marks,
                    equity=float(equity["equity"]),
                    membership_archive=ctx.sector_membership_archive,
                )
                theme_exposure_rows.append({
                    "date": day,
                    "complete": int(bool(exposure["complete"])),
                    "max_theme_cluster_exposure_pct":
                        exposure["max_theme_cluster_exposure_pct"],
                    "overlap_orders": exposure["overlap_orders"],
                    "active_orders": exposure["active_orders"],
                    "theme_exposure_json": exposure["theme_exposure_json"],
                    "missing_json": json.dumps(
                        exposure["missing"], ensure_ascii=False,
                        sort_keys=True, separators=(",", ":")),
                })
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
        # The agent's own exits, which are a **third** bucket and were
        # dropped here. `_agent_exits` built the rows correctly and the book
        # was updated, so the trades really happened — but they never
        # reached the report, which printed "卖出 0 笔" while the positions
        # carried `close_reason: agent卖出: ...` in the database. The one
        # number a reader checks to answer "did it sell" was the one number
        # wrong.
        fill_rows.extend(agent_exits)
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
    tools_after = _journal_tool_calls(_REPLAY_DIR)
    prod_hash_after = _sha256(_PRODUCTION_DIR / "memory.db")
    corpus_after = _corpus_fingerprint(_REPLAY_DIR)

    return {
        "ctx": ctx, "window": window, "initial_account": initial_account,
        "equity": equity_rows, "fills": fill_rows,
        "settlement": settle_rows, "events": event_rows,
        "theme_exposure": theme_exposure_rows,
        "by_status": by_status, "cancels": cancels, "errors": errors,
        "learning": learning_rows,
        "model_calls": {
            "mode": os.environ.get("ALPHAAGENTS_LLM_MODE", "live"),
            "journal_before": journal_before, "journal_after": journal_after,
            "tool_calls": max(0, tools_after - tools_before)},
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


def _journal_tool_calls(data_dir: Path) -> int:
    """How many tool calls every recorded exchange in this directory made.

    Read back from the journal rather than counted in memory, for the same
    reason an exit's share count is read back from ``position_exits``: the
    recording is what happened, and a counter kept alongside it drifts.

    It exists because the tools made a decision cost real work — measured at
    ~23 calls per decision — and a result reported without its cost is not a
    finding, it is an advertisement. A reader comparing two windows has to be
    able to see that one of them bought its result with four times the model
    traffic.
    """
    directory = data_dir / llm_journal.JOURNAL_DIRNAME
    if not directory.is_dir():
        return 0
    total = 0
    for path in directory.glob("*.jsonl"):
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        # A half-written final line is not a reason to lose the
                        # count of every line before it.
                        continue
                    total += len(record.get("tool_calls") or [])
        except OSError as exc:
            logger.warning("Tool-call count skipped %s: %s", path.name, exc)
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


def _assert_an_exit_exists(args) -> None:
    """Refuse a window in which nothing can close a position.

    Disabling both the stop and the target leaves the agent as the only
    closer. With no agent on the sell side there is then **no exit at all**:
    every fill stays open until the window ends, and the equity curve
    measures the window's drift rather than any decision. That is not an
    experiment, it is a missing feature, so it is refused rather than run and
    explained afterwards.

    Disabling one alone is allowed without an agent, because the other still
    exits.
    """
    if getattr(args, "mechanical_stop", True):
        return
    if getattr(args, "mechanical_target", True):
        return
    if getattr(args, "agent_exits", False):
        return
    raise SystemExit(
        "--no-stop-loss and --no-take-profit together remove every mechanical "
        "exit, and without --agent-exits nothing can close a position: every "
        "fill would stay open to the end of the window and the curve would "
        "measure drift, not decisions.\n"
        "  Add --agent-exits to make the agent the only seller, which is the "
        "experiment these flags exist for.")


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

    # Validate the whole equity history before writing even the first artifact.
    # A half-written report directory is worse than a hard failure: a later
    # comparator could mistake stale files from the same path for this run.
    performance.equity_metrics(
        result["initial_account"]["equity"], result["equity"])

    capability_matrix = replay_capabilities.build(window, _REPLAY_DIR)

    meta = {
        "run_id": ctx.run_id,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "decider": _decider_name(ctx),
        "trader": ctx.trader,
        "theme": ctx.theme,
        "selection_architecture": ctx.selection_architecture,
        "experiment_arm": ctx.experiment_arm,
        "experiment_manifest_hash": ctx.experiment_manifest_hash,
        "code_ref": ctx.code_ref,
        "policy_ref": ctx.policy_ref,
        "input_identity": ctx.input_identity,
        "world_read_set_hashes": list(ctx.world_read_set_hashes),
        "frozen_directions_hash": (
            (ctx.frozen_directions or {}).get("archive_hash")
            if hasattr(ctx, "frozen_directions") else None),
        "picks_per_day": ctx.picks,
        "participation": ctx.participation,
        "stop_pct": ctx.stop_pct,
        "entry_zone": list(ctx.entry_zone),
        "window": {"start": window[0], "end": window[-1],
                   "trading_days": len(window)},
        "initial_account": result["initial_account"],
        "replay_dir": str(_REPLAY_DIR),
        "corpus_dir": _corpus_root(),
        "llm_mode": model["mode"],
        "prompt_sha256": ctx.prompt_sha256,
        "model_expected": ctx.decider == "llm",
        "model_calls_made": model["journal_after"] - model["journal_before"],
        "model_journal_total": model["journal_after"],
        #: What the agent's questions cost. Recorded beside the orders so a
        #: window's result cannot be read without its price.
        "agent_tool_calls": model.get("tool_calls"),
        "max_turns_per_decision": ctx.max_turns,
        "trader_tools_enabled": bool(getattr(ctx, "trader_tools", False)),
        "concepts_ablated": not getattr(ctx, "concepts", True),
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
        "capability_matrix": capability_matrix,
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

    _write_csv(out_dir / "theme_exposure.csv", result["theme_exposure"], [
        "date", "complete", "max_theme_cluster_exposure_pct",
        "overlap_orders", "active_orders", "theme_exposure_json",
        "missing_json"])

    summary = _summary(
        result, out_dir, model_usage_ok, model_usage_detail,
        capability_matrix)
    (out_dir / "summary.txt").write_text(summary, encoding="utf-8")
    return {"meta": meta, "summary": summary, "out_dir": out_dir}


def _summary(result: dict, out_dir: Path, model_usage_ok: bool,
             model_usage_detail: str, capability_matrix: dict) -> str:
    ctx = result["ctx"]
    window = result["window"]
    equity = result["equity"]
    fills = result["fills"]
    by_status = result["by_status"]
    first, last = equity[0], equity[-1]
    metrics = performance.equity_metrics(
        result["initial_account"]["equity"], equity)
    buys = [f for f in fills if f["side"] == "buy"]
    sells = [f for f in fills if f["side"] == "sell"]
    oversize = [f for f in fills if f["capacity_oversize"]]
    start_capital = metrics["initial_equity"]

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
        f"区间收益      {metrics['net_return_pct']:+.3f}%",
        f"区间最大回撤  {metrics['max_drawdown_pct']:.3f}%",
        f"已实现 / 浮盈 {first['realized']:,.0f} → {last['realized']:,.0f} real / "
        f"{last['unrealized']:,.0f} unreal",
        f"期末持仓/挂单 {last['open_positions']} / {last['pending_orders']}",
        *_benchmark_lines(ctx, result, start_capital),
        "",
        "— 成交 —",
        f"买入 {len(buys)} 笔 {sum(b['amount'] or 0 for b in buys):,.0f} 元 / "
        f"卖出 {len(sells)} 笔 {sum(s['amount'] or 0 for s in sells):,.0f} 元",
        f"成交量超 ADV20 参与上限的笔数：{len(oversize)}（**已计数、尚未强制**）",
    ]
    # Who sold, and why. Without this split a reader cannot tell a book that
    # the rules disposed of from one the agent actively managed — and in the
    # first 20-day replay the answer was "all 11 were stops", which is the
    # fact that made the missing target price visible.
    exit_lines = _exit_attribution(result)
    if exit_lines:
        lines.append(f"出场归因：{'；'.join(exit_lines)}")
    lines += [
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
        *_tool_call_lines(result),
        f"抛错的日-阶段               {len(result['errors'])}"
        f"{_error_kinds(result['errors'])}",
        "",
        "— 决策器计数 —",
    ]
    if ctx.counters:
        lines += [f"  {k:<28} {v:>6}" for k, v in sorted(ctx.counters.items())]
    else:
        lines.append("  （无）")
    lines += replay_capabilities.summary_lines(capability_matrix)
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


def _tool_call_lines(result: dict) -> list[str]:
    """What the agent's questions cost, next to what they bought.

    The tools changed what a decision is: measured on a 3-day window, ~23
    tool calls per decision. A report that shows the orders but not the
    traffic lets a reader compare two windows as if both cost the same —
    and the one with tools spends several times the model calls to get its
    answer. The cost is part of the result.

    Empty for a run with no model, because "0 tool calls" beside a
    placeholder run is a number about nothing.
    """
    calls = (result.get("model_calls") or {}).get("tool_calls")
    if calls is None:
        return []
    buys = sum(1 for f in result["fills"] if f.get("side") == "buy")
    per = f"{calls / buys:.1f}" if buys else "—"
    return [
        f"agent 工具调用              {calls}"
        f"（买入 {buys} 笔，约 {per} 次/笔；只有它们能让"
        f"「结果」和「代价」一起被判断）",
    ]


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


def _benchmark_lines(ctx, result: dict, start_capital: float) -> list[str]:
    """The window's return against an equal-weight market.

    Without this line "the strategy lost 1.7%" cannot be interpreted: the
    first 20-day replay returned -1.71% on a window where the equal-weight
    market returned -2.81%, so the honest reading was "it lost less than the
    market", not "the strategy is losing". Both numbers are computed the same
    way — the account's own equity against every listed name that traded on
    both sessions, equal-weighted — so they are comparable rather than merely
    adjacent.

    A window with too little history returns a line saying so rather than a
    silently absent one.
    """
    equity = result.get("equity") or []
    if len(equity) < 2:
        return []
    days = [r["date"] for r in equity]
    index, covered = 1.0, 0
    for prev_day, day in zip(days, days[1:]):
        try:
            bars_prev = ctx.corpus.bars(prev_day)
            bars_now = ctx.corpus.bars(day)
        except Exception as exc:                      # noqa: BLE001
            logger.debug("benchmark bars unavailable for %s: %s", day, exc)
            continue
        rets = []
        for code, now in bars_now.items():
            before = bars_prev.get(code)
            if not before or not before.get("close") or now.get("change_pct") is None:
                continue
            rets.append(float(now["change_pct"]) / 100)
        if rets:
            index *= 1 + sum(rets) / len(rets)
            covered += 1
    if not covered:
        return ["等权大盘      （本窗口没有可比的行情，无法计算）"]
    agent = equity[-1]["equity"] / start_capital - 1
    market = index - 1
    return [
        f"等权大盘      {market * 100:+.3f}%（{covered} 个交易日）",
        f"超额（agent − 大盘） {(agent - market) * 100:+.3f}%",
        "  （大盘 = 当日全市场有行情的标的等权平均；没有这一行，"
        "「亏了」无法解释）",
    ]


def _exit_attribution(result: dict) -> list[str]:
    """Sells split by who decided, and by what the rule was.

    ``walk_forward``-prefixed reasons are the mechanical settlement; anything
    else came from an agent. The reasons themselves are counted rather than
    only totalled, because "12 stops" and "12 take-profits" are the same
    number and opposite outcomes.
    """
    sells = [f for f in result["fills"] if f.get("side") == "sell"]
    if not sells:
        return []
    agent = [f for f in sells if str(f.get("reason", "")).startswith("agent")]
    mechanical = [f for f in sells if f not in agent]
    out = [f"机械 {len(mechanical)} 笔", f"agent {len(agent)} 笔"]

    def _kind(reason: str) -> str:
        r = str(reason or "")
        low = r.lower()
        # Agent first: its reason is free text written by a model and may
        # mention a stop without the stop having caused the exit. Checking
        # the mechanical rules first would attribute an agent's discretionary
        # sell to the rule it happened to mention.
        if r.startswith("agent卖出"):
            return "agent 清仓"
        if r.startswith("agent减仓"):
            # Trims were invisible until the second agent-exits window: 13
            # real sells never reached the report because only the
            # `agent_exit` alert type became a fill row. Named apart from a
            # full exit because "sold 5 times" and "trimmed 12 times" are
            # also the same number and different management.
            return "agent 减仓"
        if r.startswith("agent"):
            return "agent 判断"
        # The settlement's own wording, which is not the word "stop".
        # `t1_execution` fills at the ``lower`` level (the stop) or the
        # ``upper`` one (the target), and says so as "only the lower level
        # X was touched". The first version of this classifier looked for
        # "stop"/"止损" only, so seven real exits landed in 其他 — the split
        # was reporting 止损 1 when the truth was 止损 5 / 止盈 2.
        if "lower level" in low or "止损" in r or "stop" in low:
            return "止损"
        if "upper level" in low or "止盈" in r or "target" in low:
            return "止盈"
        if "到期" in r or "expire" in low:
            return "到期"
        return "其他"

    kinds = Counter(_kind(f.get("reason")) for f in sells)
    out.append("（" + "、".join(f"{k} {v}" for k, v in kinds.most_common()) + "）")
    return out


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
    parser.add_argument(
        "--selection-architecture",
        choices=(
            "dual_rank_v0", "sector_first_v0",
            "sector_first_simple_selector", "sector_first_no_flow",
            "dual_rank_price_v1", "sector_rank_price_v1"),
        default="dual_rank_v0",
        help="candidate architecture; sector-first modes are opt-in only")
    parser.add_argument(
        "--sector-membership", type=Path, default=None,
        help="PIT membership archive required by sector-first modes")
    parser.add_argument(
        "--frozen-directions", type=Path, default=None,
        help="B-arm frozen direction archive required by C")
    parser.add_argument(
        "--experiment-manifest", type=Path, default=None,
        help="frozen A/B/C/D preregistration manifest")
    parser.add_argument(
        "--experiment-arm", choices=("A", "B", "C", "D"), default=None,
        help="arm bound to --experiment-manifest")
    parser.add_argument(
        "--selection-experiment-manifest", type=Path, default=None,
        help="frozen nf_discovery_v1 manifest; separate from legacy A/B/C/D")
    parser.add_argument(
        "--selection-experiment-arm", choices=("CONTROL", "SECTOR"),
        default=None,
        help="arm bound to --selection-experiment-manifest")
    parser.add_argument(
        "--no-stop-loss", dest="mechanical_stop",
        action="store_false", default=True,
        help="关闭机械止损（实验用）")
    parser.add_argument(
        "--no-take-profit", dest="mechanical_target",
        action="store_false", default=True,
        help="关闭机械止盈（实验用）")
    parser.add_argument(
        "--agent-exits", action="store_true",
        help="让 agent 决定卖出；只影响卖出，不再隐式增加收盘买入")
    parser.add_argument(
        "--close-buys", action="store_true",
        help="显式启用 14:55 synthetic-close 买入；日线数据不构成严格盘中证据")
    parser.add_argument(
        "--no-trader-tools", dest="trader_tools",
        action="store_false", default=True,
        help="不给买入决策器工具（回到'只能从面板里挑'的旧行为；"
             "用来和会用工具的版本跑同一窗口做对照）")
    parser.add_argument(
        "--max-turns", type=int, default=None,
        help="每次买入决策允许的轮数。不传则用决策器自己的默认值"
             "（t1_decider.propose，当前 8）；一次工具调用占一轮，"
             "1 等于没有工具。定义只存在一处，避免 CLI 与函数默认值漂移。")
    parser.add_argument("--panel-size", type=int, default=40,
                        help="securities the model may choose from (llm only).")
    parser.add_argument(
        "--no-concepts", dest="concepts", action="store_false", default=True,
        help="ablation arm: strip the concept column from the panel (llm "
             "only). The prompt text, the header and every other column stay "
             "identical between the arms, so an order difference between two "
             "same-window runs is attributable to this column alone.")
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
