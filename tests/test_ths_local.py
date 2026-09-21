"""The offline THS membership importer.

Why these exist. The corpus held at most ten members per concept because
index_builder scrapes only the first page of a paginated page -- 303 of 375
concepts sat at exactly ten. The client keeps the complete list locally, so
import_membership reads that instead.

What they pin:

1. the parser reads the GBK two-section ini correctly, including market
   prefixes and duplicate codes;
2. unknown codes are dropped, because a membership row must not dangle;
3. an empty parse **raises** rather than emptying the corpus -- an empty
   universe makes every sector select nothing and that reads as a result;
4. existing concepts are never deleted.
"""

import sqlite3

import pytest

from alpha_agents.data import ths_local as T


def _ini(names, members):
    lines = ["[ConfigInfo]", "ConfigVer=20260921.160002|20260921",
             "[BLOCK_NAME_MAP_TABLE]"]
    for key, name in names.items():
        lines.append(f"{key}={name}")
    lines.append("[BLOCK_STOCK_CONTEXT]")
    for key, value in members.items():
        lines.append(f"{key}={value}")
    return chr(10).join(lines)


def _corpus(tmp_path, stocks):
    path = tmp_path / "stocks.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE stocks (code TEXT PRIMARY KEY, name TEXT NOT NULL)")
    conn.execute("CREATE TABLE concepts (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                 " name TEXT NOT NULL UNIQUE, source TEXT NOT NULL DEFAULT 'ths')")
    conn.execute("CREATE TABLE concept_stocks (concept_id INTEGER NOT NULL,"
                 " stock_code TEXT NOT NULL, PRIMARY KEY (concept_id, stock_code))")
    conn.executemany("INSERT INTO stocks VALUES (?, ?)",
                     [(c, c) for c in stocks])
    conn.commit()
    conn.close()
    return path


def _root(tmp_path, text):
    root = tmp_path / "ths"
    (root / "BlockUpdate").mkdir(parents=True)
    (root / T.CONCEPT_FILE).write_bytes(text.encode("gbk"))
    return root


class TestTheParser:
    def test_it_reads_names_and_members_together(self):
        text = _ini({"CBCC": "国产操作系统"},
                    {"CBCC": "33:000032,17:600100"})
        assert T.parse_concept_members(text) == {
            "国产操作系统": ["000032", "600100"]}

    def test_it_keeps_the_member_order_the_file_gives(self):
        """Order is the client own and stable, so a diff means data moved."""
        text = _ini({"A1": "X"}, {"A1": "17:600003,33:000001,17:600002"})
        assert T.parse_concept_members(text)["X"] == [
            "600003", "000001", "600002"]

    def test_it_drops_duplicate_codes(self):
        text = _ini({"A1": "X"}, {"A1": "33:000001,17:000001"})
        assert T.parse_concept_members(text)["X"] == ["000001"]

    def test_it_ignores_a_block_with_no_name(self):
        """An unnamed block is not something a selector can ask for."""
        text = _ini({}, {"A1": "33:000001"})
        assert T.parse_concept_members(text) == {}

    def test_it_ignores_tokens_that_are_not_six_digit_codes(self):
        text = _ini({"A1": "X"}, {"A1": "33:000001,bogus,17:600100"})
        assert T.parse_concept_members(text)["X"] == ["000001", "600100"]


class TestTheImport:
    def test_it_writes_the_full_membership(self, tmp_path):
        text = _ini({"A1": "国产操作系统", "A2": "光刻胶"},
                    {"A1": "33:000032,17:600100", "A2": "33:000032"})
        db = _corpus(tmp_path, ["000032", "600100"])
        summary = T.import_membership(db, _root(tmp_path, text))
        assert summary["rows_after"] == 3
        assert summary["concepts_total"] == 2

    def test_it_drops_codes_the_stocks_table_does_not_know(self, tmp_path):
        """A row pointing nowhere is a dangling reference, not a narrow pool."""
        text = _ini({"A1": "X"}, {"A1": "33:000001,33:920999"})
        db = _corpus(tmp_path, ["000001"])
        summary = T.import_membership(db, _root(tmp_path, text))
        assert summary["codes_dropped_unknown"] == 1
        assert summary["rows_after"] == 1

    def test_an_empty_parse_raises_rather_than_emptying_the_corpus(self, tmp_path):
        """Empty membership makes every sector select nothing, which reads as
        a strategy result rather than as missing data."""
        db = _corpus(tmp_path, ["000001"])
        root = _root(tmp_path, "[ConfigInfo]" + chr(10) + "[BLOCK_NAME_MAP_TABLE]")
        with pytest.raises(ValueError, match="zero concepts"):
            T.import_membership(db, root)

    def test_existing_concepts_are_never_removed(self, tmp_path):
        """A shrinking universe reads as a strategy result."""
        text = _ini({"A1": "New"}, {"A1": "33:000001"})
        db = _corpus(tmp_path, ["000001"])
        conn = sqlite3.connect(db)
        conn.execute("INSERT INTO concepts (name) VALUES ('Preexisting')")
        conn.commit()
        conn.close()
        T.import_membership(db, _root(tmp_path, text))
        conn = sqlite3.connect(db)
        names = {r[0] for r in conn.execute("SELECT name FROM concepts")}
        conn.close()
        assert names == {"Preexisting", "New"}

    def test_a_dry_run_changes_nothing(self, tmp_path):
        text = _ini({"A1": "X"}, {"A1": "33:000001"})
        db = _corpus(tmp_path, ["000001"])
        summary = T.import_membership(db, _root(tmp_path, text), dry_run=True)
        assert summary["rows_after"] == summary["rows_before"] == 0
        assert summary["concepts_new"] == 1

    def test_re_importing_is_idempotent(self, tmp_path):
        text = _ini({"A1": "X"}, {"A1": "33:000001,33:000002"})
        db = _corpus(tmp_path, ["000001", "000002"])
        root = _root(tmp_path, text)
        first = T.import_membership(db, root)["rows_after"]
        second = T.import_membership(db, root)["rows_after"]
        assert first == second == 2


class TestItReportsAMissingClient:
    def test_a_missing_file_names_the_cause(self, tmp_path):
        """The two failure modes are "no client" and "format changed"; both
        should be loud rather than a silent partial import."""
        with pytest.raises(FileNotFoundError, match="Full Disk Access"):
            T.load_concept_members(tmp_path / "nowhere")
