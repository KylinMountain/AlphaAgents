import { useState } from 'react'

const categoryColors = {
  '政策': { bg: '#3b82f618', text: '#60a5fa', border: '#3b82f633' },
  '地缘': { bg: '#ef444418', text: '#f87171', border: '#ef444433' },
  '宏观': { bg: '#f59e0b18', text: '#fbbf24', border: '#f59e0b33' },
  '行业': { bg: '#10b98118', text: '#34d399', border: '#10b98133' },
  '市场': { bg: '#8b5cf618', text: '#a78bfa', border: '#8b5cf633' },
}

function EventBadge({ event }) {
  const cat = categoryColors[event.category] || { bg: '#1e2d42', text: '#8892a4', border: '#2a3f5f' }
  return (
    <div className="flex items-center gap-2 py-1">
      <span className="text-xs px-1.5 py-0.5 rounded shrink-0"
            style={{ background: cat.bg, color: cat.text, border: `1px solid ${cat.border}` }}>
        {event.category || '未知'}
      </span>
      <span className="text-xs flex-1 truncate" style={{ color: 'var(--text-secondary)' }}>
        {event.event}
      </span>
      <span className="text-xs tabular-nums" style={{ color: 'var(--text-muted)' }}>
        {event.importance && `${event.importance}/5`}
      </span>
    </div>
  )
}

function ReportSection({ label, content, color, defaultOpen = false }) {
  const [open, setOpen] = useState(defaultOpen)
  if (!content) return null

  return (
    <div className="rounded-lg overflow-hidden"
         style={{ border: `1px solid var(--border)` }}>
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-2 px-3 py-2 text-left"
        style={{ background: 'var(--bg-secondary)', border: 'none', cursor: 'pointer' }}
      >
        <span className="w-1 h-3 rounded-full shrink-0" style={{ background: color }} />
        <span className="text-xs font-semibold flex-1" style={{ color }}>{label}</span>
        <span className="text-xs" style={{ color: 'var(--text-muted)' }}>
          {open ? '▼' : '▶'}
        </span>
      </button>
      {open && (
        <pre className="px-3 py-2 text-xs leading-relaxed whitespace-pre-wrap overflow-auto"
             style={{ color: 'var(--text-secondary)', maxHeight: 500, background: 'var(--bg-card)', margin: 0 }}>
          {content}
        </pre>
      )}
    </div>
  )
}

function ReportCard({ report }) {
  const time = new Date(report.timestamp * 1000).toLocaleString('zh-CN')
  const hasRoutes = report.routes && (report.routes.stock > 0 || report.routes.futures > 0)

  return (
    <div
      className="rounded-lg p-4 animate-fade-in"
      style={{ background: 'var(--bg-card)', border: '1px solid var(--border)' }}
    >
      {/* Header */}
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <span className="text-xs font-mono" style={{ color: 'var(--accent-purple)' }}>
            #{report.cycle}
          </span>
          <span className="text-xs tabular-nums" style={{ color: 'var(--text-muted)' }}>
            {time}
          </span>
        </div>
        <div className="flex items-center gap-1.5">
          {hasRoutes && (
            <>
              {report.routes.stock > 0 && (
                <span className="text-xs px-1.5 py-0.5 rounded"
                      style={{ background: '#3b82f612', color: '#60a5fa', border: '1px solid #3b82f625' }}>
                  股票 {report.routes.stock}
                </span>
              )}
              {report.routes.futures > 0 && (
                <span className="text-xs px-1.5 py-0.5 rounded"
                      style={{ background: '#f59e0b12', color: '#fbbf24', border: '1px solid #f59e0b25' }}>
                  期货 {report.routes.futures}
                </span>
              )}
            </>
          )}
          <span className="text-xs px-1.5 py-0.5 rounded"
                style={{ background: '#10b98112', color: '#34d399', border: '1px solid #10b98125' }}>
            {report.event_count} 事件
          </span>
        </div>
      </div>

      {/* Events */}
      {report.events_summary && (
        <div className="mb-3 pb-3" style={{ borderBottom: '1px solid var(--border)' }}>
          {report.events_summary.slice(0, 5).map((e, i) => (
            <EventBadge key={i} event={e} />
          ))}
          {report.events_summary.length > 5 && (
            <div className="text-xs mt-1" style={{ color: 'var(--text-muted)' }}>
              +{report.events_summary.length - 5} more
            </div>
          )}
        </div>
      )}

      {/* Report sections */}
      <div className="space-y-2">
        <ReportSection
          label="股票分析报告"
          content={report.report}
          color="var(--accent-blue)"
        />
        <ReportSection
          label="期货分析报告"
          content={report.futures_report}
          color="var(--accent-yellow)"
        />
      </div>
    </div>
  )
}

export default function ReportPanel({ reports }) {
  if (!reports || reports.length === 0) {
    return (
      <div className="p-8 text-center">
        <div className="text-2xl mb-3 opacity-20">📊</div>
        <div className="text-xs" style={{ color: 'var(--text-muted)' }}>暂无分析报告</div>
        <div className="text-xs mt-1" style={{ color: 'var(--text-muted)' }}>等待第一轮分析完成...</div>
      </div>
    )
  }

  const sorted = [...reports].reverse()

  return (
    <div className="p-4 space-y-3">
      {sorted.map((r, i) => (
        <ReportCard key={r.cycle || i} report={r} />
      ))}
    </div>
  )
}
