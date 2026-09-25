from pathlib import Path

# Post-review hardening in the isolated audit checkout.
def change(path, old, new):
    p = Path(path)
    text = p.read_text(encoding='utf-8')
    if new in text:
        return
    assert text.count(old) == 1, (path, old[:80])
    p.write_text(text.replace(old, new, 1), encoding='utf-8')

change('alpha_agents/data/decision_frame.py',
       'not 1 <= req["max_turns"] <= 100:',
       'req["max_turns"] <= 0:')
change('alpha_agents/data/decision_frame.py',
       'max_turns must be between 1 and 100',
       'max_turns must be a positive integer')
change('alpha_agents/agents/t1_decider.py',
       '        "alpha_agents/data/decision_frame.py",\n',
       '        "alpha_agents/data/decision_frame.py",\n        "alpha_agents/llm_journal.py", "pyproject.toml", "uv.lock",\n')
p = Path('alpha_agents/data/decision_capture.py')
s = p.read_text()
if '        with conn:\n' not in s:
    start = s.index('        conn.execute(')
    end = s.index('        conn.commit()', start) + len('        conn.commit()')
    lines = s[start:end].splitlines()[:-1]
    s = s[:start] + '        with conn:\n' + '\n'.join('    ' + line for line in lines) + s[end:]
    p.write_text(s)
change('alpha_agents/data/decision_capture.py',
       '    def __enter__(self):\n        if self.enabled:',
       '    def __enter__(self):\n        if self.invocation_id is not None:\n            raise FrameError("A capture is a single invocation, not reusable state")\n        if self.enabled:')

for path in ('scripts/decision_lab.py', 'tests/test_decision_lab.py'):
    p = Path(path)
    text = p.read_text().replace('"provider_requests"', '"recorded_provider_requests"')
    p.write_text(text)
change('scripts/decision_lab.py',
       '            if row.get("kind") not in ("llm_call", "llm_error"):',
       '            if not isinstance(row, dict):\n                out["usage_complete"] = False\n                continue\n            if row.get("kind") not in ("llm_call", "llm_error"):')
change('scripts/decision_lab.py',
       '            usage = (row.get("response_json") or {}).get("usage")',
       '            response = row.get("response_json")\n            usage = response.get("usage") if isinstance(response, dict) else None')
change('scripts/decision_lab.py',
       '    result["usage"] = journal_usage(storage)\n',
       '    result["usage"] = journal_usage(storage)\n    if args.live and result["status"] == "error":\n        result["usage"]["usage_complete"] = False\n')
change('scripts/decision_lab.py',
       '    result.setdefault("usage", journal_usage(workspace / "storage"))\n',
       '    result.setdefault("usage", journal_usage(workspace / "storage"))\n    if args.live and result["status"] == "error":\n        result["usage"]["usage_complete"] = False\n')
change('scripts/decision_lab.py',
       '           "response_models": [], "usage_complete": True}',
       '           "response_models": [], "usage_complete": True,\n           "count_basis": "journal records only; in-flight/unrecorded requests may be absent"}')
change('docs/decision_lab.md',
       'capability names, planner source/template identity and displayed trader context.',
       'capability names, planner source/template/dependency-lock identity and displayed trader context.')
change('docs/decision_lab.md',
       'Actual journaled request/token counts and response model names are reported,',
       'Recorded request/token counts and response model names are reported. An interrupted\nrequest may have no terminal journal entry; failed live samples mark usage incomplete,\nnot zero-cost. The dependency lock is included in the planner fingerprint. Counts are')

extra_frames = '''

def test_capture_insert_failure_rolls_back_its_own_transaction():
    with replay_as_of("2026-01-19 09:00"):
        with Capture(frame()) as capture:
            conn = memory_store._get_conn()
            with pytest.raises(sqlite3.IntegrityError):
                capture._append("decision_input", {"duplicate": True})
            assert not conn.in_transaction
            capture.output = {"raw": "valid answer"}
    assert conn.execute("SELECT count(*) FROM decision_capture_events").fetchone()[0] == 2


def test_a_capture_cannot_reuse_an_old_answer():
    with replay_as_of("2026-01-19 09:00"):
        capture = Capture(frame())
        with capture:
            capture.output = {"raw": "first answer"}
        with pytest.raises(FrameError):
            with capture:
                pass


def test_frame_records_large_existing_turn_budget_without_imposing_new_policy():
    f = frame().as_dict()
    f["request"]["max_turns"] = 1000
    got = DecisionFrame.create(identity=f["identity"], state=TraderState.create(),
        request=f["request"], panel_codes=f["panel_codes"], producer=f["producer"])
    assert got.as_dict()["request"]["max_turns"] == 1000
'''
p = Path('tests/test_decision_frames.py')
if 'test_capture_insert_failure_rolls_back_its_own_transaction' not in p.read_text():
    p.write_text(p.read_text() + extra_frames)
extra_lab = '''

def test_dependency_lock_is_part_of_the_loaded_planner_identity():
    from alpha_agents.data.decision_frame import fingerprint
    paths = ("alpha_agents/agents/t1_decider.py", "alpha_agents/agents/json_reply.py",
             "alpha_agents/data/thesis.py", "alpha_agents/model_factory.py",
             "alpha_agents/data/decision_frame.py", "alpha_agents/llm_journal.py",
             "pyproject.toml", "uv.lock")
    expected = fingerprint({p: (lab.ROOT / p).read_text(encoding="utf-8") for p in paths})
    assert D._PLAN_CODE_HASH == expected


def test_parent_deadline_cannot_claim_zero_cost(tmp_path, monkeypatch):
    args = options(tmp_path, live=True)
    _, tasks = lab.prepare(args)
    def timeout(*args, **kwargs):
        raise lab.subprocess.TimeoutExpired("worker", 1)
    monkeypatch.setattr(lab.subprocess, "run", timeout)
    root = tmp_path / "workers"
    root.mkdir()
    result = lab._launch(tasks[0], args, root, None)
    assert result["error_type"] == "WorkerDeadline"
    assert result["usage"]["recorded_provider_requests"] == 0
    assert not result["usage"]["usage_complete"]


def test_malformed_journal_record_does_not_drop_the_sample(tmp_path):
    path = tmp_path / "llm_journal"
    path.mkdir()
    (path / "bad.jsonl").write_text('[]\\n{"kind":"llm_call","response_json":[]}')
    got = lab.journal_usage(tmp_path)
    assert got["recorded_provider_requests"] == 1
    assert not got["usage_complete"]
'''
p = Path('tests/test_decision_lab.py')
if 'test_dependency_lock_is_part_of_the_loaded_planner_identity' not in p.read_text():
    p.write_text(p.read_text() + extra_lab)
