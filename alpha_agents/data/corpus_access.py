"""Read-only access to a corpus that is shared rather than owned.

The replay model is: history is shared by reference, trader state is fresh.
``scripts/walk_bootstrap.py`` expresses "shared" as a **symlink**, so a link is
the fact this module reads — a plain file in our own data directory is ours to
write, a link into the corpus is not.

Why enforcement instead of a convention
---------------------------------------
An honourable agreement fails silently here. A replay that ran an ingest step
would append 2020 rows to a corpus that every other reader — the live pipeline
included — treats as the record, and nothing would say so. So a shared file is
opened ``mode=ro`` at the SQLite layer and a write raises

    sqlite3.OperationalError: attempt to write a readonly database

which is a hard stop rather than a corrupted corpus discovered a week later.

Two things follow for callers, and both are why the stores call :func:`connect`
instead of ``sqlite3.connect``:

* **Do not apply the schema to a shared file.** Creating a missing table is a
  write, and on shared history the tables are the corpus's business, not ours.
* **Do not set WAL on a shared file.** The pragma writes to the database header.

Neither is checked here; the stores skip both when :func:`is_shared` is true.
Verifying that they do is the job of the tests, not of this module.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def is_shared(path: str | Path) -> bool:
    """True when ``path`` points into a corpus rather than at our own data."""
    return Path(path).is_symlink()


def connect(path: str | Path, **kwargs) -> sqlite3.Connection:
    """Open ``path`` — read-only when it is shared, read-write when it is ours.

    ``kwargs`` pass through to :func:`sqlite3.connect` unchanged, so a caller
    keeps its own ``check_same_thread`` / ``isolation_level`` choices and this
    function only decides the mode.
    """
    target = Path(path)
    if is_shared(target):
        return sqlite3.connect(f"file:{target}?mode=ro", uri=True, **kwargs)
    return sqlite3.connect(str(target), **kwargs)
