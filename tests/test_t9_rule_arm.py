"""T9: a rule arm puts one human-approved Rule in force for a branch."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from alpha_agents.data import memory_store  # noqa: E402
from alpha_agents.data import trader_learning as TLD  # noqa: E402
from alpha_agents.evolution import trader_learning as TL  # noqa: E402
from scripts import walk_branch as WB  # noqa: E402
from scripts.walk_checkpoint import CheckpointError  # noqa: E402

RULE = {"claim": "5 日累计涨幅 ≥ 10% 时买入多数是错的", "action": "buy",
        "applicable_context": "run_up_5d", "approved_by": "operator"}


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_store, "MEMORY_DB_PATH", tmp_path / "m.db",
                        raising=False)
    monkeypatch.setattr(memory_store._local, "conn", None, raising=False)
    db = memory_store._get_conn()
    yield db
    db.close()
    memory_store._local.conn = None


def test_a_valid_rule_passes():
    WB.validate_rule(RULE)


@pytest.mark.parametrize("approver", ["model", "LLM", "agent"])
def test_a_model_cannot_approve_a_rule(approver):
    with pytest.raises(CheckpointError, match="not a model"):
        WB.validate_rule({**RULE, "approved_by": approver})


def test_missing_and_unknown_fields_are_refused():
    with pytest.raises(CheckpointError, match="needs"):
        WB.validate_rule({"claim": "x", "action": "buy"})
    with pytest.raises(CheckpointError, match="Unknown"):
        WB.validate_rule({**RULE, "support_count": 999})


def test_an_installed_rule_reaches_only_its_branch(conn):
    rule_id = WB.install_rule(conn, branch_id="fork-a", rule=RULE,
                              source_date="2026-02-13")
    text = TL.inject(trader_id="default", decision_horizon="3-5d",
                     as_of="2026-02-16", run_id="fork-a", conn=conn)
    assert "RULE" in text and RULE["claim"] in text
    assert TL.inject(trader_id="default", decision_horizon="3-5d",
                     as_of="2026-02-16", run_id="fork-control",
                     conn=conn) == ""
    [event] = TLD.rule_events(rule_id, conn=conn)
    assert event["reason"] == "T9 experiment arm approved by operator"


def test_prepare_accepts_rule_arms_and_tags_every_task(tmp_path, monkeypatch):
    arms = tmp_path / "arms.json"
    arms.write_text(json.dumps([{"name": "rule_run_up", "rule": RULE}]),
                    encoding="utf-8")
    monkeypatch.setattr(WB, "verify", lambda path: {
        "args": {"decider": "llm"}, "completed_through": "2026-02-13",
        "next_session": "2026-02-16", "checkpoint_id": "c",
        "source_hash": "s", "runtime_hash": "r"})

    class _Conn:
        def execute(self, *a):
            return [("2026-02-16",), ("2026-02-17",)]

        def close(self):
            pass

    monkeypatch.setattr("sqlite3.connect", lambda *a, **k: _Conn())
    (tmp_path / "ckpt").mkdir()
    args = type("A", (), dict(
        days=2, trials=1, max_branch_sessions=10, jobs=1, timeout=100,
        checkpoint=tmp_path / "ckpt", live=True, arms=arms, seed=0))()
    _, tasks = WB.prepare(args)
    assert {t["arm"] for t in tasks} == {"control", "rule_run_up"}
    assert {t["tag"] for t in tasks} == {"run_up_5d"}
