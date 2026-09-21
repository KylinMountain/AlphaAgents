"""Import concept membership from the local THS client, offline.

Why this exists. index_builder._fetch_concept_constituents_ths scrapes the
first page of each concept detail page, and its own docstring says so:
"top stocks from the first page (usually 10-20); THS blocks ajax pagination".
The corpus therefore held **at most ten members per concept** -- 303 of 375
concepts sat at exactly ten, which is a pagination boundary and not a market
fact. Measured against the THS client, the real median is 90.

That truncation is not cosmetic. concept_stocks is the input to the
within-sector scorer, to the sector benchmark in beta_calculator and to the
live get_sector_best_stocks call, so every one of them was choosing a stock
out of the first ten names of a sector that often has hundreds. 5G holds 454
members and the corpus offered 10.

The client already stores the complete list locally, so this reads that file
instead of paging a site that blocks paging. It makes no network calls: the two
failure modes are "no client installed" and "THS changed the file format", both
of which are loud, as opposed to a silent partial scrape.

**This does not make membership point-in-time.** block_conception.ini is a
current snapshot that the client overwrites daily -- it carries one ConfigVer
and no per-member date. It fixes how *deep* the pool is, never *when* a name
joined. The lookahead caveat in replay_capabilities and _limitations stays true
after this runs.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path

from alpha_agents.data import corpus_access

logger = logging.getLogger(__name__)

#: Where the THS macOS client keeps its block files. The container is
#: TCC-protected, so reading it needs Full Disk Access for whatever process
#: runs this; arranging that is the operator problem, and a permission failure
#: is reported rather than swallowed.
DEFAULT_THS_ROOT = Path.home() / (
    "Library/Containers/cn.com.10jqka.macstockPro/Data/Documents")

#: The concept dictionary: name map and member map, in one GBK ini.
CONCEPT_FILE = "BlockUpdate/block_conception.ini"

#: 33 is Shenzhen, 17 Shanghai, -105 Beijing. The prefix is the client internal
#: market id; the corpus keys on the bare six-digit code.
CODE_RE = re.compile(r"^\d{6}$")

_NAME_SECTION = "[BLOCK_NAME_MAP_TABLE]"
_MEMBER_SECTION = "[BLOCK_STOCK_CONTEXT]"


def _read_gbk(path: Path) -> str:
    """Read a THS ini as GBK, loudly.

    errors="replace" rather than strict: one malformed byte in a 720KB file
    should not lose the other 70,000 memberships, and the replacement character
    is visible if it lands in a name.
    """
    try:
        return path.read_bytes().decode("gbk", "replace")
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"no THS concept file at {path}. The client must have run at least "
            "once, and the process reading it needs Full Disk Access.") from exc


def parse_concept_members(text: str) -> dict[str, list[str]]:
    """Concept name to code list, from one block_conception.ini body.

    Pure. A block key appears in both sections and the member line may come
    before or after the name line, so names are collected first and members are
    only kept for keys that resolved to a name -- an unnamed block is not
    something a selector can ask for.

    Member order is preserved as the file gives it. That order belongs to the
    client and is stable for a fixed file, so a diff between two imports means
    the data moved rather than the parser.
    """
    names: dict[str, str] = {}
    member_lines: dict[str, str] = {}
    section = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("["):
            section = line
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        if section == _NAME_SECTION:
            if value.strip():
                names[key.strip()] = value.strip()
        elif section == _MEMBER_SECTION and value.strip():
            member_lines[key.strip()] = value.strip()

    out: dict[str, list[str]] = {}
    for key, value in member_lines.items():
        name = names.get(key)
        if not name:
            continue
        codes: list[str] = []
        for token in value.split(","):
            token = token.strip()
            if ":" not in token:
                continue
            code = token.rpartition(":")[2].strip()
            if CODE_RE.match(code) and code not in codes:
                codes.append(code)
        if codes:
            out[name] = codes
    return out


def load_concept_members(root: Path | None = None) -> dict[str, list[str]]:
    """Read the client concept dictionary. No network, no database."""
    base = Path(root) if root is not None else DEFAULT_THS_ROOT
    return parse_concept_members(_read_gbk(base / CONCEPT_FILE))


def _importable(conn: sqlite3.Connection,
                members: dict[str, list[str]]) -> tuple[dict[str, list[str]], int]:
    """Drop codes the stocks table does not know.

    index_builder applies the same rule at scrape time. A membership row
    pointing at a code no other table holds is not a narrow pool, it is a
    dangling reference, and the live selector would then ask for a quote for a
    name that does not exist.
    """
    known = {row[0] for row in conn.execute("SELECT code FROM stocks")}
    kept: dict[str, list[str]] = {}
    dropped = 0
    for name, codes in members.items():
        good = [code for code in codes if code in known]
        dropped += len(codes) - len(good)
        if good:
            kept[name] = good
    return kept, dropped


def import_membership(db_path: Path, root: Path | None = None, *,
                      dry_run: bool = False) -> dict:
    """Write the client concept membership into db_path.

    Concepts are added, never removed: a concept the corpus already tracks but
    this file does not mention keeps its rows. The alternative -- trusting the
    client file as the whole truth -- would silently delete whatever an older
    import contributed, and a shrinking universe reads as a strategy result.

    Returns counts rather than logging them, so a caller can assert on them.
    """
    members = load_concept_members(root)
    if not members:
        raise ValueError(
            "the THS concept file parsed to zero concepts; refusing to touch "
            "the corpus, because an empty membership table makes every sector "
            "select nothing and that looks like a strategy result")

    conn = corpus_access.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        kept, dropped = _importable(conn, members)
        existing = {row[0] for row in conn.execute("SELECT name FROM concepts")}
        added_names = sorted(set(kept) - existing)
        before = conn.execute(
            "SELECT COUNT(*) FROM concept_stocks").fetchone()[0]

        if not dry_run:
            for name in sorted(kept):
                conn.execute(
                    "INSERT OR IGNORE INTO concepts (name, source) "
                    + "VALUES (?, 'ths')", (name,))
            ids = {row["name"]: row["id"] for row in conn.execute(
                "SELECT id, name FROM concepts")}
            for name, codes in kept.items():
                concept_id = ids[name]
                conn.executemany(
                    "INSERT OR IGNORE INTO concept_stocks "
                    "(concept_id, stock_code) VALUES (?, ?)",
                    [(concept_id, code) for code in codes])
            conn.commit()
        after = conn.execute(
            "SELECT COUNT(*) FROM concept_stocks").fetchone()[0]
        total_concepts = conn.execute(
            "SELECT COUNT(*) FROM concepts").fetchone()[0]
    finally:
        conn.close()

    summary = {
        "concepts_in_file": len(members),
        "concepts_importable": len(kept),
        "concepts_new": len(added_names),
        "new_names": added_names,
        "members_in_file": sum(len(v) for v in members.values()),
        "members_kept": sum(len(v) for v in kept.values()),
        "codes_dropped_unknown": dropped,
        "rows_before": before,
        "rows_after": after,
        "concepts_total": total_concepts,
        "dry_run": dry_run,
    }
    logger.info(
        "THS membership import: %d concepts, %d memberships kept, %d dropped, "
        "%d -> %d rows", summary["concepts_importable"],
        summary["members_kept"], dropped, before, after)
    return summary
