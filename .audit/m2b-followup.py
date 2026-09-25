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

change('scripts/walk_checkpoint.py', '    args.no_keep_notes = True', '''    args.no_keep_notes = True
    if (getattr(args, "checkpoint_out", None) and args.decider == "llm"
            and os.environ.get("ALPHAAGENTS_LLM_MODE") != "record"):
        raise CheckpointError("An LLM checkpoint prefix requires fresh record mode")''')
change('scripts/walk_forward.py', 'def _review_trades(ctx, day: str, conn) -> int:', '''def _strict_branch_learning(ctx) -> bool:
    args = getattr(ctx, "args", None)
    return ctx.model is not None and bool(getattr(args, "checkpoint_out", None)
                                          or getattr(args, "continuation", None))


def _lost_branch_review(ctx, day: str) -> None:
    if _strict_branch_learning(ctx):
        ctx.failed_learning.append((day, "market_review_failed"))
        ctx.counters["market_review_failed"] += 1


def _review_trades(ctx, day: str, conn) -> int:''')
change('scripts/walk_forward.py',
       '    try:\n        hist = sqlite3.connect(\n            f"file:{DATA_DIR / \'market_history.db\'}?mode=ro", uri=True)',
       '    hist = None\n    try:\n        hist = sqlite3.connect(\n            f"file:{DATA_DIR / \'market_history.db\'}?mode=ro", uri=True)')
change('scripts/walk_forward.py',
       '        logger.warning("%s: close review needs market history: %s", day, exc)\n        return 0',
       '        logger.warning("%s: close review needs market history: %s", day, exc)\n        if hist is not None:\n            hist.close()\n        _lost_branch_review(ctx, day)\n        return 0')
change('scripts/walk_forward.py',
       '        logger.warning("%s: close review failed: %s", day, exc)\n        return 0',
       '        logger.warning("%s: close review failed: %s", day, exc)\n        _lost_branch_review(ctx, day)\n        return 0')
change('scripts/walk_forward.py',
       '    ctx.counters["market_reviews"] += got.get("market_review", 0)',
       '    if _strict_branch_learning(ctx) and not got.get("market_review"):\n        got["market_review_failed"] = 1\n    ctx.counters["market_reviews"] += got.get("market_review", 0)')
change('docs/walk_branch.md',
       'usable checkpoint. Ordinary runs without this option retain prior behavior.',
       'usable checkpoint. Missing close-review inputs or interpretations in model-backed\nbranches count as lost learning even when valuation succeeds. Ordinary runs\nwithout these options retain their prior checkpoint-free behavior.')
extra = '''


def test_llm_checkpoint_prefix_requires_fresh_record(monkeypatch):
    args = SimpleNamespace(days=1, decider='llm', checkpoint_out=Path('checkpoint'))
    monkeypatch.setenv('ALPHAAGENTS_LLM_MODE', 'replay-recorded')
    with pytest.raises(CP.CheckpointError, match='fresh record'):
        CP.validate_runner(args)
    monkeypatch.setenv('ALPHAAGENTS_LLM_MODE', 'record')
    CP.validate_runner(args)


@pytest.mark.parametrize('model', [None, 'stub'])
def test_missing_close_review_cannot_make_model_branch_complete(tmp_path, monkeypatch, model):
    from collections import Counter
    from scripts import walk_forward as WF
    monkeypatch.setattr(WF, 'DATA_DIR', tmp_path / 'missing')
    ctx = SimpleNamespace(args=SimpleNamespace(continuation={'checkpoint': True}),
                          model=model, failed_learning=[], counters=Counter())
    assert WF._review_trades(ctx, '2026-01-06', None) == 0
    assert bool(ctx.failed_learning) == (model is not None)


def test_copied_branches_learn_different_rules_and_read_them_next_day(tmp_path, monkeypatch):
    from alpha_agents import config, model_factory
    from alpha_agents.data.memory_store import _SCHEMA
    from alpha_agents.evolution import close_day, handbook, market_review
    from alpha_agents.evolution.replay_mode import replay_as_of
    source = tiny(tmp_path / 'prefix')
    with closing(sqlite3.connect(source / 'memory.db')) as conn:
        conn.executescript(_SCHEMA)
        conn.commit()
    checkpoint = tmp_path / 'sealed-learning'
    CP.capture(source, checkpoint, metadata=meta())
    for label in ('alpha', 'beta'):
        target = tmp_path / label
        CP.materialize(checkpoint, target, branch_id=label)
        with monkeypatch.context() as patch:
            patch.setattr(config, 'DATA_DIR', target)
            async def reply(agent, message, **kwargs):
                payload = ({'market': label, 'themes': label, 'boards': []}
                           if agent.name == 'market_review' else
                           {'rules': [{'id': 'R1', 'text': label + ' tested hypothesis', 'from': []}]})
                return SimpleNamespace(final_output=json.dumps(payload))
            patch.setattr(model_factory, 'run_agent', reply)
            with closing(sqlite3.connect(target / 'memory.db')) as conn:
                conn.row_factory = sqlite3.Row
                with closing(sqlite3.connect(':memory:')) as hist:
                    with replay_as_of('2026-01-06'):
                        got = asyncio.run(close_day.review_day(conn, hist, trader_id='default',
                            trader=None, day='2026-01-06', model='stub',
                            facts_text='Synthetic fixture, not market evidence',
                            exposure_text='Synthetic known account exposure', handbook_before='2026-01-06'))
                    assert got['market_review'] == got['handbook'] == 1
                    with replay_as_of('2026-01-07 09:00'):
                        assert label in handbook.load('default', before='2026-01-07')
                        assert label in market_review.inject(conn, 'default', before='2026-01-07')
    assert 'alpha tested hypothesis' in (tmp_path / 'alpha/traders/default/MEMORY.md').read_text()
    assert 'alpha tested hypothesis' not in (tmp_path / 'beta/traders/default/MEMORY.md').read_text()
    assert (source / 'traders/default/MEMORY.md').read_text() == 'R2 original rule'
    assert CP.verify(checkpoint)
'''
p=Path('tests/test_walk_branch.py')
if 'def test_llm_checkpoint_prefix_requires_fresh_record' not in p.read_text():
    p.write_text(p.read_text()+extra)
