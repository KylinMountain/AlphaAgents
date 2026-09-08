import { useMemo } from 'react'
import { DASH, fmtDateTime } from '../lib/format'

/* Event causality graph.
 *
 * The prototype hand-places five nodes with pixel coordinates. Real events
 * arrive in unknown numbers, so nodes are laid out on a circle and links
 * drawn between whatever pairs the store actually recorded — a fixed layout
 * would silently drop every event past the fifth. */

const CATEGORY_CLASS = {
  宏观: 'violet', 政策: 'violet', 行业: 'blue',
  资本市场: 'green', 公司: 'green',
}

function layout(events, w, h) {
  const n = events.length
  const cx = w / 2
  const cy = h / 2
  const r = Math.min(w, h) * 0.36
  return events.map((e, i) => {
    const angle = (2 * Math.PI * i) / Math.max(1, n) - Math.PI / 2
    return { ...e, x: cx + r * Math.cos(angle), y: cy + r * Math.sin(angle) }
  })
}

export default function GraphView({ graph }) {
  const events = (graph?.events || []).slice(0, 14)
  const links = graph?.links || []

  const W = 1000
  const H = 440
  const nodes = useMemo(() => layout(events, W, H), [events])
  const byId = useMemo(() => new Map(nodes.map((n) => [n.id, n])), [nodes])

  return (
    <section className="view active">
      <div className="page-head">
        <div>
          <h1>事件因果图谱</h1>
          <p>把新闻事件与它们之间的关联连接起来。事件由 digest 抽取，关联由 event_linker 建立。</p>
        </div>
        <div className="date-note">
          <b>{events.length} 个事件 · {links.length} 条关联</b>最近 50 条内
        </div>
      </div>

      {events.length === 0 ? (
        <div className="card pad">
          <p className="empty-note">
            尚无事件。事件在新闻 digest 阶段抽取，需要调度器运行。
          </p>
        </div>
      ) : (
        <div className="card" style={{ padding: 0, overflow: 'hidden' }}>
          <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', height: H }}>
            {links.map((l, i) => {
              const a = byId.get(l.source_event_id)
              const b = byId.get(l.target_event_id)
              if (!a || !b) return null
              return (
                <line key={i} x1={a.x} y1={a.y} x2={b.x} y2={b.y}
                      stroke="var(--border-strong)" strokeWidth="1" />
              )
            })}
            {nodes.map((n) => (
              <g key={n.id}>
                <circle cx={n.x} cy={n.y} r="7"
                        fill="var(--surface)" stroke="var(--blue)" strokeWidth="2" />
                <text x={n.x} y={n.y - 13} textAnchor="middle"
                      fontSize="10" fill="var(--text-2)">
                  {String(n.title || '').slice(0, 16)}
                </text>
              </g>
            ))}
          </svg>
        </div>
      )}

      {events.length > 0 && (
        <div className="card table-wrap" style={{ marginTop: 14 }}>
          <table className="table">
            <thead>
              <tr><th>事件</th><th>分类</th><th>重要性</th><th>时间</th></tr>
            </thead>
            <tbody>
              {events.map((e) => (
                <tr key={e.id}>
                  <td><b>{e.title}</b></td>
                  <td>
                    <span className={`stage-chip stage-${
                      CATEGORY_CLASS[e.category] === 'green' ? 'main' : 'active'}`}>
                      {e.category || DASH}
                    </span>
                  </td>
                  <td>{e.importance ?? DASH}</td>
                  <td>{fmtDateTime(e.timestamp)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
