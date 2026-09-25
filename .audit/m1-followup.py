from pathlib import Path

def change(path, old, new):
    file = Path(path)
    text = file.read_text()
    if new in text:
        return
    assert old in text, path
    file.write_text(text.replace(old, new, 1))

change('alpha_agents/evolution/close_day.py',
       '"""Run all three steps for one trader. Never raises; returns counts."""',
       '"""Return completion counts; temporal-integrity faults propagate."""')
change('alpha_agents/evolution/trade_review.py',
       '"""Ask the trader for its review of one trade. Never raises.',
       '"""Ask for an interpretation; only temporal-integrity faults propagate.')
change('scripts/walk_forward.py',
       '    except Exception as exc:                          # noqa: BLE001\n        logger.warning("%s: close review failed: %s", day, exc)',
       '    except Exception as exc:                          # noqa: BLE001\n        from alpha_agents.data.clock import LookAheadError\n        if isinstance(exc, LookAheadError):\n            raise\n        logger.warning("%s: close review failed: %s", day, exc)')
change('scripts/walk_forward.py',
       '        if got.get(kind):\n            ctx.failed_learning.append((day, kind))',
       '        if got.get(kind) and (kind != "trade_review_pending" or ctx.model is not None):\n            ctx.failed_learning.append((day, kind))')
change('scripts/walk_forward.py',
       "    Returns the number of trade reviews written (the counter's old meaning);",
       "    Returns completed interpretations, not facts-only records;")
file = Path('tests/test_trader_lifecycle_m1.py')
text = file.read_text()
extra = '''

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
'''
if 'test_replay_facts_only_is_not_failed_learning' not in text:
    file.write_text(text + extra)
# The fixture represents a historical write; production keeps actual availability.
file = Path('tests/test_walk_forward.py')
text = file.read_text()
start = text.index('    def test_a_trade_review_reaches_the_next_day_and_not_the_day_it_was_written(')
end = text.find('\n    def ', start + 1)
end = len(text) if end == -1 else end
section = text[start:end]
if 'ctx.trader, as_of=_START)' not in section:
    assert '                 ctx.trader)' in section
    section = section.replace('                 ctx.trader)', '                 ctx.trader, as_of=_START)', 1)
    file.write_text(text[:start] + section + text[end:])
manifest = Path('/tmp/m1-validation/changed-paths.json')
if manifest.exists():
    import json
    paths = json.loads(manifest.read_text())
    if 'tests/test_walk_forward.py' not in paths:
        paths.append('tests/test_walk_forward.py')
        manifest.write_text(json.dumps(paths))

# Assert the new diagnostic label, retaining the legacy-data warning.
change('tests/test_walk_forward.py',
       'assert "吐回11.7个点" in block and "下次：先兑现一半" in block',
       'assert "峰值差11.7个点（非可达利润）" in block and "下次：先兑现一半" in block\n        assert "不纳入持有期极值统计" in block')
