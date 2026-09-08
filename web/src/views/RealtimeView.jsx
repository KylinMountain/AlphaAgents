import { useMemo, useState } from 'react'
import { DASH, bodyOf, fmtAge, fmtClock, sourceClass } from '../lib/format'

/* The 7x24 flash feed, source filters, and the two side rails the prototype
 * puts here: scheduler status and source health. */

function statusIcon(status) {
  if (status === 'ok') return 'ok'
  if (status === 'degraded' || status === 'running') return 'warn'
  return 'bad'
}

export default function RealtimeView({ news, activity, sources }) {
  const [active, setActive] = useState('全部')

  const counts = useMemo(() => {
    const m = new Map()
    news.forEach((n) => m.set(n.source, (m.get(n.source) || 0) + 1))
    return [...m.entries()].sort((a, b) => b[1] - a[1])
  }, [news])

  const shown = active === '全部' ? news : news.filter((n) => n.source === active)

  // Counted against the newest item rather than the wall clock: reading
  // Date.now() during render is impure, and "5 minutes before the latest
  // flash" is the more useful window anyway — it stays meaningful when the
  // ingest task has been stopped for an hour.
  const recentFive = useMemo(() => {
    const stamps = news
      .map((n) => new Date(String(n.time).replace(' ', 'T')).getTime())
      .filter((t) => !Number.isNaN(t))
    if (!stamps.length) return 0
    const newest = Math.max(...stamps)
    return stamps.filter((t) => t >= newest - 5 * 60 * 1000).length
  }, [news])

  const latestByTask = useMemo(() => {
    const seen = new Set()
    const out = []
    for (const a of activity) {
      if (!a.task || seen.has(a.task)) continue
      seen.add(a.task)
      out.push(a)
    }
    return out
  }, [activity])

  const healthy = sources.filter((s) => s.healthy).length

  return (
    <section className="view active">
      <div className="page-head">
        <div>
          <h1>实时资讯</h1>
          <p>原始快讯流 + 按源筛选。金十的英文重播已在摄取和读取两侧过滤掉。</p>
        </div>
        <div className="date-note">
          <b><span className="live-dot" />缓存 {news.length} 条</b>
          {news[0] ? `最新 ${fmtAge(news[0].time)}` : '暂无数据'}
        </div>
      </div>

      <div className="kpi-strip">
        <div className="kpi"><div className="kpi-head"><span>当前缓存</span></div>
          <b>{news.length}</b><small>实时快讯</small></div>
        <div className="kpi"><div className="kpi-head"><span>过去 5 分钟</span></div>
          <b>{recentFive}</b><small>新增资讯</small></div>
        <div className="kpi"><div className="kpi-head"><span>资讯来源</span></div>
          <b>{counts.length}</b><small>有数据的源</small></div>
        <div className="kpi"><div className="kpi-head"><span>数据源健康</span></div>
          <b className={healthy === sources.length ? 'up' : ''}>
            {sources.length ? `${healthy} / ${sources.length}` : DASH}
          </b>
          <small>{sources.length - healthy} 个源降级</small></div>
      </div>

      <div className="realtime-layout">
        <div className="card realtime-main">
          <div className="feed-toolbar">
            <div className="filter-group">
              {[['全部', news.length], ...counts].map(([name, count]) => (
                <button key={name}
                        className={`filter ${active === name ? 'active' : ''}`}
                        onClick={() => setActive(name)}>
                  {name} {count}
                </button>
              ))}
            </div>
          </div>
          <div className="news-feed">
            {shown.length === 0 && (
              <p className="empty-note" style={{ padding: '14px' }}>
                暂无快讯。news_ingest 每 5 分钟摄取一次。
              </p>
            )}
            {shown.map((n, i) => {
              const body = bodyOf(n)
              return (
                <div className="news-row" key={`${n.time}-${n.source}-${i}`}>
                  <div className="news-time">{fmtClock(n.time)}</div>
                  <div>
                    <div className="news-title">
                      <span className={`source-tag ${sourceClass(n.source)}`}>{n.source}</span>
                      {n.url ? (
                        <a href={n.url} target="_blank" rel="noreferrer"
                           style={{ color: 'inherit', textDecoration: 'none' }}>
                          {n.title}
                        </a>
                      ) : n.title}
                    </div>
                    {body && <div className="news-desc">{body}</div>}
                  </div>
                </div>
              )
            })}
          </div>
        </div>

        <aside className="realtime-side">
          <div className="card pad status-card">
            <div className="card-title"><h3>系统状态</h3><span>scheduler</span></div>
            {latestByTask.length === 0 ? (
              <p className="empty-note">暂无调度记录</p>
            ) : latestByTask.map((a) => (
              <div className="status-item" key={a.id}>
                <i className={`status-icon ${statusIcon(a.status)}`} />
                <div>
                  <b>{a.task}</b>
                  <span>{a.status === 'ok' ? '运行正常' : a.message || a.status}</span>
                </div>
                <span className="status-time">{fmtAge(a.ts)}</span>
              </div>
            ))}
          </div>

          <div className="card pad">
            <div className="card-title">
              <h3>数据源健康</h3>
              <span>{sources.length ? `${healthy} / ${sources.length}` : DASH}</span>
            </div>
            {['domestic', 'international', 'social'].map((type) => {
              const group = sources.filter((s) => s.type === type)
              if (!group.length) return null
              const label = { domestic: '国内', international: '海外', social: '社交' }[type]
              return (
                <div className="health-group" key={type}>
                  <h4>{label}</h4>
                  {group.map((s) => (
                    <div className="health-source" key={s.id}>
                      <span className="name">
                        <i className="tiny-dot"
                           style={s.healthy ? undefined : { background: 'var(--red)' }} />
                        {s.name}
                      </span>
                      <span>{s.total_items || 0}</span>
                    </div>
                  ))}
                </div>
              )
            })}
          </div>
        </aside>
      </div>
    </section>
  )
}
