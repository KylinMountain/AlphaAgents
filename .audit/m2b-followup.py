from pathlib import Path


def change(path, old, new):
    file = Path(path)
    text = file.read_text()
    if new in text:
        return
    assert old in text, path
    file.write_text(text.replace(old, new, 1))


change('tests/test_walk_branch.py', '    import test_walk_forward as TW',
       '    from tests import test_walk_forward as TW')

change('scripts/walk_checkpoint.py', '\ndef restore_runtime(ctx, value: dict) -> None:', '''
def require_fresh_prefix(source: Path) -> None:
    """Do not certify a reused sandbox containing later observations."""
    if (source / "branch-origin.json").exists():
        raise CheckpointError("Use a fresh bootstrap directory for a new prefix")
    for name in STATE[1:]:
        path = source / name
        if path.is_symlink() or path.is_file() or (path.exists() and any(path.rglob("*"))):
            raise CheckpointError("A prefix must not inherit unverified private state")
    db = source / "memory.db"
    _regular(db)
    with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for name in tables:
            quoted = '"' + name.replace('"', '""') + '"'
            if conn.execute(f"SELECT 1 FROM {quoted} LIMIT 1").fetchone():
                raise CheckpointError("A prefix must start with fresh state, not an old run")


def restore_runtime(ctx, value: dict) -> None:''')
change('scripts/walk_forward.py',
       '        checkpoint.validate_runner(args)\n        source_hash = checkpoint.source_identity()',
       '        checkpoint.validate_runner(args)\n        if not getattr(args, "continuation", None):\n            checkpoint.require_fresh_prefix(Path(DATA_DIR))\n        source_hash = checkpoint.source_identity()')
change('scripts/walk_forward.py', '    initial_account = _initial_account(ctx, window[0])\n    journal_before', '''    if getattr(args, "continuation", None):
        from scripts.walk_checkpoint import CheckpointError
        with replay_as_of(args.continuation["completed_through"]):
            initial_account = _initial_account(ctx, window[0])
        if abs(initial_account["equity"] - args.continuation["end_account"]["equity"]) > 0.01:
            raise CheckpointError("Restored equity does not match the checkpoint boundary")
    else:
        initial_account = _initial_account(ctx, window[0])
    journal_before''')
change('docs/walk_branch.md',
       'complete window. An arbitrary historical directory is not a restore point.',
       'complete window. Prefix state must be fresh: nonempty account tables or private\nmemory directories are rejected before any decision. An arbitrary historical\ndirectory is not a restore point.')
extra = '''


def test_prefix_rejects_preexisting_future_memory(tmp_path):
    source = tiny(tmp_path / 'source')
    with pytest.raises(CP.CheckpointError):
        CP.require_fresh_prefix(source)
    import shutil
    shutil.rmtree(source / 'traders')
    with pytest.raises(CP.CheckpointError, match='fresh state'):
        CP.require_fresh_prefix(source)
    with closing(sqlite3.connect(source / 'memory.db')) as c:
        c.execute('DELETE FROM facts')
        c.commit()
    CP.require_fresh_prefix(source)


def test_source_early_equity_guard_precedes_first_decision():
    import inspect
    from scripts import walk_forward as WF
    body = inspect.getsource(WF._run_window)
    assert body.index('Restored equity does not match') < body.index('_run_decider(ctx')
'''
p=Path('tests/test_walk_branch.py')
if 'def test_prefix_rejects_preexisting_future_memory' not in p.read_text():
    p.write_text(p.read_text()+extra)
