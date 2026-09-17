import { DASH, fmtPct, trendClass } from '../lib/format'
import { Blocked, SectionMeta, WorkspaceCard, WorkspaceHead }
  from '../components/WorkspaceSection'

/* The virtual portfolio, end to end.
 *
 * The picks were visible everywhere and their outcome nowhere: an order
 * was created with an entry zone and a stop, and after that the dashboard
 * said nothing. Whether the picking works is decided here, not on the
 * recommendation page.
 *
 * Everything is simulated — no broker is connected, nothing is ordered.
 */

const CLOSE_LABEL = {
  stop_loss: '止损',
  take_profit: '止盈',
  trailing_stop: '跟踪止盈',
  expired: '到期',
  theme_dead: '主线走弱',
  bearish: '转空',
  cancelled: '未成交取消',
}

function money(v) {
  if (v == null || Number.isNaN(Number(v))) return DASH
  return `${Math.round(Number(v)).toLocaleString('zh-CN')}`
}

function Kpi({ label, value, valueClass = '', note }) {
  return (
    <div className="kpi">
      <div className="kpi-head"><span>{label}</span></div>
      <b className={valueClass}>{value}</b>
      <small>{note}</small>
    </div>
  )
}

function Empty({ children }) {
  return <p className="empty-note" style={{ padding: '14px' }}>{children}</p>
}

/* One row per trader.
 *
 * Several traders exist to be compared: same market, same tools, different
 * instructions, each with its own money. A single pooled capital line would
 * hide exactly the thing they were built to show, so the books are listed
 * side by side.
 *
 * Hidden when there is only one, because then there is nothing to compare
 * and the section would just be a second copy of the KPI strip.
 */
function TraderBooks({ traders }) {
  if (!traders || traders.length < 2) return null
  return (
    <article className="card table-wrap" style={{ marginBottom: 14 }}>
      <div className="card-title" style={{ padding: '14px 14px 0' }}>
        <h3>交易员</h3><span>{traders.length} 个独立账本</span>
      </div>
      <table className="table">
        <thead>
          <tr>
            <th>交易员</th>
            <th className="num">本金</th><th className="num">可用</th>
            <th className="num">持仓</th><th className="num">挂单</th>
            <th className="num">回撤</th>
          </tr>
        </thead>
        <tbody>
          {traders.map((t) => (
            <tr key={t.id}>
              <td>
                <b>{t.name}</b>
                {t.legacy
                  ? <span className="stage-chip stage-sprout">仅清理旧仓</span>
                  : null}
                {t.blocked
                  ? <span className="stage-chip stage-fade">回撤停手</span>
                  : null}
              </td>
              <td className="num">{money(t.capital)}</td>
              <td className="num">{money(t.available)}</td>
              <td className="num">{t.positions}</td>
              <td className="num">{t.pending}</td>
              <td className="num">
                {t.drawdown_pct == null ? DASH : `${t.drawdown_pct}%`}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </article>
  )
}

/* A position and the thesis it exists to test.
 *
 * These used to be a wide table of numbers, which was the right shape when
 * a position was just a fill. It is not any more: the position is held
 * *because of* a claim, and the claim names what would end it. Showing the
 * numbers without the exit conditions hides the only part that is a
 * decision — everything else is arithmetic.
 */
function PositionCard({ pos }) {
  const th = pos.thesis
  // An open row has no ``return_pct``: that column is written when the
  // position *ends*, so the headline read 0.0% on every card and a book
  // holding winners and losers looked break-even. While the position is open
  // the return is the unrealized one, priced at the last close on disk — and
  // the card says which, because a number without its as-of reads as live.
  const unrealized = pos.unrealized_pct != null
  const ret = unrealized ? pos.unrealized_pct : Number(pos.return_pct ?? 0)
  const pnl = pos.unrealized_amount
  return (
    <div className="pos-card">
      <div className="pos-head">
        <div>
          <b>{pos.name}</b> <span className="soft">{pos.code}</span>
          {pos.theme ? <span className="stage-chip stage-active">{pos.theme}</span> : null}
          {pos.trader_name
            ? <span className="stage-chip stage-fade">{pos.trader_name}</span> : null}
        </div>
        <div style={{ textAlign: 'right' }}>
          <b className={trendClass(ret)} style={{ fontSize: 19 }}>{fmtPct(ret)}</b>
          <div className="soft" style={{ fontSize: 11 }}>
            {pnl != null
              ? `${pnl > 0 ? '+' : ''}${Math.round(pnl).toLocaleString()} 元 · ` : ''}
            {unrealized ? '按最近收盘' : '无市价'}
          </div>
        </div>
      </div>

      <div className="pos-facts">
        <div><span>成本</span><b>{pos.open_price ?? DASH}</b></div>
        <div><span>股数</span><b>{pos.shares ?? DASH}</b></div>
        <div><span>现价</span><b>{pos.last_price ?? DASH}</b></div>
        <div><span>峰值</span>
          <b className={trendClass(pos.peak_return_pct)}>{fmtPct(pos.peak_return_pct)}</b>
        </div>
        <div><span>回撤</span><b className="down">{fmtPct(pos.max_drawdown_pct)}</b></div>
        <div><span>持有</span><b>{pos.holding_days ?? 0} 天</b></div>
        <div><span>止损</span><b>{pos.stop_loss ?? DASH}</b></div>
      </div>

      {th ? (
        <div className="thesis">
          <div className="thesis-claim">{th.claim || '（未写论点）'}</div>
          <div className="thesis-meta">
            <span>自报概率 <b>{Math.round((th.prob ?? 0) * 100)}%</b></span>
            <span>期限 <b>{th.horizon_days} 天</b></span>
            <span>仓位权重 <b>{Math.round((th.conviction ?? 0) * 100)}%</b></span>
          </div>
          {th.conditions?.length ? (
            <ul className="thesis-conds">
              {th.conditions.map((c, i) => (
                <li key={i}>
                  <code>{c.kind}</code> {c.text}
                </li>
              ))}
            </ul>
          ) : (
            <p className="thesis-warn">
              没有写失效条件——这笔仓位只能到期或触及风控硬线才会退出，
              亏损会被记为盲点。
            </p>
          )}
        </div>
      ) : (
        <p className="thesis-warn">
          无关联论点（建仓于论点机制上线之前）。规则信号会交给模型判断。
        </p>
      )}
    </div>
  )
}

/* The trade workspace. It reads the workspace read model, so the book, the
 * attribution chain and the intent trail all come from one place — and when
 * a section cannot be read, the reason is shown rather than an empty list. */
export default function PortfolioView({ workspace }) {
  const sections = workspace?.sections || {}
  const book = sections.book
  /* The book is not one card but five, and each of them would otherwise
   * read "暂无持仓" for a table that is not there. So the whole group is
   * gated once, and every "not yet" label says which of the two it is. */
  const bookBlocked = book != null && book.state === 'unavailable'
  const noBook = bookBlocked
    ? `读不到 ${(book.missing || []).join('、') || book.source}`
    : ''
  const portfolio = sections.book?.value
  const pending = portfolio?.pending || []
  const positions = portfolio?.positions || []
  const closed = portfolio?.closed || []
  const stats = portfolio?.stats || null
  const capital = portfolio?.capital || null
  const traders = portfolio?.traders || []

  const floating = positions.reduce(
    (a, p) => a + (Number(p.return_amount) || 0), 0,
  )

  return (
    <section className="view active">
      <WorkspaceHead
        title="交易工作台"
        payload={workspace}
        blurb="推荐之后发生了什么：挂单是否成交、持仓浮盈浮亏、因为什么理由卖出。
               全部为模拟撮合，不连接任何券商，不产生真实委托。" />

      <div className="kpi-strip">
        <Kpi label="可用资金"
             value={capital ? money(capital.available) : DASH}
             note={capital ? `总额 ${money(capital.total)}`
               : (noBook || '暂无资金记录')} />
        <Kpi label="持仓市值"
             value={capital ? money(capital.invested) : DASH}
             note={noBook || `${positions.length} 只标的`} />
        <Kpi label="持仓浮动盈亏"
             value={positions.length ? money(floating) : DASH}
             valueClass={trendClass(floating)}
             note={positions.length ? '按最近一次盘中检查'
               : (noBook || '当前无持仓')} />
        <Kpi label="近 30 日胜率"
             value={stats?.total_closed ? `${stats.win_rate}%` : DASH}
             note={stats?.total_closed
               ? `${stats.wins} 胜 / ${stats.losses} 负`
               : (noBook || '尚无已平仓交易')} />
      </div>

      {bookBlocked ? (
        <article className="card table-wrap" style={{ marginBottom: 14 }}>
          <div className="card-title" style={{ padding: '14px 14px 0' }}>
            <h3>持仓账本</h3>
            <SectionMeta sec={book} />
          </div>
          <Blocked sec={book} />
        </article>
      ) : (
        <>
          <TraderBooks traders={traders} />

          <article className="card table-wrap" style={{ marginBottom: 14 }}>
            <div className="card-title" style={{ padding: '14px 14px 0' }}>
              <h3>当前持仓</h3><span>{positions.length} 只</span>
            </div>
        {positions.length === 0 ? (
          <Empty>
            当前无持仓。挂单要等价格进入入场区间才会成交——这是设计如此，
            不是故障。
          </Empty>
        ) : (
          <div className="pos-list">
            {positions.map((p) => <PositionCard key={p.id} pos={p} />)}
          </div>
        )}
      </article>

      <article className="card table-wrap" style={{ marginBottom: 14 }}>
        <div className="card-title" style={{ padding: '14px 14px 0' }}>
          <h3>待成交挂单</h3><span>{pending.length} 笔</span>
        </div>
        {pending.length === 0 ? (
          <Empty>暂无挂单。晨扫与盘中追因会为每个推荐标的建一笔。</Empty>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>标的</th><th>主线</th><th>交易员</th><th>入场区间</th><th>止损</th>
                <th>目标价</th><th>下单日</th><th>有效期</th>
              </tr>
            </thead>
            <tbody>
              {pending.map((o) => (
                <tr key={o.id}>
                  <td><b>{o.name}</b> · {o.code}</td>
                  <td>{o.theme || DASH}</td>
                  <td>{o.trader_name || o.trader_id || DASH}</td>
                  <td>
                    {o.entry_low != null && o.entry_high != null
                      ? `${o.entry_low} – ${o.entry_high}` : DASH}
                  </td>
                  <td>{o.stop_loss ?? DASH}</td>
                  <td>{o.target_price ?? DASH}</td>
                  <td>{o.order_date || DASH}</td>
                  <td>{o.expire_days != null ? `${o.expire_days} 天` : DASH}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </article>

      <article className="card table-wrap" style={{ marginBottom: 14 }}>
        <div className="card-title" style={{ padding: '14px 14px 0' }}>
          <h3>已结束</h3>
          <span>
            {stats?.total_closed
              ? `平均 ${fmtPct(stats.avg_return)} · 持有 ${stats.avg_holding_days} 天`
              : `${closed.length} 笔`}
          </span>
        </div>
        {closed.length === 0 ? (
          <Empty>尚无已结束的交易。</Empty>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>标的</th><th>主线</th><th>结果</th><th>收益</th>
                <th>持有天数</th><th>结束原因</th><th>日期</th>
              </tr>
            </thead>
            <tbody>
              {closed.map((c) => (
                <tr key={c.id}>
                  <td><b>{c.name}</b> · {c.code}</td>
                  <td>{c.theme || DASH}</td>
                  <td>
                    <span className={`stage-chip ${
                      c.status === 'cancelled' ? 'stage-fade'
                        : (c.return_pct || 0) > 0 ? 'stage-main' : 'stage-active'}`}>
                      {CLOSE_LABEL[c.status] || c.status}
                    </span>
                  </td>
                  <td className={trendClass(c.return_pct)}>
                    {c.status === 'cancelled' ? DASH : fmtPct(c.return_pct)}
                  </td>
                  <td>{c.holding_days ?? DASH}</td>
                  <td style={{ color: 'var(--text-2)' }}>
                    {c.close_reason || CLOSE_LABEL[c.status] || DASH}
                  </td>
                  <td>{c.close_date || c.order_date || DASH}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </article>

      {stats?.total_closed > 0 && Object.keys(stats.by_theme || {}).length > 0 && (
        /* A `WorkspaceCard` like every sibling, not a hand-built `card pad`.
         *
         * Two spacing models had met here — this card carried `marginTop`
         * while the rest carried `marginBottom` — and one padding model was
         * missing: `.card.pad` insets its title at 16px where the shared
         * wrapper insets at 14px and its body at 12px. So this card's heading
         * sat 2px right of the heading below it, which is the same
         * "风格不一致" the reach card had. The page has one card now. */
        <WorkspaceCard title="按主线"
                       sec={{ state: 'present', schema: 'complete' }}
                       badge={<span>近 30 日已平仓</span>}>
          <div className="card-body">
            <div className="health-group" style={{ marginTop: 0 }}>
              {Object.entries(stats.by_theme).map(([theme, v]) => (
                <div className="health-source" key={theme}>
                  <span className="name">{theme}</span>
                  <span className={trendClass(v.avg_return)}>
                    {v.count} 笔 · {fmtPct(v.avg_return)}
                  </span>
                </div>
              ))}
            </div>
          </div>
        </WorkspaceCard>
      )}

        </>
      )}

      <WorkspaceCard title="归因链" sec={sections.attribution}
                     subtitle="已实现收益里，有多少指得出是哪个论点赚的">
        <Attribution sec={sections.attribution} />
      </WorkspaceCard>

      <WorkspaceCard title="改账审计" sec={sections.intents}
                     subtitle="每一笔改账都过同一个入口，这里是它的痕迹">
        <IntentTrail sec={sections.intents} />
      </WorkspaceCard>

      <WorkspaceCard title="资金与结算" sec={sections.settlement}
                     subtitle="挂在活单上的现金，以及还在 T+1 里的股数">
        <Settlement sec={sections.settlement} />
      </WorkspaceCard>
    </section>
  )
}

/* ── 归因链：thesis → order → exits ─────────────────────────────── */
function Attribution({ sec }) {
  const v = sec?.value || {}
  const exits = v.exits || {}
  const orders = v.orders || {}
  // The headline is the ledger's own number, so this card and the ledger
  // cannot disagree. The split is what the chain adds: how much of that
  // money a thesis can actually be named for. Hiding the unattributable part
  // would make the chain look complete while the ledger holds a loss the
  // chain cannot explain.
  const r = v.realized || {}
  const u = r.unattributed || {}
  const rows = v.by_thesis || []
  const yuan = (n) => `${n > 0 ? '+' : ''}${Math.round(n).toLocaleString()} 元`
  return (
    <div className="card-body">
      {r.total != null && (
        <div className="health-group" style={{ marginBottom: 12 }}>
          <div className="health-source">
            <span className="name">已实现合计</span>
            <span className={trendClass(r.total)}>{yuan(r.total)}</span>
          </div>
          <div className="health-source">
            <span className="name">指得出论点的</span>
            <span className={trendClass(r.attributed)}>{yuan(r.attributed)}</span>
          </div>
          <div className="health-source">
            <span className="name">指不出的（旧仓 / 无论点）</span>
            <span className={trendClass(u.total || 0)}>{yuan(u.total || 0)}</span>
          </div>
        </div>
      )}
      {rows.length === 0 ? (
        <Empty>
          还没有一笔平仓能指回论点——已实现收益全部来自论点机制上线之前的旧仓
          {u.positions ? `（${u.positions} 笔）` : ''}。链路是通的，只是还没有第一笔走完它。
        </Empty>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>论点</th><th>标的</th><th className="num">退出腿</th>
              <th className="num">盈利腿</th><th className="num">旧仓平仓</th>
              <th className="num">已实现净额</th>
              <th>结局</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((t) => (
              <tr key={t.thesis_id}>
                {/* One string, not `#` + value: renderToString separates
                    adjacent text nodes with a comment, and any assertion on
                    the rendered row then has nothing to match against. */}
                <td>{`#${t.thesis_id}`}</td>
                <td>{t.code || DASH}</td>
                <td className="num">{t.legs}</td>
                <td className="num">{t.wins}</td>
                <td className="num">{t.positions || DASH}</td>
                <td className={`num ${trendClass(t.total)}`}>{Math.round(t.total)}</td>
                <td>{t.thesis_status || DASH}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="soft">
        订单 {orders.orders ?? 0} 笔，其中未挂论点 {orders.without_thesis ?? 0} 笔；
        退出腿 {exits.legs ?? 0} 条，其中无法归因 {exits.unattributed ?? 0} 条。
        「指不出」不是舍入误差，是账本与想法之间的缺口，所以单独报出来而不是丢掉。
      </p>
    </div>
  )
}

/* ── 改账审计：one door's trail ─────────────────────────────────── */
const INTENT_LABEL = {
  accepted: '已受理',
  rejected: '已拒绝',
  pending: '待裁决',
}

function IntentTrail({ sec }) {
  const v = sec?.value || {}
  const never = v.never_decided || []
  const faults = v.faults || 0
  return (
    <div className="card-body">
      <div className="health-group">
        {Object.entries(v.by_status || {}).map(([status, n]) => (
          <div className="health-source" key={status}>
            <span className="name">{INTENT_LABEL[status] || status}</span>
            <span>{n}</span>
          </div>
        ))}
      </div>
      {/* A fault is not a verdict (D32). Counting it under 已拒绝 made an
          outage — 68 closes failing on one missing column — read as 68
          ordinary policy refusals, and a normal refusal is exactly what
          nobody re-examines. Named separately, with its own colour. */}
      {faults ? (
        <div className="ws-integrity">
          {/* One string, not `系统故障 ` + value + ` 条`: renderToString
              separates adjacent text nodes with a comment, so an assertion
              on the rendered line would have nothing to match against. */}
          <b>{`系统故障 ${faults} 条`}</b>
          <p className="soft">
            这些不是「业务规则拒绝了它」，而是写入路径当场抛错 ——
            账本因此没被改动。它们和策略裁决是两回事，所以分开计数。
          </p>
        </div>
      ) : null}
      {never.length ? (
        <div className="ws-integrity">
          <b>已提交但未裁决 {never.length} 条</b>
          <p className="soft">
            这是唯一能证明某次写入在「决定」与「执行」之间崩过的证据，
            账本里没有别的地方记它。
          </p>
        </div>
      ) : null}
      {v.recent?.length ? (
        <table className="table">
          <thead>
            <tr>
              <th>#</th><th>动作</th><th>状态</th><th>标的</th>
              <th>信息边界</th><th>拒绝原因</th>
            </tr>
          </thead>
          <tbody>
            {v.recent.slice(0, 20).map((r) => (
              <tr key={r.id}>
                <td>{r.id}</td>
                <td>{r.action}</td>
                <td>
                  {r.fault
                    ? <span className="stage-chip stage-sprout">故障</span>
                    : (
                      <span className={`stage-chip ${
                        r.status === 'accepted' ? 'stage-main'
                          : r.status === 'rejected' ? 'stage-fade' : 'stage-sprout'}`}>
                        {INTENT_LABEL[r.status] || r.status}
                      </span>
                    )}
                </td>
                <td>{r.code || DASH}</td>
                <td>{r.information_cutoff || DASH}</td>
                {/* The reason, with the writer's fault marker removed: it is
                    a storage detail, and printing `!fault: ` on the page is
                    the same leak as printing a table name. The row's own
                    故障 chip already says which population it belongs to. */}
                <td style={{
                  color: 'var(--text-2)', maxWidth: 280,
                  overflow: 'hidden', textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }} title={faultText(r)}>
                  {faultText(r) || DASH}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </div>
  )
}

/** The reason as a reader should see it: no storage marker, and a fault
 *  prefixed with the word for what it was. */
function faultText(r) {
  const raw = r.reject_reason
  if (!raw) return ''
  if (!r.fault) return raw
  return `系统故障：${raw.replace(/^!fault:\s*/, '')}`
}

/* ── 资金与结算：reservations + T+1 lots ────────────────────────── */
function Settlement({ sec }) {
  const v = sec?.value || {}
  const lots = v.lots || {}
  return (
    <div className="card-body">
      <table className="table">
        <thead>
          <tr><th>预留状态</th><th className="num">笔数</th>
            <th className="num">金额</th><th className="num">已消耗</th></tr>
        </thead>
        <tbody>
          {(v.reservations || []).map((r) => (
            <tr key={r.state}>
              <td>{r.state}</td>
              <td className="num">{r.n}</td>
              <td className="num">{r.amount == null ? DASH : Math.round(r.amount)}</td>
              <td className="num">{r.consumed == null ? DASH : Math.round(r.consumed)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="soft">
        T+1 批次 {lots.lots ?? 0} 个，剩余 {lots.remaining ?? 0} 股
        {lots.earliest ? `，最早解禁 ${lots.earliest}` : ''}
        {lots.latest ? `，最晚解禁 ${lots.latest}` : ''}。
      </p>
    </div>
  )
}
