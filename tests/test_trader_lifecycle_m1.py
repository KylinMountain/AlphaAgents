"""M1: original facts, bounded review completion and trader-local memory."""
import asyncio
import importlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from alpha_agents import config
from alpha_agents.data import memory_store, trader_session, trade_review_store as RS
from alpha_agents.evolution import day_record as DR, session_facts as SF, trade_review as TR
from alpha_agents.evolution.replay_mode import replay_as_of
from alpha_agents.pipeline.tasks import close_review as CR, session_memory as SM
from tests.test_trade_review import hist, _close, _pos


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(memory_store, 'MEMORY_DB_PATH', tmp_path / 'memory.db')
    monkeypatch.setattr(memory_store._local, 'conn', None, raising=False)
    c = memory_store._get_conn()
    yield c
    c.close()
    memory_store._local.conn = None


def run(conn, hist, day='2026-01-19', model='stub', stats=None):
    with replay_as_of(day + ' 19:00'):
        return asyncio.run(TR.review_closed(conn, hist, trader_id='default', as_of=day,
                                            model=model, stats=stats))


def words(text='new evidence'):
    return {**TR.NO_WORDS, 'verdict': 'uncertain', 'next_time': text}


def state(conn):
    return dict(conn.execute('SELECT * FROM trade_reviews WHERE position_id=1').fetchone())


def test_missing_today_never_returns_yesterdays_move(hist):
    assert DR._bar(hist, '601360', '2026-01-20') is None
    assert DR._bar(hist, '601360', '2026-01-19')[0] == 12.56


def test_partial_market_and_missing_flow_are_explicit(hist, monkeypatch):
    monkeypatch.setattr(SF, 'MIN_MEMBERS', 1)
    hist.execute("INSERT INTO daily_kline VALUES ('600002','2026-01-16',10,10,10,10)")
    result = SF.compute(hist, day='2026-01-19', members={'AI':['601360','601360','600002']},
                        unseen_label='unknown')
    assert (result['market']['n'], result['market']['previous_sample_n']) == (1, 2)
    b = result['top'][0]
    assert (b['covered'], b['total'], b['flow_covered'], b['ours']) == (1,2,0,'unknown')
    text = SF.render(result)
    assert '1/2' in text and '\u4e3b\u529b\u51c0\u989d\u7f3a\u5931' in text
    assert '\u5168\u5e02\u573a 1 \u53ea' not in text


def test_missing_account_prices_are_named(conn, hist, monkeypatch):
    _close(conn)
    _close(conn, pid=2, code='600002')
    monkeypatch.setattr(CR, '_members', lambda: ({},{}))
    monkeypatch.setattr(CR, '_news_search', lambda day: None)
    monkeypatch.setattr(SF, 'load_news', lambda day, prev: ([],[]))
    text = CR._day_facts(hist, '2026-01-19', 'default')
    assert '1/2' in text and '600002' in text


def test_morning_not_reconstructed_from_evening_themes(conn, hist, monkeypatch):
    monkeypatch.setattr(memory_store,'get_active_themes',lambda: pytest.fail('mutable evening read'))
    with replay_as_of('2026-01-19 09:00'):
        trader_session.append(trader_id='default', kind='morning_input', payload={'themes':['original']})
    with replay_as_of('2026-01-19 14:00'):
        trader_session.append(trader_id='default', kind='morning_input', payload={'themes':['late']})
    with replay_as_of('2026-01-19 19:00'):
        text = CR._record(hist,'default','2026-01-19')
        assert 'original' in text and 'late' not in text
        assert 'original' not in CR._record(hist,'other','2026-01-19')
        assert set(CR._ours('default','2026-01-19')) == {'original'}


def test_session_rows_immutable_and_idempotent(conn):
    with replay_as_of('2026-01-19 09:00'):
        a=trader_session.append(trader_id='a',kind='morning_input',payload={'themes':[]})
        b=trader_session.append(trader_id='a',kind='morning_input',payload={'themes':[]})
        assert a == b and len(trader_session.read(trader_id='a',kind='morning_input')) == 1
    with pytest.raises(sqlite3.DatabaseError): conn.execute("UPDATE trader_session_events SET payload_json='{}'")
    with pytest.raises(sqlite3.DatabaseError): conn.execute('DELETE FROM trader_session_events')


def test_boundary_extrema_not_assumed_held(conn,hist):
    _close(conn)
    f=TR.facts(_pos(conn),hist)
    assert f['peak_pct'] == 0
    assert f['worst_pct'] == pytest.approx((12.56/13.19-1)*100,abs=.01)
    assert f['potential_intraday_high_pct'] > 6 and f['achievable_exit'] is None
    assert f['boundary_extrema_excluded']
    assert '\u975e\u53ef\u8fbe\u5229\u6da6' in TR.facts_line(f)


def test_interior_extrema_include_only_held_sessions(conn,hist):
    hist.execute("INSERT INTO daily_kline VALUES ('601360','2026-01-20',12,40,2,12)")
    hist.execute("UPDATE daily_kline SET high=15,low=12 WHERE date='2026-01-19'")
    _close(conn,close_date='2026-01-20')
    f=TR.facts(_pos(conn),hist)
    assert f['peak_date'] == '2026-01-19' and f['peak_source'] == 'interior_daily_bar'
    assert f['peak_pct'] == pytest.approx((15/13.19-1)*100,abs=.01)
    assert f['worst_pct'] == pytest.approx((12/13.19-1)*100,abs=.01)
    assert f['potential_intraday_high_pct'] > 200


def test_missing_entry_bar_waits(conn,hist):
    _close(conn,open_date='2026-01-13')
    assert TR.facts(_pos(conn),hist) is None


def test_same_day_unknown_phase_uses_actual_fills_only(hist):
    f=TR.facts({'id':1,'code':'601360','open_date':'2026-01-16','close_date':'2026-01-16',
                'open_price':13.19,'close_price':13.19},hist)
    assert f['peak_pct'] == f['worst_pct'] == f['giveback_pp'] == 0
    assert f['achievable_exit'] is None


def test_failed_review_retries_without_backdating(conn,hist,monkeypatch):
    _close(conn)
    calls=[]
    async def answer(*args,**kwargs):
        assert not conn.in_transaction
        assert state(conn)['facts_available_on'] == '2026-01-19'
        calls.append(1)
        return dict(TR.NO_WORDS) if len(calls)==1 else words('learned-on-20')
    monkeypatch.setattr(TR,'write_words',answer)
    stats={}
    assert run(conn,hist,stats=stats)==0
    assert stats['trade_review_failed']==stats['trade_review_pending']==1
    assert state(conn)['review_status']=='failed'
    assert run(conn,hist)==0 and len(calls)==1
    assert run(conn,hist,'2026-01-20')==1
    assert state(conn)['review_available_on']=='2026-01-20'
    assert 'learned-on-20' not in TR.inject(conn,'default',before='2026-01-20')
    assert 'learned-on-20' in TR.inject(conn,'default',before='2026-01-21')
    assert TR.reviews_for(conn,'default',up_to='2026-01-19')[0][1] == TR.NO_WORDS
    assert run(conn,hist,'2026-01-21')==0 and len(calls)==2
    assert conn.execute('SELECT count(*) FROM trade_reviews').fetchone()[0]==1
    assert conn.execute('SELECT count(*) FROM trade_review_attempts').fetchone()[0]==4


def test_no_model_retains_facts_without_consuming_attempt(conn,hist,monkeypatch):
    _close(conn)
    stats={}
    assert run(conn,hist,model=None,stats=stats)==0
    assert stats['trade_review_facts']==1 and state(conn)['attempt_count']==0
    async def answer(*args,**kwargs): return words()
    monkeypatch.setattr(TR,'write_words',answer)
    assert run(conn,hist)==1


def test_attempts_bounded_and_auditable(conn,hist,monkeypatch):
    _close(conn)
    async def answer(*args,**kwargs): return {**TR.NO_WORDS,'_error':'timeout'}
    monkeypatch.setattr(TR,'write_words',answer)
    stats={}
    for d in ('19','20','21','22'): run(conn,hist,'2026-01-'+d,stats=stats)
    assert state(conn)['attempt_count']==3
    assert stats['trade_review_pending']==stats['trade_review_exhausted']==1
    rows=conn.execute("SELECT error FROM trade_review_attempts WHERE status='failed'").fetchall()
    assert len(rows)==3 and all(row[0]=='timeout' for row in rows)
    with pytest.raises(sqlite3.DatabaseError): conn.execute("UPDATE trade_review_attempts SET error='erased'")


def test_interrupted_attempt_survives_and_resumes(conn,hist,monkeypatch):
    _close(conn)
    async def interrupted(*args,**kwargs): raise asyncio.CancelledError()
    monkeypatch.setattr(TR,'write_words',interrupted)
    with pytest.raises(asyncio.CancelledError): run(conn,hist)
    assert state(conn)['review_status']=='pending' and state(conn)['attempt_count']==1
    assert conn.execute('SELECT status FROM trade_review_attempts').fetchone()[0]=='started'
    async def answer(*args,**kwargs): return words()
    monkeypatch.setattr(TR,'write_words',answer)
    assert run(conn,hist,'2026-01-20')==1


def test_cas_no_duplicate_or_overwrite(conn,hist):
    _close(conn)
    RS.save_facts(conn,TR.facts(_pos(conn),hist),'default','2026-01-19')
    assert RS.claim(conn,1,'2026-01-19')==1 and RS.claim(conn,1,'2026-01-19') is None
    assert RS.finish(conn,1,1,words('original'),'2026-01-19')
    assert not RS.finish(conn,1,1,words('overwrite'),'2026-01-19')
    assert 'original' in state(conn)['lesson_json'] and 'overwrite' not in state(conn)['lesson_json']


def test_late_facts_not_visible_earlier(conn,hist):
    _close(conn)
    run(conn,hist,'2026-01-21',model=None)
    assert TR.inject(conn,'default',before='2026-01-20')==''
    assert TR.inject(conn,'default',before='2026-01-22')


def test_legacy_migration_uses_creation_not_close():
    c=sqlite3.connect(':memory:')
    c.execute('CREATE TABLE trade_reviews (id INTEGER PRIMARY KEY,close_date TEXT,created_at TEXT,lesson_json TEXT)')
    c.execute('INSERT INTO trade_reviews VALUES (1,?,?,?)',('2026-01-05','2026-01-10T10:00:00Z',json.dumps(words())))
    c.execute('INSERT INTO trade_reviews VALUES (2,?,?,?)',('2026-01-05','2026-01-10T10:00:00Z',json.dumps(TR.NO_WORDS)))
    RS.migrate(c);RS.migrate(c)
    assert c.execute('SELECT facts_available_on,review_available_on,review_status FROM trade_reviews WHERE id=1').fetchone()==('2026-01-10','2026-01-10','complete')
    assert c.execute('SELECT review_status FROM trade_reviews WHERE id=2').fetchone()[0]=='pending'
    c.close()


def test_verdict_only_not_complete():
    assert not RS.complete({'verdict':'good'})
    assert not RS.complete({'verdict':{},'next_time':['fake']})
    assert RS.complete(words())


def test_run_and_trader_isolation(conn):
    with replay_as_of('2026-01-19 10:00'):
        SM.note_decline('600001',10,'trader-a',trader_id='a',run_id='r1')
        SM.note_decline('600001',11,'trader-b',trader_id='b',run_id='r1')
    with replay_as_of('2026-01-19 10:05'):
        a=SM.recall_decline('600001',12,trader_id='a',run_id='r1')
        b=SM.recall_decline('600001',12,trader_id='b',run_id='r1')
        assert 'trader-a' in a and 'trader-b' not in a
        assert 'trader-b' in b and 'trader-a' not in b
        assert SM.recall_decline('600001',12,trader_id='a',run_id='r2') is None
        assert SM.recall_decline('600001',12,trader_id='other',run_id='r1') is None


def test_module_restart_and_database_reopen_preserve_memory(conn):
    with replay_as_of('2026-01-19 10:00'):
        SM.note_decline('600001',10,'persisted',trader_id='a')
    importlib.reload(SM)
    c=sqlite3.connect(memory_store.MEMORY_DB_PATH)
    try:
        with replay_as_of('2026-01-19 10:05'):
            assert 'persisted' in SM.recall_decline('600001',11,trader_id='a')
            assert trader_session.read(trader_id='a',kind='entry_observation',conn=c)[0]['payload']['reason']=='persisted'
    finally:
        c.close()


@pytest.mark.parametrize('at',['2026-01-19 09:59','2026-01-19 10:46','2026-01-20 10:05'])
def test_future_expired_and_old_session_excluded(conn,at):
    with replay_as_of('2026-01-19 10:00'): SM.note_decline('600001',10,'before',trader_id='a')
    with replay_as_of(at): assert SM.recall_decline('600001',11,trader_id='a') is None


def test_no_answer_not_intentional_decline(conn):
    with replay_as_of('2026-01-19 10:00'): SM.note_decline('600001',10,'explicit',trader_id='a')
    with replay_as_of('2026-01-19 10:05'):
        SM.note_undecided('600001',11,trader_id='a')
        assert SM.recall_decline('600001',11,trader_id='a') is None
        rows=trader_session.read(trader_id='a',kind='entry_observation',code='600001')
        assert [row['payload']['action'] for row in rows]==['decline','undecided']


def test_identity_and_reason_required(conn):
    with pytest.raises(TypeError): SM.note_decline('600001',10,'reason')
    with pytest.raises(ValueError): SM.note_decline('600001',10,'',trader_id='a')
    with pytest.raises(ValueError): SM.note_decline('600001',10,'reason',trader_id='')


def test_lost_replay_context_fails_closed(monkeypatch):
    from alpha_agents.evolution import replay_mode
    from alpha_agents.data.clock import LookAheadError
    monkeypatch.setattr(replay_mode,'_REPLAY_PROCESS',True)
    monkeypatch.setattr(replay_mode,'get_replay_as_of',lambda: None)
    with pytest.raises(LookAheadError): trader_session.instant()


def test_actual_morning_input_saved_before_failure(conn,monkeypatch):
    from alpha_agents.pipeline.tasks import morning_scan as MS
    from alpha_agents.evolution import context_builder
    import alpha_agents.evolution as EV
    monkeypatch.setattr(context_builder,'knowledge_in_force',lambda:'')
    monkeypatch.setattr(EV,'build_morning_context',lambda **kw:'own state')
    async def answer(*args,**kwargs):
        rows=trader_session.read(trader_id='a',kind='morning_input')
        assert rows[0]['payload']['stats_context']=='own state'
        return '[failed]'
    monkeypatch.setattr(MS,'run_morning_analysis',answer)
    with replay_as_of('2026-01-19 09:00'):
        asyncio.run(MS._scan_for(SimpleNamespace(id='a'),'news','themes','stats',themes=[{'name':'original'}]))
        assert trader_session.read(trader_id='a',kind='morning_input')[0]['payload']['themes']==['original']


def test_actual_pricing_enrichment_does_not_mutate_shared_candidates(conn,monkeypatch):
    from alpha_agents.pipeline.tasks import intraday_monitor as IM, entry_pricing as EP
    with replay_as_of('2026-01-19 10:00'):
        SM.note_decline('600001',10,'only-a',trader_id='a')
        seen={}
        async def price(candidates,trader): seen[trader.id]=candidates; return {}
        monkeypatch.setattr(EP,'enabled',lambda:True)
        monkeypatch.setattr(EP,'price',price)
        shared=[{'code':'600001','prior_view':'foreign'}]
        for trader in ('a','b'): asyncio.run(IM._price_for(SimpleNamespace(id=trader),shared,{'600001':11}))
        assert 'only-a' in seen['a'][0]['prior_view']
        assert 'prior_view' not in seen['b'][0]
        assert shared==[{'code':'600001','prior_view':'foreign'}]


def test_processing_day_cannot_backdate(conn,hist,monkeypatch):
    _close(conn)
    async def answer(*args,**kwargs): return words('late')
    monkeypatch.setattr(TR,'write_words',answer)
    with replay_as_of('2026-01-22 19:00'):
        assert asyncio.run(TR.review_closed(conn,hist,trader_id='default',as_of='2026-01-19',model='stub'))==1
    assert state(conn)['facts_available_on']==state(conn)['review_available_on']=='2026-01-22'
    assert TR.inject(conn,'default',before='2026-01-22 09:00')==''
    assert 'late' in TR.inject(conn,'default',before='2026-01-23')


def test_completion_crossing_midnight_is_late(conn,hist):
    _close(conn)
    RS.save_facts(conn,TR.facts(_pos(conn),hist),'default','2026-01-19')
    attempt=RS.claim(conn,1,'2026-01-19')
    assert RS.finish(conn,1,attempt,words('midnight'),'2026-01-19',completed_on='2026-01-20')
    assert 'midnight' not in TR.inject(conn,'default',before='2026-01-20')
    assert 'midnight' in TR.inject(conn,'default',before='2026-01-21')


def test_old_attempt_cannot_overwrite_new(conn,hist):
    _close(conn)
    RS.save_facts(conn,TR.facts(_pos(conn),hist),'default','2026-01-19')
    assert RS.claim(conn,1,'2026-01-19')==1 and RS.claim(conn,1,'2026-01-20')==2
    assert not RS.finish(conn,1,1,words('stale'),'2026-01-19')
    assert RS.finish(conn,1,2,words('current'),'2026-01-20')
    assert 'stale' not in state(conn)['lesson_json']


def test_failed_review_not_completed_learning(conn,hist,monkeypatch):
    from alpha_agents.evolution import close_day,handbook
    _close(conn)
    async def failed(*args,**kwargs): return dict(TR.NO_WORDS)
    async def never(*args,**kwargs): pytest.fail('facts alone cannot trigger consolidation')
    monkeypatch.setattr(TR,'write_words',failed)
    monkeypatch.setattr(handbook,'consolidate',never)
    with replay_as_of('2026-01-19 19:00'):
        stats=asyncio.run(close_day.review_day(conn,hist,trader_id='default',trader=None,
                          day='2026-01-19',model='stub',facts_text='',review_market=False))
    assert stats['trade_reviews']==stats['handbook']==0
    assert stats['trade_review_facts']==stats['trade_review_failed']==stats['trade_review_pending']==1


def test_missing_exit_not_fabricated(conn,hist):
    _close(conn); pos=_pos(conn);pos['close_price']=None
    assert TR.facts(pos,hist) is None


def test_legacy_extrema_not_pooled_with_new(conn,hist):
    _close(conn);new=TR.facts(_pos(conn),hist)
    old={**new,'boundary_extrema_excluded':False,'peak_pct':99,'giveback_pp':100}
    text=TR.summary_line([new,old])
    assert '99' not in text and '100' not in text
    assert '\u65e7\u8bb0\u5f55' in text and '\u4e0d\u7eb3\u5165' in text


@pytest.mark.parametrize('has_model', [False, True])
def test_replay_facts_only_is_not_failed_learning(conn, tmp_path, monkeypatch, has_model):
    from collections import Counter
    from scripts import walk_forward as WF
    from alpha_agents.evolution import close_day
    monkeypatch.setattr(WF, 'DATA_DIR', tmp_path)
    db = sqlite3.connect(tmp_path / 'market_history.db')
    db.execute('CREATE TABLE daily_kline(date TEXT)')
    db.commit()
    db.close()
    sqlite3.connect(tmp_path / 'market_snapshots.db').close()
    monkeypatch.setattr(WF, '_review_members', lambda ctx: {})
    monkeypatch.setattr(WF, '_review_names', lambda ctx: {})
    monkeypatch.setattr(WF, '_ours_today', lambda *args: {})
    monkeypatch.setattr(WF, '_day_record', lambda *args: '')
    monkeypatch.setattr(WF, '_news_search', lambda *args: None)
    monkeypatch.setattr(SF, 'load_news', lambda *args: ([], []))
    monkeypatch.setattr(SF, 'compute', lambda *args, **kw: {})
    monkeypatch.setattr(SF, 'render', lambda *args: '')
    async def review(*args, **kwargs):
        return {'trade_reviews': 0, 'trade_review_facts': 1, 'trade_review_pending': 1}
    monkeypatch.setattr(close_day, 'review_day', review)
    ctx = SimpleNamespace(trader='default', model='stub' if has_model else None,
                          counters=Counter(), failed_learning=[], exposure_log=[], loop=None)
    with replay_as_of('2026-01-19 19:00'):
        assert WF._review_trades(ctx, '2026-01-19', conn) == 0
    assert ctx.counters['trade_review_facts'] == ctx.counters['trade_review_pending'] == 1
    assert bool(ctx.failed_learning) == has_model


def test_replay_close_review_cannot_swallow_temporal_fault(conn, tmp_path, monkeypatch):
    from scripts import walk_forward as WF
    from alpha_agents.data.clock import LookAheadError
    monkeypatch.setattr(WF, 'DATA_DIR', tmp_path)
    for name in ('market_history.db', 'market_snapshots.db'):
        sqlite3.connect(tmp_path / name).close()
    def lost(ctx):
        raise LookAheadError('lost replay clock')
    monkeypatch.setattr(WF, '_review_members', lost)
    with pytest.raises(LookAheadError):
        WF._review_trades(SimpleNamespace(loop=None), '2026-01-19', conn)
