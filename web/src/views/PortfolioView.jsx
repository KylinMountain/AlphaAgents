import { DASH, fmtPct, trendClass } from '../lib/format'

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
  const ret = Number(pos.return_pct)
  return (
    <div className="pos-card">
      <div className="pos-head">
        <div>
          <b>{pos.name}</b> <span className="soft">{pos.code}</span>
          {pos.theme ? <span className="stage-chip stage-active">{pos.theme}</span> : null}
        </div>
        <b className={trendClass(ret)} style={{ fontSize: 19 }}>{fmtPct(ret)}</b>
      </div>

      <div className="pos-facts">
        <div><span>成本</span><b>{pos.open_price ?? DASH}</b></div>
        <div><span>股数</span><b>{pos.shares ?? DASH}</b></div>
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

export default function PortfolioView({ portfolio }) {
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
      <div className="page-head">
        <div>
          <h1>虚拟持仓</h1>
          <p>
            推荐之后发生了什么：挂单是否成交、持仓浮盈浮亏、因为什么理由卖出。
            全部为模拟撮合，不连接任何券商，不产生真实委托。
          </p>
        </div>
        <div className="date-note">
          <b>{positions.length} 持仓 · {pending.length} 挂单</b>
          已结束 {closed.length} 笔
        </div>
      </div>

      <div className="kpi-strip">
        <Kpi label="可用资金"
             value={capital ? money(capital.available) : DASH}
             note={capital ? `总额 ${money(capital.total)}` : '暂无资金记录'} />
        <Kpi label="持仓市值"
             value={capital ? money(capital.invested) : DASH}
             note={`${positions.length} 只标的`} />
        <Kpi label="持仓浮动盈亏"
             value={positions.length ? money(floating) : DASH}
             valueClass={trendClass(floating)}
             note={positions.length ? '按最近一次盘中检查' : '当前无持仓'} />
        <Kpi label="近 30 日胜率"
             value={stats?.total_closed ? `${stats.win_rate}%` : DASH}
             note={stats?.total_closed
               ? `${stats.wins} 胜 / ${stats.losses} 负`
               : '尚无已平仓交易'} />
      </div>

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
                <th>标的</th><th>主线</th><th>入场区间</th><th>止损</th>
                <th>目标价</th><th>下单日</th><th>有效期</th>
              </tr>
            </thead>
            <tbody>
              {pending.map((o) => (
                <tr key={o.id}>
                  <td><b>{o.name}</b> · {o.code}</td>
                  <td>{o.theme || DASH}</td>
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

      <article className="card table-wrap">
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
        <article className="card pad" style={{ marginTop: 14 }}>
          <div className="card-title"><h3>按主线</h3><span>近 30 日已平仓</span></div>
          <div className="health-group">
            {Object.entries(stats.by_theme).map(([theme, v]) => (
              <div className="health-source" key={theme}>
                <span className="name">{theme}</span>
                <span className={trendClass(v.avg_return)}>
                  {v.count} 笔 · {fmtPct(v.avg_return)}
                </span>
              </div>
            ))}
          </div>
        </article>
      )}
    </section>
  )
}
