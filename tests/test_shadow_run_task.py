"""The daily half of the experiment surface: feed it, grade it, report progress.

Before this task existed, ``shadow.open_run`` / ``emit_for_date`` /
``holdout_gate.run_gate`` had no caller but the tests — and a mechanism whose
only caller is a test is, operationally, not there. That is the whole of D7's
title, and this is the half of the answer that runs by itself.

The task is deliberately small: it emits and it grades. What it must never do is
ask the gate (a verdict is evidence for a person's decision, and a daily
"insufficient" row is governance in shape only) or take the trading day down
with it (the shadow branch is graded and never traded).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from alpha_agents.data import memory_store, policy_registry as PR
from alpha_agents.data import scoring
from alpha_agents.evolution import holdout_gate, shadow
from alpha_agents.pipeline.tasks.shadow_run import run_shadow_run

CANDIDATE = "600000"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_store, "MEMORY_DB_PATH",
                        tmp_path / "memory.db", raising=False)
    monkeypatch.setattr(memory_store._local, "conn", None, raising=False)
    yield
    conn = getattr(memory_store._local, "conn", None)
    if conn is not None:
        conn.close()
    memory_store._local.conn = None


def _day(offset: int) -> str:
    return (datetime.now().date() + timedelta(days=offset)).strftime("%Y-%m-%d")


def _sources(tag: str, decision: dict | None = None) -> dict:
    return {
        "prompts": {"morning_scan.md": tag},
        "model": {"agent_model": "qwen-plus"},
        "retrieval": {"feedback._PLAYBOOKS_BUDGET": 400},
        "rules": {"holdout_gate.MIN_VALIDATION_SAMPLES": 20},
        "knowledge": {"snapshot_id": None},
        "decision": decision or dict(scoring.DEFAULT_DECISION_PARAMS),
    }


@pytest.fixture()
def experiment(store) -> dict:
    """An incumbent in force, and an open experiment on a distinct candidate."""
    incumbent = PR.freeze(sources=_sources("incumbent"), created_by="kylin",
                          reason="the incumbent", frozen_at=_day(-30))
    PR.install(version_id=incumbent, actor="kylin", reason="first policy")
    target = PR.freeze(
        sources=_sources("candidate",
                         {**scoring.DEFAULT_DECISION_PARAMS, "dim_step": 0.09}),
        created_by="kylin", reason="a bolder mapping", frozen_at=_day(-30))
    run_id = shadow.open_run(policy_version_id=target, reason="measure it",
                             report_type="morning",
                             producer=shadow.CANDIDATE_NAME,
                             opened_at=_day(-30))
    return {"incumbent": incumbent, "target": target, "run": run_id}


def _champion(date: str, code: str, *, report_type: str = "morning") -> int:
    return memory_store.save_prediction(
        date, report_type, code, "X", "看多", "high", "t", 1.0, "reason",
        prob=0.6, trader_id="default", horizon_days=5)


class TestItFeedsAndGrades:
    def test_nothing_open_is_a_quiet_no_op(self, store):
        """No experiment is not a failure, and a daily "nothing happened"
        report is noise in a feed a person reads."""
        assert asyncio.run(run_shadow_run()) is None
        assert shadow.runs() == []

    def test_an_open_run_gets_the_days_panel(self, store, experiment):
        _champion(_day(0), CANDIDATE)
        report = asyncio.run(run_shadow_run())

        assert f"run #{experiment['run']}" in report
        assert "写入 1 条预测" in report
        rows = shadow.predictions_for(experiment["run"])
        assert [(r["date"], r["code"]) for r in rows] == [(_day(0), CANDIDATE)]
        # Its own version's mapping, not the one in force.
        assert rows[0]["prob"] == pytest.approx(0.58)

    def test_an_empty_panel_is_reported_not_passed_off_as_quiet(
            self, store, experiment):
        """A day the champion did not pick is not a day the challenger agreed
        with it; the difference decides whether the panel is even paired."""
        report = asyncio.run(run_shadow_run())
        assert "没有可配对的面板" in report
        assert shadow.predictions_for(experiment["run"]) == []

    def test_grading_reports_the_three_counts_apart(self, store, experiment):
        """D10's split, on the challenger side: a window the market has not
        traded shut is not a data gap."""
        shadow.emit_for_date(experiment["run"], _day(-10),
                            panel=[CANDIDATE], horizon_days=5)
        report = asyncio.run(run_shadow_run())
        assert "0 已评 / 1 窗口未收 / 0 已删失" in report

    def test_the_report_carries_the_progress_meter(self, store, experiment):
        report = asyncio.run(run_shadow_run())
        assert (f"配对进度 0/{holdout_gate.MIN_VALIDATION_SAMPLES}"
                in report)
        assert "还差" in report

    def test_it_is_idempotent_within_a_day(self, store, experiment):
        """Re-running does not double the panel: two rows for one stock would
        be two forecasts, and the paired test would count it twice."""
        _champion(_day(0), CANDIDATE)
        asyncio.run(run_shadow_run())
        asyncio.run(run_shadow_run())
        assert len(shadow.predictions_for(experiment["run"])) == 1


class TestItDoesNotAskEarly:
    def test_no_verdict_before_the_sample_is_there(self, store, experiment):
        """A verdict is evidence for a person's decision, and there is none to
        give yet."""
        _champion(_day(0), CANDIDATE)
        asyncio.run(run_shadow_run())
        assert holdout_gate.get_gate_decisions() == []
        assert PR.active()["version_id"] == experiment["incumbent"]


class TestItAsksTheGateOnce:
    """§12's stopping rule, as code: one look, at the preregistered point.

    Asking daily would be optional stopping — many looks at one experiment, and
    "one of them said promote" stops being evidence. It would also write a
    near-identical `insufficient` row every day until the count filled, which is
    the shape of governance without the substance.

    ``coverage`` is stubbed to say the sample is there; the question itself, the
    grading and the emit are real. Twenty paired days of champion and challenger
    are what the real path needs, and building them is
    ``test_gate_candidate_bound``'s job, not this one's.
    """

    def _ready(self, monkeypatch, experiment, *, paired=20, verdicts=()):
        calls: list = []
        monkeypatch.setattr(shadow, "coverage", lambda *a, **k: {"runs": [
            {"run_id": experiment["run"],
             "policy_version_id": experiment["target"],
             "status": "open", "report_type": "morning",
             "opened_at": _day(-30), "forecasts": paired, "scored": paired,
             "scored_days": paired, "paired": paired,
             "needed": holdout_gate.MIN_VALIDATION_SAMPLES,
             "remaining": max(0, holdout_gate.MIN_VALIDATION_SAMPLES - paired)}]})
        monkeypatch.setattr(holdout_gate, "get_gate_decisions",
                            lambda *a, **k: list(verdicts))
        monkeypatch.setattr(holdout_gate, "run_gate", lambda version, **kw: (
            calls.append(version) or {
                "outcome": "promote",
                "validation_days": holdout_gate.MIN_VALIDATION_SAMPLES,
                "evidence_scope": "candidate_policy",
                "n": holdout_gate.MIN_VALIDATION_SAMPLES,
                "reason": "beat the champion on a paired panel"}))
        return calls

    def test_it_asks_once_when_the_sample_is_there(self, store, experiment,
                                                  monkeypatch):
        calls = self._ready(monkeypatch, experiment)
        _champion(_day(0), CANDIDATE)
        report = asyncio.run(run_shadow_run())
        assert calls == [experiment["target"]]
        assert "裁决已记录：promote" in report
        assert "人工 approve" in report

    def test_it_does_not_ask_again_the_next_day(self, store, experiment,
                                               monkeypatch):
        """One verdict at the bar is the stopping rule. A second look would be
        the same evidence counted twice."""
        calls = self._ready(monkeypatch, experiment, verdicts=[{
            "policy_version_id": experiment["target"], "run_id": experiment["run"],
            "outcome": "promote",
            "validation_days": holdout_gate.MIN_VALIDATION_SAMPLES}])
        report = asyncio.run(run_shadow_run())
        assert calls == []
        assert "已裁决：promote" in report
        assert "人工 approve" in report

    def test_a_person_looking_early_does_not_suppress_the_ask(
            self, store, experiment, monkeypatch):
        """A manual `gate` before the count is reached is short of the bar, so
        the preregistered question still gets asked."""
        calls = self._ready(monkeypatch, experiment, verdicts=[{
            "policy_version_id": experiment["target"], "run_id": experiment["run"],
            "outcome": "insufficient", "validation_days": 3}])
        asyncio.run(run_shadow_run())
        assert calls == [experiment["target"]]

    def test_it_still_asks_when_the_gate_is_ready_but_the_panel_is_empty(
            self, store, experiment, monkeypatch):
        """The emit and the question are independent steps: a day with no
        champion picks does not stop the count reaching the bar."""
        calls = self._ready(monkeypatch, experiment)
        report = asyncio.run(run_shadow_run())
        assert "没有可配对的面板" in report
        assert calls == [experiment["target"]]

    def test_a_gate_refusal_is_reported_not_raised(self, store, experiment,
                                                   monkeypatch):
        self._ready(monkeypatch, experiment)

        def refuse(version, **kw):
            raise holdout_gate.GateError("two shadow runs are open")

        monkeypatch.setattr(holdout_gate, "run_gate", refuse)
        report = asyncio.run(run_shadow_run())
        assert "闸门拒绝回答" in report
        assert "two shadow runs are open" in report


class TestItCannotFailTheDay:
    def test_a_broken_run_table_is_not_an_exception(self, store, monkeypatch):
        monkeypatch.setattr(shadow, "runs",
                            lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
        assert asyncio.run(run_shadow_run()) is None

    def test_a_failed_emit_is_reported_and_the_rest_still_runs(
            self, store, experiment, monkeypatch):
        def explode(*a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr(shadow, "emit_for_date", explode)
        report = asyncio.run(run_shadow_run())
        assert "写入失败" in report
        assert "评分：" in report, "grading still ran"

    def test_a_broken_coverage_does_not_lose_the_report(
            self, store, experiment, monkeypatch):
        monkeypatch.setattr(shadow, "coverage",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
        report = asyncio.run(run_shadow_run())
        assert "【影子实验】" in report
