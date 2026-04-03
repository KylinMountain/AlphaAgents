import { useRef, useEffect } from 'react'

const stageLabels = {
  'fetch': '抓取',
  'dedup': '去重',
  'digest': '摘要',
  'agent': 'Agent',
  'pipeline': '管线',
}

const stageIcons = {
  'fetch': '📡',
  'dedup': '🔄',
  'digest': '🧠',
  'agent': '🤖',
  'pipeline': '⚡',
}

const statusStyles = {
  running: { color: 'var(--accent-blue)', badge: '运行' },
  success: { color: 'var(--accent-green)', badge: '完成' },
  error: { color: 'var(--accent-red)', badge: '错误' },
  idle: { color: 'var(--text-muted)', badge: '空闲' },
}

const categoryColors = {
  '政策': { bg: '#3b82f618', text: '#60a5fa', border: '#3b82f633' },
  '地缘': { bg: '#ef444418', text: '#f87171', border: '#ef444433' },
  '宏观': { bg: '#f59e0b18', text: '#fbbf24', border: '#f59e0b33' },
  '行业': { bg: '#10b98118', text: '#34d399', border: '#10b98133' },
  '市场': { bg: '#8b5cf618', text: '#a78bfa', border: '#8b5cf633' },
}

function ImportanceBar({ level }) {
  const max = 5
  return (
    <div className="flex items-center gap-0.5">
      {Array.from({ length: max }, (_, i) => (
        <div
          key={i}
          className="rounded-sm"
          style={{
            width: 3,
            height: i < level ? 10 + i * 2 : 6,
            background: i < level
              ? level >= 4 ? 'var(--accent-red)' : level >= 3 ? 'var(--accent-yellow)' : 'var(--accent-green)'
              : 'var(--border)',
          }}
        />
      ))}
    </div>
  )
}

function DigestEventCard({ event, index }) {
  const cat = categoryColors[event.category] || { bg: '#1e2d42', text: '#8892a4', border: '#2a3f5f' }
  const market = event.target_market || 'both'

  return (
    <div
      className="rounded-lg p-3 animate-fade-in card-hover"
      style={{
        background: 'var(--bg-card)',
        border: `1px solid var(--border)`,
        animationDelay: `${index * 50}ms`,
        animationFillMode: 'backwards',
      }}
    >
      <div className="flex items-start gap-2">
        {/* Importance */}
        <div className="shrink-0 pt-0.5">
          <ImportanceBar level={event.importance || 0} />
        </div>

        <div className="flex-1 min-w-0">
          {/* Tags row */}
          <div className="flex items-center gap-1.5 mb-1.5 flex-wrap">
            <span className="text-xs px-1.5 py-0.5 rounded"
                  style={{ background: cat.bg, color: cat.text, border: `1px solid ${cat.border}` }}>
              {event.category || '未知'}
            </span>
            {market !== 'both' && (
              <span className="text-xs px-1.5 py-0.5 rounded"
                    style={{
                      background: market === 'stock' ? '#3b82f612' : '#f59e0b12',
                      color: market === 'stock' ? '#60a5fa' : '#fbbf24',
                      border: `1px solid ${market === 'stock' ? '#3b82f625' : '#f59e0b25'}`,
                    }}>
                {market === 'stock' ? '→ 股票' : '→ 期货'}
              </span>
            )}
            {market === 'both' && (
              <span className="text-xs px-1.5 py-0.5 rounded"
                    style={{ background: '#10b98112', color: '#34d399', border: '1px solid #10b98125' }}>
                → 股+期
              </span>
            )}
            <span className="text-xs" style={{ color: 'var(--text-muted)' }}>
              {event.importance}/5
            </span>
          </div>

          {/* Event title */}
          <div className="text-xs leading-relaxed" style={{ color: 'var(--text-primary)' }}>
            {event.event}
          </div>

          {/* Summary if available */}
          {event.summary && (
            <div className="text-xs mt-1 leading-relaxed" style={{ color: 'var(--text-muted)' }}>
              {event.summary}
            </div>
          )}

          {/* Market impact */}
          {event.market_impact?.a_share && (
            <div className="flex flex-wrap gap-1 mt-2">
              {event.market_impact.a_share.sectors_bullish?.slice(0, 3).map((s, i) => (
                <span key={i} className="text-xs px-1.5 py-0.5 rounded"
                      style={{ background: '#10b98112', color: '#34d399', border: '1px solid #10b98125' }}>
                  ↑ {typeof s === 'string' ? s : s.name || s.sector}
                </span>
              ))}
              {event.market_impact.a_share.sectors_bearish?.slice(0, 3).map((s, i) => (
                <span key={i} className="text-xs px-1.5 py-0.5 rounded"
                      style={{ background: '#ef444412', color: '#f87171', border: '1px solid #ef444425' }}>
                  ↓ {typeof s === 'string' ? s : s.name || s.sector}
                </span>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function PipelineEvent({ event }) {
  const stage = event.stage?.replace('source_', '') || ''
  const isSource = event.stage?.startsWith('source_')
  const st = statusStyles[event.status] || statusStyles.idle
  const label = isSource ? stage : (stageLabels[stage] || stage)
  const icon = isSource ? '📰' : (stageIcons[stage] || '⚙')
  const time = new Date(event.timestamp * 1000).toLocaleTimeString('zh-CN')

  // Skip idle events and source-level noise
  if (event.status === 'idle') return null
  if (isSource && event.status === 'running') return null

  return (
    <div className="flex items-start gap-2 py-1.5 px-4 animate-fade-in"
         style={{ borderBottom: '1px solid var(--border)' }}>
      {/* Timeline dot */}
      <div className="shrink-0 mt-1">
        <div className="w-1.5 h-1.5 rounded-full" style={{ background: st.color }} />
      </div>

      {/* Content */}
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <span style={{ fontSize: 11 }}>{icon}</span>
          <span className="text-xs font-medium" style={{ color: st.color }}>{label}</span>
          <span className="text-xs px-1 rounded"
                style={{ background: `${st.color}15`, color: st.color, fontSize: 10 }}>
            {st.badge}
          </span>
          <span className="text-xs ml-auto tabular-nums" style={{ color: 'var(--text-muted)' }}>
            {time}
          </span>
        </div>
        {event.message && (
          <div className="text-xs mt-0.5" style={{ color: 'var(--text-secondary)' }}>
            {event.message}
          </div>
        )}
      </div>
    </div>
  )
}

export default function EventTimeline({ events, digestEvents, stages }) {
  const scrollRef = useRef(null)

  // Auto-scroll to bottom
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [events, digestEvents])

  const hasDigestEvents = digestEvents && digestEvents.length > 0

  return (
    <div ref={scrollRef} className="h-full overflow-y-auto">
      {/* Digest events section — the main attraction */}
      {hasDigestEvents && (
        <div className="px-4 py-3">
          <div className="flex items-center gap-2 mb-3">
            <span className="text-xs font-semibold uppercase tracking-wider"
                  style={{ color: 'var(--accent-green)' }}>
              筛选事件
            </span>
            <span className="text-xs px-1.5 py-0.5 rounded"
                  style={{ background: '#10b98118', color: '#34d399' }}>
              {digestEvents.length}
            </span>
            <div className="flex-1 h-px" style={{ background: 'var(--border)' }} />

            {/* Category breakdown */}
            <div className="flex gap-1">
              {Object.entries(
                digestEvents.reduce((acc, e) => {
                  const c = e.category || '?'
                  acc[c] = (acc[c] || 0) + 1
                  return acc
                }, {})
              ).map(([cat, count]) => {
                const cc = categoryColors[cat] || { text: '#8892a4' }
                return (
                  <span key={cat} className="text-xs" style={{ color: cc.text }}>
                    {cat}{count}
                  </span>
                )
              })}
            </div>
          </div>

          <div className="space-y-2">
            {digestEvents
              .sort((a, b) => (b.importance || 0) - (a.importance || 0))
              .map((e, i) => (
                <DigestEventCard key={i} event={e} index={i} />
              ))}
          </div>
        </div>
      )}

      {/* Pipeline events stream */}
      <div>
        <div className="px-4 py-2 sticky top-0 z-10"
             style={{ background: 'var(--bg-primary)', borderBottom: '1px solid var(--border)' }}>
          <span className="text-xs font-semibold uppercase tracking-wider"
                style={{ color: 'var(--text-muted)' }}>
            管线日志
          </span>
        </div>

        {events.length === 0 ? (
          <div className="p-8 text-center">
            <div className="text-xs" style={{ color: 'var(--text-muted)' }}>
              等待管线启动...
            </div>
          </div>
        ) : (
          events.map((e, i) => (
            <PipelineEvent key={e._id || i} event={e} />
          ))
        )}
      </div>
    </div>
  )
}
