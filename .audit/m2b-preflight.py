from pathlib import Path


def change(path, old, new):
    p = Path(path)
    text = p.read_text()
    if new in text:
        return
    assert old in text, path
    p.write_text(text.replace(old, new, 1))


change('scripts/walk_checkpoint.py', '    args.no_keep_notes = True\n', '''    destination = getattr(args, "checkpoint_out", None)
    if destination is not None:
        from alpha_agents.config import DATA_DIR
        source, destination = Path(DATA_DIR).resolve(), Path(destination).absolute()
        if destination.exists() or destination.is_symlink():
            raise CheckpointError("Checkpoint destination already exists")
        if (destination.resolve().is_relative_to(source)
                or source.is_relative_to(destination.resolve())):
            raise CheckpointError("Checkpoint and source must not contain one another")
    args.no_keep_notes = True
''')
change('scripts/walk_checkpoint.py',
       '                              k.startswith(("RESEARCH_", "TRADABLE_"))}',
       '                              k.startswith(("RESEARCH_", "TRADABLE_"))\n                              or k in {"TOOL_TIMEOUT"}}')
if 'def assert_initial_account(' not in Path('scripts/walk_checkpoint.py').read_text():
    change('scripts/walk_checkpoint.py', 'def require_fresh_prefix(source: Path) -> None:', '''def assert_initial_account(actual: dict, expected: dict) -> None:
    """Reconcile the restored mark before any continuation decision or fill."""
    import math
    for key in ("cash", "invested", "market_value", "realized", "unrealized", "equity",
                "open_positions", "pending_orders"):
        left, right = actual.get(key), expected.get(key)
        if (type(left) not in (int, float) or type(right) not in (int, float)
                or not math.isfinite(left) or not math.isfinite(right)
                or abs(left - right) > 0.01):
            raise CheckpointError(f"Restored account differs at {key}")


def require_fresh_prefix(source: Path) -> None:''')
change('scripts/walk_forward.py', '    journal_before = _journal_records(_REPLAY_DIR)\n', '''    if getattr(args, "continuation", None):
        from scripts.walk_checkpoint import assert_initial_account
        assert_initial_account(initial_account, args.continuation["end_account"])
    journal_before = _journal_records(_REPLAY_DIR)
''')
change('docs/walk_branch.md',
       "The last marked\nprefix equity must equal each branch's initial equity.",
       'The restored cash, holdings counts and valuation fields must match the\ncheckpoint mark before the first continuation decision or fill.')
extra = '''


@pytest.mark.parametrize('field', ['cash', 'invested', 'market_value', 'realized',
                                  'unrealized', 'equity', 'open_positions', 'pending_orders'])
def test_account_restore_rejects_any_changed_mark(field):
    expected = dict.fromkeys(['cash', 'invested', 'market_value', 'realized',
                             'unrealized', 'equity', 'open_positions', 'pending_orders'], 100.0)
    CP.assert_initial_account(expected, expected)
    actual = {**expected, field: 101.0}
    with pytest.raises(CP.CheckpointError, match=field):
        CP.assert_initial_account(actual, expected)


@pytest.mark.parametrize('invalid', [None, float('nan'), float('inf'), '100', True])
def test_account_restore_rejects_unmeasurable_mark(invalid):
    with pytest.raises(CP.CheckpointError):
        CP.assert_initial_account({'cash': invalid}, {'cash': 100})


def test_existing_checkpoint_refused_at_runner_preflight(tmp_path):
    target = tmp_path / 'existing'
    target.mkdir()
    args = SimpleNamespace(days=1, checkpoint_out=target)
    with pytest.raises(CP.CheckpointError, match='already exists'):
        CP.validate_runner(args)


def test_account_reconciliation_precedes_decision_loop():
    import inspect
    from scripts import walk_forward as WF
    source = inspect.getsource(WF._run_window)
    assert source.index('assert_initial_account(') < source.index('for day in window:')
'''
p = Path('tests/test_walk_branch.py')
if 'test_account_restore_rejects_any_changed_mark' not in p.read_text():
    p.write_text(p.read_text() + extra)
