"""Unexplained inactivity and unversioned experience are not valid evidence."""

import asyncio
import json
import sqlite3

import pytest

from alpha_agents.agents import t1_decider as D
from alpha_agents.data import opportunity_journal as OJ
from alpha_agents.evolution import day_record as DR
from alpha_agents.evolution import handbook as HB
from alpha_agents.evolution import trade_review as TR
from tests.test_handbook import conn, _review, _rewrite  # noqa: F401
from tests.test_trade_review import hist, _close  # noqa: F401


def _rules(text="Only trade queue leaders"):
    return json.dumps({"rules": [{"id": "R2", "text": text, "from": [1]}]})


@pytest.mark.parametrize("reason", [None, "", "  ", [], 4, False])
def test_empty_orders_need_a_real_explanation(reason):
    result = D.parse_orders(json.dumps({"orders": [], "no_trade_reason": reason}), {"600001"})
    assert result["orders"] == []
    assert result["parse_error"]
    assert result["decision_status"] == "incomplete"


def test_unexplained_empty_orders_are_not_abstention():
    assert D.parse_orders('{"orders": []}', {"600001"})["parse_error"]


def test_explained_abstention_preserves_rules_and_rejections():
    payload = {"orders": [], "no_trade_reason": "R2 excludes these candidates",
               "rejected": [{"code": "600001", "reason": "Not in queue", "rule_ids": ["R2"]}]}
    result = D.parse_orders(json.dumps(payload), {"600001"})
    assert result["parse_error"] is None
    assert result["decision_status"] == "abstained"
    assert result["no_trade_reason"] == payload["no_trade_reason"]
    assert result["rejected"] == payload["rejected"]


def test_legacy_top_level_reason_is_preserved():
    result = D.parse_orders('{"orders": [], "reason": "No entry"}', {"600001"})
    assert result["parse_error"] is None
    assert result["no_trade_reason"] == "No entry"


@pytest.mark.parametrize("rejected", [None, {}, [None], [{"code": {}, "reason": "x"}],
    [{"code": "600009", "reason": "x"}], [{"code": "600001", "reason": ""}],
    [{"code": "600001", "reason": "x", "rule_ids": "R2"}]])
def test_malformed_rejections_are_named(rejected):
    result = D.parse_orders(json.dumps({"orders": [], "no_trade_reason": "x", "rejected": rejected}), {"600001"})
    assert result["orders"] == []
    if isinstance(rejected, list):
        assert result["parse_error"] is None
        assert result["decision_status"] == "refused"
        assert result["refused"][0]["why"] == "invalid_rejected"
    else:
        # A malformed collection has no per-item boundary at which to recover.
        assert result["parse_error"]


def test_a_code_cannot_be_ordered_and_rejected():
    result = D.parse_orders(json.dumps({"orders": [{"code": "600001", "entry_high": 10,
        "stop_loss": 9}], "rejected": [{"code": "600001", "reason": "No entry"}]}), {"600001"})
    assert result["parse_error"] and result["orders"] == []


def test_valid_empty_rules_clear_current_but_not_past(conn, monkeypatch):
    _review(conn, 1, "2026-01-05", -2, 1)
    assert _rewrite(conn, monkeypatch, _rules(), "2026-01-05")
    assert _rewrite(conn, monkeypatch, '{"rules": []}', "2026-01-06")
    assert HB.rule_ids("default") == []
    assert "Only trade" not in HB.load("default")
    assert "Only trade" in HB.load("default", before="2026-01-06")
    assert "Only trade" not in HB.load("default", before="2026-01-07")
    payload = json.loads((HB._history("default") / "2026-01-06.json").read_text())
    assert payload["removed"][0]["id"] == "R2"
    assert "rule_ref" in payload["removed"][0]


@pytest.mark.parametrize("items", [[{}], [None], [{"id": "R2", "text": ""}],
    [{"id": "R2", "text": []}], [{"id": "R2", "text": "x", "from": 4}],
    [{"id": "R2", "text": "valid"}, {"id": "R3", "text": ""}]])
def test_malformed_rules_cannot_clear_or_partially_replace(conn, monkeypatch, items):
    _review(conn, 1, "2026-01-05", -2, 1)
    _rewrite(conn, monkeypatch, _rules(), "2026-01-05")
    original = HB.load("default")
    assert _rewrite(conn, monkeypatch, json.dumps({"rules": items}), "2026-01-06") is False
    assert HB.load("default") == original


def test_reworded_rule_starts_a_new_version(conn, monkeypatch):
    _review(conn, 1, "2026-01-05", -2, 1)
    _rewrite(conn, monkeypatch, _rules(), "2026-01-05")
    old = HB._rules("default")[0]
    _rewrite(conn, monkeypatch, _rules("Prefer queue leaders"), "2026-01-06")
    new = HB._rules("default")[0]
    assert old["version"] == 1 and new["version"] == 2
    assert new["since"] == "2026-01-06"
    assert old["content_hash"] != new["content_hash"]
    assert HB.rule_ref(old) != HB.rule_ref(new)


def test_unchanged_rule_and_whitespace_keep_version(conn, monkeypatch):
    _review(conn, 1, "2026-01-05", -2, 1)
    _rewrite(conn, monkeypatch, _rules(), "2026-01-05")
    old = HB._rules("default")[0]
    _rewrite(conn, monkeypatch, _rules("Only  trade queue leaders"), "2026-01-06")
    new = HB._rules("default")[0]
    assert HB.rule_ref(new) == HB.rule_ref(old)
    assert new["since"] == "2026-01-05"


def test_clear_and_reintroduce_does_not_reuse_evidence_identity(conn, monkeypatch):
    _review(conn, 1, "2026-01-05", -2, 1)
    _rewrite(conn, monkeypatch, _rules(), "2026-01-05")
    old = HB._rules("default")[0]
    _rewrite(conn, monkeypatch, '{"rules": []}', "2026-01-06")
    _rewrite(conn, monkeypatch, _rules(), "2026-01-07")
    new = HB._rules("default")[0]
    assert new["version"] == 2 and new["since"] == "2026-01-07"
    assert HB.rule_ref(new) != HB.rule_ref(old)


def test_forward_evidence_requires_order_date_and_exact_binding():
    rule = {"id": "R2", "text": "New rule", "version": 2, "since": "2026-01-06", "from": []}
    def row(pid, order, version):
        return ({"position_id": pid, "order_date": order, "close_date": "2026-01-10", "return_pct": 4},
                {"followed": ["R2"], "rule_versions": {"R2": version}})
    rows = [row(1, "2026-01-05", HB.rule_ref(rule)),
            row(2, "2026-01-07", "wrong-version"), row(3, "2026-01-07", HB.rule_ref(rule)),
            ({"position_id": 4, "open_date": "2026-01-07", "close_date": "2026-01-10", "return_pct": 50},
             {"followed": ["R2"]})]
    result = HB.evidence(rule, rows)
    assert "\u9075\u5b88 1 \u7b14" in result
    assert "+4.0%" in result and "+50.0%" not in result


def test_review_uses_order_time_rules_not_close_time_rules(conn, hist, monkeypatch):
    _review(conn, 9, "2026-01-05", -2, 1)
    _rewrite(conn, monkeypatch, _rules("Old entry rule"), "2026-01-05")
    entry_bindings = HB.bindings("default", before="2026-01-16")
    _rewrite(conn, monkeypatch, _rules("Later rule"), "2026-01-18")
    _close(conn)
    seen = []
    async def words(f, *, model, trader=None, rules=""):
        seen.append(rules)
        return {"verdict": "reviewed", "next_time": "testable rule", "followed": ["R2", "R999"], "broke": [],
                "rule_versions": {"R2": "model invented this"}}
    monkeypatch.setattr(TR, "write_words", words)
    asyncio.run(TR.review_closed(conn, hist, trader_id="default", as_of="2026-01-19",
                                 model="stub-model", handbook_before="2026-01-19"))
    assert "Old entry rule" in seen[0] and "Later rule" not in seen[0]
    lesson = json.loads(conn.execute("SELECT lesson_json FROM trade_reviews WHERE position_id=1").fetchone()[0])
    assert lesson["rule_versions"] == entry_bindings
    assert lesson["followed"] == ["R2"]


def test_manual_edit_is_not_certified_as_the_old_binding(conn, monkeypatch):
    _review(conn, 1, "2026-01-05", -2, 1)
    _rewrite(conn, monkeypatch, _rules(), "2026-01-05")
    HB.path("default").write_text("Manual unversioned rule")
    assert HB.bindings("default") == {}
    assert HB.bindings("default", before="2026-01-06")


def test_planner_reason_reaches_close_review_from_immutable_record(conn):
    detail = {"status": "abstained", "no_trade_reason": "Blocked by R2",
              "rejected": [{"code": "600001", "reason": "Not in queue", "rule_ids": ["R2"]}]}
    OJ.record_decision(run_id="test", trader_id="default", day="2026-01-06", phase="open",
        information_cutoff="2026-01-06 09:00:00", panel=[{"code": "600001"}], orders=[],
        refusals=[], research=None, parse_error=None, raw="{}", conn=conn,
        context={"decision_explanation": detail})
    text = DR.plans_text(conn, trader_id="default", day="2026-01-06")
    assert "Blocked by R2" in text and "Not in queue" in text
    assert "Blocked by R2" not in DR.plans_text(conn, trader_id="other", day="2026-01-06")
    assert "Blocked by R2" not in DR.plans_text(conn, trader_id="default", day="2026-01-05")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE opportunity_contexts SET context_json='{}'")


def test_incomplete_plan_is_not_rendered_as_successful_abstention(conn):
    OJ.record_decision(run_id="test", trader_id="default", day="2026-01-06", phase="open",
        information_cutoff="2026-01-06 09:00:00", panel=[{"code": "600001"}], orders=[],
        refusals=[], research=None, parse_error="missing explanation", raw="{}", conn=conn)
    text = DR.plans_text(conn, trader_id="default", day="2026-01-06")
    assert "missing explanation" in text and "\u4e0d\u662f\u4e3b\u52a8\u89c2\u671b" in text
