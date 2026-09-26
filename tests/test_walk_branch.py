"""Checkpoints preserve a trader; branches never borrow tomorrow's answers."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
import csv
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import walk_checkpoint as CP
from scripts import walk_branch as WB
from scripts import walk_resume as WR


def tiny(root):
    root.mkdir()
    with closing(sqlite3.connect(root / 'memory.db')) as c:
        c.execute('CREATE TABLE facts (value TEXT)')
        c.execute("INSERT INTO facts VALUES ('prefix')")
        c.commit()
    with closing(sqlite3.connect(root / 'market_history.db')) as c:
        c.execute('CREATE TABLE daily_kline(date TEXT)')
        c.executemany('INSERT INTO daily_kline VALUES (?)',
                      [('2026-01-05',), ('2026-01-06',), ('2026-01-07',)])
        c.commit()
    handbook = root / 'traders/default'
    handbook.mkdir(parents=True)
    (handbook / 'MEMORY.md').write_text('R2 original rule')
    (root / 'llm_journal').mkdir()
    (root / 'llm_journal/old.jsonl').write_text('PARENT FUTURE RESPONSE DO NOT COPY')
    return root


def meta():
    return {'source_hash': CP.source_identity(), 'runtime_hash': 'runtime', 'parent_run_id': 'parent',
            'completed_through': '2026-01-05', 'next_session': '2026-01-06',
            'args': {'decider': 'placeholder'}, 'runtime_state': {},
            'end_account': {'equity': 100}, 'input_paths': {}}


@pytest.fixture
def checkpoint(tmp_path):
    source = tiny(tmp_path / 'source')
    destination = tmp_path / 'checkpoint'
    CP.capture(source, destination, metadata=meta())
    return destination


def test_snapshot_clones_all_account_tables_and_committed_wal(tmp_path):
    source = tiny(tmp_path / 'source')
    with closing(sqlite3.connect(source / 'memory.db')) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE pending_settlements(amount INTEGER)')
        db.execute('INSERT INTO pending_settlements VALUES (1234)')
        db.commit()
        target = tmp_path / 'checkpoint'
        CP.capture(source, target, metadata=meta())
        with closing(sqlite3.connect((target / 'state/memory.db').as_uri()
                                     + '?mode=ro&immutable=1', uri=True)) as clone:
            assert clone.execute('SELECT amount FROM pending_settlements').fetchone()[0] == 1234
    assert CP.verify(target)['files']['state/memory.db']
    assert not (target / 'state/llm_journal').exists()


def test_two_branches_do_not_share_writable_state_or_future_responses(checkpoint, tmp_path):
    a, b = tmp_path / 'a', tmp_path / 'b'
    for name, path in [('a', a), ('b', b)]:
        CP.materialize(checkpoint, path, branch_id=name)
        assert not (path / 'llm_journal').exists()
        assert (path / 'market_history.db').is_symlink()
        assert not (path / 'memory.db').is_symlink()
    with closing(sqlite3.connect(a / 'memory.db')) as db:
        db.execute("UPDATE facts SET value='a learned something'")
        db.commit()
    (a / 'traders/default/MEMORY.md').write_text('a new rule')
    with closing(sqlite3.connect(b / 'memory.db')) as db:
        assert db.execute('SELECT value FROM facts').fetchone()[0] == 'prefix'
    assert (b / 'traders/default/MEMORY.md').read_text() == 'R2 original rule'
    assert CP.verify(checkpoint)
    assert (a / 'memory.db').stat().st_ino != (b / 'memory.db').stat().st_ino


@pytest.mark.parametrize('name', ['state/memory.db', 'state/traders/default/MEMORY.md',
                                  'corpus/market_history.db'])
def test_content_tampering_fails_before_branch(checkpoint, tmp_path, name):
    p = checkpoint / name
    p.chmod(0o600)
    with p.open('ab') as f:
        f.write(b'tampered')
    with pytest.raises(CP.CheckpointError):
        CP.materialize(checkpoint, tmp_path / 'child', branch_id='a')
    assert not (tmp_path / 'child').exists()


def test_manifest_tampering_and_extra_file_rejected(checkpoint):
    (checkpoint / 'unexpected').write_text('new')
    with pytest.raises(CP.CheckpointError, match='file set'):
        CP.verify(checkpoint)
    (checkpoint / 'unexpected').unlink()
    p = checkpoint / CP.MANIFEST
    p.chmod(0o600)
    value = json.loads(p.read_text())
    value['next_session'] = '2026-01-07'
    p.write_text(json.dumps(value))
    with pytest.raises(CP.CheckpointError, match='hash'):
        CP.verify(checkpoint)


@pytest.mark.parametrize('name', ['../secret', '/absolute', 'state/../secret', 'state\\secret'])
def test_traversal_rejected(name):
    with pytest.raises(CP.CheckpointError):
        CP._relative(name)


def test_existing_or_nested_destination_is_never_overwritten(checkpoint, tmp_path):
    with pytest.raises(CP.CheckpointError):
        CP.materialize(checkpoint, checkpoint / 'child', branch_id='a')
    existing = tmp_path / 'existing'
    existing.mkdir()
    (existing / 'keep').write_text('keep')
    with pytest.raises(FileExistsError):
        CP.materialize(checkpoint, existing, branch_id='a')
    assert (existing / 'keep').read_text() == 'keep'


def test_private_symlinks_refused(tmp_path):
    source = tiny(tmp_path / 'source')
    (source / 'traders/default/alias.md').symlink_to(source / 'traders/default/MEMORY.md')
    with pytest.raises(CP.CheckpointError, match='linked'):
        CP.capture(source, tmp_path / 'out', metadata=meta())


def test_mutation_during_capture_leaves_no_usable_manifest(tmp_path, monkeypatch):
    source = tiny(tmp_path / 'source')
    original = CP._copy_file
    def changed(src, dst):
        original(src, dst)
        if src.name == 'memory.db':
            (source / 'traders/default/MEMORY.md').write_text('changed during capture')
    monkeypatch.setattr(CP, '_copy_file', changed)
    with pytest.raises(CP.CheckpointError, match='changed'):
        CP.capture(source, tmp_path / 'out', metadata=meta())
    assert not (tmp_path / 'out/checkpoint.json').exists()


def test_code_mismatch_rejected(checkpoint, monkeypatch):
    monkeypatch.setattr(CP, 'source_identity', lambda: 'different')
    with pytest.raises(CP.CheckpointError, match='exact recorded source'):
        CP.verify(checkpoint)


def test_restored_review_exposure_and_start_are_not_reset(monkeypatch):
    monkeypatch.setattr(CP, 'runtime_identity', lambda: 'runtime')
    value = meta()
    value['runtime_state'] = {'window_start': '2025-12-01',
        'exposure_log': [['2026-01-05', 20.0, 100.0]], 'pending_signals': [{'id': 1}],
        'review': [{'dimension': 'entry', 'question': 'entry?', 'n': 8,
                    'value': 55.0, 'unit': '%', 'too_thin': False, 'detail': [], 'note': ''}]}
    ctx = SimpleNamespace(run_id='child', args=SimpleNamespace(start='2026-01-06'),
                          corpus=SimpleNamespace(previous=lambda day: '2026-01-05'))
    CP.restore_runtime(ctx, value)
    assert ctx.window_start == '2025-12-01'
    assert ctx.review_lineage == ('parent', 'child')
    assert ctx.review[0].value == 55
    assert ctx.exposure_log[0][1] == 20
    assert ctx.pending_signals == [{'id': 1}]
    ctx.args.start = '2026-01-07'
    with pytest.raises(CP.CheckpointError):
        CP.restore_runtime(ctx, value)


def test_mismatched_runtime_rejected(monkeypatch):
    monkeypatch.setattr(CP, 'runtime_identity', lambda: 'different')
    ctx = SimpleNamespace(args=SimpleNamespace(start='2026-01-06'),
                          corpus=SimpleNamespace(previous=lambda day: '2026-01-05'))
    with pytest.raises(CP.CheckpointError, match='configuration|config'):
        CP.restore_runtime(ctx, meta())


@pytest.mark.parametrize('setting', ['keep_going', 'experiment_manifest',
                                   'frozen_directions'])
def test_unsupported_continuations_fail(setting):
    args = SimpleNamespace(days=1, **{setting: True})
    with pytest.raises(CP.CheckpointError):
        CP.validate_runner(args)


def test_resume_cannot_rebuild_a_branch(tmp_path, capsys):
    (tmp_path / 'branch-origin.json').write_text('{}')
    assert WR.main(['--target', str(tmp_path)]) == 1
    assert 'independent branch' in capsys.readouterr().out


def test_intervention_is_only_first_open_plan_of_first_day():
    from scripts import walk_forward as WF
    ctx = SimpleNamespace(branch_intervention={'name': 'x', 'old': 'a', 'new': 'b'},
                          branch_start='2026-01-06', branch_intervention_used=False)
    assert WF._plan_intervention(ctx, '2026-01-06', 'close') is None
    assert WF._plan_intervention(ctx, '2026-01-07', 'open') is None
    assert WF._plan_intervention(ctx, '2026-01-06', 'open') == ctx.branch_intervention
    assert WF._plan_intervention(ctx, '2026-01-06', 'open') is None


def test_actual_plan_capture_has_perturbed_input_and_parent_hash(monkeypatch):
    from alpha_agents.agents import t1_decider as TD
    from alpha_agents.data.decision_capture import read_inputs
    from alpha_agents.data.memory_store import _get_conn
    from alpha_agents.evolution.replay_mode import replay_as_of
    seen = []
    async def fake(frame, **kwargs):
        seen.append(frame.as_dict())
        return {'orders': [], 'parse_error': None, 'no_trade_reason': 'price too high'}
    monkeypatch.setattr(TD, '_run_frame', fake)
    with replay_as_of('2026-01-06 09:00'):
        asyncio.run(TD.propose(day='2026-01-06', prev_day='2026-01-05', panel=[], news=[],
                    model=SimpleNamespace(model='stub'), knowledge='R2 only leaders',
                    trader_id='default', run_id='branch', template='{knowledge}',
                    knowledge_intervention={'name': 'x', 'old': 'only leaders', 'new': 'consider leaders'}))
    assert seen[0]['request']['message'] == 'R2 consider leaders'
    assert seen[0]['provenance']['parent_frame_hash']
    assert read_inputs(_get_conn())[0]['frame'] == seen[0]
    task = {"branch_id": "branch", "intervention": {
        "name": "x", "old": "only leaders", "new": "consider leaders"}}
    evidence = WB.intervention_evidence(_get_conn(), task, "2026-01-06")
    assert evidence["frame_hash"] == seen[0]["frame_hash"]
    assert WB.intervention_evidence(_get_conn(), task, "2026-01-07") is None
    assert WB.intervention_evidence(_get_conn(), {**task, "branch_id": "other"}, "2026-01-06") is None



def options(checkpoint, tmp_path, **changes):
    values = dict(checkpoint=checkpoint, output=tmp_path / 'experiment', days=2, trials=1,
                  max_branch_sessions=10, timeout=60, jobs=1, seed=0, arms=None,
                  live=False, mechanical=True)
    values.update(changes)
    return argparse.Namespace(**values)


@pytest.mark.parametrize('over', [{'days': 31}, {'trials': 9}, {'jobs': 3},
    {'timeout': 0}, {'max_branch_sessions': 201}, {'max_branch_sessions': 1}, {'live': True}])
def test_budget_and_mode_reject_before_output(checkpoint, tmp_path, over):
    args = options(checkpoint, tmp_path, **over)
    with pytest.raises(CP.CheckpointError):
        WB.run(args)
    assert not args.output.exists()


def test_full_calendar_required(checkpoint, tmp_path):
    with pytest.raises(CP.CheckpointError, match='cover'):
        WB.prepare(options(checkpoint, tmp_path, days=3))


def test_deadline_is_retained_as_failed_branch(tmp_path, monkeypatch):
    def expired(*a, **k):
        raise subprocess.TimeoutExpired('worker', 1)
    monkeypatch.setattr(WB.subprocess, 'run', expired)
    task = {'branch_id': 'a', 'arm': 'control', 'trial': 1, 'intervention': None}
    result = WB.launch(task, options(tmp_path, tmp_path, live=True), tmp_path, ['2026-01-06'])
    assert result['status'] == 'error'
    assert not result['usage']['usage_complete']
    assert json.loads((tmp_path / 'a/branch.json').read_text())['error_type'] == 'BranchDeadline'


def test_mechanical_network_guard():
    with pytest.raises(RuntimeError):
        WB._deny_network('socket.getaddrinfo', ('example.com',))


def _run_script(script, argv, env):
    code = ("import runpy,sys; from scripts.walk_branch import _deny_network; "
            "sys.addaudithook(_deny_network); sys.argv=sys.argv[1:]; "
            "runpy.run_path(sys.argv[0],run_name='__main__')")
    proc = subprocess.run([sys.executable, '-c', code, str(CP.ROOT / script), *argv],
                          env=env, cwd=CP.ROOT, capture_output=True, text=True, timeout=100)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-5000:]
    return proc


def test_real_runner_uninterrupted_equals_checkpoint_suffix(tmp_path):
    from tests import test_walk_forward as TW
    series, instruments = TW._normal()
    day2 = TW._SESSIONS[22]
    series['600001'][day2] = TW._bar(open_=9.0, high=9.1, low=8.9, close=9.0)
    corpus = TW._write_corpus(tmp_path / 'corpus', series=series, instruments=instruments)
    env = dict(os.environ, ALPHAAGENTS_LLM_MODE='live', ALPHAAGENTS_ENV_FILE=str(tmp_path / 'empty-env'),
               OPENAI_AGENTS_DISABLE_TRACING='1', WF_STALL_DUMP_SECONDS='0')
    whole, prefix = tmp_path / 'whole', tmp_path / 'prefix'
    checkpoint = tmp_path / 'sealed'
    for path, count in ((whole, 5), (prefix, 2)):
        isolated = dict(env, ALPHAAGENTS_DATA_DIR=str(path))
        _run_script('scripts/walk_bootstrap.py', ['--target', str(path), '--corpus', str(corpus)], isolated)
        args = ['--target', str(path), '--start', TW._START, '--days', str(count),
                '--trader', 'pullback', '--picks', '1', '--no-keep-notes',
                '--decider', 'placeholder']
        if path == prefix:
            args += ['--checkpoint-out', str(checkpoint)]
        _run_script('scripts/walk_forward.py', args, isolated)
    output = tmp_path / 'experiment'
    isolated = dict(env, ALPHAAGENTS_DATA_DIR=str(tmp_path / 'unused'))
    _run_script('scripts/walk_branch.py', ['run', '--checkpoint', str(checkpoint),
        '--days', '3', '--trials', '2', '--max-branch-sessions', '6', '--timeout', '80',
        '--output', str(output), '--mechanical'], isolated)
    result = json.loads((output / 'summary.json').read_text())
    assert result['complete_branches'] == 2
    with next(whole.glob('walk-reports/*/equity.csv')).open() as f:
        expected = list(csv.DictReader(f))[2:]
    for branch in result['results']:
        with (output / branch['branch_id'] / 'report/equity.csv').open() as f:
            actual = list(csv.DictReader(f))
        assert actual == expected
        assert branch['initial_account']['equity'] == CP.verify(checkpoint)['end_account']['equity']
    assert CP.verify(checkpoint)
    assert not (prefix / 'branch-origin.json').exists()



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


def test_copied_branches_quarantine_different_lessons_without_rule_leakage(
        tmp_path, monkeypatch):
    from alpha_agents import config, model_factory
    from alpha_agents.data import trader_learning
    from alpha_agents.data.memory_store import _SCHEMA
    from alpha_agents.evolution import close_day, market_review
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
                assert agent.name == 'market_review'
                payload = {
                    'market': label, 'themes': label,
                    'boards': [{
                        'name': '算力', 'kind': '错过', 'driver': '情绪',
                        'evidence': 'synthetic', 'morning': 'synthetic',
                        'verdict': 'synthetic',
                        'lesson': label + ' tested hypothesis',
                    }],
                }
                return SimpleNamespace(final_output=json.dumps(payload))
            patch.setattr(model_factory, 'run_agent', reply)
            with closing(sqlite3.connect(target / 'memory.db')) as conn:
                conn.row_factory = sqlite3.Row
                with closing(sqlite3.connect(':memory:')) as hist:
                    with replay_as_of('2026-01-06'):
                        got = asyncio.run(close_day.review_day(
                            conn, hist, trader_id='default', trader=None,
                            day='2026-01-06', model='stub',
                            facts_text='Synthetic fixture, not market evidence',
                            exposure_text='Synthetic known account exposure',
                            handbook_before='2026-01-06'))
                    assert got['market_review'] == 1
                    assert got['handbook'] == 0
                    assert got['lesson_candidates'] == 1
                    rows = trader_learning.lesson_candidates(
                        run_id='live', trader_id='default', conn=conn)
                    assert len(rows) == 1
                    assert rows[0]['claim'] == label + ' tested hypothesis'
                    with replay_as_of('2026-01-07 09:00'):
                        assert label in market_review.inject(
                            conn, 'default', before='2026-01-07')
        assert (target / 'traders/default/MEMORY.md').read_text() == 'R2 original rule'
    assert (source / 'traders/default/MEMORY.md').read_text() == 'R2 original rule'
    assert CP.verify(checkpoint)



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



def test_measured_review_keeps_parent_lineage_and_excludes_other_runs():
    from tests.test_review import _book, _hist, MEMBERS
    from alpha_agents.evolution import review as RV
    with closing(_book()) as book, closing(_hist()) as hist:
        for sid, run, day in [(1, 'parent', '2026-01-05'), (2, 'child', '2026-01-06'),
                              (3, 'unrelated', '2026-01-07')]:
            book.execute('INSERT INTO theme_opportunity_sets VALUES (?,?,?)', (sid, run, day))
            book.execute('INSERT INTO opportunity_sets VALUES (?,?,?)', (sid, run, day))
            for offset, theme, code, status in [(0, 'strong', 'AAA', 'agent_selected'),
                                                (1, 'flat', 'BBB', 'offered_not_researched'),
                                                (2, 'weak', 'CCC', 'offered_not_researched'),
                                                (3, 'mild', 'DDD', 'offered_not_researched')]:
                book.execute('INSERT INTO theme_opportunity_items VALUES (?,?,?,?)',
                             (sid*10+offset, sid, theme, status))
                book.execute('INSERT INTO opportunity_items VALUES (?,?,?,?,?)',
                             (sid*10+offset, sid, code, status, '{"primary_theme":"strong"}'))
        for day in ('2026-01-05', '2026-01-06', '2026-01-07'):
            for index in range(50):
                hist.execute('INSERT INTO daily_kline VALUES (?,?,?,?,?,?,?)',
                             (f'benchmark{index}', day, 10, 10, 10, 10, 0.0))
        scope = ('parent', 'child')
        assert RV.direction_choice(book, hist, MEMBERS, run_id=scope, horizon=1).n == 2
        assert RV.direction_choice(book, hist, MEMBERS, run_id='child', horizon=1).n == 1
        assert RV.stock_choice(book, hist, run_id=scope, horizon=1).n == 2
        assert RV.absence(book, hist, run_id=scope).n == 2
        assert [r[0] for r in book.execute('SELECT run_id FROM opportunity_sets ORDER BY id')] == [
            'parent', 'child', 'unrelated']


@pytest.mark.parametrize('scope', [(), ('',), (None,), ['parent']])
def test_invalid_review_lineage_is_not_silently_unscoped(scope):
    from alpha_agents.evolution.review import _run_scope
    with pytest.raises(ValueError):
        _run_scope(scope)



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
