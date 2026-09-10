import { DASH } from '../lib/format'

/* Where the tokens went.
 *
 * "We are burning tokens" was as specific as anyone could get before this
 * page existed, and the fix depends entirely on which half is burning
 * them — a digest running after hours and an agent loop taking forty
 * turns are different problems with different answers. So the module
 * split is the page, and the totals are context for it.
 *
 * Three horizons, because they answer different questions: today is
 * "what is it doing right now", the window is "what does a normal day
 * cost", and all-time is the number you actually budget on.
 */

function n(v) {
  if (v == null || Number.isNaN(Number(v))) return DASH
  return Number(v).toLocaleString('zh-CN')
}

/* Tokens are unreadable at full precision past a few thousand — the
 * question is always "is this thousands or millions", never the digits. */
function short(v) {
  const x = Number(v)
  if (!Number.isFinite(x)) return DASH
  if (x >= 1e6) return `${(x / 1e6).toFixed(2)}M`
  if (x >= 1e4) return `${(x / 1e3).toFixed(1)}K`
  return x.toLocaleString('zh-CN')
}

function Kpi({ label, value, note, accent = false }) {
  return (
    <div className="kpi">
      <div className="kpi-head"><span>{label}</span></div>
      <b style={accent ? { color: 'var(--amber, #c07a3e)' } : undefined}>{value}</b>
      <small>{note}</small>
    </div>
  )
}

function Empty({ children }) {
  return <p className="empty-note" style={{ padding: '14px' }}>{children}</p>
}

/* Chat and embedding bill at completely different rates. Summing them
 * into one number makes the cheap half look like the expensive one, so
 * they are counted together and read apart. */
const KIND_LABEL = { chat: '对话模型', embedding: '向量' }

function ModuleTable({ rows, title, note }) {
  const total = rows.reduce((a, r) => a + (Number(r.total) || 0), 0)
  return (
    <article className="card table-wrap" style={{ marginBottom: 14 }}>
      <div className="card-title" style={{ padding: '14px 14px 0' }}>
        <h3>{title}</h3><span>{note}</span>
      </div>
      {rows.length === 0 ? (
        <Empty>还没有记录。统计从调度器装上这个钩子的那一刻开始，
          之前跑掉的调用无法追溯。</Empty>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>模块</th><th>类型</th>
              <th className="num">调用</th>
              <th className="num">输入</th><th className="num">输出</th>
              <th className="num">合计</th>
              <th className="num">占比</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const pct = total ? (Number(r.total) || 0) / total * 100 : 0
              return (
                <tr key={`${r.module}-${r.kind}`}>
                  <td><b>{r.module}</b></td>
                  <td className="soft">{KIND_LABEL[r.kind] || r.kind}</td>
                  <td className="num">{n(r.calls)}</td>
                  <td className="num soft">{r.inp == null ? DASH : short(r.inp)}</td>
                  <td className="num soft">{r.outp == null ? DASH : short(r.outp)}</td>
                  <td className="num"><b>{short(r.total)}</b></td>
                  <td className="num">
                    <div className="bar" style={{ width: 70, display: 'inline-block',
                                                  verticalAlign: 'middle' }}>
                      <i style={{ width: `${Math.min(100, pct)}%` }} />
                    </div>
                    <span className="soft" style={{ marginLeft: 7 }}>
                      {pct.toFixed(0)}%
                    </span>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
    </article>
  )
}

export default function UsageView({ usage }) {
  const u = usage || {}
  const today = u.today || {}
  const win = u.window || {}
  const all = u.alltime || {}
  const modules = u.modules || []
  const allModules = u.alltime_modules || []
  const byDay = u.by_day || []
  const models = u.models || []
  const recent = u.recent || []

  // Chat is the bill; embedding is rounding. Separating them here keeps
  // the headline honest.
  const chatToday = modules
    .filter((m) => m.kind === 'chat')
    .reduce((a, m) => a + (Number(m.total) || 0), 0)

  // Daily totals, one bar per day. A spike is the thing worth seeing;
  // the exact heights are in the table below.
  const days = {}
  byDay.forEach((d) => {
    days[d.date] = (days[d.date] || 0) + (Number(d.total) || 0)
  })
  const dayRows = Object.entries(days).sort(([a], [b]) => a.localeCompare(b))
  const peak = Math.max(1, ...dayRows.map(([, v]) => v))

  return (
    <section className="view active">
      <div className="page-head">
        <div>
          <h1>Token 消耗</h1>
          <p>
            每一次模型调用按模块记账。统计挂在 agents SDK 的 tracing 钩子和
            OpenAI 客户端上，不是挂在调用点上 —— 新增一个调用点会自动被计入。
          </p>
        </div>
        <div className="date-note">
          <b>{all.since ? `起自 ${all.since}` : '尚无记录'}</b>
          {all.active_days ? `${all.active_days} 天有调用` : ''}
        </div>
      </div>

      <div className="kpi-strip">
        <Kpi label="今日消耗" accent
             value={short(today.total_tokens || 0)}
             note={`${n(today.calls || 0)} 次调用`} />
        <Kpi label={`近 ${u.days || 14} 天`}
             value={short(win.total_tokens || 0)}
             note={`${n(win.calls || 0)} 次 · 其中对话模型 ${short(chatToday)}`} />
        <Kpi label="累计总量"
             value={short(all.total_tokens || 0)}
             note={`${n(all.calls || 0)} 次调用`} />
        <Kpi label="累计输入 / 输出"
             value={`${short(all.input_tokens || 0)} / ${short(all.output_tokens || 0)}`}
             note={all.total_tokens
               ? `输出占 ${((all.output_tokens || 0) / all.total_tokens * 100).toFixed(0)}%`
               : '尚无记录'} />
      </div>

      <ModuleTable rows={modules} title="分模块 · 近期"
                   note={`近 ${u.days || 14} 天`} />

      <ModuleTable rows={allModules} title="分模块 · 累计"
                   note="自统计启用以来" />

      {dayRows.length > 0 && (
        <article className="card pad" style={{ marginBottom: 14 }}>
          <div className="card-title"><h3>每日消耗</h3>
            <span>{dayRows.length} 天</span></div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 7,
                        marginTop: 12 }}>
            {dayRows.map(([date, total]) => (
              <div key={date} style={{ display: 'grid',
                                       gridTemplateColumns: '92px 1fr 74px',
                                       gap: 11, alignItems: 'center' }}>
                <span className="soft" style={{ fontSize: 12.5 }}>{date}</span>
                <div className="bar">
                  <i style={{ width: `${total / peak * 100}%` }} />
                </div>
                <span className="num" style={{ fontSize: 12.5 }}>{short(total)}</span>
              </div>
            ))}
          </div>
        </article>
      )}

      {models.length > 0 && (
        <article className="card table-wrap" style={{ marginBottom: 14 }}>
          <div className="card-title" style={{ padding: '14px 14px 0' }}>
            <h3>分模型</h3><span>{models.length} 个</span>
          </div>
          <table className="table">
            <thead><tr>
              <th>模型</th><th>类型</th>
              <th className="num">调用</th><th className="num">合计</th>
            </tr></thead>
            <tbody>
              {models.map((m) => (
                <tr key={`${m.model}-${m.kind}`}>
                  <td>{m.model}</td>
                  <td className="soft">{KIND_LABEL[m.kind] || m.kind}</td>
                  <td className="num">{n(m.calls)}</td>
                  <td className="num"><b>{short(m.total)}</b></td>
                </tr>
              ))}
            </tbody>
          </table>
        </article>
      )}

      <article className="card table-wrap">
        <div className="card-title" style={{ padding: '14px 14px 0' }}>
          <h3>最近调用</h3><span>{recent.length} 条</span>
        </div>
        {recent.length === 0 ? (
          <Empty>还没有调用记录。</Empty>
        ) : (
          <table className="table">
            <thead><tr>
              <th>时间</th><th>模块</th><th>模型</th>
              <th className="num">输入</th><th className="num">输出</th>
              <th className="num">合计</th>
            </tr></thead>
            <tbody>
              {recent.map((r, i) => (
                <tr key={i}>
                  <td className="soft">{(r.ts || '').slice(5)}</td>
                  <td><b>{r.module}</b></td>
                  <td className="soft">{r.model}</td>
                  <td className="num soft">{n(r.input_tokens)}</td>
                  <td className="num soft">{n(r.output_tokens)}</td>
                  <td className="num">{n(r.total_tokens)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </article>
    </section>
  )
}
