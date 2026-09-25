from pathlib import Path


def change(path, old, new):
    file = Path(path)
    text = file.read_text()
    if new in text:
        return
    assert old in text, path
    file.write_text(text.replace(old, new, 1))


change('alpha_agents/evolution/review.py',
       'def direction_choice(book: sqlite3.Connection, hist: sqlite3.Connection,', '''def _run_scope(run_id: str | tuple[str, ...] | None, *, alias: str = "") -> tuple[str, tuple]:
    """Keep parent observations identifiable while selecting one branch lineage."""
    if run_id is None:
        return "", ()
    ids = (run_id,) if isinstance(run_id, str) else run_id
    if not isinstance(ids, tuple) or not ids or any(not isinstance(x, str) or not x for x in ids):
        raise ValueError("Run scope must name one run or a nonempty lineage")
    ids = tuple(dict.fromkeys(ids))
    return f"WHERE {alias}run_id IN ({','.join('?' for _ in ids)}) ", ids


def direction_choice(book: sqlite3.Connection, hist: sqlite3.Connection,''')
p=Path('alpha_agents/evolution/review.py')
s=p.read_text().replace('run_id: str | None = None', 'run_id: str | tuple[str, ...] | None = None')
for query_start in ('SELECT s.day, i.sector_id', 'SELECT s.day, i.code'):
    index=s.index('    rows = book.execute(\n        "'+query_start)
    before=s[:index]
    if not before.endswith('    scope, params = _run_scope(run_id, alias="s.")\n'):
        s=before+'    scope, params = _run_scope(run_id, alias="s.")\n'+s[index:]
s=s.replace('+ ("WHERE s.run_id = ? " if run_id else "")\n        + "ORDER BY s.day", (run_id,) if run_id else ()).fetchall()',
            '+ scope + "ORDER BY s.day", params).fetchall()')
if '    scope, params = _run_scope(run_id)\n    days =' not in s:
    s=s.replace('    days = [r["day"] for r in book.execute(',
                '    scope, params = _run_scope(run_id)\n    days = [r["day"] for r in book.execute(')
s=s.replace('+ ("WHERE run_id = ? " if run_id else "")\n        + "ORDER BY day", (run_id,) if run_id else ()).fetchall()]',
            '+ scope + "ORDER BY day", params).fetchall()]')
p.write_text(s)
change('scripts/walk_forward.py', '                         run_id=ctx.run_id, trader_id=ctx.trader, as_of=day)',
       '                         run_id=getattr(ctx, "review_lineage", ctx.run_id),\n                         trader_id=ctx.trader, as_of=day)')
change('scripts/walk_checkpoint.py', '    ctx.branch_start = value["next_session"]',
       '    ctx.branch_start = value["next_session"]\n    ctx.review_lineage = (value["parent_run_id"], ctx.run_id)')
change('tests/test_walk_branch.py',
       "    return {'source_hash': CP.source_identity(), 'runtime_hash': 'runtime',",
       "    return {'source_hash': CP.source_identity(), 'runtime_hash': 'runtime', 'parent_run_id': 'parent',")
change('tests/test_walk_branch.py',
       "    ctx = SimpleNamespace(args=SimpleNamespace(start='2026-01-06'),",
       "    ctx = SimpleNamespace(run_id='child', args=SimpleNamespace(start='2026-01-06'),")
change('tests/test_walk_branch.py', "    assert ctx.window_start == '2025-12-01'",
       "    assert ctx.window_start == '2025-12-01'\n    assert ctx.review_lineage == ('parent', 'child')")
extra = '''


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
'''
p=Path('tests/test_walk_branch.py')
if 'def test_measured_review_keeps_parent_lineage' not in p.read_text():
    p.write_text(p.read_text()+extra)
change('docs/walk_branch.md',
       'process image. Session events keep historical run IDs; new calls get new IDs.',
       'process image. Session events keep historical run IDs; new calls get new IDs.\n  Measured reviews query the explicit parent/child lineage, so a new run ID\n  neither erases parent samples nor incorporates unrelated runs.')
