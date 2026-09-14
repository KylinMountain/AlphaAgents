/* Render check: do the three workspaces survive every state they can be in?
 *
 * Every section can arrive as `unavailable`, `empty` or `present`, and the
 * page must render a different thing for each. This drives that matrix
 * through the real components.
 *
 * Run with: npm run check:render
 *
 * It exists because `npm run lint` and `npm run build` both pass on a page
 * that renders "暂无持仓" for a table that is not there. The pair of bugs
 * this caught on first run: `sec.value` (instead of `sec?.value`) crashed
 * any view handed a null payload, and `trade.book` — five hand-built cards,
 * no shared wrapper — turned "the table is gone" into "you hold nothing".
 */
import { renderToString } from 'react-dom/server'
import PortfolioView from './src/views/PortfolioView'
import LearnView from './src/views/LearnView'
import EvolveView from './src/views/EvolveView'

const absent = (source, missing) => ({
  source, needs: [], schema: 'absent', state: 'unavailable', rows: null,
  value: null, missing, note: 'section note', status_note: '表不在，读模型不建表',
})

const partial = (source, missing) => ({
  source, needs: [], schema: 'partial', state: 'unavailable', rows: null,
  value: null, missing, note: 'section note', status_note: 'schema 落后于代码',
})

const empty = (source, value) => ({
  source, needs: [], schema: 'complete', state: 'empty', rows: 0,
  value, missing: [], note: 'section note', status_note: '',
})

const present = (source, value, rows) => ({
  source, needs: [], schema: 'complete', state: 'present', rows,
  value, missing: [], note: 'section note', status_note: '',
})

const bookValue = {
  pending: [{ id: 1, code: '300684', name: '中石科技', theme: '散热',
             entry_low: 20, entry_high: 21, stop_loss: 19, target_price: 24,
             order_date: '2026-09-11', expire_days: 2 }],
  positions: [
    { id: 9, code: '600460', name: '士兰微', theme: '半导体', open_price: 30,
      shares: 100, return_pct: 4.2, return_amount: 126, peak_return_pct: 6.1,
      max_drawdown_pct: -2.0, holding_days: 3, stop_loss: 28,
      thesis: { claim: '功率半导体涨价', prob: 0.6, horizon_days: 30,
                conviction: 0.3, conditions: [{ kind: 'price_below', value: 28,
                                                note: '', text: '跌破 28' }] } },
    { id: 10, code: '301232', name: '飞沃科技', theme: null, open_price: 40,
      shares: 50, return_pct: -1.1, return_amount: -22, peak_return_pct: 0.5,
      max_drawdown_pct: -3.2, holding_days: 1, thesis: null },
  ],
  closed: [{ id: 3, code: '002409', name: '雅克科技', theme: '半导体',
             status: 'expired', return_pct: -2.78, holding_days: 5,
             close_reason: '到期', close_date: '2026-09-10' }],
  stats: { total_closed: 9, wins: 4, losses: 5, win_rate: 44.4, avg_return: -0.6,
           avg_holding_days: 4, by_theme: { 半导体: { count: 3, avg_return: 1.2 } } },
  capital: { total: 100000, available: 95000, invested: 5000 },
  traders: [
    { id: 'default', name: '默认', legacy: false, blocked: false,
      capital: 100000, available: 95000, positions: 2, pending: 1,
      drawdown_pct: 1.4 },
    { id: 'legacy', name: '旧账本', legacy: true, blocked: true,
      capital: 0, available: 0, positions: 0, pending: 0, drawdown_pct: null },
  ],
}

const tradeSections = {
  book: present('virtual_portfolio (+ theses, traders/)', bookValue, 53),
  attribution: empty('position_exits → theses',
                     { by_thesis: [], exits: { legs: 0, unattributed: 0, net: null },
                       orders: { orders: 117, without_thesis: 40 } }),
  intents: empty('intents',
                 { recent: [], by_status: {}, never_decided: [] }),
  settlement: empty('reservations + settlement_lots',
                    { reservations: [], lots: {} }),
}

const tradeBlocked = {
  book: absent('virtual_portfolio', ['virtual_portfolio']),
  attribution: absent('position_exits → theses', ['position_exits']),
  intents: absent('intents', ['intents']),
  settlement: absent('reservations + settlement_lots', ['reservations']),
}

const learnSections = {
  episodes: empty('episodes + episode_events',
                  { coverage: { decisions: 0, verdicts: 0, no_verdict: 0,
                                refused: 0, traded: 0, cancelled: 0,
                                fill_rate: null }, open: [] }),
  outcomes: empty('outcomes', { counts: {}, pending: [], integrity: ['示例问题'] }),
  candidates: partial('learning_candidates + candidate_transitions',
                      ['learning_candidates.evidence_episode_ids',
                       'candidate_transitions']),
  forecasts: present('predictions',
                     { rows: 232, scored: 202, brier_scored: 0, prob_rows: 48,
                       recent_hit_rate: { total: 10, hits: 6, hit_rate: 60,
                                          by_confidence: { high: { total: 2, hits: 2, hit_rate: 100 } } },
                       pricing: { unscored: 48, ripe_unscored: 0, unripe: 48,
                                  min_remaining_days: 1, archive_readable: true,
                                  batches: [
                                    { date: '2026-09-08', rows: 7, horizon: 5,
                                      need: 6, have: 5, remaining: 1 },
                                    { date: '2026-09-09', rows: 29, horizon: 5,
                                      need: 6, have: 4, remaining: 2 }] } },
                     232),
}

/* The other branch of the evaluation currency: a window the market has
 * traded shut with no score behind it. This one IS a defect — the only
 * branch the page renders as a warning — so the matrix must hold both and
 * their copy must not be substrings of each other. */
const learnRipe = {
  ...learnSections,
  forecasts: present('predictions',
                     { rows: 232, scored: 202, brier_scored: 0, prob_rows: 48,
                       recent_hit_rate: {},
                       pricing: { unscored: 48, ripe_unscored: 7, unripe: 41,
                                  min_remaining_days: 2, archive_readable: true,
                                  batches: [{ date: '2026-09-07', rows: 7,
                                              horizon: 5, need: 6, have: 6,
                                              remaining: 0 }] } },
                     232),
}

/* And the branch where the currency can never exist: no row carries a prob. */
const learnUnpriceable = {
  ...learnSections,
  forecasts: present('predictions',
                     { rows: 100, scored: 80, brier_scored: 0, prob_rows: 0,
                       recent_hit_rate: {},
                       pricing: { unscored: 0, ripe_unscored: 0, unripe: 0,
                                  min_remaining_days: null,
                                  archive_readable: true, batches: [] } },
                     100),
}

const evolveSections = {
  pointer: absent('policy_versions + active_policy + policy_approvals',
                  ['policy_versions', 'active_policy', 'policy_approvals']),
  shadow: absent('shadow_runs + shadow_predictions',
                 ['shadow_runs', 'shadow_predictions']),
  gates: partial('gate_decisions',
                 ['gate_decisions.validation_days', 'gate_decisions.evidence_scope']),
  knowledge: empty('knowledge_snapshots + knowledge_snapshot_items',
                   { snapshots: [], counts: { snapshots: 0, items: 0 },
                     drifted: [], integrity: [] }),
}

/* Two headings that must never both appear, because they send you to
 * different fixes: "run the pipeline" vs "restart on current code". */
const ABSENT_HEAD = '这些表在这个库里不存在'
const PARTIAL_HEAD = '表在，但这个库的 schema 落后于代码'

const CASES = [
  ['trade · all absent', PortfolioView,
   { workspace: { workspace: 'trade', generated_at: '2026-09-13T04:30:00+00:00',
                  states: {}, sections: tradeBlocked } },
   { want: [ABSENT_HEAD, 'virtual_portfolio', '持仓账本'],
     reject: [PARTIAL_HEAD, '当前无持仓'] }],
  ['trade · book present', PortfolioView,
   { workspace: { workspace: 'trade', generated_at: '2026-09-13T04:30:00+00:00',
                  states: {}, sections: tradeSections } },
   { want: ['中石科技', '士兰微', '归因链', '改账审计', '资金与结算'],
     reject: [ABSENT_HEAD, '整个读模型没到'] }],
  ['learn · empty + partial', LearnView,
   { learn: { workspace: 'learn', generated_at: '2026-09-13T04:30:00+00:00',
              states: {}, sections: learnSections } },
   { want: ['学习日志', '这是合法状态，不是缺失', PARTIAL_HEAD, '成熟度，不是缺口',
            '最早一批还差 1 个交易日', '可定价（带 prob）',
            'learning_candidates.evidence_episode_ids'],
     reject: [ABSENT_HEAD, '整个读模型没到', '当前最要紧的缺口'] }],
  ['learn · a batch is ripe but unscored', LearnView,
   { learn: { workspace: 'learn', generated_at: '2026-09-14T04:30:00+00:00',
              states: {}, sections: learnRipe } },
   { want: ['窗口已收口却还没有 Brier', '这是要修的'],
     reject: ['成熟度，不是缺口'] }],
  ['learn · nothing carries a prob', LearnView,
   { learn: { workspace: 'learn', generated_at: '2026-09-14T04:30:00+00:00',
              states: {}, sections: learnUnpriceable } },
   { want: ['没有一行带概率（prob）的预测', '闸门会一直 abstain'],
     reject: ['成熟度，不是缺口'] }],
  ['evolve · absent + partial', EvolveView,
   { evolve: { workspace: 'evolve', generated_at: '2026-09-13T04:30:00+00:00',
               states: {},
               sections: evolveSections,
               code: { producers: ['constant_0.5'], baseline: 'constant_0.5',
                       candidate_producers: [], promotion_accepts: 'candidate_policy',
                       reachable: false } } },
   { want: [ABSENT_HEAD, PARTIAL_HEAD, '本构建不可达', 'baseline_only',
            'candidate_policy'],
     reject: ['整个读模型没到', '至少有一个候选生产者已登记'] }],
  /* The other branch of the same banner. Added 2026-09-13, the day the build
   * registered a candidate and `reachable` flipped: the matrix had no case for
   * the branch the real page now takes, and `check:render` would have kept
   * passing because it asserts about its own payload. The `want` strings are
   * the branch's own copy, not "可达" — that is a substring of the *other*
   * branch's "本构建不可达", so asserting it would be satisfied by the branch
   * being absent. */
  ['evolve · promotion reachable', EvolveView,
   { evolve: { workspace: 'evolve', generated_at: '2026-09-14T04:30:00+00:00',
               states: {},
               sections: evolveSections,
               code: { producers: ['constant_0.5', 'remap_confidence'],
                       baseline: 'constant_0.5',
                       candidate_producers: ['remap_confidence'],
                       promotion_accepts: 'candidate_policy',
                       reachable: true } } },
   { want: ['至少有一个候选生产者已登记', '闸门可产出候选级证据',
            'remap_confidence', 'constant_0.5'],
     reject: ['本构建不可达', '晋升在本构建里不可达', '这不来自数据库',
              '整个读模型没到'] }],
  ['learn · nothing at all', LearnView, { learn: null },
   { want: ['整个读模型没到'], reject: ['0 有数据', '读不到'] }],
  ['evolve · nothing at all', EvolveView, { evolve: null },
   { want: ['整个读模型没到'], reject: ['0 有数据', '读不到'] }],
  ['trade · nothing at all', PortfolioView, { workspace: null },
   { want: ['整个读模型没到'], reject: ['0 有数据', '读不到'] }],
]

/* Each case declares what must appear and what must not. A state that
 * renders the same pixels as another state is a state the page does not
 * actually have — and an absent workspace must NOT print the zero-counts
 * line, which is exactly what a healthy empty page prints. */
let failures = 0
for (const [label, Component, props, { want = [], reject = [] }] of CASES) {
  let html
  try {
    html = renderToString(<Component {...props} />)
  } catch (e) {
    failures += 1
    console.log(`${label}: 渲染失败 → ${e.message}`)
    continue
  }
  const missing = want.filter((w) => !html.includes(w))
  const leaked = reject.filter((r) => html.includes(r))
  if (missing.length || leaked.length) failures += 1
  const verdict = missing.length ? `缺少 ${missing.map((m) => `「${m}」`).join(' ')}`
    : leaked.length ? `不该出现 ${leaked.map((m) => `「${m}」`).join(' ')}`
      : 'OK'
  console.log(`${label}: ${html.length} 字节 · ${verdict}`)
}
console.log(failures ? `\n${failures} 个用例不合格` : '\nOK: 三种状态 × 三个视图全部可渲染且可区分')
process.exit(failures ? 1 : 0)
