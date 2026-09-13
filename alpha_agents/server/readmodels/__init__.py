"""The three workspaces, reading facts the kernel already records.

§14 names Phase 5 as "Trader, episode, and experiment read models across APIs
and the existing frontend", and states its completion meaning: *"Trade
workspace, Learn journal, and Evolve laboratory expose the same facts."*

**Why a read model needs its own module.** All of the facts are in the
database and most already have readers — but the readers are ops scripts and
tests. Nothing said, in one place, what a workspace shows and which table each
number comes from. Assembling that inside a route handler makes the API the
projection, and two routes that show the same number become two projections
that can disagree. So the projection lives here and the route stays a route.

**Three states, because one "empty" would be a lie.** Measured on the
production database, 2026-09-13: 117 portfolio rows and 40 theses, so Trade
has facts; `episodes` and `outcomes` exist and hold zero rows, so Learn is
wired and has not run; `policy_versions` does not exist at all while
`gate_decisions` exists with a schema older than the code, so Evolve is not
merely empty. Rendering those three the same way collapses "not yet run",
"never wired" and "schema behind the code" into one word.

**A read model must not migrate.** Every data module's reader calls its own
``init_schema`` on entry — ``scripts/episode_coverage.py`` had to document
that its "read-only" report rewrites the schema of an older database. A page
that does the same turns opening a browser tab into a schema change, and
"who changed production" stops having one answer. :func:`section` therefore
probes first and refuses to call the reader when the tables or columns it
declares are missing; the missing pieces are reported, never repaired.
"""

from __future__ import annotations

import importlib
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

WORKSPACES = ("trade", "learn", "evolve")

#: How complete this database's schema is, relative to what a section declares.
SCHEMA_COMPLETE = "complete"
SCHEMA_PARTIAL = "partial"
SCHEMA_ABSENT = "absent"

#: What a reader can do with the section. Orthogonal to ``schema``: an
#: incomplete schema reports ``unavailable`` whatever the row count would have
#: been, because from here that count is not knowable.
STATE_UNAVAILABLE = "unavailable"
STATE_EMPTY = "empty"
STATE_PRESENT = "present"

ABSENT_NOTE = (
    "依赖的表在本库不存在。它们由各数据模块的 init_schema 在首次读写时建立；"
    "读模型不建表 —— 打开页面不应改 schema，补 schema 是运维动作。"
)
PARTIAL_NOTE = (
    "表存在但缺列：本库的 schema 落后于当前代码。读模型只报状态，不补列。"
)


@dataclass(frozen=True)
class Need:
    """One table a section reads, and the columns it actually depends on.

    Declaring columns is what makes "schema behind the code" visible instead
    of theoretical: ``gate_decisions`` gained ``evidence_scope`` in Phase 4,
    and a database that predates it has the table without the column.
    """

    table: str
    columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class Section:
    """A read model built from a probe plus a reader.

    ``read`` is called **only** when every declared table and column is
    present, and it returns ``(value, rows)`` — ``rows`` explicitly, rather
    than having :func:`section` guess emptiness from the shape of ``value``.
    """

    source: str
    needs: tuple[Need, ...]
    read: Callable[[], tuple[Any, int]]
    note: str = ""


def load(name: str):
    """The module for one workspace, imported lazily.

    Lazy so the contract module can be imported by every workspace module
    without a cycle: ``readmodels.trade`` imports ``readmodels``, and this
    only reaches back into ``readmodels.trade`` when asked.
    """
    if name not in WORKSPACES:
        raise KeyError(f"未知工作台 {name!r}，只有 {WORKSPACES}")
    return importlib.import_module(f"{__name__}.{name}")


def table_names(conn: sqlite3.Connection) -> set[str]:
    """Every table in this database, sorted so callers can diff it."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row[0] for row in rows}


def _missing(conn: sqlite3.Connection, needs: tuple[Need, ...]) -> list[str]:
    """Declared tables and columns this database does not have."""
    present = table_names(conn)
    out: list[str] = []
    for need in needs:
        if need.table not in present:
            out.append(need.table)
            continue
        have = {row[1] for row in
                conn.execute(f"PRAGMA table_info({need.table})").fetchall()}
        out.extend(f"{need.table}.{name}"
                   for name in need.columns if name not in have)
    return out


def _schema_state(conn: sqlite3.Connection,
                  needs: tuple[Need, ...]) -> tuple[str, list[str]]:
    """``(complete | partial | absent, what is missing)`` for one section.

    ``absent`` means *every* declared table is missing — the module that owns
    them has never run here. Anything else that is missing is ``partial``: the
    database is older than the code.
    """
    if not needs:
        return SCHEMA_COMPLETE, []
    gone = _missing(conn, needs)
    if not gone:
        return SCHEMA_COMPLETE, []
    tables = {need.table for need in needs}
    present = table_names(conn)
    if not (tables & present):
        return SCHEMA_ABSENT, gone
    return SCHEMA_PARTIAL, gone


def section(conn: sqlite3.Connection, spec: Section) -> dict:
    """One fact group: its source, what it declared, what could be read.

    ``needs`` travels in the payload on purpose. A page that is told "the
    table is missing" should also be able to say *which* table it asked for
    and what it wanted from it, without re-deriving that from a second list
    that could disagree with this one.
    """
    schema, gone = _schema_state(conn, spec.needs)
    out: dict = {
        "source": spec.source,
        "needs": [{"table": need.table, "columns": list(need.columns)}
                  for need in spec.needs],
        "schema": schema,
        "state": STATE_UNAVAILABLE,
        "rows": None,
        "value": None,
        "missing": gone,
        "note": spec.note,
        # Kept apart from ``note`` on purpose. ``note`` is what the section is
        # about; this is why it could not be read. Letting the first override
        # the second would hide a schema that is older than the code behind a
        # sentence about the domain — the exact failure this field exists for.
        "status_note": "",
    }
    if schema == SCHEMA_ABSENT:
        out["status_note"] = ABSENT_NOTE
        return out
    if schema == SCHEMA_PARTIAL:
        out["status_note"] = PARTIAL_NOTE
        return out
    value, rows = spec.read()
    out["value"] = value
    out["rows"] = int(rows)
    out["state"] = STATE_EMPTY if not rows else STATE_PRESENT
    return out


@dataclass
class Payload:
    """A workspace's read model.

    ``code`` carries facts about the shipped code rather than the database —
    which producers are registered, which of them the promotion path accepts.
    They belong in the payload because the difference between "no data yet"
    and "this build cannot produce that data" is exactly the difference a
    reader needs and the database cannot express.
    """

    workspace: str
    sections: dict = field(default_factory=dict)
    code: dict = field(default_factory=dict)

    def build(self) -> dict:
        return {
            "workspace": self.workspace,
            "generated_at": datetime.now(timezone.utc)
                           .isoformat(timespec="seconds"),
            "states": {name: sec["state"]
                       for name, sec in self.sections.items()},
            "sections": self.sections,
            "code": self.code,
        }


def workspace(name: str, sections: dict, code: dict | None = None) -> dict:
    """Assemble one workspace payload from already-built sections."""
    return Payload(name, sections, code or {}).build()
