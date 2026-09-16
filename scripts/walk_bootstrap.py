"""Create a replay's own data directory: fresh trader state, shared corpus.

Why this exists
---------------
A walk-forward replay that starts in 2020 and runs against ``data/`` would open
2026's ``memory.db``. Even with every market reader pinned to an as-of date, the
agent's *brain* would be 2026's — its principles, its lessons, its frozen policy,
its candidates — which is knowledge leakage rather than a stale-cache problem.
Copying that file is no better: a copy is still 2026 state wearing a 2020 date.

So the isolation is a **split**, not a snapshot:

    corpus (shared by reference, read-only)   →  market_history.db, the news
                                                 store, the instrument list
    trader state (created empty, never shared) →  memory.db
    evidence (created empty, never shared)     →  llm_journal/ — the run's own
                                                 model exchanges

The design already says historical replay is a *separate run and ledger*; this
script is the mechanical part of that sentence.

What it refuses to do
---------------------
* It never copies, links or reads the corpus's ``memory.db``. That file is the
  trader, and the replay gets a new one built from the schema.
* It refuses to bootstrap into a directory that already holds trader state
  (rows in ``virtual_portfolio``, ``predictions``, ``learning_candidates``,
  ``episodes``, ``outcomes``, ``policy_versions``, ``pending_settlements``)
  unless ``--force`` is passed. A replay directory that has already run is not
  something to silently wipe.
* It does not have to check that the run *stays* read-only on the shared corpus:
  the links it plants are exactly what ``alpha_agents.data.corpus_access`` treats
  as "shared", and a shared file is opened ``mode=ro``, so a write raises at the
  SQLite layer rather than reaching the live corpus. (Before D22 these links were
  a convention and the sentence here admitted as much. Corrected rather than
  deleted: "shared by reference" and "cannot be written" are different claims,
  and only the second one is true now.)
* It does not keep an old recording. A replay's directory also holds its own LLM
  journal (``llm_journal/``, see ``alpha_agents.llm_journal``), and bootstrap
  clears it: starting a fresh run while last run's answers sat in the same
  directory would let ``replay-recorded`` serve the wrong run's conversations.

Usage
-----
    .venv/bin/python scripts/walk_bootstrap.py --target /tmp/walk-2020
    ALPHAAGENTS_DATA_DIR=/tmp/walk-2020 .venv/bin/python scripts/walk_forward.py …
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alpha_agents import llm_journal  # noqa: E402
from alpha_agents.config import PROJECT_ROOT  # noqa: E402
from alpha_agents.data.memory_store import _SCHEMA  # noqa: E402

logger = logging.getLogger("walk_bootstrap")

#: Shared by reference. The replay reads these and must never write them.
CORPUS_FILES = ("market_history.db", "market_snapshots.db", "stocks.db")

#: Created empty from the schema. This is where a trader's memory lives.
STATE_FILES = ("memory.db",)

#: Rows in any of these mean the directory already holds a trader.
STATE_TABLES = ("virtual_portfolio", "predictions", "learning_candidates",
                "episodes", "outcomes", "policy_versions",
                "pending_settlements", "theses")


def trader_state_rows(db: Path) -> dict[str, int]:
    """Row counts for the state tables that exist, zero-counts omitted.

    Missing file and missing table both answer "no state"; a table that is
    absent cannot be holding a trader.
    """
    if not db.exists():
        return {}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        present = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        out: dict[str, int] = {}
        for table in STATE_TABLES:
            if table not in present:
                continue
            count = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if count:
                out[table] = int(count)
        return out
    finally:
        con.close()


def _create_fresh(db: Path) -> None:
    """Apply the live schema to a brand-new file, so a run has its tables."""
    db.parent.mkdir(parents=True, exist_ok=True)
    if db.exists():
        db.unlink()
    con = sqlite3.connect(db)
    try:
        con.executescript(_SCHEMA)
        con.commit()
    finally:
        con.close()


def _link_corpus(source: Path, target: Path) -> None:
    """Share one corpus file by symlink, replacing any previous link."""
    if target.is_symlink() or target.exists():
        target.unlink()
    os.symlink(source, target)


def _clear_journal(target: Path) -> list[str]:
    """Remove a previous run's recordings, naming every file removed.

    Not decoration. ``replay-recorded`` serves a run's answers by position from
    ``llm_journal/<run_id>.jsonl``, so bootstrapping a fresh run into a directory
    that still held the previous run's journal would let the new run answer from
    the old conversations — evidence that is *wrong* rather than absent, which is
    the one outcome this plan exists to prevent.

    Only removes a real directory inside the target; a symlink is left alone and
    named by its presence in the returned list, not followed.
    """
    directory = target / llm_journal.JOURNAL_DIRNAME
    if directory.is_symlink() or not directory.is_dir():
        return []
    dropped = sorted(child.name for child in directory.iterdir())
    shutil.rmtree(directory)
    return dropped


def bootstrap(target: Path, corpus: Path, *, force: bool = False) -> dict:
    """Materialise a replay data directory and report exactly what it did."""
    if not corpus.is_dir():
        raise FileNotFoundError(f"corpus directory not found: {corpus}")

    held = trader_state_rows(target / "memory.db")
    if held and not force:
        raise FileExistsError(
            f"{target} already holds trader state {held}; pass --force to "
            "replace it (a replay directory that has run is not disposable "
            "by accident)"
        )

    target.mkdir(parents=True, exist_ok=True)
    dropped_journal = _clear_journal(target)

    fresh = []
    for name in STATE_FILES:
        _create_fresh(target / name)
        fresh.append(name)

    shared, missing = [], []
    for name in CORPUS_FILES:
        source = corpus / name
        if source.exists():
            _link_corpus(source, target / name)
            shared.append(name)
        else:
            missing.append(name)

    return {
        "target": str(target),
        "corpus": str(corpus),
        "fresh": fresh,
        "shared": shared,
        "missing": missing,
        "journal": str(target / llm_journal.JOURNAL_DIRNAME),
        "journal_dropped": dropped_journal,
        "state_rows": trader_state_rows(target / "memory.db"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", type=Path, required=True,
                        help="the replay's own data directory.")
    parser.add_argument("--corpus", type=Path, default=PROJECT_ROOT / "data",
                        help="where the read-only history lives.")
    parser.add_argument("--force", action="store_true",
                        help="replace a target that already holds trader state.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        report = bootstrap(args.target, args.corpus, force=args.force)
    except (FileNotFoundError, FileExistsError) as exc:
        logger.error("%s", exc)
        return 1

    print(f"replay data dir: {report['target']}")
    print(f"  fresh (empty, never inherited): {', '.join(report['fresh'])}")
    print(f"  shared by reference (each opens read-only): "
          f"{', '.join(report['shared']) or 'none'}")
    if report["missing"]:
        print(f"  absent from the corpus: {', '.join(report['missing'])}")
    print(f"  trader rows in the new state: {report['state_rows'] or 'none'}")
    if report["journal_dropped"]:
        print(f"  recordings cleared: {', '.join(report['journal_dropped'])}")
    print(f"  llm journal: {report['journal']}/")
    print()
    print("Run the replay with the data dir pointed here, and say whether this")
    print("pass is allowed to call the model:")
    print(f"  ALPHAAGENTS_DATA_DIR={report['target']}")
    print("  ALPHAAGENTS_LLM_MODE=record            # first pass: writes the journal")
    print("  ALPHAAGENTS_LLM_MODE=replay-recorded   # later passes: answers from it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
