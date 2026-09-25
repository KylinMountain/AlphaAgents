"""The trader's handbook: its words, the market's evidence, never the future.

``data/traders/<id>/MEMORY.md`` is what its decisions load. These pin that a
rewrite keeps rule ids, that evidence under a rule comes from the trades and
not from the model, that a replay morning reads only the version written
before it, and that an unreadable rewrite leaves the old handbook standing.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from alpha_agents import config
from alpha_agents.data import memory_store
from alpha_agents.evolution import handbook as HB
from alpha_agents.evolution import trade_review as TR


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr(memory_store, "MEMORY_DB_PATH", tmp_path / "memory.db",
                        raising=False)
    monkeypatch.setattr(memory_store._local, "conn", None, raising=False)
    c = memory_store._get_conn()
    yield c
    c.close()
    memory_store._local.conn = None


def _review(conn, pid, close, ret, peak, followed=(), broke=()):
    f = {"position_id": pid, "code": f"60000{pid}", "name": f"票{pid}",
         "open_date": close, "close_date": close, "sessions": 2,
         "t1_change_pct": 4.0, "peak_pct": peak, "peak_session": 0,
         "worst_pct": -5.0, "return_pct": ret, "giveback_pp": peak - ret}
    TR.save(conn, f, {"verdict": "错", "right": "", "wrong": "冲高没走",
                      "next_time": "冲高先兑现一半", "followed": list(followed),
                      "broke": list(broke),
                      "rule_versions": HB.bindings("default", before=close)}, "default")
    conn.commit()


def _patch_runner(monkeypatch, reply):
    import agents

    class _Result:
        final_output = reply

    async def run(agent, message, max_turns=2):
        return _Result()
    monkeypatch.setattr(agents.Runner, "run", staticmethod(run))


def _rewrite(conn, monkeypatch, reply, as_of):
    _patch_runner(monkeypatch, reply)
    # A model name is a valid Agent model; the runner is patched, so no call
    # leaves the process.
    return asyncio.run(HB.consolidate(conn, "default", as_of=as_of,
                                      model="stub-model"))


RULES_V1 = json.dumps({"rules": [
    {"id": "R1", "text": "买入当天冲高超过1个ATR先兑现一半", "from": [1, 2]},
    {"id": "R2", "text": "买前一日涨超4%不追", "from": [2]}]}, ensure_ascii=False)


class TestTheHandbookIsAFile:
    def test_it_is_written_where_decisions_load_it(self, conn, monkeypatch):
        _review(conn, 1, "2026-01-05", -4.8, 6.9)
        _review(conn, 2, "2026-01-06", -3.0, 2.0)
        assert _rewrite(conn, monkeypatch, RULES_V1, "2026-01-06")
        text = HB.path("default").read_text("utf-8")
        assert "## R1　买入当天冲高超过1个ATR先兑现一半" in text
        assert HB.load("default") == text.strip()

    def test_evidence_is_computed_from_the_trades(self, conn, monkeypatch):
        _review(conn, 1, "2026-01-05", -4.8, 6.9)
        _review(conn, 2, "2026-01-06", -3.0, 2.0)
        _rewrite(conn, monkeypatch, RULES_V1, "2026-01-06")
        text = HB.load("default")
        assert "来源 2 笔复盘：中位到手 -3.9%" in text
        assert "写下后还没有交易检验过" in text

    def test_later_trades_grade_the_rule(self, conn, monkeypatch):
        _review(conn, 1, "2026-01-05", -4.8, 6.9)
        _review(conn, 2, "2026-01-06", -3.0, 2.0)
        _rewrite(conn, monkeypatch, RULES_V1, "2026-01-06")
        _review(conn, 3, "2026-01-08", 5.0, 7.0, followed=["R1"])
        _review(conn, 4, "2026-01-09", -6.0, 3.0, broke=["R1"])
        _rewrite(conn, monkeypatch, RULES_V1, "2026-01-09")
        text = HB.load("default")
        assert "写下后遵守 1 笔（中位 +5.0%），违反 1 笔（中位 -6.0%）" in text


class TestRewriting:
    def test_a_kept_rule_keeps_its_id_and_its_start_date(self, conn, monkeypatch):
        _review(conn, 1, "2026-01-05", -4.8, 6.9)
        _rewrite(conn, monkeypatch, RULES_V1, "2026-01-05")
        _review(conn, 2, "2026-01-07", 1.0, 3.0)
        _rewrite(conn, monkeypatch, RULES_V1, "2026-01-07")
        rules = {r["id"]: r for r in HB._rules("default")}
        assert rules["R1"]["since"] == "2026-01-05"

    def test_a_bad_or_duplicate_id_gets_a_new_one(self, conn, monkeypatch):
        _review(conn, 1, "2026-01-05", -4.8, 6.9)
        reply = json.dumps({"rules": [{"id": "R1", "text": "a", "from": [1]},
                                      {"id": "R1", "text": "b", "from": [1]},
                                      {"id": "x", "text": "c", "from": []}]})
        _rewrite(conn, monkeypatch, reply, "2026-01-05")
        assert HB.rule_ids("default") == ["R1", "R2", "R3"]

    def test_an_unreadable_rewrite_keeps_the_old_handbook(self, conn, monkeypatch):
        _review(conn, 1, "2026-01-05", -4.8, 6.9)
        _rewrite(conn, monkeypatch, RULES_V1, "2026-01-05")
        before = HB.load("default")
        _review(conn, 2, "2026-01-07", 1.0, 3.0)
        assert _rewrite(conn, monkeypatch, "我觉得都挺好", "2026-01-07") is False
        assert HB.load("default") == before

    def test_no_model_writes_nothing(self, conn):
        _review(conn, 1, "2026-01-05", -4.8, 6.9)
        assert asyncio.run(HB.consolidate(conn, "default", as_of="2026-01-05",
                                          model=None)) is None
        assert HB.load("default") == ""


class TestNoFutureRules:
    def test_a_replay_morning_reads_the_version_before_it(self, conn, monkeypatch):
        _review(conn, 1, "2026-01-05", -4.8, 6.9)
        _rewrite(conn, monkeypatch, RULES_V1, "2026-01-05")
        assert HB.load("default", before="2026-01-05") == ""
        assert "R1" in HB.load("default", before="2026-01-06")

    def test_the_decision_block_leads_with_the_handbook(self, conn, monkeypatch):
        _review(conn, 1, "2026-01-05", -4.8, 6.9)
        _rewrite(conn, monkeypatch, RULES_V1, "2026-01-05")
        block = TR.inject(conn, "default", before="2026-01-06")
        assert block.index("我的交易守则") < block.index("【你自己的逐笔复盘】")

    def test_a_review_marks_the_rules_it_followed_and_broke(self):
        got = TR._parse('{"verdict":"错","wrong":"x","next_time":"y",'
                        '"followed":["r1"],"broke":["R2"]}')
        assert got["followed"] == ["R1"] and got["broke"] == ["R2"]
