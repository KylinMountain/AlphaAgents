"""Candidate-only Evolve regressions; all data is synthetic and local to tmp_path.

Run with --noconftest: the repository collection hook probes a business DB.
"""

import asyncio
import json
import socket
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest


# Own the test schema rather than following concurrent memory_store migrations.
_SCHEMA = """
CREATE TABLE daily_lessons (
    id INTEGER PRIMARY KEY, date TEXT, lesson_type TEXT, theme TEXT,
    content TEXT, source TEXT DEFAULT 'review', relevance_tags TEXT DEFAULT '',
    consolidated_into INTEGER, UNIQUE(date, content)
);
CREATE TABLE trading_principles (
    id INTEGER PRIMARY KEY, principle TEXT UNIQUE, pattern_description TEXT,
    category TEXT, action_guidance TEXT, evidence TEXT DEFAULT '[]',
    evidence_count INTEGER DEFAULT 1, win_rate REAL, first_learned TEXT,
    last_reinforced TEXT, status TEXT DEFAULT 'active'
);
CREATE TABLE playbooks (
    id INTEGER PRIMARY KEY, name TEXT UNIQUE, pattern_json TEXT,
    created_date TEXT, last_updated TEXT, status TEXT DEFAULT 'active',
    weight REAL DEFAULT 1.0, total_trades INTEGER DEFAULT 0,
    wins INTEGER DEFAULT 0, hit_rate REAL DEFAULT 0.0,
    avg_return REAL DEFAULT 0.0, annotation TEXT DEFAULT '',
    annotation_date TEXT DEFAULT '', version_history TEXT DEFAULT '[]'
);
CREATE TABLE predictions (
    id INTEGER PRIMARY KEY, date TEXT, report_type TEXT, code TEXT,
    hit INTEGER, brier REAL, scored_at TEXT, features_json TEXT DEFAULT '{}',
    next_day_return REAL
);
CREATE TABLE evolution_metrics (
    date TEXT PRIMARY KEY, intraday_hit_rate_7d REAL, intraday_count_7d INTEGER,
    matched_hit_rate_7d REAL, matched_count_7d INTEGER,
    unmatched_hit_rate_7d REAL, unmatched_count_7d INTEGER,
    active_principles INTEGER, weakened_principles INTEGER,
    active_playbooks INTEGER, degraded_playbooks INTEGER, lessons_count_7d INTEGER
);
"""


@pytest.fixture(autouse=True)
def scratch_store(tmp_path, monkeypatch):
    """Block network/other SQLite files, replace the path and thread-local state."""
    def no_network(*args, **kwargs):
        pytest.fail('Network access is forbidden in quarantine regressions')

    def block_internet(method):
        def guarded(sock, *args, **kwargs):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                no_network()
            # Local IPC is required by the sandbox's filesystem broker.
            return method(sock, *args, **kwargs)
        return guarded

    for attr in ('connect', 'connect_ex', 'sendto'):
        monkeypatch.setattr(socket.socket, attr, block_internet(getattr(socket.socket, attr)))
    monkeypatch.setattr(socket, 'create_connection', no_network)
    monkeypatch.setattr(socket, 'getaddrinfo', no_network)

    path = tmp_path / 'quarantine.db'
    connect = sqlite3.connect
    connections = []

    def isolated_connect(database, *args, **kwargs):
        assert Path(database).resolve() == path.resolve(), 'Non-scratch DB access'
        conn = connect(database, *args, **kwargs)
        connections.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, 'connect', isolated_connect)
    from alpha_agents import config
    from alpha_agents.data import memory_store as ms
    from alpha_agents.evolution import lessons, playbook, principle_scoring

    monkeypatch.setattr(config, 'MEMORY_DB_PATH', path)
    monkeypatch.setattr(config, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(ms, 'MEMORY_DB_PATH', path)
    monkeypatch.setattr(ms, '_local', threading.local())
    monkeypatch.setattr(ms, '_write_lock', threading.Lock())
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    ms._local.conn = conn

    def scratch_connection():
        # Worker threads use the same scratch file, never the production schema.
        current = getattr(ms._local, 'conn', None)
        if current is None:
            current = sqlite3.connect(path, check_same_thread=False)
            current.row_factory = sqlite3.Row
            ms._local.conn = current
        return current

    monkeypatch.setattr(ms, '_get_conn', scratch_connection)
    monkeypatch.setattr(principle_scoring, '_get_conn', scratch_connection)
    monkeypatch.setattr(lessons, '_call_consolidation_llm', Mock(return_value={'operations': []}))
    monkeypatch.setattr(lessons, '_log_consolidation_failure', Mock())
    monkeypatch.setattr(playbook, 'annotate_degraded', Mock(return_value='Synthetic observation'))
    yield ms, conn
    for connection in connections:
        connection.close()


@pytest.fixture
def today():
    return datetime.now().strftime('%Y-%m-%d')


def _rows(conn, table):
    return [dict(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY id')]


def _knowledge(conn):
    return {table: _rows(conn, table) for table in ('trading_principles', 'playbooks')}


def _principle(conn, today, status='active', evidence=None):
    cur = conn.execute(
        'INSERT INTO trading_principles '
        '(principle, pattern_description, category, action_guidance, evidence, '
        'evidence_count, win_rate, first_learned, last_reinforced, status) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (f'Existing {status}', 'Existing pattern', 'entry', 'Existing guidance',
         json.dumps(evidence or []), len(evidence or []), 0.75, today, today, status),
    )
    conn.commit()
    return cur.lastrowid


def _playbook(conn, today, name='Existing', status='active', weight=1.0,
              total=0, hit_rate=0.0, updated=None, theme=None):
    pattern = {'conditions': [{'field': 'theme', 'op': '==', 'value': theme or name}]}
    cur = conn.execute(
        'INSERT INTO playbooks (name, pattern_json, created_date, last_updated, '
        'status, weight, total_trades, wins, hit_rate, annotation) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (name, json.dumps(pattern), today, updated or today, status, weight,
         total, int(total * hit_rate), hit_rate, 'Existing annotation'),
    )
    conn.commit()
    return cur.lastrowid


def _grades(conn, today, count=6, hit=0, theme='Novel'):
    evidence = []
    for i in range(count):
        code = f'SYNTH-{i}'
        conn.execute(
            'INSERT INTO predictions (date, report_type, code, hit, brier, '
            'scored_at, features_json, next_day_return) '
            "VALUES (?, 'intraday', ?, ?, 0.8, ?, ?, ?)",
            (today, code, hit, today, json.dumps({'theme': theme}), 2.0 if hit else -2.0),
        )
        evidence.append({'code': code, 'date': today, 'outcome': 'Synthetic'})
    conn.commit()
    return evidence


def _proposals(monkeypatch, conn, today, pid):
    from alpha_agents.evolution import lessons

    conn.execute(
        'INSERT INTO daily_lessons (date, lesson_type, content) VALUES (?, ?, ?)',
        (today, 'insight', 'Synthetic observation'),
    )
    conn.commit()
    operations = [
        {'op': 'create', 'principle': 'Novel principle', 'pattern_description': 'Novel pattern',
         'category': 'entry', 'action_guidance': 'Unapproved guidance',
         'evidence': [{'code': 'SYNTH-NEW', 'date': today, 'outcome': 'Synthetic'}]},
        {'op': 'reinforce', 'principle_id': pid,
         'new_case': {'code': 'SYNTH-CASE', 'date': today, 'outcome': 'Synthetic'}},
    ]
    monkeypatch.setattr(lessons, '_call_consolidation_llm', Mock(return_value={'operations': operations}))
    return operations


def test_consolidation_direct_call_quarantines_create_and_reinforce(scratch_store, monkeypatch, today):
    from alpha_agents.evolution import lessons

    _, conn = scratch_store
    pid = _principle(conn, today, status='weakened')
    operations = _proposals(monkeypatch, conn, today, pid)
    before = _knowledge(conn)
    counts = lessons.consolidate_principles(today)

    assert _knowledge(conn) == before
    candidates = _rows(conn, 'learning_candidates')
    assert [row['operation'] for row in candidates] == ['create', 'reinforce']
    assert all(row['status'] == 'candidate' for row in candidates)
    assert [json.loads(row['payload_json'])['proposal'] for row in candidates] == operations
    assert json.loads(candidates[0]['payload_json'])['lessons'][0]['content'] == 'Synthetic observation'
    assert candidates[1]['target_id'] == pid
    assert counts['created'] == counts['reinforced'] == counts['weakened'] == 0
    assert counts['candidates'] == 2
    lessons.consolidate_principles(today)
    assert _rows(conn, 'learning_candidates') == candidates


@pytest.mark.parametrize('scenario', ['degrade', 'restore', 'deprecate', 'boost'])
def test_daily_review_skips_lifecycle_rules(scratch_store, monkeypatch, today, scenario):
    from alpha_agents.evolution import lessons, playbook

    _, conn = scratch_store
    old = (datetime.strptime(today, '%Y-%m-%d') - timedelta(days=15)).strftime('%Y-%m-%d')
    status = 'degraded' if scenario in ('restore', 'deprecate') else 'active'
    _playbook(conn, today, status=status, weight=0.5 if status == 'degraded' else 1.0,
              total=12, hit_rate=0.8 if scenario == 'boost' else 0.2, updated=old)
    monkeypatch.setattr(playbook, '_recent_hit_rate', lambda *a, **k: (0.8 if scenario == 'restore' else 0.2, 6))
    lifecycle = Mock(wraps=playbook.update_playbook_stats)
    monkeypatch.setattr(playbook, 'update_playbook_stats', lifecycle)
    before = _knowledge(conn)
    asyncio.run(lessons.post_review(today, 'No new lessons'))
    assert _knowledge(conn) == before
    lifecycle.assert_not_called()
    playbook.annotate_degraded.assert_not_called()


@pytest.mark.parametrize('count', [0, 12, 14])
def test_discovery_quarantines_even_at_or_over_capacity(scratch_store, today, count):
    from alpha_agents.evolution import lessons, playbook

    _, conn = scratch_store
    for i in range(count):
        _playbook(conn, today, name=f'Existing-{i}')
    _grades(conn, today, hit=1)
    before = _knowledge(conn)
    summary = asyncio.run(lessons.post_review(today, 'No new lessons'))
    assert _knowledge(conn) == before
    candidates = _rows(conn, 'learning_candidates')
    assert len(candidates) == 1
    assert 'candidate' in summary.lower()
    assert candidates[0]['entity_type'] == 'playbook'
    assert candidates[0]['operation'] == 'create'
    payload = json.loads(candidates[0]['payload_json'])
    assert payload['cluster']['hits'] == 6
    assert payload['pattern_json']['conditions']
    assert playbook.match_playbook({'theme': 'Novel'}) is None
    asyncio.run(lessons.post_review(today, 'No new lessons'))
    assert _rows(conn, 'learning_candidates') == candidates


def test_daily_review_never_enforces_active_capacity(scratch_store, monkeypatch, today):
    from alpha_agents.evolution import lessons, playbook

    _, conn = scratch_store
    for i in range(playbook.MAX_ACTIVE_PLAYBOOKS + 2):
        _playbook(conn, today, name=f'Existing-{i}', total=10, hit_rate=i / 20)
    capacity = Mock(wraps=playbook.enforce_capacity)
    monkeypatch.setattr(playbook, 'enforce_capacity', capacity)
    before = _knowledge(conn)
    asyncio.run(lessons.post_review(today, 'No new lessons'))
    assert _knowledge(conn) == before
    capacity.assert_not_called()


def test_post_review_measures_without_calling_legacy_rescorer(scratch_store, monkeypatch, today):
    from alpha_agents.evolution import lessons, principle_scoring

    _, conn = scratch_store
    pid = _principle(conn, today, evidence=_grades(conn, today))

    def legacy_rescore(*args, **kwargs):
        conn.execute("UPDATE trading_principles SET status = 'weakened', win_rate = 0")
        conn.commit()
        return {'scored': 1, 'retired': 1, 'total': 1}

    rescore = Mock(side_effect=legacy_rescore)
    monkeypatch.setattr(principle_scoring, 'rescore_all_principles', rescore)
    before = _knowledge(conn)
    summary = asyncio.run(lessons.post_review(today, 'No new lessons'))
    assert _knowledge(conn) == before
    rescore.assert_not_called()
    assert 'measurement' in summary.lower()
    observation = _rows(conn, 'learning_observations')[0]
    assert observation['target_id'] == pid
    payload = json.loads(observation['payload_json'])
    assert payload['win_rate'] == 0.0
    assert payload['graded'] == 6
    assert payload['should_retire'] is True
    asyncio.run(lessons.post_review(today, 'No new lessons'))
    assert _rows(conn, 'learning_observations') == [observation]


def test_daily_review_preserves_live_matching(scratch_store, today):
    from alpha_agents.evolution import lessons, playbook

    _, conn = scratch_store
    _playbook(conn, today, name='First', theme='Shared', total=12, hit_rate=0.2)
    _playbook(conn, today, name='Second', theme='Shared', total=12, hit_rate=0.8)
    before = playbook.match_playbook({'theme': 'Shared'})
    asyncio.run(lessons.post_review(today, 'No new lessons'))
    assert playbook.match_playbook({'theme': 'Shared'}) == before


@pytest.mark.parametrize('gate_result', [
    {'promote': False, 'abstained': False},
    {'promote': False, 'abstained': True, 'n': 0},
    RuntimeError('Synthetic gate failure'),
    {'promote': True, 'abstained': False, 'n': 100},
], ids=['reject', 'no-samples', 'error', 'nominal-pass'])
def test_post_review_never_promotes_or_claims_gate_approval(scratch_store, monkeypatch, today, gate_result):
    from alpha_agents.evolution import holdout_gate, lessons

    _, conn = scratch_store
    pid = _principle(conn, today, evidence=_grades(conn, today, hit=0))
    _principle(conn, today, status='weakened')
    for i in range(14):
        _playbook(conn, today, name=f'Existing-{i}', total=12, hit_rate=0.1 if i == 0 else 0.8)
    _proposals(monkeypatch, conn, today, pid)
    _grades(conn, today, hit=1)
    gate = Mock(side_effect=gate_result) if isinstance(gate_result, Exception) else Mock(return_value=gate_result)
    monkeypatch.setattr(holdout_gate, 'run_gate', gate)
    report = '<!-- LESSONS: [{"type":"success","content":"New synthetic lesson"}] -->'
    before = _knowledge(conn)
    summary = asyncio.run(lessons.post_review(today, report))

    assert _knowledge(conn) == before
    assert len(_rows(conn, 'daily_lessons')) == 2
    assert _rows(conn, 'learning_candidates')
    assert conn.execute('SELECT COUNT(*) FROM evolution_metrics').fetchone()[0] == 1
    assert 'candidate' in summary.lower()
    gate.assert_not_called()


def test_store_initializes_itself_and_persists_across_reopen(scratch_store, today):
    from alpha_agents.data import learning_candidates as store

    ms, conn = scratch_store
    store.init_schema(conn)
    store.init_schema(conn)
    kwargs = dict(entity_type='principle', operation='create', source='test',
                  source_date=today, payload={'proposal': {'principle': 'Synthetic'}})
    cid = store.save_candidate(**kwargs)
    assert store.save_candidate(**kwargs) == cid
    with sqlite3.connect(ms.MEMORY_DB_PATH) as reader:
        row = reader.execute('SELECT status, payload_json FROM learning_candidates').fetchone()
    assert row[0] == 'candidate'
    assert json.loads(row[1]) == kwargs['payload']
    assert not _rows(conn, 'trading_principles')


def test_candidate_storage_failure_is_visible_and_never_falls_back(scratch_store, monkeypatch, today, caplog):
    from alpha_agents.data import learning_candidates as store
    from alpha_agents.evolution import lessons

    _, conn = scratch_store
    pid = _principle(conn, today)
    _proposals(monkeypatch, conn, today, pid)
    store.init_schema(conn)
    conn.execute("CREATE TRIGGER reject_candidates BEFORE INSERT ON learning_candidates "
                 "BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END")
    conn.commit()
    before = _knowledge(conn)
    with pytest.raises(sqlite3.IntegrityError, match='synthetic write failure'):
        store.save_candidate(entity_type='playbook', operation='create', source='test',
                             source_date=today, payload={'name': 'Synthetic'})
    counts = lessons.consolidate_principles(today)
    assert counts['failed'] == 2
    assert counts['candidates'] == 0
    assert 'synthetic write failure' in caplog.text
    _grades(conn, today, hit=1)
    summary = asyncio.run(lessons.post_review(today, 'No new lessons'))
    assert 'failures' in summary.lower()
    assert _knowledge(conn) == before
    assert not _rows(conn, 'learning_candidates')
    assert _rows(conn, 'daily_lessons')


def test_discovery_returns_one_candidate_per_nonempty_pattern(scratch_store, monkeypatch, today):
    from alpha_agents.evolution import lessons, playbook

    _, conn = scratch_store
    cluster = {'theme': 'Novel', 'hits': 6, 'total': 6, 'avg_return': 2.0}
    monkeypatch.setattr(playbook, '_query_hit_clusters', Mock(return_value=[
        cluster, dict(cluster), {'hits': 6, 'total': 6, 'avg_return': 2.0},
    ]))
    asyncio.run(lessons.post_review(today, 'No new lessons'))
    assert len(_rows(conn, 'learning_candidates')) == 1
    assert not _rows(conn, 'playbooks')


@pytest.mark.parametrize('result', [None, [], {'operations': None}, {'operations': [None, 1]}])
def test_malformed_consolidation_fails_closed(scratch_store, monkeypatch, today, result):
    from alpha_agents.evolution import lessons

    _, conn = scratch_store
    pid = _principle(conn, today, status='weakened')
    _proposals(monkeypatch, conn, today, pid)
    monkeypatch.setattr(lessons, '_call_consolidation_llm', Mock(return_value=result))
    before = _knowledge(conn)
    counts = lessons.consolidate_principles(today)
    assert counts['failed'] > 0
    assert counts['candidates'] == counts['created'] == counts['reinforced'] == 0
    assert _knowledge(conn) == before


@pytest.mark.parametrize('failure', ['llm', 'candidate', 'measurement', 'metrics'])
def test_post_review_failure_keeps_rules_and_reports_failure(scratch_store, monkeypatch, today, failure):
    from alpha_agents.evolution import lessons, metrics, principle_scoring

    _, conn = scratch_store
    pid = _principle(conn, today, evidence=_grades(conn, today))
    _proposals(monkeypatch, conn, today, pid)
    for i in range(14):
        _playbook(conn, today, name=f'Existing-{i}', total=12, hit_rate=0.8)
    _grades(conn, today, hit=1)
    error = Mock(side_effect=RuntimeError('Synthetic unavailable dependency'))
    if failure == 'llm':
        monkeypatch.setattr(lessons, '_call_consolidation_llm', error)
    elif failure == 'candidate':
        monkeypatch.setattr(lessons, 'save_candidate', error)
    elif failure == 'measurement':
        monkeypatch.setattr(principle_scoring, 'score_principle', error)
    else:
        monkeypatch.setattr(metrics, 'compute_evolution_metrics', error)
    before = _knowledge(conn)
    summary = asyncio.run(lessons.post_review(
        today, '<!-- LESSONS: [{"type":"insight","content":"New observation"}] -->',
    ))
    assert _knowledge(conn) == before
    assert 'failures' in summary.lower()
    assert 'not approved' in summary.lower()
    assert _rows(conn, 'daily_lessons')


def test_store_canonical_retry_concurrency_and_changed_evidence(scratch_store, today):
    from concurrent.futures import ThreadPoolExecutor
    from alpha_agents.data import learning_candidates as store

    _, conn = scratch_store
    kwargs = dict(entity_type='principle', operation='create', source='test', source_date=today)
    first_payload = {'proposal': {'principle': 'Synthetic', 'category': 'entry'}, 'cases': [1]}
    reordered = {'cases': [1], 'proposal': {'category': 'entry', 'principle': 'Synthetic'}}
    with ThreadPoolExecutor(max_workers=4) as executor:
        ids = list(executor.map(lambda i: store.save_candidate(
            **kwargs, payload=first_payload if i % 2 else reordered), range(12)))
    assert len(set(ids)) == 1
    original = _rows(conn, 'learning_candidates')[0]
    changed = store.save_candidate(**kwargs, payload={**first_payload, 'cases': [1, 2]})
    assert changed != ids[0]
    assert _rows(conn, 'learning_candidates')[0] == original
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE learning_candidates SET status = 'active' WHERE id = ?", (ids[0],))
    conn.rollback()
    assert all(row['status'] == 'candidate' for row in _rows(conn, 'learning_candidates'))


@pytest.mark.parametrize('changes', [
    {'target_id': 0}, {'target_id': True}, {'target_id': -1},
    {'operation': 'reinforce', 'target_id': None},
    {'operation': 'create', 'target_id': 1},
    {'entity_type': 'playbook', 'operation': 'reinforce', 'target_id': 1},
    {'payload': []}, {'source': ''}, {'source_date': 'not-a-date'},
])
def test_store_rejects_invalid_candidate_envelopes(scratch_store, today, changes):
    from alpha_agents.data import learning_candidates as store

    _, conn = scratch_store
    kwargs = dict(entity_type='principle', operation='create', source='test',
                  source_date=today, payload={'proposal': 'Synthetic'})
    with pytest.raises(ValueError):
        store.save_candidate(**{**kwargs, **changes})
    assert not _rows(conn, 'trading_principles')


def test_lower_level_explicit_memory_operations_remain_compatible(scratch_store, today):
    ms, conn = scratch_store
    pid = ms.create_trading_principle(principle='Explicit', pattern_description='Pattern',
                                     category='entry', action_guidance='Guidance', evidence=[], today=today)
    ms.set_principle_status(pid, 'weakened')
    ms.reinforce_trading_principle(pid, today=today, new_case={'code': 'SYNTH', 'date': today})
    assert _rows(conn, 'trading_principles')[0]['status'] == 'active'
    pbid = ms.create_playbook(name='Explicit', pattern_json={'conditions': []}, today=today)
    ms.update_playbook_status(pbid, status='degraded', weight=0.5,
                              reason='Explicit operation', hit_rate_at_change=0.2, today=today)
    assert _rows(conn, 'playbooks')[0]['status'] == 'degraded'


@pytest.mark.parametrize('dependency', [
    'rescore_all_principles', 'update_playbook_stats',
    'scan_and_auto_create', 'enforce_capacity',
])
def test_post_review_does_not_depend_on_quarantined_legacy_implementations(
        scratch_store, monkeypatch, today, dependency):
    """The scheduled boundary must stay safe with original mutating APIs."""
    from alpha_agents.evolution import lessons, playbook, principle_scoring

    ms, conn = scratch_store
    pid = _principle(conn, today, status='weakened')
    _proposals(monkeypatch, conn, today, pid)
    _playbook(conn, today)
    _grades(conn, today, hit=1)

    def mutate_active_rules(*args, **kwargs):
        worker = ms._get_conn()
        worker.execute("UPDATE trading_principles SET status = 'active', evidence_count = 999")
        worker.execute("UPDATE playbooks SET status = 'deprecated', weight = 0")
        worker.commit()
        return {} if dependency == 'rescore_all_principles' else []

    owner = principle_scoring if dependency == 'rescore_all_principles' else playbook
    legacy = Mock(side_effect=mutate_active_rules)
    monkeypatch.setattr(owner, dependency, legacy)
    before = _knowledge(conn)
    report = '<!-- LESSONS: [{"type":"insight","content":"New synthetic observation"}] -->'
    summary = asyncio.run(lessons.post_review(today, report))

    legacy.assert_not_called()
    assert _knowledge(conn) == before
    assert 'not approved' in summary.lower()
    candidates = _rows(conn, 'learning_candidates')
    assert [(c['entity_type'], c['operation']) for c in candidates] == [
        ('principle', 'create'), ('principle', 'reinforce'), ('playbook', 'create'),
    ]
    asyncio.run(lessons.post_review(today, report))
    assert _rows(conn, 'learning_candidates') == candidates
    assert _knowledge(conn) == before


def test_discovery_storage_failure_does_not_discard_other_patterns(
        scratch_store, monkeypatch, today, caplog):
    from alpha_agents.data import learning_candidates as store
    from alpha_agents.evolution import lessons, playbook

    _, conn = scratch_store
    store.init_schema(conn)
    conn.execute(
        "CREATE TRIGGER reject_one_candidate BEFORE INSERT ON learning_candidates "
        "WHEN json_extract(NEW.payload_json, '$.cluster.theme') = 'Rejected' "
        "BEGIN SELECT RAISE(ABORT, 'synthetic rejected pattern'); END"
    )
    conn.commit()
    monkeypatch.setattr(playbook, '_query_hit_clusters', Mock(return_value=[
        {'theme': 'Rejected', 'hits': 6, 'total': 6, 'avg_return': 2.0},
        {'theme': 'Retained', 'hits': 6, 'total': 6, 'avg_return': 2.0},
    ]))
    summary = asyncio.run(lessons.post_review(today, 'No new lessons'))
    candidates = _rows(conn, 'learning_candidates')
    assert len(candidates) == 1
    assert json.loads(candidates[0]['payload_json'])['cluster']['theme'] == 'Retained'
    assert 'failures' in summary.lower()
    assert 'synthetic rejected pattern' in caplog.text
    assert not _rows(conn, 'playbooks')


def test_identical_principle_proposals_deduplicate_despite_input_order(
        scratch_store, monkeypatch, today):
    from alpha_agents.evolution import lessons

    _, conn = scratch_store
    pid = _principle(conn, today)
    _proposals(monkeypatch, conn, today, pid)
    conn.execute('INSERT INTO daily_lessons (date, lesson_type, content) VALUES (?, ?, ?)',
                 (today, 'insight', 'Second synthetic observation'))
    conn.commit()
    observations = _rows(conn, 'daily_lessons')
    monkeypatch.setattr(lessons, 'get_recent_daily_lessons', Mock(side_effect=[
        observations, list(reversed(observations)),
    ]))
    lessons.consolidate_principles(today)
    candidates = _rows(conn, 'learning_candidates')
    lessons.consolidate_principles(today)
    assert _rows(conn, 'learning_candidates') == candidates
