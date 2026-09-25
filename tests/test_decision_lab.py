"""M2-A tests use stubs, never an actual model or historical market corpus."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from agents.models.interface import Model

from alpha_agents.agents import t1_decider as D
from alpha_agents.data import memory_store
from alpha_agents.data.decision_capture import read_inputs
from alpha_agents.data.decision_frame import DecisionFrame, FrameError, TraderState
from alpha_agents.evolution.replay_mode import replay_as_of
from scripts import decision_lab as lab


class StubModel(Model):
    model = "test-model"

    async def get_response(self, *args, **kwargs):
        pytest.fail("No provider is called in these tests")

    def stream_response(self, *args, **kwargs):
        pytest.fail("No streaming in these tests")


PANEL = [{"code": "600001", "name": "A", "close": 10, "change_pct": 1, "adv20": 5000}]
RAW = '{"orders":[],"no_trade_reason":"No executable entry"}'
TEMPLATE = "{book}\n\n{knowledge}\n\n{panel}\n\n{research_packet}"


def frozen(*, tools=None, phase="open"):
    return D.make_frame(day="2026-01-19", phase=phase, message="BOOK\nR2: only leaders\nPANEL",
        state=TraderState.create(book="BOOK", knowledge="R2: only leaders"),
        panel_codes=["600001"], model=StubModel(), template=TEMPLATE, max_turns=3,
        tools=tools or [], trader_id="default", run_id="source-run", origin="walk_forward")


def options(tmp_path, *, live=False, trials=2, arms=None, **overrides):
    frame_path = tmp_path / "frame.json"
    if not frame_path.exists():
        lab.write_json(frame_path, frozen().as_dict())
    raw = tmp_path / "response.txt"
    raw.write_text(RAW)
    args = SimpleNamespace(frame=[frame_path], trials=trials, max_samples=24, jobs=1,
        timeout=20, seed=3, arms=arms, live=live, response_file=None if live else raw,
        output=tmp_path / "experiment")
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


@pytest.mark.asyncio
async def test_normal_and_frozen_plans_use_identical_model_input_and_parser(monkeypatch):
    calls = []
    async def answer(agent, message, **kwargs):
        calls.append((agent.name, agent.instructions, message, list(agent.tools)))
        if len(calls) == 1:
            assert len(read_inputs(memory_store._get_conn())) == 1
        return SimpleNamespace(final_output=RAW)
    monkeypatch.setattr(D, "run_agent", answer)
    with replay_as_of("2026-01-19 09:00"):
        normal = await D.propose(day="2026-01-19", prev_day="2026-01-16", panel=PANEL,
            news=[], book="BOOK", knowledge="R2: only leaders", template=TEMPLATE,
            model=StubModel(), tools=[], trader_id="default", run_id="source-run")
    capture = read_inputs(memory_store._get_conn())[0]
    stored = DecisionFrame.from_dict(capture["frame"])
    monkeypatch.setattr(D, "build_message", lambda **kw: pytest.fail("Re-rendered frozen input"))
    conn = memory_store._get_conn()
    before = conn.execute("SELECT count(*) FROM decision_capture_events").fetchone()[0]
    replay = await D.propose_frame(stored, model=StubModel())
    assert calls[0] == calls[1]
    for key in ("orders", "refused", "parse_error", "no_trade_reason", "decision_status", "raw"):
        assert normal[key] == replay[key]
    assert conn.execute("SELECT count(*) FROM decision_capture_events").fetchone()[0] == before


@pytest.mark.asyncio
async def test_experiment_has_one_attempt_no_tools(monkeypatch):
    seen = {}
    async def answer(agent, message, **kwargs):
        seen.update(kwargs)
        assert not agent.tools
        return SimpleNamespace(final_output='{"orders":[]}')
    monkeypatch.setattr(D, "run_agent", answer)
    result = await D.propose_frame(frozen(), model=StubModel(), timeout=12)
    assert seen["attempts"] == 1 and seen["timeout"] == 12
    assert result["decision_status"] == "incomplete"


@pytest.mark.asyncio
async def test_model_mismatch_rejected_before_call(monkeypatch):
    monkeypatch.setattr(D, "run_agent", lambda *a, **k: pytest.fail("Wrong model called"))
    other = StubModel()
    other.model = "other"
    with pytest.raises(FrameError):
        await D.propose_frame(frozen(), model=other)


def test_source_code_mismatch_refused():
    f = frozen().as_dict()
    changed = DecisionFrame.create(identity=f["identity"], state=TraderState.create(),
        request=f["request"], panel_codes=f["panel_codes"],
        producer={**f["producer"], "code_hash": "different"})
    with pytest.raises(FrameError):
        D.require_frozen_plan(changed)


def test_tool_input_is_not_a_complete_replay():
    f = frozen(tools=[SimpleNamespace(name="get_price_levels")])
    with pytest.raises(FrameError):
        D.require_frozen_plan(f)


def test_synthetic_close_is_not_labelled_strict_1455():
    f = frozen(phase="close").as_dict()
    assert f["identity"]["information_grade"] == "synthetic_close"
    assert f["identity"]["information_cutoff"].endswith("15:00:00")


def test_response_reparse_uses_original_panel():
    raw = '{"orders":[{"code":"600002","entry_high":10,"stop_loss":9}]}'
    result = D.parse_frozen_plan(frozen(), raw)
    assert not result["orders"] and result["refused"][0]["why"] == "outside_panel"


def test_budget_checked_before_any_output(tmp_path):
    args = options(tmp_path, trials=3, max_samples=2)
    with pytest.raises(FrameError):
        lab.run(args)
    assert not args.output.exists()


@pytest.mark.parametrize("field,value", [("jobs", 0), ("jobs", 5), ("trials", 0),
    ("trials", 21), ("timeout", 0), ("timeout", 601), ("max_samples", 201)])
def test_invalid_experiment_controls(tmp_path, field, value):
    with pytest.raises(FrameError):
        lab.prepare(options(tmp_path, **{field: value}))


def test_duplicate_case_and_arms_are_not_extra_evidence(tmp_path):
    args = options(tmp_path)
    args.frame *= 2
    with pytest.raises(FrameError):
        lab.prepare(args)
    for changes in ([{"name": "control", "old": "only leaders", "new": "all"}],
                    [{"name": "x", "old": "missing", "new": "all"}],
                    [{"name": "x", "old": "only leaders", "new": "all", "model": "different"}]):
        with pytest.raises(FrameError):
            lab.arms_for(frozen(), changes)


def test_schedule_seed_and_nonintervened_input_fixed(tmp_path):
    arms = tmp_path / "arms.json"
    lab.write_json(arms, [{"name": "no_rule", "old": "R2: only leaders", "new": ""}])
    args = options(tmp_path, arms=arms)
    manifest, tasks = lab.prepare(args)
    _, again = lab.prepare(args)
    assert tasks == again and manifest["planned_samples"] == 4
    assert len({t["frame"]["policy_build_hash"] for t in tasks}) == 1
    assert len({t["frame"]["state_snapshot_hash"] for t in tasks}) == 2
    assert len({t["frame"]["identity"]["run_id"] for t in tasks}) == 1


def test_offline_checks_use_private_processes_and_never_overwrite(tmp_path):
    args = options(tmp_path, jobs=2)
    before = args.frame[0].read_bytes()
    summary = lab.run(args)
    assert summary["mode"] == "parser_check"
    assert summary["finished_samples"] == 2
    assert all(r["status"] == "abstained" for r in summary["results"]), summary
    assert all(r["usage"]["provider_requests"] == 0 for r in summary["results"])
    assert all((args.output / r["sample_id"] / "storage").is_dir() for r in summary["results"])
    assert args.frame[0].read_bytes() == before
    with pytest.raises(FileExistsError):
        lab.run(args)


def test_failed_trials_remain_in_denominator(tmp_path, monkeypatch):
    args = options(tmp_path, live=True)
    calls = []
    def fail(task, args, root, raw):
        calls.append(task["sample_id"])
        return {"arm": task["arm"], "status": "error", "error_type": "StubFailure"}
    monkeypatch.setattr(lab, "_launch", fail)
    summary = lab.run(args)
    assert len(calls) == 2
    assert summary["groups"]["control"]["n"] == 2
    assert summary["groups"]["control"]["status_counts"] == {"error": 2}


def test_explicit_mode_required(tmp_path):
    with pytest.raises(SystemExit):
        lab.parser().parse_args(["run", "--frame", "x.json", "--output", str(tmp_path / "out")])


def test_factory_can_disable_failover_and_sdk_retries(monkeypatch):
    from alpha_agents import model_factory as mf
    seen = {}
    def make_client(**kwargs):
        seen.update(kwargs)
        return MagicMock()
    monkeypatch.setattr(mf, "AsyncOpenAI", make_client)
    monkeypatch.setattr(mf, "with_failover", lambda *a: pytest.fail("Fallback in experiment"))
    monkeypatch.setattr(mf.llm_journal, "journaled", lambda c: c)
    mf.create_model(timeout=12, allow_failover=False, max_retries=0)
    assert seen["timeout"] == 12 and seen["max_retries"] == 0


@pytest.mark.asyncio
async def test_online_entry_uses_same_envelope_but_is_observation_only(monkeypatch):
    from alpha_agents.pipeline.tasks import entry_pricing as EP
    from alpha_agents import model_factory as mf
    from agents import Runner
    async def answer(agent, message, **kwargs):
        capture = read_inputs(memory_store._get_conn())[0]
        f = DecisionFrame.from_dict(capture["frame"])
        assert f.as_dict()["request"]["message"] == message
        assert f.as_dict()["request"]["instructions"] == agent.instructions
        assert f.as_dict()["identity"]["trader_id"] == "online"
        assert f.as_dict()["request"]["tools"]
        with pytest.raises(FrameError):
            D.require_frozen_plan(f)
        return SimpleNamespace(final_output='<!-- ORDERS: [{"code":"600001","action":"skip","reason":"high"}] -->')
    monkeypatch.setattr(mf, "create_model", lambda: StubModel())
    monkeypatch.setattr(Runner, "run", answer)
    trader = SimpleNamespace(id="online", extra_prompt="", max_position_pct=0.1)
    with replay_as_of("2026-01-19 10:00"):
        result = await EP.price([{"code": "600001", "price": 10}], trader)
    assert result["600001"]["action"] == "skip"


def test_incomplete_journal_does_not_erase_failed_sample(tmp_path):
    directory = tmp_path / "llm_journal"
    directory.mkdir()
    (directory / "sample.jsonl").write_text('{"kind":"llm_error"}\n{"broken":')
    usage = lab.journal_usage(tmp_path)
    assert usage["provider_requests"] == 1
    assert not usage["usage_complete"] and usage["unreadable_journal_lines"] == 1
