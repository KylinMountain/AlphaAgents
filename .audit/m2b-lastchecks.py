from pathlib import Path


def change(path, old, new):
    file = Path(path)
    text = file.read_text()
    if new not in text:
        assert old in text, path
        file.write_text(text.replace(old, new, 1))


change('scripts/walk_branch.py', '\ndef worker(args) -> int:', '''
def intervention_evidence(conn, task: dict, session: str) -> dict | None:
    """An attempted edit is not evidence that it reached a captured input."""
    if task.get("intervention") is None:
        return None
    from alpha_agents.data.decision_capture import read_inputs
    expected = {"field": "knowledge", **task["intervention"]}
    matched = []
    for record in read_inputs(conn, run_id=task["branch_id"], limit=10000):
        frame = record["frame"]
        identity = frame["identity"]
        provenance = frame["provenance"]
        if (identity["session_day"] == session and identity["phase"] == "open"
                and provenance.get("parent_frame_hash")
                and provenance.get("interventions", [])[-1:] == [expected]):
            matched.append({"decision_id": record["invocation_id"],
                            "frame_hash": frame["frame_hash"],
                            "parent_frame_hash": provenance["parent_frame_hash"]})
    if len(matched) > 1:
        raise CheckpointError("A one-shot intervention was applied more than once")
    return matched[0] if matched else None


def worker(args) -> int:''')
change('scripts/walk_branch.py',
       '        applied = getattr(got["ctx"], "branch_intervention_used", False)',
       '        from alpha_agents.data.memory_store import _get_conn\n        evidence = intervention_evidence(_get_conn(), task, value["next_session"])\n        applied = evidence is not None')
change('scripts/walk_branch.py', '                      intervention_applied=applied,',
       '                      intervention_attempted=getattr(got["ctx"], "branch_intervention_used", False),\n                      intervention_evidence=evidence, intervention_applied=applied,')
change('scripts/walk_checkpoint.py', '\ndef capture_result(ctx, args, result: dict, *, source_hash: str, runtime_hash: str) -> dict:', '''
def require_stable_membership(ctx, path: Path) -> None:
    """Do not seal new archive bytes for membership loaded at prefix start."""
    expected = ctx.input_identity.get("files", {}).get("sector_membership", {}).get("sha256")
    if not expected or file_hash(path) != expected:
        raise CheckpointError("Membership archive changed since the prefix started")


def capture_result(ctx, args, result: dict, *, source_hash: str, runtime_hash: str) -> dict:''')
change('scripts/walk_checkpoint.py',
       '    if getattr(args, "sector_membership", None):\n        inputs["sector_membership.json"]',
       '    if getattr(args, "sector_membership", None):\n        require_stable_membership(ctx, Path(args.sector_membership))\n        inputs["sector_membership.json"]')
p=Path('tests/test_walk_branch.py')
text=p.read_text()
old="    assert read_inputs(_get_conn())[0]['frame'] == seen[0]"
new=old+'''
    task = {"branch_id": "branch", "intervention": {
        "name": "x", "old": "only leaders", "new": "consider leaders"}}
    evidence = WB.intervention_evidence(_get_conn(), task, "2026-01-06")
    assert evidence["frame_hash"] == seen[0]["frame_hash"]
    assert WB.intervention_evidence(_get_conn(), task, "2026-01-07") is None
    assert WB.intervention_evidence(_get_conn(), {**task, "branch_id": "other"}, "2026-01-06") is None
'''
if new not in text:
    assert old in text
    p.write_text(text.replace(old,new,1))
extra='''


def test_attempted_but_uncaptured_intervention_is_not_applied():
    with closing(sqlite3.connect(':memory:')) as conn:
        task = {'branch_id': 'branch', 'intervention': {'name': 'x', 'old': 'a', 'new': 'b'}}
        assert WB.intervention_evidence(conn, task, '2026-01-06') is None


def test_membership_bytes_must_match_loaded_prefix(tmp_path):
    path = tmp_path / 'membership.json'
    path.write_text('original')
    ctx = SimpleNamespace(input_identity={'files': {'sector_membership': {'sha256': CP.file_hash(path)}}})
    CP.require_stable_membership(ctx, path)
    path.write_text('changed')
    with pytest.raises(CP.CheckpointError, match='Membership archive changed'):
        CP.require_stable_membership(ctx, path)
'''
if 'def test_attempted_but_uncaptured_intervention_is_not_applied' not in p.read_text():
    p.write_text(p.read_text()+extra)
change('docs/walk_branch.md',
       'The changed frame stores its parent\nhash and intervention.',
       'The changed frame stores its parent\nhash and intervention. Application is reported only from a verified captured\ninput, not from the fact that an edit was attempted.')
change('scripts/walk_branch.py', 'provenance.get("interventions", [])[-1:] == [expected]',
       'provenance.get("intervention") == expected')
