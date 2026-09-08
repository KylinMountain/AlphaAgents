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

export default function PortfolioView({ portfolio }) {
  const pending = portfolio?.pending || []
  const positions = portfolio?.positions || []
  const closed = portfolio?.closed || []
  const stats = portfolio?.stats || null
  const capital = portfolio?.capital || null

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
          <table className="table">
            <thead>
              <tr>
                <th>标的</th><th>主线</th><th>开仓价</th><th>股数</th>
                <th>浮动收益</th><th>峰值</th><th>最大回撤</th>
                <th>持有天数</th><th>止损</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((p) => (
                <tr key={p.id}>
                  <td><b>{p.name}</b> · {p.code}</td>
                  <td>{p.theme || DASH}</td>
                  <td>{p.open_price ?? DASH}</td>
                  <td>{p.shares ?? DASH}</td>
                  <td className={trendClass(p.return_pct)}>{fmtPct(p.return_pct)}</td>
                  <td className={trendClass(p.peak_return_pct)}>
                    {fmtPct(p.peak_return_pct)}
                  </td>
                  <td className="down">{fmtPct(p.max_drawdown_pct)}</td>
                  <td>{p.holding_days ?? DASH}</td>
                  <td>{p.stop_loss ?? DASH}</td>
                </tr>
              ))}
            </tbody>
          </table>
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
