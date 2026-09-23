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
import HomeView from './src/views/HomeView'

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
             trader_id: 'pullback', trader_name: '回调派',
             entry_low: 20, entry_high: 21, stop_loss: 19, target_price: 24,
             order_date: '2026-09-11', expire_days: 2 }],
  positions: [
    { id: 9, code: '600460', name: '士兰微', theme: '半导体', open_price: 30,
      trader_id: 'default', trader_name: '默认交易员',
      shares: 100, return_pct: null, return_amount: null,
      last_price: 31.4, unrealized_pct: 4.67, unrealized_amount: 140,
      peak_return_pct: 6.1, max_drawdown_pct: -2.0, holding_days: 3,
      stop_loss: 28,
      thesis: { claim: '功率半导体涨价', prob: 0.6, horizon_days: 30,
                conviction: 0.3, conditions: [{ kind: 'price_below', value: 28,
                                                note: '', text: '跌破 28' }] } },
    /* The pair: this one carries no price (a code the local K-line store has
     * never seen), so it must fall back to the closed-column branch and say
     * so rather than print a confident 0.0%. */
    { id: 10, code: '301232', name: '飞沃科技', theme: null, open_price: 40,
      trader_id: 'breakout', trader_name: '突破派',
      shares: 50, return_pct: -1.1, return_amount: -22,
      last_price: null, unrealized_pct: null, unrealized_amount: null,
      peak_return_pct: 0.5, max_drawdown_pct: -3.2, holding_days: 1,
      thesis: null },
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
  /* The state production is actually in: no exit leg has ever been written,
   * so `by_thesis` is empty — but the ledger still holds realised money from
   * nine positions closed before the thesis mechanism, and those rows count.
   * That is why this is `present` and not `empty`: a section whose read model
   * reports rows renders its children, and the ones here exist to name the
   * gap instead of letting the card read "贡献: 0 元" next to a ledger at
   * −5,616.78. */
  attribution: present('position_exits → theses',
                       { by_thesis: [],
                         exits: { legs: 0, unattributed: 0, net: null },
                         orders: { orders: 117, without_thesis: 40 },
                         realized: { total: -5616.78, attributed: 0,
                                     unattributed: { legs: 0, legs_net: 0,
                                                     positions: 9,
                                                     legacy_net: -5616.78,
                                                     total: -5616.78 } } },
                       9),
  intents: empty('intents',
                 { recent: [], by_status: {}, never_decided: [] }),
  settlement: empty('reservations + settlement_lots',
                    { reservations: [], lots: {} }),
}

/* And the branch where at least one thesis has walked the whole chain. The
 * pair matters: a table that renders only when rows exist would silently
 * drop the "指不出" half, and a card that only ever rendered the gap would
 * never show a working chain. */
const attributionPresent = {
  ...tradeSections,
  attribution: present('position_exits → theses',
                       { by_thesis: [{ thesis_id: 7, code: '600460', legs: 2,
                                       wins: 1, net: 812.4, legacy_net: 0,
                                       positions: 0, total: 812.4,
                                       thesis_status: 'validated' }],
                         exits: { legs: 2, unattributed: 0, net: 812.4 },
                         orders: { orders: 117, without_thesis: 40 },
                         realized: { total: 812.4, attributed: 812.4,
                                     unattributed: { legs: 0, legs_net: 0,
                                                     positions: 0,
                                                     legacy_net: 0, total: 0 } } },
                       2),
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
  outcomes: empty('outcomes', { counts: {}, pending: [], broken_chains: 0,
                                breakdown: { pending: 0, overdue: 0,
                                             awaiting: 0 } }),
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

/* 三类结果 in the state production is actually in: 61 live pending labels,
 * 44 of which have a window that has already shut with nothing after them.
 * The card has to name the 44 as a defect and the other 17 as the design's
 * own calendar — and it must NOT print the checker's English sentence.
 * `outcomes.integrity` writes that sentence for
 * `scripts/episode_coverage.py`, and until 2026-09-16 it was piped straight
 * into this card. */
const learnOutcomesSplit = {
  ...learnSections,
  outcomes: present('outcomes',
                    { counts: { forecast: { pending: 44, matured: 64,
                                            censored: 0, revised: 14 },
                                trade: { pending: 6, matured: 0,
                                         censored: 0, revised: 0 },
                                process: { pending: 0, matured: 0,
                                           censored: 0, revised: 0 } },
                      pending: [],
                      breakdown: { pending: 61, overdue: 44, awaiting: 17 },
                      /* A key the read model no longer sends, kept on
                       * purpose. Without it the `reject` list below is
                       * vacuous — nothing in the payload could produce the
                       * English, so re-adding `<Integrity items={v.integrity}/>`
                       * to this card would keep the case green. With it, the
                       * case fails the moment the passthrough comes back. */
                      integrity: ['outcome #12 is pending but claims to be '
                                  + 'available on 2026-09-18, after the kernel '
                                  + 'clock'],
                      broken_chains: 0 },
                    61),
}

/* The same card with a chain broken and nothing overdue. The two
 * complaints are separate facts and must not be each other's copy. */
const learnOutcomesBrokenChain = {
  ...learnOutcomesSplit,
  outcomes: present('outcomes',
                    { counts: { forecast: { pending: 0, matured: 1,
                                            censored: 0, revised: 0 },
                                trade: { pending: 0, matured: 0,
                                         censored: 0, revised: 0 },
                                process: { pending: 0, matured: 0,
                                           censored: 0, revised: 0 } },
                      pending: [],
                      breakdown: { pending: 0, overdue: 0, awaiting: 0 },
                      broken_chains: 1 },
                    1),
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
  /* 最新报告: the report is already structured — a 【section】 header and
   * `•` bullets — and the card used to flatten all of it into one run-on
   * paragraph via `reportSummary`, which joins lines with a space. The
   * marker and the em-dash of each bullet then sat inside the sentence.
   *
   * The payload is the real intraday report text, because a synthetic one
   * would let the case pass while the actual shape stayed unhandled. */
  ['home · the report renders as an outline, not one paragraph', HomeView,
   { reports: [{ id: 334, timestamp: 1789615678, event_count: 0,
                 report_type: 'intraday_monitor',
                 report_text: '=== 盘中提醒 | 11:27 ===\n\n'
                   + '【实时市场数据异动】\n'
                   + '• 🔵暗流涌动: 电子纸 仅涨0.6% 但净流入21.7亿 — 资金暗中布局\n'
                   + '• 🔵暗流涌动: 长安汽车概念 仅涨0.5% 但净流入20.5亿 — 资金暗中布局\n\n'
                   + '【信号确认】（已涨停，不可买入）\n'
                   + '• 605058 澳弘电子 涨停封板(5连板)' }],
     themes: [], stats: null, news: [], signals: [], market: null },
   { want: ['brief-outline', 'brief-outline-head', '实时市场数据异动',
            '电子纸 仅涨0.6% 但净流入21.7亿', 'brief-outline-row'],
     /* The bullet markers and the section brackets must not survive into the
      * text, and the flattened summary must not be what renders. */
     reject: ['brief-summary', '• 🔵', '【实时市场数据异动】'] }],
  ['trade · all absent', PortfolioView,
   { workspace: { workspace: 'trade', generated_at: '2026-09-13T04:30:00+00:00',
                  states: {}, sections: tradeBlocked } },
   { want: [ABSENT_HEAD, 'virtual_portfolio', '持仓账本'],
     reject: [PARTIAL_HEAD, '当前无持仓'] }],
  ['trade · book present', PortfolioView,
   { workspace: { workspace: 'trade', generated_at: '2026-09-13T04:30:00+00:00',
                  states: {}, sections: tradeSections } },
   { want: ['中石科技', '士兰微', '归因链', '改账审计', '资金与结算',
            /* The two unrealized branches must both render: a card priced at
             * the last close says which day it is from, and a card with no
             * price at all must not print a confident 0.0% in its place. */
            '按最近收盘', '无市价',
            /* Who owns the row — the pending table and the position card. */
            '交易员', '回调派', '默认交易员',
            /* Attribution with no rows yet: the ledger's total is still
             * shown, and the gap is named rather than hidden as 0 元. */
            '已实现合计', '指不出的（旧仓 / 无论点）',
            '还没有一笔平仓能指回论点'],
     reject: [ABSENT_HEAD, '整个读模型没到'] }],
  ['trade · attribution has a thesis', PortfolioView,
   { workspace: { workspace: 'trade', generated_at: '2026-09-13T04:30:00+00:00',
                  states: {}, sections: attributionPresent } },
   { want: ['退出腿', '旧仓平仓', '#7', '已实现合计'],
     reject: ['还没有一笔平仓能指回论点'] }],
  /* The same structural rule on the trade page. 按主线 was a hand-built
   * `card pad` — 16px inset, where its six siblings use the shared wrapper's
   * 14px title and 12px body — so its heading sat 2px right of theirs. */
  ['trade · every card shares one wrapper', PortfolioView,
   { workspace: { workspace: 'trade', generated_at: '2026-09-17T04:30:00+00:00',
                  states: {}, sections: tradeSections } },
   { want: ['按主线', '近 30 日已平仓', 'card table-wrap'],
     reject: ['card pad'] }],
  /* The 改账审计 panel with an outage in its history. D32: a raised
   * exception was stored in the same column as a policy refusal, so D26's
   * 68 failed closes printed as 68 ordinary 已拒绝 rows, with the raw
   * `OperationalError: no such column: command_id` under 拒绝原因 — which is
   * the string the page still shows today.
   *
   * Both directions are asserted. A fix that relabelled *every* rejection a
   * fault would satisfy `want` alone, so the genuine refusal must still read
   * as a refusal. */
  ['trade · a fault is not a refusal', PortfolioView,
   { workspace: { workspace: 'trade', generated_at: '2026-09-17T04:30:00+00:00',
                  states: {},
                  sections: { ...tradeSections, intents: present('intents', {
                    recent: [
                      { id: 105, action: 'close', status: 'rejected', code: '600460',
                        information_cutoff: '2026-09-16',
                        reject_reason: 'OperationalError: no such column: command_id',
                        fault: true },
                      { id: 120, action: 'close', status: 'rejected', code: '000001',
                        information_cutoff: '2026-09-17',
                        reject_reason: 'refused by the order path (duplicate, theme too weak, no capital, or a rejected link)' },
                    ],
                    by_status: { accepted: 30, rejected: 81 },
                    never_decided: [], faults: 1,
                  }, 2) } } },
   { want: ['系统故障 1 条', '故障', 'refused by the order path'],
     /* The marker is a storage detail and must not reach the page. */
     reject: ['!fault:'] }],
  ['learn · empty + partial', LearnView,
   { learn: { workspace: 'learn', generated_at: '2026-09-13T04:30:00+00:00',
              states: {}, sections: learnSections } },
   { want: ['学习日志', PARTIAL_HEAD, '成熟度，不是缺口',
            '最早一批还差 1 个交易日', '可定价（带 prob）',
            'learning_candidates.evidence_episode_ids'],
     /* The developer-facing copy that used to be on this page: the section
      * rationale, the source table names, and the shell command that
      * reprints the coverage list. None of them is actionable for a reader.
      * `这是合法状态，不是缺失` was the empty-state sentence itself. */
     reject: [ABSENT_HEAD, '整个读模型没到', '当前最要紧的缺口',
              '这是合法状态，不是缺失', '来源：',
              'python scripts/episode_coverage.py'] }],
  ['learn · a batch is ripe but unscored', LearnView,
   { learn: { workspace: 'learn', generated_at: '2026-09-14T04:30:00+00:00',
              states: {}, sections: learnRipe } },
   { want: ['窗口已收口却还没有 Brier'],
     reject: ['成熟度，不是缺口', '这是要修的'] }],
  ['learn · nothing carries a prob', LearnView,
   { learn: { workspace: 'learn', generated_at: '2026-09-14T04:30:00+00:00',
              states: {}, sections: learnUnpriceable } },
   { want: ['没有一行带概率（prob）的预测', '闸门会一直 abstain'],
     reject: ['成熟度，不是缺口'] }],
  /* The 三类结果 card, both branches. Added 2026-09-16 with the fix for the
   * panel that printed `outcome #12 is pending but claims to be available on
   * …` into a Chinese product page — a machine's complaint, about rows the
   * checker had wrongly accused. The `reject` list is the actual regression:
   * the English sentence must not reach the page, and the two complaints
   * must not render for each other's cause. */
  ['learn · pending labels split by the calendar', LearnView,
   { learn: { workspace: 'learn', generated_at: '2026-09-16T04:30:00+00:00',
              states: {}, sections: learnOutcomesSplit } },
   { want: ['窗口已收口却没收尾 44 条', '是成熟度、不是缺口'],
     reject: ['still pending', 'is pending but claims', '标签链断裂',
              'episode_coverage.py', 'available_at'] }],
  ['learn · a label chain is broken', LearnView,
   { learn: { workspace: 'learn', generated_at: '2026-09-16T04:30:00+00:00',
              states: {}, sections: learnOutcomesBrokenChain } },
   { want: ['标签链断裂 1 条', '两个答案'],
     reject: ['窗口已收口却没收尾', '是成熟度、不是缺口'] }],
  /* Every card on a page is the same card.
   *
   * 晋升路径是否可达 was hand-built as `card pad ws-reach` while its four
   * siblings were `WorkspaceCard`s, so its heading sat 5px right of theirs
   * and its body 7px — a misalignment a reader notices without being able to
   * name. Asserting the *class list* is what catches that; asserting the copy
   * cannot, because the copy was never wrong.
   *
   * The check is deliberately "one wrapper class for every card on the page"
   * rather than a pixel rule: `check:render` runs in Node with no layout
   * engine, so the class list is the strongest thing it can honestly assert.
   * A second wrapper is not allowed, and since 2026-09-23 neither is a tone
   * modifier: the accent border and the monospace fact grid were the second
   * "风格不一致" on the same card, so its facts are `.table` rows now, the
   * same anatomy as 什么在生效 beneath it. */
  ['evolve · every card shares one wrapper', EvolveView,
   { evolve: { workspace: 'evolve', generated_at: '2026-09-17T04:30:00+00:00',
               states: {}, sections: evolveSections,
               code: { producers: ['constant_0.5', 'remap_confidence'],
                       baseline: 'constant_0.5',
                       candidate_producers: ['remap_confidence'],
                       promotion_accepts: 'candidate_policy',
                       reachable: true } } },
   { want: ['<article class="card table-wrap" style="margin-bottom:14px"><div class="card-title" style="padding:14px 14px 0"><h3>晋升路径是否可达',
            '<td>其中候选生产者</td>'],
     reject: ['card pad ws-reach', 'ws-reach ', 'ws-codefact'] }],
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
  /* The empty knowledge card. It used to render its own heading as a
   * sentence — 「保留」与「生效」之间的那道可审计关口 —— 这是合法状态，
   * 不是缺失。 — followed by the read model's design note and the raw table
   * names. All three are written for whoever maintains the read model, not
   * for the reader of the page. */
  ['evolve · an empty card says only that it is empty', EvolveView,
   { evolve: { workspace: 'evolve', generated_at: '2026-09-17T04:30:00+00:00',
               states: {}, sections: evolveSections } },
   { want: ['已批准知识快照', '还没有记录'],
     reject: ['这是合法状态，不是缺失', '来源：', 'knowledge_snapshots',
              'U5 之后检索引擎', '可审计关口'] }],
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
